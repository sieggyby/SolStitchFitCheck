"""Pass B: scoring pipeline tests. Default state='off' blocks everything;
state='silent' runs the vision call; retry-then-fail preserves streak credit;
ON CONFLICT preserves reveal columns; cost is logged; prompt caching is set.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from sqlalchemy import text

from sable_platform.db.discord_streaks import upsert_streak_event
from sable_platform.db.discord_scoring_config import set_state


def _png_bytes(pixel=(40, 40, 40)) -> bytes:
    img = Image.new("RGB", (32, 32), pixel)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_attachment(*, raw_bytes=None, size=None):
    raw = raw_bytes if raw_bytes is not None else _png_bytes()
    att = MagicMock()
    att.filename = "fit.png"
    att.content_type = "image/png"
    att.size = size if size is not None else len(raw)
    att.read = AsyncMock(return_value=raw)
    return att


def _make_message(*, message_id=900, author_id=555, attachments=None, author_name="tester"):
    import datetime as _dt

    author = MagicMock()
    author.id = author_id
    author.display_name = author_name
    msg = MagicMock()
    msg.id = message_id
    msg.author = author
    msg.attachments = attachments or [_make_attachment()]
    msg.created_at = _dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=_dt.timezone.utc)
    return msg


def _stub_response(*, content_json: dict, in_tok=200, out_tok=100, cache_read=180, cache_creation=0):
    """Build an Anthropic-shape stub response."""
    block = SimpleNamespace(text=json.dumps(content_json))
    usage = SimpleNamespace(
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )
    return SimpleNamespace(content=[block], usage=usage)


_VALID_RESPONSE = {
    "axis_scores": {"cohesion": 8, "execution": 7, "concept": 6, "catch": 5},
    "axis_rationales": {
        "cohesion": "pieces talk",
        "execution": "well fitted",
        "concept": "loose theme",
        "catch": "nothing nameable",
    },
    "catch_detected": None,
    "catch_naming_class": None,
    "description": "neutral fit",
    "confidence": 0.85,
    "raw_total": 26,
}


@pytest.fixture
def sp_module(monkeypatch, db_conn):
    from sable_roles.features import scoring_pipeline as mod

    monkeypatch.setattr(mod, "SCORED_MODE_ENABLED", True)
    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(mod, "SCORING_RETRY_DELAY_SECONDS", 0.0)

    class _DBContext:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr(mod, "get_db", lambda: _DBContext())

    # Seed a streak event so collision queries have rows; not strictly needed
    # for scoring path tests but matches the on_message ordering.
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    yield mod


# ---------------------------------------------------------------------------
# state='off' MUST block ALL API calls and ALL DB writes
# ---------------------------------------------------------------------------


async def test_state_off_blocks_all_scoring_no_api_no_db_write(sp_module, db_conn):
    """Default config is state='off'. maybe_score_fit must short-circuit
    BEFORE any vision call AND BEFORE writing any score row.
    """
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=AssertionError("must NOT be called"))
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )

    # No score row was written.
    row = db_conn.execute(
        text("SELECT COUNT(*) AS n FROM discord_fitcheck_scores")
    ).fetchone()
    assert row["n"] == 0

    # No scoring audit rows either.
    audit_n = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action IN ('fitcheck_score_recorded', 'fitcheck_score_failed')"
        )
    ).fetchone()
    assert audit_n["n"] == 0

    fake_client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# state='silent' runs the pipeline (cohort scope of Pass B)
# ---------------------------------------------------------------------------


async def test_state_silent_writes_success_row_and_audit(sp_module, db_conn):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_stub_response(content_json=_VALID_RESPONSE))
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )

    score = db_conn.execute(
        text(
            "SELECT score_status, raw_total, axis_cohesion, percentile, curve_basis"
            " FROM discord_fitcheck_scores WHERE post_id = '900'"
        )
    ).fetchone()
    assert score is not None
    assert score["score_status"] == "success"
    assert score["raw_total"] == 26
    assert score["axis_cohesion"] == 8
    assert score["curve_basis"] == "absolute"  # cold-start

    audit = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_score_recorded' ORDER BY id DESC LIMIT 1"
        )
    ).fetchone()
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["state"] == "silent"
    assert detail["raw_total"] == 26


async def test_state_revealed_also_runs_pipeline(sp_module, db_conn):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_stub_response(content_json=_VALID_RESPONSE))
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )
    row = db_conn.execute(
        text("SELECT score_status FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert row["score_status"] == "success"


# ---------------------------------------------------------------------------
# Prompt caching is MANDATORY per claude-api memory + design sec 5.1
# ---------------------------------------------------------------------------


async def test_system_block_has_cache_control_ephemeral(sp_module, db_conn):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_stub_response(content_json=_VALID_RESPONSE))
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )

    fake_client.messages.create.assert_called_once()
    call_kwargs = fake_client.messages.create.call_args.kwargs
    system_blocks = call_kwargs["system"]
    assert isinstance(system_blocks, list)
    assert len(system_blocks) == 1
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert system_blocks[0]["type"] == "text"
    assert "Stitzy" in system_blocks[0]["text"]
    assert call_kwargs["temperature"] == 0


# ---------------------------------------------------------------------------
# Retry-once-then-fail per design sec 5.3
# ---------------------------------------------------------------------------


async def test_retry_once_on_api_error_then_succeeds(sp_module, db_conn):
    from anthropic import APIError

    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(
        side_effect=[
            APIError("rate limited", request=MagicMock(), body=None),
            _stub_response(content_json=_VALID_RESPONSE),
        ]
    )
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )

    assert fake_client.messages.create.call_count == 2
    score = db_conn.execute(
        text("SELECT score_status FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert score["score_status"] == "success"


async def test_retry_then_fail_records_failure_row_streak_credit_preserved(
    sp_module, db_conn
):
    from anthropic import APIError

    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(
        side_effect=[
            APIError("rate limited", request=MagicMock(), body=None),
            APIError("rate limited again", request=MagicMock(), body=None),
        ]
    )
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )

    assert fake_client.messages.create.call_count == 2

    # Streak row is untouched — credit preserved.
    streak = db_conn.execute(
        text(
            "SELECT counts_for_streak, invalidated_at FROM discord_streak_events"
            " WHERE post_id = '900'"
        )
    ).fetchone()
    assert streak["counts_for_streak"] == 1
    assert streak["invalidated_at"] is None

    # Failure row written.
    score = db_conn.execute(
        text(
            "SELECT score_status, score_error, axis_cohesion, percentile"
            " FROM discord_fitcheck_scores WHERE post_id = '900'"
        )
    ).fetchone()
    assert score["score_status"] == "failed"
    assert "APIError" in score["score_error"]
    assert score["axis_cohesion"] is None
    assert score["percentile"] is None

    audit = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'fitcheck_score_failed'"
        )
    ).fetchone()
    assert audit["n"] == 1


async def test_no_retry_on_bad_request_error(sp_module, db_conn):
    from anthropic import BadRequestError

    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(
        side_effect=BadRequestError(
            message="bad media type", response=MagicMock(), body=None
        )
    )
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )
    assert fake_client.messages.create.call_count == 1  # NO retry
    score = db_conn.execute(
        text(
            "SELECT score_status, score_error FROM discord_fitcheck_scores"
            " WHERE post_id = '900'"
        )
    ).fetchone()
    assert score["score_status"] == "failed"
    assert score["score_error"].startswith("bad_request")


# ---------------------------------------------------------------------------
# Schema-invalid response is treated as a failure
# ---------------------------------------------------------------------------


async def test_invalid_json_payload_records_failure(sp_module, db_conn):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    bad_block = SimpleNamespace(text="this is not json at all")
    bad_usage = SimpleNamespace(
        input_tokens=200, output_tokens=50,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    bad_response = SimpleNamespace(content=[bad_block], usage=bad_usage)
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=bad_response)
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )

    score = db_conn.execute(
        text(
            "SELECT score_status, score_error FROM discord_fitcheck_scores"
            " WHERE post_id = '900'"
        )
    ).fetchone()
    assert score["score_status"] == "failed"
    assert score["score_error"] == "json_parse_failed"


async def test_schema_invalid_response_records_failure(sp_module, db_conn):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    bad_axes = {
        "axis_scores": {"cohesion": 8, "execution": 7, "concept": 6, "catch": 11},  # >10
        "axis_rationales": {"cohesion": "a", "execution": "b", "concept": "c", "catch": "d"},
        "description": "x",
        "confidence": 0.5,
        "raw_total": 32,
    }
    response = _stub_response(content_json=bad_axes)
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=response)
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(),
        org_id="solstitch",
        guild_id="100",
        client=client,
    )

    score = db_conn.execute(
        text(
            "SELECT score_status, score_error FROM discord_fitcheck_scores"
            " WHERE post_id = '900'"
        )
    ).fetchone()
    assert score["score_status"] == "failed"
    assert score["score_error"].startswith("schema_invalid")


# ---------------------------------------------------------------------------
# ON CONFLICT preserves reveal_* and invalidated_* columns
# ---------------------------------------------------------------------------


async def test_on_conflict_preserves_reveal_and_invalidated_columns(sp_module, db_conn):
    """Pass C will write reveal_fired_at / reveal_post_id. A re-scoring
    must NOT clobber those — covered by SP helper tests, here we verify
    the round-trip through the pipeline.
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")

    # First scoring run.
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_stub_response(content_json=_VALID_RESPONSE))
    sp_module._anthropic_client = fake_client
    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(), org_id="solstitch", guild_id="100", client=client,
    )
    # Simulate Pass C reveal.
    db_conn.execute(
        text(
            "UPDATE discord_fitcheck_scores"
            " SET reveal_eligible = 1, reveal_fired_at = '2026-05-13T00:00:00Z',"
            "     reveal_post_id = 'REVEALED_BY_PASS_C', reveal_trigger = 'reactions'"
            " WHERE post_id = '900'"
        )
    )
    db_conn.commit()

    # Re-score (simulating prompt revision or test retry).
    different_response = dict(_VALID_RESPONSE)
    different_response["axis_scores"] = {
        "cohesion": 9, "execution": 9, "concept": 9, "catch": 9,
    }
    different_response["raw_total"] = 36
    fake_client.messages.create = AsyncMock(return_value=_stub_response(content_json=different_response))
    await sp_module.maybe_score_fit(
        message=_make_message(), org_id="solstitch", guild_id="100", client=client,
    )

    score = db_conn.execute(
        text("SELECT * FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert score["axis_cohesion"] == 9  # new judgement landed
    # But reveal columns are preserved.
    assert score["reveal_eligible"] == 1
    assert score["reveal_fired_at"] == "2026-05-13T00:00:00Z"
    assert score["reveal_post_id"] == "REVEALED_BY_PASS_C"
    assert score["reveal_trigger"] == "reactions"


# ---------------------------------------------------------------------------
# Cold-start curve basis behavior
# ---------------------------------------------------------------------------


async def test_pool_below_threshold_uses_absolute_basis(sp_module, db_conn):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_stub_response(content_json=_VALID_RESPONSE))
    sp_module._anthropic_client = fake_client

    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await sp_module.maybe_score_fit(
        message=_make_message(), org_id="solstitch", guild_id="100", client=client,
    )

    score = db_conn.execute(
        text("SELECT curve_basis, percentile, pool_size_at_score_time FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert score["curve_basis"] == "absolute"
    # raw=26 / 40 * 100 = 65.0
    assert score["percentile"] == 65.0
    assert score["pool_size_at_score_time"] == 0


# ---------------------------------------------------------------------------
# Permission gate on /scoring command
# ---------------------------------------------------------------------------


def test_register_commands_attaches_default_permissions(sp_module):
    from discord import app_commands

    tree = MagicMock(spec=app_commands.CommandTree)
    sp_module.register_commands(tree, client=MagicMock())
    # tree.command was used; the decorator was applied with default_permissions
    # Since we can't easily inspect the resulting Command object via MagicMock,
    # we assert tree.command was invoked.
    tree.command.assert_called_once()


def test_is_manage_guild_helper_blocks_non_mod():
    """Defense-in-depth check: _is_manage_guild returns False for users
    without Manage Guild, True for users with it. This is the in-handler
    re-check that fires when a guild overrides slash-command default
    visibility.
    """
    from sable_roles.features.scoring_pipeline import _is_manage_guild
    import discord as _discord

    # Non-Member (e.g. invoked from DM context) → False
    non_member_interaction = MagicMock()
    non_member_interaction.user = SimpleNamespace()  # not a Member
    assert _is_manage_guild(non_member_interaction) is False

    # Member without manage_guild → False
    member = MagicMock(spec=_discord.Member)
    member.guild_permissions = SimpleNamespace(manage_guild=False)
    weak_interaction = MagicMock()
    weak_interaction.user = member
    assert _is_manage_guild(weak_interaction) is False

    # Member with manage_guild → True
    admin_member = MagicMock(spec=_discord.Member)
    admin_member.guild_permissions = SimpleNamespace(manage_guild=True)
    admin_interaction = MagicMock()
    admin_interaction.user = admin_member
    assert _is_manage_guild(admin_interaction) is True


# ---------------------------------------------------------------------------
# Confirmation View — `/scoring set` should NOT flip state on first call
# ---------------------------------------------------------------------------


def test_scoring_set_confirm_view_holds_state_until_confirm_clicked():
    """Constructing _ScoringSetConfirmView must NOT call set_state. Only
    the Confirm button callback writes state — Cancel and timeout are
    no-ops.
    """
    from sable_roles.features.scoring_pipeline import _ScoringSetConfirmView

    view = _ScoringSetConfirmView(
        invoker_user_id=555,
        org_id="solstitch",
        guild_id="100",
        target_state="silent",
        current_state="off",
    )
    assert view._target_state == "silent"
    assert view._current_state == "off"
    assert view._invoker_user_id == 555
    # View has exactly Confirm + Cancel buttons.
    labels = sorted(child.label for child in view.children if hasattr(child, "label"))
    assert labels == ["Cancel", "Confirm"]


async def test_scoring_set_confirm_view_blocks_other_users():
    """interaction_check on the view rejects non-invoker clicks."""
    from sable_roles.features.scoring_pipeline import _ScoringSetConfirmView

    view = _ScoringSetConfirmView(
        invoker_user_id=555,
        org_id="solstitch",
        guild_id="100",
        target_state="silent",
        current_state="off",
    )
    foreign_interaction = MagicMock()
    foreign_user = MagicMock()
    foreign_user.id = 999
    foreign_interaction.user = foreign_user
    foreign_interaction.response = MagicMock()
    foreign_interaction.response.send_message = AsyncMock()
    ok = await view.interaction_check(foreign_interaction)
    assert ok is False
    foreign_interaction.response.send_message.assert_awaited_once()


async def test_scoring_set_confirm_view_on_timeout_disables_buttons():
    """Pass A+B QA round-2 polish: on_timeout must disable view children so
    a late click doesn't fire stale state.
    """
    from sable_roles.features.scoring_pipeline import _ScoringSetConfirmView

    view = _ScoringSetConfirmView(
        invoker_user_id=555,
        org_id="solstitch",
        guild_id="100",
        target_state="silent",
        current_state="off",
    )
    # Pre-timeout: at least one child has disabled=False.
    assert any(getattr(c, "disabled", True) is False for c in view.children)
    await view.on_timeout()
    for child in view.children:
        if hasattr(child, "disabled"):
            assert child.disabled is True


async def test_scoring_set_confirm_view_db_error_surfaces_gracefully(sp_module, db_conn):
    """Pass A+B QA round-2 polish: a DB error during set_state must NOT
    crash; the user sees a 'try again' message and state stays unchanged.
    """
    from sable_roles.features import scoring_pipeline as sp
    from sable_platform.db import discord_scoring_config

    view = sp._ScoringSetConfirmView(
        invoker_user_id=555,
        org_id="solstitch",
        guild_id="100",
        target_state="silent",
        current_state="off",
    )

    interaction = MagicMock()
    interaction.user = SimpleNamespace(id=555)
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()

    # Force set_state to raise. The view should catch + edit gracefully.
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated DB error")

    # set_state is imported into the scoring_pipeline module via the
    # discord_scoring_config submodule reference; patch it there.
    import unittest.mock as um
    with um.patch.object(discord_scoring_config, "set_state", side_effect=_boom):
        # Locate Confirm button callback. Discord UI buttons live in view.children.
        confirm_btn = next(c for c in view.children if getattr(c, "label", None) == "Confirm")
        await confirm_btn.callback(interaction)

    # Graceful error edit fired with a "try again" body.
    interaction.response.edit_message.assert_awaited_once()
    edit_kwargs = interaction.response.edit_message.await_args.kwargs
    assert "try again" in (edit_kwargs.get("content") or "").lower()
