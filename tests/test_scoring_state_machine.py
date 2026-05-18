"""Integration tests for the 3-state machine — off / silent / revealed.

Confirms:
- Fresh deploy default is `off` (covered redundantly in SP-side test for
  defense in depth; this test exercises it through the bot's call site).
- Transitioning to silent then back to off blocks API calls again.
- silent -> revealed -> silent works (rollback per design sec 10.3).
- No code path in the pipeline writes 'silent' or 'revealed' as initial.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from sqlalchemy import text

from sable_platform.db.discord_scoring_config import get_config, set_state
from sable_platform.db.discord_streaks import upsert_streak_event


def _png_bytes() -> bytes:
    img = Image.new("RGB", (32, 32), (40, 40, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_attachment():
    att = MagicMock()
    att.filename = "fit.png"
    att.content_type = "image/png"
    raw = _png_bytes()
    att.size = len(raw)
    att.read = AsyncMock(return_value=raw)
    return att


def _make_message(*, message_id=1100):
    import datetime as _dt
    author = MagicMock()
    author.id = 555
    author.display_name = "tester"
    msg = MagicMock()
    msg.id = message_id
    msg.author = author
    msg.attachments = [_make_attachment()]
    msg.created_at = _dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=_dt.timezone.utc)
    return msg


def _valid_stub_response():
    block = SimpleNamespace(
        text=json.dumps(
            {
                "axis_scores": {"cohesion": 7, "execution": 7, "concept": 7, "catch": 5},
                "axis_rationales": {
                    "cohesion": "a", "execution": "b", "concept": "c", "catch": "d",
                },
                "catch_detected": None,
                "catch_naming_class": None,
                "description": "neutral",
                "confidence": 0.8,
                "raw_total": 26,
            }
        )
    )
    usage = SimpleNamespace(
        input_tokens=200, output_tokens=100,
        cache_read_input_tokens=180, cache_creation_input_tokens=0,
    )
    return SimpleNamespace(content=[block], usage=usage)


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
    yield mod


# ---------------------------------------------------------------------------
# Default state on first deploy must be 'off'
# ---------------------------------------------------------------------------


def test_first_deploy_default_state_is_off(db_conn):
    """Confirm zero-config guild -> get_config returns state='off'. Critical
    safety invariant — Sieggy must explicitly flip to silent.
    """
    cfg = get_config(db_conn, "1501026101730869290")  # SolStitch's real guild_id
    assert cfg["state"] == "off"


def test_no_pipeline_code_path_writes_initial_silent_or_revealed(db_conn):
    """Defense: scan that scoring_pipeline source contains NO literal
    'silent' or 'revealed' assignment as an initial state. The pipeline
    only READS state. set_state is the only writer, and it requires an
    explicit caller action through /scoring.
    """
    import inspect
    from sable_roles.features import scoring_pipeline as sp

    src = inspect.getsource(sp)
    # set_state(..., state='silent') anywhere in scoring_pipeline would
    # indicate a hidden state-flip — disallowed. The only set_state call
    # path is via the /scoring set slash command, which uses the user's
    # choice value (state.value).
    assert "state=\"silent\"" not in src
    assert "state='silent'" not in src
    assert "state=\"revealed\"" not in src
    assert "state='revealed'" not in src


# ---------------------------------------------------------------------------
# Off -> Silent -> Off cycles cleanly
# ---------------------------------------------------------------------------


async def test_off_then_silent_then_off_blocks_after_rollback(sp_module, db_conn):
    """Flip silent -> score happens. Flip off -> next score short-circuits."""
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "1100", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_valid_stub_response())
    sp_module._anthropic_client = fake_client
    client = SimpleNamespace(user=SimpleNamespace(id=99999))

    # Phase 1: state='off' (default) -> no scoring
    await sp_module.maybe_score_fit(
        message=_make_message(message_id=1100), org_id="solstitch", guild_id="100", client=client,
    )
    n = db_conn.execute(text("SELECT COUNT(*) AS n FROM discord_fitcheck_scores")).fetchone()["n"]
    assert n == 0
    assert fake_client.messages.create.call_count == 0

    # Phase 2: flip to silent
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "1101", "555",
        "2026-05-12T13:00:00Z", "2026-05-12", 1, 1,
    )
    await sp_module.maybe_score_fit(
        message=_make_message(message_id=1101), org_id="solstitch", guild_id="100", client=client,
    )
    n = db_conn.execute(text("SELECT COUNT(*) AS n FROM discord_fitcheck_scores")).fetchone()["n"]
    assert n == 1
    assert fake_client.messages.create.call_count == 1

    # Phase 3: rollback to off
    set_state(db_conn, org_id="solstitch", guild_id="100", state="off", updated_by="ADMIN")
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "1102", "555",
        "2026-05-12T14:00:00Z", "2026-05-12", 1, 1,
    )
    await sp_module.maybe_score_fit(
        message=_make_message(message_id=1102), org_id="solstitch", guild_id="100", client=client,
    )
    n = db_conn.execute(text("SELECT COUNT(*) AS n FROM discord_fitcheck_scores")).fetchone()["n"]
    assert n == 1  # still just the silent-period row
    assert fake_client.messages.create.call_count == 1  # no new API call


# ---------------------------------------------------------------------------
# Revealed -> Silent rollback preserves existing rows
# ---------------------------------------------------------------------------


async def test_revealed_then_silent_keeps_writing_no_rollback_of_rows(sp_module, db_conn):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "1110", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_valid_stub_response())
    sp_module._anthropic_client = fake_client
    client = SimpleNamespace(user=SimpleNamespace(id=99999))

    await sp_module.maybe_score_fit(
        message=_make_message(message_id=1110), org_id="solstitch", guild_id="100", client=client,
    )

    # Rollback to silent — existing rows preserved.
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    row = db_conn.execute(
        text("SELECT score_status FROM discord_fitcheck_scores WHERE post_id = '1110'")
    ).fetchone()
    assert row is not None
    assert row["score_status"] == "success"


# ---------------------------------------------------------------------------
# Audit log captures every state change with prior/new
# ---------------------------------------------------------------------------


def test_audit_chain_captures_every_transition(db_conn):
    """Off -> Silent -> Revealed -> Silent -> Off — 4 audit rows, each with
    prior + new in detail.
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="A")
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="A")
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="A")
    set_state(db_conn, org_id="solstitch", guild_id="100", state="off", updated_by="A")

    rows = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_scoring_state_changed' ORDER BY id"
        )
    ).fetchall()
    transitions = [
        (json.loads(r["detail_json"])["prior_state"], json.loads(r["detail_json"])["new_state"])
        for r in rows
    ]
    assert transitions == [
        ("off", "silent"),
        ("silent", "revealed"),
        ("revealed", "silent"),
        ("silent", "off"),
    ]
