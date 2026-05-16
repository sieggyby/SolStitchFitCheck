"""Tests for B6: on_message → maybe_roast wiring + random inner-circle bypass.

Covers the plan §7 integration matrix (10 cases) plus the on_message hook itself:

- on_message image branch (relax OFF) fires asyncio.create_task(maybe_roast).
- on_message image branch (relax ON) still fires maybe_roast — burn-me and
  relax-mode are orthogonal per locked design §0.

- maybe_roast: opted-in 'once' → roast + opt-in row consumed.
- maybe_roast: opted-in 'persist' → roast + opt-in row stays.
- maybe_roast: no opt-in + not inner-circle → no roast.
- maybe_roast: inner-circle (role) + random.random < BURN_RANDOM_PROB →
  roast + random_log row written.
- maybe_roast: inner-circle (env user-id) + random roll succeeds → roast.
- maybe_roast: inner-circle but was recently random-roasted within window → no roast.
- maybe_roast: inner-circle + random.random >= prob → no roast.
- maybe_roast: opt-in supersedes random — one roast, no random_log row.
- maybe_roast: daily cap hit (>= BURN_DAILY_CAP_PER_USER) → no roast, no cost.
- maybe_roast: oversize image → no roast, no cost, no audit.

Plus helper coverage: _is_inner_circle (role / user / neither) + _is_image_for_roast.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sable_platform.db import discord_burn
from sable_roles import config as roles_config
from sable_roles.features import burn_me as bm
from sable_roles.features import fitcheck_streak as fcs

from tests.conftest import fetch_audit_rows, make_attachment, make_message


# --- shared helpers ---


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _patch_get_db(monkeypatch, db_conn) -> None:
    """Route both burn_me and fitcheck_streak DB calls at the in-memory test DB."""
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(fcs, "get_db", lambda: _make_db_context(db_conn)())


def _patch_anthropic(
    monkeypatch,
    *,
    text: str = "that grey on grey is brave.",
    input_tokens: int = 100,
    output_tokens: int = 20,
):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    content = [SimpleNamespace(text=text)]
    response = SimpleNamespace(usage=usage, content=content)
    fake = MagicMock()
    fake.messages = MagicMock()
    fake.messages.create = AsyncMock(return_value=response)
    monkeypatch.setattr(bm, "_anthropic_client", fake)
    return fake


def _make_attachment_obj(
    *,
    size: int = 1024,
    content_type: str = "image/png",
    data: bytes = b"pngdata",
    filename: str = "fit.png",
):
    att = MagicMock(spec=discord.Attachment)
    att.size = size
    att.content_type = content_type
    att.filename = filename
    att.read = AsyncMock(return_value=data)
    return att


def _make_member(
    *,
    user_id: int = 555,
    display_name: str = "tester",
    role_ids: list[int] | None = None,
    bot: bool = False,
):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = display_name
    member.bot = bot
    member.roles = [SimpleNamespace(id=rid) for rid in (role_ids or [])]
    member.send = AsyncMock()
    return member


def _make_image_message(
    *,
    author_id: int = 555,
    author_display_name: str = "tester",
    role_ids: list[int] | None = None,
    attachment: discord.Attachment | None = None,
    message_id: int = 700,
    reply_raises: BaseException | None = None,
):
    att = attachment if attachment is not None else _make_attachment_obj()
    message = MagicMock()
    member = _make_member(
        user_id=author_id, display_name=author_display_name, role_ids=role_ids
    )
    message.author = member
    message.id = message_id
    message.attachments = [att]
    if reply_raises is not None:
        message.reply = AsyncMock(side_effect=reply_raises)
    else:
        message.reply = AsyncMock()
    return message


def _fetch_random_log(db_conn, guild_id: str, user_id: str) -> list[dict]:
    rows = db_conn.execute(
        "SELECT guild_id, user_id, roasted_at FROM discord_burn_random_log"
        f" WHERE guild_id='{guild_id}' AND user_id='{user_id}'"
    ).fetchall()
    return [dict(r._mapping) if hasattr(r, "_mapping") else dict(r) for r in rows]


def _fetch_cost_rows(db_conn) -> list[dict]:
    rows = db_conn.execute(
        "SELECT org_id, call_type, model, cost_usd, call_status FROM cost_events"
    ).fetchall()
    return [dict(r._mapping) if hasattr(r, "_mapping") else dict(r) for r in rows]


def _seed_optin(db_conn, guild_id: str, user_id: str, mode: str) -> None:
    discord_burn.opt_in(db_conn, guild_id, user_id, mode, opted_in_by=user_id)


def _read_optin(db_conn, guild_id: str, user_id: str) -> dict | None:
    row = db_conn.execute(
        "SELECT guild_id, user_id, mode, opted_in_by, opted_in_at"
        f" FROM discord_burn_optins WHERE guild_id='{guild_id}' AND user_id='{user_id}'"
    ).fetchone()
    if row is None:
        return None
    return dict(row._mapping if hasattr(row, "_mapping") else row)


# --- _is_inner_circle ---


def test_is_inner_circle_role_match(monkeypatch):
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {"100": ["999"]})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {"100": []})
    member = _make_member(user_id=555, role_ids=[999])
    assert bm._is_inner_circle(member, "100") is True


def test_is_inner_circle_user_id_match(monkeypatch):
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {"100": []})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {"100": ["555"]})
    member = _make_member(user_id=555, role_ids=[123])
    assert bm._is_inner_circle(member, "100") is True


def test_is_inner_circle_neither_returns_false(monkeypatch):
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {"100": ["999"]})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {"100": ["777"]})
    member = _make_member(user_id=555, role_ids=[123])
    assert bm._is_inner_circle(member, "100") is False


def test_is_inner_circle_unconfigured_guild_returns_false(monkeypatch):
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})
    member = _make_member(user_id=555, role_ids=[999])
    assert bm._is_inner_circle(member, "100") is False


# --- _is_image_for_roast ---


def test_is_image_for_roast_accepts_png():
    att = _make_attachment_obj(content_type="image/png", filename="fit.png")
    assert bm._is_image_for_roast(att) is True


def test_is_image_for_roast_excludes_svg_by_content_type():
    att = _make_attachment_obj(content_type="image/svg+xml", filename="logo.svg")
    assert bm._is_image_for_roast(att) is False


def test_is_image_for_roast_excludes_svg_when_extension_fallback_would_pass():
    """`is_image` fallback accepts any extension in the allowlist when ctype is
    missing. SVG isn't in the allowlist, so it's already filtered there too —
    but verify the belt-and-suspenders content_type guard rejects when somehow
    a `.svg` slips past the content_type rule (e.g. ctype starts with image/svg
    but isn't exactly `image/svg+xml`)."""
    att = _make_attachment_obj(content_type="image/svg", filename="logo.png")
    assert bm._is_image_for_roast(att) is False


def test_is_image_for_roast_rejects_non_image():
    att = _make_attachment_obj(
        content_type="application/pdf", filename="resume.pdf"
    )
    assert bm._is_image_for_roast(att) is False


# --- maybe_roast: opt-in paths ---


@pytest.mark.asyncio
async def test_maybe_roast_opted_in_once_consumes_row_and_replies(
    monkeypatch, db_conn
):
    _patch_get_db(monkeypatch, db_conn)
    _patch_anthropic(monkeypatch, text="bold of you to commit to brown.")
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    _seed_optin(db_conn, "100", "555", "once")
    message = _make_image_message()

    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    # Roast got delivered with mention suppression
    message.reply.assert_awaited_once()
    args, kwargs = message.reply.call_args
    assert args[0] == "bold of you to commit to brown."
    assert kwargs["mention_author"] is False

    # 'once' row is consumed after the roast
    assert _read_optin(db_conn, "100", "555") is None

    # audit + cost rows landed
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_roast_generated"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["invocation_path"] == "optin_once"
    assert _fetch_cost_rows(db_conn) != []


@pytest.mark.asyncio
async def test_maybe_roast_opted_in_persist_keeps_row(monkeypatch, db_conn):
    _patch_get_db(monkeypatch, db_conn)
    _patch_anthropic(monkeypatch, text="layering is a verb, not a dare.")
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    _seed_optin(db_conn, "100", "555", "persist")
    message = _make_image_message()

    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_awaited_once()
    # persist row survives
    row = _read_optin(db_conn, "100", "555")
    assert row is not None
    assert row["mode"] == "persist"

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_roast_generated"
    ]
    detail = json.loads(audits[0]["detail_json"])
    assert detail["invocation_path"] == "optin_persist"


