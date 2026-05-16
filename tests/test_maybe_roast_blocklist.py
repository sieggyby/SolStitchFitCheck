"""R4 tests: maybe_roast must gate on the sticky stop-pls blocklist BEFORE
any other DB read (daily-cap check, opt-in consume, random-bypass roll).

The blocklist gate sits at position 0 in the order-of-operations chain
because (a) the user has explicitly consented to be ignored, so no audit
trail is needed for the skip, and (b) R7's peer-roast helpers will reuse
the same gate ordering — moving it later would let blocked targets eat
peer tokens before the bounce.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sable_platform.db import discord_burn, discord_roast
from sable_roles import config as roles_config
from sable_roles.features import burn_me as bm
from sable_roles.features import fitcheck_streak as fcs

from tests.conftest import fetch_audit_rows


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _patch_get_db(monkeypatch, db_conn) -> None:
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(fcs, "get_db", lambda: _make_db_context(db_conn)())


def _patch_anthropic(monkeypatch):
    """Install a fake anthropic client. Tests assert it is NOT awaited when
    the blocklist gate skips."""
    usage = SimpleNamespace(
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    response = SimpleNamespace(usage=usage, content=[SimpleNamespace(text="x")])
    fake = MagicMock()
    fake.messages = MagicMock()
    fake.messages.create = AsyncMock(return_value=response)
    monkeypatch.setattr(bm, "_anthropic_client", fake)
    return fake


def _make_attachment_obj():
    att = MagicMock(spec=discord.Attachment)
    att.size = 1024
    att.content_type = "image/png"
    att.filename = "fit.png"
    att.read = AsyncMock(return_value=b"pngdata")
    return att


def _make_member(*, user_id: int = 555, role_ids: list[int] | None = None):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = "tester"
    member.bot = False
    member.roles = [SimpleNamespace(id=rid) for rid in (role_ids or [])]
    member.send = AsyncMock()
    return member


def _make_image_message(*, author_id: int = 555, role_ids: list[int] | None = None):
    message = MagicMock()
    message.author = _make_member(user_id=author_id, role_ids=role_ids)
    message.id = 700
    message.attachments = [_make_attachment_obj()]
    message.reply = AsyncMock()
    return message


def _block(db_conn, guild_id: str, user_id: str) -> None:
    """Seed a blocklist row via the SP helper (matches the production path)."""
    landed = discord_roast.insert_blocklist(db_conn, guild_id, user_id)
    assert landed is True


# --- Core: blocklist short-circuits BEFORE daily cap ---


@pytest.mark.asyncio
async def test_blocklisted_user_skipped_before_daily_cap_check(monkeypatch, db_conn):
    """The blocklist gate fires first. We patch count_roasts_today to raise so
    the test FAILS if the daily-cap path is reached at all — proves the gate
    sits position 0 in the order of operations."""
    _patch_get_db(monkeypatch, db_conn)
    fake = _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    _block(db_conn, "100", "555")

    def _boom(*_args, **_kwargs):
        raise AssertionError(
            "count_roasts_today was called — blocklist gate did not short-circuit"
        )

    monkeypatch.setattr(discord_burn, "count_roasts_today", _boom)

    message = _make_image_message()
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_not_awaited()
    fake.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocklisted_user_does_not_consume_optin(monkeypatch, db_conn):
    """User had an opt-in row when the blocklist landed (e.g. legacy state).
    The opt-in must NOT be consumed when the blocklist gate fires."""
    _patch_get_db(monkeypatch, db_conn)
    fake = _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    discord_burn.opt_in(db_conn, "100", "555", "once", opted_in_by="555")
    _block(db_conn, "100", "555")

    message = _make_image_message()
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_not_awaited()
    fake.messages.create.assert_not_awaited()
    # 'once' opt-in row survives — gate fired before consume_optin_if_present.
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_burn_optins"
        " WHERE guild_id='100' AND user_id='555'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 1


@pytest.mark.asyncio
async def test_blocklisted_inner_circle_user_skips_random_roll(monkeypatch, db_conn):
    """Blocked + inner-circle user: random.random must NOT be called. Patch
    bm.random.random with a sentinel that fails if invoked."""
    _patch_get_db(monkeypatch, db_conn)
    fake = _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {"100": ["999"]})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    _block(db_conn, "100", "555")

    def _boom_random():
        raise AssertionError(
            "random.random was called — blocklist did not short-circuit"
            " inner-circle random-bypass path"
        )

    monkeypatch.setattr(bm.random, "random", _boom_random)

    message = _make_image_message(role_ids=[999])
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_not_awaited()
    fake.messages.create.assert_not_awaited()
    # No random-log row written.
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_burn_random_log"
        " WHERE guild_id='100' AND user_id='555'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0


@pytest.mark.asyncio
async def test_blocklisted_user_no_audit_row_silent_skip(monkeypatch, db_conn):
    """The blocklist gate is silent — no fitcheck_roast_skipped audit row."""
    _patch_get_db(monkeypatch, db_conn)
    _patch_anthropic(monkeypatch)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    _block(db_conn, "100", "555")
    message = _make_image_message()

    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    roast_audits = [
        a for a in fetch_audit_rows(db_conn) if a["action"].startswith("fitcheck_roast")
    ]
    assert roast_audits == []


@pytest.mark.asyncio
async def test_non_blocklisted_user_proceeds_normally(monkeypatch, db_conn):
    """Regression: ensuring the new gate doesn't break the happy opt-in path."""
    _patch_get_db(monkeypatch, db_conn)
    # Provide a real-shape anthropic response so the pipeline can record it.
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    response = SimpleNamespace(
        usage=usage, content=[SimpleNamespace(text="bold of you to commit to brown.")]
    )
    fake = MagicMock()
    fake.messages = MagicMock()
    fake.messages.create = AsyncMock(return_value=response)
    monkeypatch.setattr(bm, "_anthropic_client", fake)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    discord_burn.opt_in(db_conn, "100", "555", "once", opted_in_by="555")

    message = _make_image_message()
    await bm.maybe_roast(message, org_id="solstitch", guild_id="100")

    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocklist_scoped_per_guild_in_maybe_roast(monkeypatch, db_conn):
    """User blocked in guild A must NOT be blocked in guild B (sanity that
    the SP helper's guild_id filter flows through the gate)."""
    _patch_get_db(monkeypatch, db_conn)
    # Real-shape response since the guild B path should reach the LLM call.
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    response = SimpleNamespace(
        usage=usage, content=[SimpleNamespace(text="other guild roast.")]
    )
    fake = MagicMock()
    fake.messages = MagicMock()
    fake.messages.create = AsyncMock(return_value=response)
    monkeypatch.setattr(bm, "_anthropic_client", fake)
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_ROLES", {})
    monkeypatch.setattr(roles_config, "INNER_CIRCLE_USERS", {})

    _block(db_conn, "100", "555")
    discord_burn.opt_in(db_conn, "200", "555", "once", opted_in_by="555")

    message = _make_image_message()
    await bm.maybe_roast(message, org_id="solstitch", guild_id="200")

    # The guild-200 invocation proceeds because the block is guild-100-scoped.
    message.reply.assert_awaited_once()
