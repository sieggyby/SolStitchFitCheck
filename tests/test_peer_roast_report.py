"""Tests for /peer-roast-report mod-only aggregation command (R9).

R9 ships:
  * `/peer-roast-report` slash command with optional `days:int=30` arg
  * `_handle_peer_roast_report(interaction, days)` underlying handler
  * `_format_peer_roast_report(...)` pure renderer

SP `aggregate_peer_roast_report` shipped in R1; R9 only wraps it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_platform.db import discord_roast
from sable_platform.db.discord_guild_config import set_personalize_mode
from sable_roles.features import roast

from tests.conftest import fetch_audit_rows


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _make_member(*, user_id: int) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = "tester"
    member.roles = []
    return member


def _make_interaction(
    *,
    guild_id: int | None = 100,
    user_id: int = 999,
    in_dm: bool = False,
    user_is_member: bool = True,
) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    if in_dm:
        interaction.guild_id = None
        interaction.guild = None
    else:
        interaction.guild_id = guild_id
        interaction.guild = MagicMock()
    if user_is_member:
        user = _make_member(user_id=user_id)
    else:
        user = MagicMock(spec=discord.User)
        user.id = user_id
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _last_followup(interaction: MagicMock) -> str:
    for call in interaction.followup.send.call_args_list:
        assert call.kwargs.get("ephemeral") is True
    args, _ = interaction.followup.send.call_args
    return args[0]


@pytest.fixture
def patched_roast(monkeypatch, db_conn):
    monkeypatch.setattr(roast, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(roast, "_is_mod", lambda member, guild_id: True)
    monkeypatch.setattr(roast, "PERSONALIZE_ADMINS", {})
    return roast


def _seed_peer_roast(
    db_conn, *, target, actor, post_id, bot_reply_id, flagged=False
):
    """Seed a complete peer-roast audit trail: generated + replied (+ flag)."""
    detail_gen = json.dumps({
        "guild_id": "100", "user_id": target, "actor_user_id": actor,
        "post_id": post_id, "invocation_path": "peer_roast",
    })
    row = db_conn.execute(
        "INSERT INTO audit_log (actor, action, org_id, detail_json, source)"
        " VALUES (?, ?, ?, ?, ?) RETURNING id",
        ("discord:bot:auto", "fitcheck_roast_generated", "solstitch",
         detail_gen, "sable-roles"),
    ).fetchone()
    db_conn.commit()
    gen_id = row[0] if not hasattr(row, "_mapping") else row._mapping["id"]
    detail_rep = json.dumps({
        "audit_log_id": int(gen_id), "bot_reply_id": bot_reply_id,
        "guild_id": "100", "target_user_id": target,
        "actor_user_id": actor, "post_id": post_id,
    })
    db_conn.execute(
        "INSERT INTO audit_log (actor, action, org_id, detail_json, source)"
        " VALUES (?, ?, ?, ?, ?)",
        ("discord:bot:auto", "fitcheck_roast_replied", "solstitch",
         detail_rep, "sable-roles"),
    )
    db_conn.commit()
    if flagged:
        discord_roast.insert_flag(
            db_conn,
            guild_id="100", target_user_id=target, actor_user_id=actor,
            post_id=post_id, bot_reply_id=bot_reply_id,
            reactor_user_id=target,  # self-flag
        )


# ---------------------------------------------------------------------------
# Tree registration
# ---------------------------------------------------------------------------


def test_register_commands_installs_peer_roast_report(patched_roast):
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    roast.register_commands(tree, client=client)
    cmd = tree.get_command("peer-roast-report")
    assert cmd is not None
    assert not isinstance(cmd, app_commands.ContextMenu)
    assert "(mods)" in (cmd.description or "")


# ---------------------------------------------------------------------------
# Gate behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_bounce(patched_roast, db_conn):
    interaction = _make_interaction(in_dm=True)
    await roast._handle_peer_roast_report(interaction, 30)
    assert "inside a server" in _last_followup(interaction).lower()


@pytest.mark.asyncio
async def test_unconfigured_guild_bounce(patched_roast, db_conn):
    interaction = _make_interaction(guild_id=999)
    await roast._handle_peer_roast_report(interaction, 30)
    assert "not configured" in _last_followup(interaction).lower()


@pytest.mark.asyncio
async def test_non_member_bounce(patched_roast, db_conn):
    interaction = _make_interaction(user_is_member=False)
    await roast._handle_peer_roast_report(interaction, 30)
    assert "inside the server" in _last_followup(interaction).lower()


@pytest.mark.asyncio
async def test_non_mod_bounce(patched_roast, db_conn, monkeypatch):
    monkeypatch.setattr(roast, "_is_mod", lambda member, guild_id: False)
    interaction = _make_interaction()
    await roast._handle_peer_roast_report(interaction, 30)
    body = _last_followup(interaction).lower()
    assert "not a mod" in body
    # No audit row on bounce
    assert fetch_audit_rows(db_conn) == []


# ---------------------------------------------------------------------------
# Render correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_window_renders_friendly_message(patched_roast, db_conn):
    interaction = _make_interaction()
    await roast._handle_peer_roast_report(interaction, 30)
    body = _last_followup(interaction)
    assert "no peer-roast activity" in body.lower()
    assert "personalize: off" in body  # default state


@pytest.mark.asyncio
async def test_aggregates_grouped_by_actor_target(patched_roast, db_conn):
    _seed_peer_roast(
        db_conn, target="555", actor="999",
        post_id="p1", bot_reply_id="br1",
    )
    _seed_peer_roast(
        db_conn, target="555", actor="999",
        post_id="p2", bot_reply_id="br2",
    )
    _seed_peer_roast(
        db_conn, target="666", actor="999",
        post_id="p3", bot_reply_id="br3", flagged=True,
    )
    interaction = _make_interaction()
    await roast._handle_peer_roast_report(interaction, 30)
    body = _last_followup(interaction)
    # 999 → 555 has count 2
    assert "999 → 555: 2" in body
    # 999 → 666 has count 1 with 1 flag (self-flag because reactor=target in seed)
    assert "999 → 666: 1" in body
    assert "flags: 1" in body


@pytest.mark.asyncio
async def test_header_shows_personalize_on(patched_roast, db_conn):
    """Pre-toggle personalize_mode_on=True; report header reflects it."""
    set_personalize_mode(
        db_conn, guild_id="100", on=True, updated_by="seed_admin"
    )
    interaction = _make_interaction()
    await roast._handle_peer_roast_report(interaction, 30)
    body = _last_followup(interaction)
    assert "personalize: on" in body


@pytest.mark.asyncio
async def test_blocklist_count_renders(patched_roast, db_conn):
    discord_roast.insert_blocklist(db_conn, "100", "555")
    discord_roast.insert_blocklist(db_conn, "100", "666")
    discord_roast.insert_blocklist(db_conn, "100", "777")
    interaction = _make_interaction()
    await roast._handle_peer_roast_report(interaction, 30)
    body = _last_followup(interaction)
    assert "blocklisted users (all-time): 3" in body
    # Each user id appears.
    assert "555" in body and "666" in body and "777" in body


@pytest.mark.asyncio
async def test_days_param_clamped_to_safe_range(patched_roast, db_conn):
    """Negative or zero days clamps to 1; oversize clamps to 365."""
    interaction = _make_interaction()
    await roast._handle_peer_roast_report(interaction, -5)
    assert "last 1 days" in _last_followup(interaction).lower()

    interaction2 = _make_interaction()
    await roast._handle_peer_roast_report(interaction2, 100000)
    assert "last 365 days" in _last_followup(interaction2).lower()


# ---------------------------------------------------------------------------
# Pure renderer tests
# ---------------------------------------------------------------------------


def test_format_empty():
    body = roast._format_peer_roast_report(
        days=30, personalize_on=False, rows=[], blocklisted=[],
    )
    assert "personalize: off" in body
    assert "no peer-roast activity" in body
    assert "blocklisted users (all-time): 0" in body


def test_format_with_rows_and_blocklist():
    rows = [
        {"actor_user_id": "999", "target_user_id": "555",
         "n": 2, "flag_count": 1, "self_flag_count": 0},
    ]
    body = roast._format_peer_roast_report(
        days=14, personalize_on=True, rows=rows, blocklisted=["aaa"],
    )
    assert "personalize: on" in body
    assert "999 → 555: 2 (flags: 1 · self-flags: 0)" in body
    assert "blocklisted users (all-time): 1" in body
    assert "aaa" in body
