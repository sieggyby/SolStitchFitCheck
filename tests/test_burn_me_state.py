"""Tests for /burn-me + /stop-pls slash commands and opt-in state CRUD (B4).

Covers plan §7 state matrix:
- self opt-in via /burn-me writes a row with mode=current_burn_mode and
  opted_in_by=invoker_id.
- mod-target opt-in via /burn-me @user requires _is_mod; writes a row with
  opted_in_by=mod_id and user_id=target.id.
- non-mod target attempt: denied, no row written, ephemeral reply.
- /stop-pls removes the opt-in row; idempotent ephemeral reply when no row.
- /burn-me 30s invoke cooldown: second invocation within window denied.
- Multi-guild isolation: a row in guild A doesn't affect guild B reads.
- Default mode: unconfigured guild falls back to "once" per
  discord_guild_config.get_config defaults.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_roles.features import burn_me as bm
from sable_roles.features import fitcheck_streak as fcs

from tests.conftest import fetch_audit_rows


# Reset the SHARED cross-module cooldown via .clear() (NOT rebind). Rebinding
# (`monkeypatch.setattr(bm, "_burn_invoke_cooldown", {})`) silently severs the
# identity that roast.py imports by reference, so cross-feature tests would
# stop seeing /burn-me's cooldown. Mirror the pattern in test_roast_peer_path.py.
@pytest.fixture(autouse=True)
def _reset_burn_cooldown():
    bm._burn_invoke_cooldown.clear()
    yield
    bm._burn_invoke_cooldown.clear()


def _make_interaction(
    *,
    guild_id: int | None,
    user_id: int,
    user_role_ids: list[int],
):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = [SimpleNamespace(id=rid) for rid in user_role_ids]
    interaction.user = member
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_target_member(*, user_id: int, display_name: str = "victim"):
    target = MagicMock(spec=discord.Member)
    target.id = user_id
    target.display_name = display_name
    return target


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _register_and_get(monkeypatch, db_conn) -> tuple:
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(fcs, "MOD_ROLES", {"100": ["999"]})

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    bm.register_commands(tree)
    burn_cmd = tree.get_command("burn-me")
    stop_cmd = tree.get_command("stop-pls")
    assert burn_cmd is not None
    assert stop_cmd is not None
    return burn_cmd, stop_cmd


def _read_optin(db_conn, guild_id: str, user_id: str) -> dict | None:
    row = db_conn.execute(
        "SELECT guild_id, user_id, mode, opted_in_by, opted_in_at"
        f" FROM discord_burn_optins WHERE guild_id='{guild_id}' AND user_id='{user_id}'"
    ).fetchone()
    if row is None:
        return None
    return dict(row._mapping if hasattr(row, "_mapping") else row)


def test_register_commands_registers_burn_me_and_stop_pls(monkeypatch, db_conn):
    burn_cmd, stop_cmd = _register_and_get(monkeypatch, db_conn)
    assert burn_cmd.name == "burn-me"
    assert stop_cmd.name == "stop-pls"


@pytest.mark.asyncio
async def test_self_optin_writes_row_default_mode_once(monkeypatch, db_conn):
    """Unconfigured guild_config → current_burn_mode defaults to 'once'."""
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])

    await burn_cmd.callback(interaction, None)

    row = _read_optin(db_conn, "100", "555")
    assert row is not None
    assert row["mode"] == "once"
    assert row["opted_in_by"] == "555"
    assert row["user_id"] == "555"

    audits = [
        a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_burn_optin"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["user_id"] == "555"
    assert detail["opted_in_by"] == "555"
    assert detail["mode"] == "once"
    assert detail["self_optin"] is True
    assert audits[0]["actor"] == "discord:user:555"
    assert audits[0]["org_id"] == "solstitch"
    assert audits[0]["source"] == "sable-roles"

    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "once" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_self_optin_uses_persist_when_guild_mode_persist(monkeypatch, db_conn):
    """A mod set persist via /set-burn-mode → /burn-me picks it up."""
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)
    from sable_platform.db import discord_guild_config

    discord_guild_config.set_burn_mode(db_conn, "100", "persist", updated_by="999")

    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])
    await burn_cmd.callback(interaction, None)

    row = _read_optin(db_conn, "100", "555")
    assert row is not None
    assert row["mode"] == "persist"

    args, _ = interaction.followup.send.call_args
    assert "persist" in args[0].lower()


@pytest.mark.asyncio
async def test_mod_can_target_other_user(monkeypatch, db_conn):
    """Mod with role 999 opts user 777 in; opted_in_by=mod, user_id=target."""
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)
    mod_interaction = _make_interaction(
        guild_id=100, user_id=555, user_role_ids=[999]
    )
    target = _make_target_member(user_id=777, display_name="victim")

    await burn_cmd.callback(mod_interaction, target)

    row = _read_optin(db_conn, "100", "777")
    assert row is not None
    assert row["user_id"] == "777"
    assert row["opted_in_by"] == "555"

    audits = [
        a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_burn_optin"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["user_id"] == "777"
    assert detail["opted_in_by"] == "555"
    assert detail["self_optin"] is False
    # Audit actor is the invoker (mod), not the target.
    assert audits[0]["actor"] == "discord:user:555"

    args, _ = mod_interaction.followup.send.call_args
    # Mod-target replies should name the target.
    assert "victim" in args[0].lower()


@pytest.mark.asyncio
async def test_non_mod_target_denied_no_row(monkeypatch, db_conn):
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)
    non_mod = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])
    target = _make_target_member(user_id=777)

    await burn_cmd.callback(non_mod, target)

    # No row for the target, and no row for the invoker either.
    assert _read_optin(db_conn, "100", "777") is None
    assert _read_optin(db_conn, "100", "555") is None

    audits = [
        a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_burn_optin"
    ]
    assert audits == []

    args, kwargs = non_mod.followup.send.call_args
    assert "mod" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_stop_pls_removes_row(monkeypatch, db_conn):
    burn_cmd, stop_cmd = _register_and_get(monkeypatch, db_conn)

    # Opt in first.
    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])
    await burn_cmd.callback(interaction, None)
    assert _read_optin(db_conn, "100", "555") is not None

    # Now stop. Use a fresh interaction mock so .call_args isolates the stop reply.
    stop_interaction = _make_interaction(
        guild_id=100, user_id=555, user_role_ids=[123]
    )
    await stop_cmd.callback(stop_interaction)

    assert _read_optin(db_conn, "100", "555") is None

    audits = [
        a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_burn_optout"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["user_id"] == "555"
    assert detail["guild_id"] == "100"
    assert audits[0]["actor"] == "discord:user:555"

    args, kwargs = stop_interaction.followup.send.call_args
    assert "no more burns" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_stop_pls_no_optin_writes_no_optout_audit(monkeypatch, db_conn):
    """User runs /stop-pls without ever having opted in. opt_out returns False
    → no fitcheck_burn_optout audit row. R4 still blocklists + audits the
    blocklist insert (covered in test_stop_pls_blocklist.py); this test
    pins the optout-audit absence."""
    _, stop_cmd = _register_and_get(monkeypatch, db_conn)
    stop_interaction = _make_interaction(
        guild_id=100, user_id=555, user_role_ids=[123]
    )

    await stop_cmd.callback(stop_interaction)

    optout_audits = [
        a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_burn_optout"
    ]
    assert optout_audits == []

    args, _ = stop_interaction.followup.send.call_args
    # R4 reply: newly_blocked=True path even without prior opt-in.
    assert "no more burns" in args[0].lower()


@pytest.mark.asyncio
async def test_burn_me_cooldown_blocks_rapid_reinvoke(monkeypatch, db_conn):
    """Second /burn-me from same user within 30s window is denied with no DB write."""
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)

    # First call: lands.
    first = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])
    await burn_cmd.callback(first, None)
    assert _read_optin(db_conn, "100", "555") is not None

    # Wipe the row to prove the second call doesn't even reach opt_in.
    db_conn.execute("DELETE FROM discord_burn_optins WHERE user_id='555'")
    db_conn.commit()

    second = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])
    await burn_cmd.callback(second, None)

    assert _read_optin(db_conn, "100", "555") is None

    args, kwargs = second.followup.send.call_args
    assert "slow down" in args[0].lower() or "try again" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_burn_me_cooldown_expires(monkeypatch, db_conn):
    """After the window elapses, a second invocation lands."""
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)

    # Seed the cooldown dict directly to simulate an expired-window state.
    bm._burn_invoke_cooldown[555] = datetime.now(timezone.utc) - timedelta(seconds=60)

    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])
    await burn_cmd.callback(interaction, None)

    assert _read_optin(db_conn, "100", "555") is not None


@pytest.mark.asyncio
async def test_burn_me_cooldown_is_per_user(monkeypatch, db_conn):
    """Cooldown is keyed by invoker user_id; a different user is not blocked."""
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)

    a = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])
    await burn_cmd.callback(a, None)
    assert _read_optin(db_conn, "100", "555") is not None

    b = _make_interaction(guild_id=100, user_id=666, user_role_ids=[123])
    await burn_cmd.callback(b, None)
    assert _read_optin(db_conn, "100", "666") is not None


@pytest.mark.asyncio
async def test_unconfigured_guild_returns_not_configured_burn_me(
    monkeypatch, db_conn
):
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=999, user_id=555, user_role_ids=[123])

    await burn_cmd.callback(interaction, None)

    assert _read_optin(db_conn, "999", "555") is None
    args, _ = interaction.followup.send.call_args
    assert "not configured" in args[0].lower()


@pytest.mark.asyncio
async def test_unconfigured_guild_returns_not_configured_stop_pls(
    monkeypatch, db_conn
):
    _, stop_cmd = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=999, user_id=555, user_role_ids=[123])

    await stop_cmd.callback(interaction)

    args, _ = interaction.followup.send.call_args
    assert "not configured" in args[0].lower()


@pytest.mark.asyncio
async def test_non_member_user_rejected_burn_me(monkeypatch, db_conn):
    """Slash command invoked by a discord.User (DM context) is rejected before DB."""
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 100
    user = MagicMock(spec=discord.User)
    user.id = 555
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await burn_cmd.callback(interaction, None)

    assert _read_optin(db_conn, "100", "555") is None
    args, _ = interaction.followup.send.call_args
    assert "inside the server" in args[0].lower() or "must be" in args[0].lower()


@pytest.mark.asyncio
async def test_multi_guild_isolation_optin(monkeypatch, db_conn):
    """Opt-in to guild A does not produce a row in guild B."""
    burn_cmd, _ = _register_and_get(monkeypatch, db_conn)
    # Add a second guild to GUILD_TO_ORG so /burn-me works there too.
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch", "200": "solstitch"})

    a = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])
    await burn_cmd.callback(a, None)

    assert _read_optin(db_conn, "100", "555") is not None
    assert _read_optin(db_conn, "200", "555") is None
