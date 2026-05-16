"""Tests for the /set-burn-mode slash command (mod-only). B3 first slice —
mod gate, mode update, audit row, ephemeral reply.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_roles.features import burn_me as bm
from sable_roles.features import fitcheck_streak as fcs

from tests.conftest import fetch_audit_rows


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


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _register_and_get(monkeypatch, db_conn) -> tuple:
    # burn_me reads GUILD_TO_ORG + get_db from its own module scope; _is_mod
    # reads MOD_ROLES from fitcheck_streak's scope (imported verbatim).
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(fcs, "MOD_ROLES", {"100": ["999"]})

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    bm.register_commands(tree)
    cmd = tree.get_command("set-burn-mode")
    assert cmd is not None
    return cmd, tree


def test_register_commands_registers_set_burn_mode(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    assert cmd.name == "set-burn-mode"
    assert "(mods)" in (cmd.description or "")


@pytest.mark.asyncio
async def test_mod_sets_burn_mode_once(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[999])

    await cmd.callback(interaction, app_commands.Choice(name="once", value="once"))

    row = db_conn.execute(
        "SELECT current_burn_mode, updated_by FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    assert row is not None
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["current_burn_mode"] == "once"
    assert rd["updated_by"] == "555"

    audits = [
        a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_burn_mode_set"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["mode"] == "once"
    assert detail["guild_id"] == "100"
    assert detail["by_user_id"] == "555"
    assert audits[0]["org_id"] == "solstitch"
    assert audits[0]["actor"] == "discord:user:555"
    assert audits[0]["source"] == "sable-roles"

    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "once" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_mod_sets_burn_mode_persist(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[999])

    await cmd.callback(
        interaction, app_commands.Choice(name="persist", value="persist")
    )

    row = db_conn.execute(
        "SELECT current_burn_mode FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["current_burn_mode"] == "persist"

    args, _ = interaction.followup.send.call_args
    assert "persist" in args[0].lower()


@pytest.mark.asyncio
async def test_mod_can_toggle_mode_and_relax_state_preserved(monkeypatch, db_conn):
    """set_burn_mode preserves relax_mode_on; verify second call replaces mode only."""
    cmd, _ = _register_and_get(monkeypatch, db_conn)

    # Seed a relax_mode_on=1 row first, like /relax-mode did.
    from sable_platform.db import discord_guild_config

    discord_guild_config.set_relax_mode(db_conn, "100", on=True, updated_by="777")

    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[999])
    await cmd.callback(
        interaction, app_commands.Choice(name="persist", value="persist")
    )

    row = db_conn.execute(
        "SELECT relax_mode_on, current_burn_mode, updated_by"
        " FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["relax_mode_on"] == 1  # preserved
    assert rd["current_burn_mode"] == "persist"
    assert rd["updated_by"] == "555"  # most recent toggler


@pytest.mark.asyncio
async def test_non_mod_denied_no_db_write(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])

    await cmd.callback(interaction, app_commands.Choice(name="once", value="once"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["n"] == 0

    audits = [
        a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_burn_mode_set"
    ]
    assert audits == []

    args, kwargs = interaction.followup.send.call_args
    assert "not a mod" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_unconfigured_guild_returns_not_configured(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=999, user_id=555, user_role_ids=[999])

    await cmd.callback(interaction, app_commands.Choice(name="once", value="once"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["n"] == 0

    args, _ = interaction.followup.send.call_args
    assert "not configured" in args[0].lower()


@pytest.mark.asyncio
async def test_non_member_user_returns_must_be_in_server(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 100
    user = MagicMock(spec=discord.User)
    user.id = 555
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await cmd.callback(interaction, app_commands.Choice(name="once", value="once"))

    args, _ = interaction.followup.send.call_args
    assert (
        "inside the server" in args[0].lower() or "must be" in args[0].lower()
    )


@pytest.mark.asyncio
async def test_no_guild_id_returns_not_configured(monkeypatch, db_conn):
    """Slash command invoked in DM (guild_id=None) → not-configured branch."""
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=None, user_id=555, user_role_ids=[999])

    await cmd.callback(interaction, app_commands.Choice(name="once", value="once"))

    args, _ = interaction.followup.send.call_args
    assert "not configured" in args[0].lower()
