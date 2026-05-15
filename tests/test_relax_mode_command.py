"""Tests for the /relax-mode slash command (mod-only)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_roles.features import fitcheck_streak as mod

from tests.conftest import fetch_audit_rows


def _make_interaction(*, guild_id: int | None, user_id: int, user_role_ids: list[int]):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    # Use a real-ish Member double — isinstance check needs spec=discord.Member.
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
    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": ["999"]})
    monkeypatch.setattr(mod, "get_db", lambda: _make_db_context(db_conn)())

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    mod.register_commands(tree)
    cmd = tree.get_command("relax-mode")
    assert cmd is not None
    return cmd, tree


def test_register_commands_registers_relax_mode(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    assert cmd.name == "relax-mode"
    assert "(mods)" in (cmd.description or "")


@pytest.mark.asyncio
async def test_mod_toggles_relax_mode_on(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[999])
    mode_choice = app_commands.Choice(name="on", value="on")

    await cmd.callback(interaction, mode_choice)

    # DB row reflects relax_mode_on=1
    row = db_conn.execute(
        "SELECT relax_mode_on, updated_by FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    assert row is not None
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["relax_mode_on"] == 1
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["updated_by"] == "555"

    # Audit row written
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_relax_mode_toggled"]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["on"] is True
    assert detail["guild_id"] == "100"
    assert detail["by_user_id"] == "555"
    assert audits[0]["org_id"] == "solstitch"
    assert audits[0]["actor"] == "discord:user:555"
    assert audits[0]["source"] == "sable-roles"

    # Reply confirms
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "relax-mode" in args[0].lower()
    assert " on " in args[0].lower() or args[0].lower().startswith("relax-mode **on")
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_mod_toggles_relax_mode_off(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[999])

    await cmd.callback(interaction, app_commands.Choice(name="off", value="off"))

    row = db_conn.execute(
        "SELECT relax_mode_on FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["relax_mode_on"] == 0

    args, _ = interaction.followup.send.call_args
    assert "off" in args[0].lower()


@pytest.mark.asyncio
async def test_non_mod_denied_no_db_write(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555, user_role_ids=[123])  # not 999

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    # No DB row
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0

    # No audit row
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_relax_mode_toggled"]
    assert audits == []

    # Friendly denial
    args, kwargs = interaction.followup.send.call_args
    assert "not a mod" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_unconfigured_guild_returns_not_configured(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    # guild 999 is not in GUILD_TO_ORG
    interaction = _make_interaction(guild_id=999, user_id=555, user_role_ids=[999])

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0

    args, _ = interaction.followup.send.call_args
    assert "not configured" in args[0].lower()


@pytest.mark.asyncio
async def test_non_member_user_returns_must_be_in_server(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 100
    # discord.User (not Member) — happens when /relax-mode is somehow invoked in DM context
    user = MagicMock(spec=discord.User)
    user.id = 555
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    args, _ = interaction.followup.send.call_args
    assert "inside the server" in args[0].lower() or "must be" in args[0].lower()