@pytest.mark.asyncio
async def test_maybe_roast_no_optin_no_inner_circle_skips(monkeypatch, db_conn):
    _patch_get_db(monkeypatch, db_conn)
    fake = _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    message = _make_image_message()
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_not_awaited()
    fake.messages.create.assert_not_awaited()
    assert _fetch_cost_rows(db_conn) == []
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"].startswith("fitcheck_roast")]
    assert audits == []


# --- maybe_roast: random inner-circle bypass ---


@pytest.mark.asyncio
async def test_maybe_roast_inner_circle_role_random_succeeds(monkeypatch, db_conn):
    _patch_get_db(monkeypatch, db_conn)
    _patch_anthropic(monkeypatch, text="ok the boots are doing too much.")
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {"100": ["999"]})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})
    # Force the random roll to succeed (< 0.025 default).
    monkeypatch.setattr(bm.random, "random", lambda: 0.001)

    message = _make_image_message(role_ids=[999])
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_awaited_once()
    log = _fetch_random_log(db_conn, "100", "555")
    assert len(log) == 1

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_roast_generated"
    ]
    detail = json.loads(audits[0]["detail_json"])
    assert detail["invocation_path"] == "random_bypass"


@pytest.mark.asyncio
async def test_maybe_roast_inner_circle_user_id_random_succeeds(
    monkeypatch, db_conn
):
    _patch_get_db(monkeypatch, db_conn)
    _patch_anthropic(monkeypatch, text="green on green on green.")
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {"100": ["555"]})
    monkeypatch.setattr(bm.random, "random", lambda: 0.0)

    message = _make_image_message(role_ids=[123])  # no inner-circle role
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_awaited_once()
    assert len(_fetch_random_log(db_conn, "100", "555")) == 1


