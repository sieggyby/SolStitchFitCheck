"""Unit tests for `_format_streak` (plan §4 `/streak` output).

Synthetic state dicts only — no Discord I/O. Covers both branches called out in
the plan: posted-today vs no-fit-today, and most-reacted vs no-best-fit.
"""
from __future__ import annotations

import discord
import pytest
from discord import app_commands

from sable_roles.features import fitcheck_streak
from sable_roles.features.fitcheck_streak import _format_streak


def _state(**overrides) -> dict:
    base = {
        "current_streak": 0,
        "longest_streak": 0,
        "total_fits": 0,
        "most_reacted_post_id": None,
        "most_reacted_reaction_count": 0,
        "most_reacted_channel_id": None,
        "most_reacted_guild_id": None,
        "today_post_id": None,
        "today_reaction_count": 0,
        "posted_today": False,
    }
    base.update(overrides)
    return base


# --- happy path: streak + posted today + best fit ever ---

def test_format_streak_full_state_posted_today_with_best_fit():
    state = _state(
        current_streak=5,
        longest_streak=12,
        total_fits=18,
        most_reacted_post_id="9001",
        most_reacted_reaction_count=7,
        most_reacted_channel_id="200",
        most_reacted_guild_id="100",
        today_post_id="9002",
        today_reaction_count=3,
        posted_today=True,
    )
    out = _format_streak(state, guild_id=100)
    assert out == (
        "your fit-check streak\n\n"
        "current: 5 day(s)\n"
        "longest: 12 day(s)\n"
        "total fits: 18\n\n"
        "today: posted · 3 reaction(s)\n"
        "best fit ever: <https://discord.com/channels/100/200/9001> · 7 reaction(s)"
    )


# --- no fit today branch ---

def test_format_streak_no_fit_today_renders_no_fit_yet_today():
    state = _state(
        current_streak=0,
        longest_streak=4,
        total_fits=4,
        most_reacted_post_id="9001",
        most_reacted_reaction_count=2,
        most_reacted_channel_id="200",
        most_reacted_guild_id="100",
        posted_today=False,
    )
    out = _format_streak(state, guild_id=100)
    assert "today: no fit yet today" in out
    assert "current: 0 day(s)" in out


def test_format_streak_posted_today_with_zero_reactions():
    state = _state(
        current_streak=1,
        longest_streak=1,
        total_fits=1,
        most_reacted_post_id="9002",
        most_reacted_reaction_count=0,
        most_reacted_channel_id="200",
        most_reacted_guild_id="100",
        today_post_id="9002",
        today_reaction_count=0,
        posted_today=True,
    )
    out = _format_streak(state, guild_id=100)
    assert "today: posted · 0 reaction(s)" in out


# --- no best fit ever branch (user has never posted) ---

def test_format_streak_no_posts_ever_renders_none_yet():
    state = _state()  # all zero / None
    out = _format_streak(state, guild_id=100)
    assert "best fit ever: none yet" in out
    assert "today: no fit yet today" in out
    assert "current: 0 day(s)" in out
    assert "longest: 0 day(s)" in out
    assert "total fits: 0" in out


def test_format_streak_best_post_with_zero_reactions_still_renders_url():
    # User has posts but no one reacted — `most_reacted_post_id` is set, so
    # the URL branch fires with `0 reaction(s)`. Plan §4 keys on post_id, not score.
    state = _state(
        current_streak=1,
        longest_streak=1,
        total_fits=1,
        most_reacted_post_id="9001",
        most_reacted_reaction_count=0,
        most_reacted_channel_id="200",
        most_reacted_guild_id="100",
        posted_today=False,
    )
    out = _format_streak(state, guild_id=100)
    assert "best fit ever: <https://discord.com/channels/100/200/9001> · 0 reaction(s)" in out


# --- URL formatting ---

def test_format_streak_url_is_angle_bracketed_to_suppress_embed():
    state = _state(
        current_streak=1, longest_streak=1, total_fits=1,
        most_reacted_post_id="9001", most_reacted_reaction_count=1,
        most_reacted_channel_id="200", most_reacted_guild_id="100",
        posted_today=True, today_reaction_count=1,
    )
    out = _format_streak(state, guild_id=100)
    assert "<https://discord.com/channels/100/200/9001>" in out


def test_format_streak_uses_state_guild_id_over_interaction_guild_id():
    # If the row's stored guild differs from the interaction guild, the stored
    # one wins so the jump-link still resolves.
    state = _state(
        current_streak=1, longest_streak=1, total_fits=1,
        most_reacted_post_id="9001", most_reacted_reaction_count=1,
        most_reacted_channel_id="200", most_reacted_guild_id="100",
        posted_today=True, today_reaction_count=1,
    )
    out = _format_streak(state, guild_id=999)  # different interaction guild
    assert "channels/100/200/9001" in out
    assert "channels/999/" not in out


def test_format_streak_falls_back_to_interaction_guild_id_when_state_missing():
    # Defensive: if for any reason the compute helper returns
    # `most_reacted_guild_id=None`, fall back to the interaction's guild_id.
    state = _state(
        current_streak=1, longest_streak=1, total_fits=1,
        most_reacted_post_id="9001", most_reacted_reaction_count=1,
        most_reacted_channel_id="200", most_reacted_guild_id=None,
        posted_today=True, today_reaction_count=1,
    )
    out = _format_streak(state, guild_id=100)
    assert "channels/100/200/9001" in out


# --- register_commands smoke ---

def test_register_commands_registers_streak():
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    fitcheck_streak.register_commands(tree)
    cmd = tree.get_command("streak")
    assert cmd is not None
    assert cmd.name == "streak"
    assert "fit-check streak" in (cmd.description or "").lower()


# --- /streak handler integration via the real compute helper ---

@pytest.mark.asyncio
async def test_streak_handler_unconfigured_guild_returns_not_configured(
    monkeypatch, db_conn,
):
    """Guild not in `GUILD_TO_ORG` short-circuits to the friendly message."""
    from unittest.mock import AsyncMock, MagicMock

    from sable_roles.features import fitcheck_streak as mod

    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    mod.register_commands(tree)
    streak_cmd = tree.get_command("streak")
    assert streak_cmd is not None

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 555  # not in GUILD_TO_ORG
    interaction.user = MagicMock()
    interaction.user.id = 123
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await streak_cmd.callback(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "not configured" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_streak_handler_returns_formatted_state_for_configured_guild(
    monkeypatch, db_conn,
):
    """Configured guild: handler defers, computes state, sends formatted text."""
    from unittest.mock import AsyncMock, MagicMock

    from sable_roles.features import fitcheck_streak as mod

    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})

    class _DBContext:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr(mod, "get_db", lambda: _DBContext())

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    mod.register_commands(tree)
    streak_cmd = tree.get_command("streak")

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 100
    interaction.user = MagicMock()
    interaction.user.id = 555
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await streak_cmd.callback(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    body = args[0]
    # Empty-state shape — user 555 has no rows in db_conn yet.
    assert body.startswith("your fit-check streak")
    assert "current: 0 day(s)" in body
    assert "best fit ever: none yet" in body
    assert kwargs.get("ephemeral") is True