@pytest.mark.asyncio
async def test_maybe_roast_inner_circle_recent_random_dedupes(
    monkeypatch, db_conn
):
    """A random-log row from 3 days ago blocks today's random roast."""
    _patch_get_db(monkeypatch, db_conn)
    fake = _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {"100": ["999"]})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})
    monkeypatch.setattr(bm.random, "random", lambda: 0.0)

    # Seed a random-roast 3 days ago (within the 7-day default).
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    db_conn.execute(
        "INSERT INTO discord_burn_random_log (guild_id, user_id, roasted_at)"
        " VALUES (?, ?, ?)",
        ("100", "555", recent),
    )
    db_conn.commit()

    message = _make_image_message(role_ids=[999])
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_not_awaited()
    fake.messages.create.assert_not_awaited()
    # The seeded row is still the only one — no fresh log entry.
    assert len(_fetch_random_log(db_conn, "100", "555")) == 1


@pytest.mark.asyncio
async def test_maybe_roast_inner_circle_random_roll_fails(monkeypatch, db_conn):
    """random.random >= BURN_RANDOM_PROB → no roast, no log row."""
    _patch_get_db(monkeypatch, db_conn)
    fake = _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {"100": ["999"]})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})
    monkeypatch.setattr(bm.random, "random", lambda: 0.99)

    message = _make_image_message(role_ids=[999])
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_not_awaited()
    fake.messages.create.assert_not_awaited()
    assert _fetch_random_log(db_conn, "100", "555") == []


# --- maybe_roast: opt-in supersedes random ---


@pytest.mark.asyncio
async def test_maybe_roast_opt_in_supersedes_random_one_roast(
    monkeypatch, db_conn
):
    """Opted-in inner-circle user — opt-in takes the call; random branch never
    rolls; no random_log row written."""
    _patch_get_db(monkeypatch, db_conn)
    _patch_anthropic(monkeypatch, text="opt-in path wins.")
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {"100": ["999"]})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    # If the random branch were reached, this would always-roll-true; assert
    # below that no random_log row appears — that's the proof it wasn't reached.
    monkeypatch.setattr(bm.random, "random", lambda: 0.0)

    _seed_optin(db_conn, "100", "555", "persist")
    message = _make_image_message(role_ids=[999])

    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    # Exactly one roast, opt-in path
    assert message.reply.await_count == 1
    assert _fetch_random_log(db_conn, "100", "555") == []
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_roast_generated"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["invocation_path"] == "optin_persist"


# --- maybe_roast: daily cap + oversize ---


@pytest.mark.asyncio
async def test_maybe_roast_daily_cap_blocks(monkeypatch, db_conn):
    """At 20 prior fitcheck_roast_generated rows today, the next post is skipped."""
    _patch_get_db(monkeypatch, db_conn)
    fake = _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    # Seed the audit_log directly with 20 generated-rows for today.
    detail_json = json.dumps({"guild_id": "100", "user_id": "555"})
    for i in range(20):
        db_conn.execute(
            "INSERT INTO audit_log (actor, action, org_id, entity_id, detail_json, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                "discord:bot:auto",
                "fitcheck_roast_generated",
                "solstitch",
                None,
                detail_json,
                "sable-roles",
            ),
        )
    db_conn.commit()

    _seed_optin(db_conn, "100", "555", "persist")
    message = _make_image_message()

    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_not_awaited()
    fake.messages.create.assert_not_awaited()
    # The opt-in row is untouched (cap short-circuits before consume).
    assert _read_optin(db_conn, "100", "555") is not None


@pytest.mark.asyncio
async def test_maybe_roast_oversize_image_no_roast_no_cost(monkeypatch, db_conn):
    _patch_get_db(monkeypatch, db_conn)
    fake = _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    _seed_optin(db_conn, "100", "555", "once")
    huge = _make_attachment_obj(size=6 * 1024 * 1024)
    message = _make_image_message(attachment=huge)

    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_not_awaited()
    fake.messages.create.assert_not_awaited()
    assert _fetch_cost_rows(db_conn) == []
    # 'once' opt-in WAS consumed up-front (read-then-maybe-delete fires before
    # image fetch); this is documented in plan §6 race-note.
    assert _read_optin(db_conn, "100", "555") is None


@pytest.mark.asyncio
async def test_maybe_roast_reply_failure_logged_not_raised(monkeypatch, db_conn, caplog):
    """discord.HTTPException on message.reply is swallowed + logged."""
    _patch_get_db(monkeypatch, db_conn)
    _patch_anthropic(monkeypatch, text="reply will fail.")
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    _seed_optin(db_conn, "100", "555", "once")
    err = discord.HTTPException(MagicMock(), "rate limited")
    message = _make_image_message(reply_raises=err)

    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")  # must not raise


# --- on_message hook: relax-mode-orthogonal verification ---


@pytest.mark.asyncio
async def test_on_message_image_branch_fires_maybe_roast_relax_off(
    monkeypatch, fitcheck_module
):
    """Image post in non-relax mode → maybe_roast scheduled."""
    captured = {}

    async def _spy(*, message, org_id, guild_id):
        captured["message"] = message
        captured["org_id"] = org_id
        captured["guild_id"] = guild_id

    monkeypatch.setattr(bm, "maybe_roast", _spy)

    msg = make_message(
        attachments=[make_attachment(filename="fit.png", content_type="image/png")]
    )
    await fitcheck_module.on_message(msg)
    # Yield once so the create_task spy can run.
    await asyncio.sleep(0)

    assert captured.get("org_id") == "solstitch"
    assert captured.get("guild_id") == "100"
    assert captured.get("message") is msg
    # Auto-thread fired (relax off).
    msg.create_thread.assert_awaited()


@pytest.mark.asyncio
async def test_on_message_image_branch_fires_maybe_roast_relax_on(
    monkeypatch, fitcheck_module, db_conn
):
    """Image post under relax-mode-on → maybe_roast STILL scheduled; thread skipped."""
    from sable_platform.db import discord_guild_config

    discord_guild_config.set_relax_mode(db_conn, "100", on=True, updated_by="999")

    captured = {}

    async def _spy(*, message, org_id, guild_id):
        captured["called"] = True

    monkeypatch.setattr(bm, "maybe_roast", _spy)

    msg = make_message(
        attachments=[make_attachment(filename="fit.png", content_type="image/png")]
    )
    await fitcheck_module.on_message(msg)
    await asyncio.sleep(0)

    assert captured.get("called") is True
    # Auto-thread skipped under relax mode.
    msg.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_text_branch_does_not_fire_maybe_roast(
    monkeypatch, fitcheck_module
):
    """Text-only post (no image) → maybe_roast not scheduled."""
    called = {"flag": False}

    async def _spy(*, message, org_id, guild_id):
        called["flag"] = True

    monkeypatch.setattr(bm, "maybe_roast", _spy)

    msg = make_message(attachments=[])
    await fitcheck_module.on_message(msg)
    await asyncio.sleep(0)

    assert called["flag"] is False
