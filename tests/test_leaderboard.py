"""Tests for Pass D /leaderboard slash command + pure formatters.

Covers:
  - _truncate_catch / _build_jump_link / _format_entry / _format_leaderboard
  - empty-board text path
  - 30d window vs all-time labelling
  - public vs ephemeral footer wording
  - rate-limit gate (1/min per user) + recovery
  - display-name cache hit/miss + Discord HTTP failure fallback
  - register_commands wires the slash command onto the tree
  - guild-not-configured + missing-guild paths

Heavy SP-side query coverage is in
tests/db/test_discord_fitcheck_leaderboard.py — this file focuses on
the leaderboard.py surface (formatters + slash-command branches) with
mocked SP helpers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands

from sable_roles.features import leaderboard


# ---------------------------------------------------------------------------
# Autouse: reset module-level dicts between tests so state doesn't leak
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state():
    leaderboard._INVOKE_COOLDOWN.clear()
    leaderboard._DISPLAY_NAME_CACHE.clear()
    yield
    leaderboard._INVOKE_COOLDOWN.clear()
    leaderboard._DISPLAY_NAME_CACHE.clear()


# ---------------------------------------------------------------------------
# _truncate_catch
# ---------------------------------------------------------------------------


def test_truncate_catch_none_returns_empty():
    assert leaderboard._truncate_catch(None) == ""


def test_truncate_catch_short_passes_through():
    assert leaderboard._truncate_catch("short") == "short"


def test_truncate_catch_long_gets_ellipsis():
    long = "a" * 200
    out = leaderboard._truncate_catch(long, max_len=80)
    assert len(out) == 80
    assert out.endswith("...")


def test_truncate_catch_exact_max_passes_through():
    s = "a" * 80
    assert leaderboard._truncate_catch(s, max_len=80) == s


# ---------------------------------------------------------------------------
# _build_jump_link
# ---------------------------------------------------------------------------


def test_jump_link_with_channel():
    url = leaderboard._build_jump_link("g1", "c1", "p1")
    assert url == "https://discord.com/channels/g1/c1/p1"


def test_jump_link_missing_channel_falls_back():
    url = leaderboard._build_jump_link("g1", None, "p1")
    assert "discord.com/channels/g1" in url
    # No channel/post path — graceful fallback.
    assert url == "https://discord.com/channels/g1"


# ---------------------------------------------------------------------------
# _format_entry
# ---------------------------------------------------------------------------


def test_format_entry_with_catch():
    out = leaderboard._format_entry(
        rank=1,
        display_name="monasex",
        percentile=87.4,
        catch="late-90s Raf bomber silhouette",
        jump_link="https://discord.com/channels/g/c/p",
    )
    # int(round(87.4)) == 87
    assert " 1. monasex · 87 · caught: late-90s Raf bomber silhouette" in out
    assert "https://discord.com/channels/g/c/p" in out


def test_format_entry_without_catch_omits_caught_clause():
    out = leaderboard._format_entry(
        rank=2,
        display_name="sieggy",
        percentile=58.0,
        catch=None,
        jump_link="https://discord.com/channels/g/c/p",
    )
    assert "caught:" not in out
    assert " 2. sieggy · 58" in out


def test_format_entry_rank_padding():
    out = leaderboard._format_entry(
        rank=10,
        display_name="x",
        percentile=50.0,
        catch=None,
        jump_link="L",
    )
    # Right-pad single-digit ranks for alignment; 10 → "10" no pad needed.
    assert out.startswith("10. x · 50")


# ---------------------------------------------------------------------------
# _format_leaderboard
# ---------------------------------------------------------------------------


def _row(post_id, user_id, pct, catch=None, channel_id="chan_1", guild_id="g1"):
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "post_id": post_id,
        "user_id": user_id,
        "percentile": pct,
        "catch_detected": catch,
        "reveal_fired_at": "2026-05-17T12:00:00Z",
        "reveal_trigger": "reactions",
        "posted_at": "2026-05-17T11:55:00Z",
        "catch_naming_class": "family_only" if catch else None,
    }


def test_format_leaderboard_empty_rows_returns_empty_text():
    out = leaderboard._format_leaderboard(
        [], {}, board="top_revealed", window="all_time", public=False
    )
    assert out == leaderboard.EMPTY_BOARD_TEXT


def test_format_leaderboard_top_revealed_header():
    rows = [_row("p1", "u1", 87.0, "Raf silhouette")]
    out = leaderboard._format_leaderboard(
        rows, {"u1": "monasex"}, board="top_revealed", window="all_time", public=False
    )
    assert "top revealed fits — #fitcheck — all-time" in out


def test_format_leaderboard_best_per_user_header():
    rows = [_row("p1", "u1", 87.0)]
    out = leaderboard._format_leaderboard(
        rows, {"u1": "monasex"}, board="best_per_user", window="30d", public=False
    )
    assert "best per user — #fitcheck — last 30 days" in out


def test_format_leaderboard_ephemeral_footer_mentions_public_toggle():
    rows = [_row("p1", "u1", 87.0)]
    out = leaderboard._format_leaderboard(
        rows, {"u1": "monasex"}, board="top_revealed", window="all_time", public=False
    )
    assert "ephemeral" in out
    assert "public:true to share" in out


def test_format_leaderboard_public_footer_drops_share_hint():
    rows = [_row("p1", "u1", 87.0)]
    out = leaderboard._format_leaderboard(
        rows, {"u1": "monasex"}, board="top_revealed", window="all_time", public=True
    )
    assert "ephemeral" not in out
    assert "public:true to share" not in out


def test_format_leaderboard_missing_display_name_falls_back():
    rows = [_row("p1", "user_long_id_string", 87.0)]
    # Empty display_names dict → uses fallback.
    out = leaderboard._format_leaderboard(
        rows, {}, board="top_revealed", window="all_time", public=False
    )
    assert "unknown" in out


def test_format_leaderboard_truncates_long_catch():
    long_catch = "Raf SS03 Consumed bomber paired with a Helmut Lang minimalist tee in a perfect grey-on-grey palette that absolutely sells the late-90s archive moment to anyone who knows"
    rows = [_row("p1", "u1", 87.0, long_catch)]
    out = leaderboard._format_leaderboard(
        rows, {"u1": "monasex"}, board="top_revealed", window="all_time", public=False
    )
    # Truncated body must be present + the "..." marker.
    assert "..." in out
    assert long_catch not in out  # Full text shouldn't appear


# ---------------------------------------------------------------------------
# Rate-limit gate
# ---------------------------------------------------------------------------


def test_rate_limit_allows_first_call():
    assert leaderboard._check_and_set_rate_limit(123) is None


def test_rate_limit_blocks_second_call_within_window():
    leaderboard._check_and_set_rate_limit(123)
    remaining = leaderboard._check_and_set_rate_limit(123)
    assert remaining is not None
    assert 0 < remaining <= leaderboard._COOLDOWN_SECONDS


def test_rate_limit_allows_after_window_elapses(monkeypatch):
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    leaderboard._INVOKE_COOLDOWN[123] = now - timedelta(seconds=120)
    # Real datetime.now reflects current real time (well past now+120s).
    remaining = leaderboard._check_and_set_rate_limit(123)
    assert remaining is None


def test_rate_limit_is_per_user_not_global():
    leaderboard._check_and_set_rate_limit(123)
    # Different user, second call — must not be rate-limited.
    assert leaderboard._check_and_set_rate_limit(456) is None


def test_rate_limit_cap_evicts_oldest():
    # Force the cap to a small value via monkeypatch.
    import sable_roles.features.leaderboard as lb

    original_cap = lb._COOLDOWN_CAP
    lb._COOLDOWN_CAP = 3
    try:
        lb._check_and_set_rate_limit(1)
        lb._check_and_set_rate_limit(2)
        lb._check_and_set_rate_limit(3)
        lb._check_and_set_rate_limit(4)  # triggers eviction of user 1
        assert 1 not in lb._INVOKE_COOLDOWN
        assert {2, 3, 4}.issubset(set(lb._INVOKE_COOLDOWN.keys()))
    finally:
        lb._COOLDOWN_CAP = original_cap


# ---------------------------------------------------------------------------
# Display-name cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_display_name_caches_result():
    mock_user = SimpleNamespace(display_name="monasex", name="mona_user")
    mock_client = MagicMock()
    mock_client.fetch_user = AsyncMock(return_value=mock_user)

    name1 = await leaderboard._resolve_display_name(mock_client, 12345)
    name2 = await leaderboard._resolve_display_name(mock_client, 12345)
    assert name1 == "monasex"
    assert name2 == "monasex"
    # Cached — fetch_user called only once.
    assert mock_client.fetch_user.call_count == 1


@pytest.mark.asyncio
async def test_resolve_display_name_handles_not_found():
    mock_client = MagicMock()
    mock_client.fetch_user = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "user not found")
    )
    name = await leaderboard._resolve_display_name(mock_client, 999)
    assert name.startswith("unknown")


@pytest.mark.asyncio
async def test_resolve_display_name_handles_http_failure():
    mock_client = MagicMock()
    mock_client.fetch_user = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "transient")
    )
    name = await leaderboard._resolve_display_name(mock_client, 999)
    assert name.startswith("unknown")


# ---------------------------------------------------------------------------
# register_commands
# ---------------------------------------------------------------------------


def _real_client_tree():
    """Build a real (un-connected) Client + CommandTree. Tree internals
    poke client.http during init, so MagicMock(spec=Client) doesn't work.
    The Client never logs in or connects in tests.
    """
    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    return client, tree


def test_register_commands_adds_leaderboard():
    """Smoke that the slash command lands on the tree without error."""
    client, tree = _real_client_tree()
    leaderboard.register_commands(tree, client=client)
    cmd_names = {c.name for c in tree.get_commands()}
    assert "leaderboard" in cmd_names


# ---------------------------------------------------------------------------
# Slash-command callback paths (interaction.response.send_message assertions)
# ---------------------------------------------------------------------------


def _make_interaction(user_id=42, guild_id=100):
    """Build a minimal mocked Interaction for callback invocation."""
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.guild_id = guild_id
    interaction.client = MagicMock()
    interaction.client.fetch_user = AsyncMock(
        return_value=SimpleNamespace(display_name="alice", name="alice")
    )
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _build_tree_with_leaderboard():
    client, tree = _real_client_tree()
    leaderboard.register_commands(tree, client=client)
    cmd = next(c for c in tree.get_commands() if c.name == "leaderboard")
    return cmd


@pytest.mark.asyncio
async def test_callback_missing_guild_rejected():
    cmd = _build_tree_with_leaderboard()
    interaction = _make_interaction(guild_id=None)
    await cmd.callback(interaction)
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "guild" in args[0].lower()


@pytest.mark.asyncio
async def test_callback_unconfigured_guild_rejected(monkeypatch):
    monkeypatch.setattr(leaderboard, "GUILD_TO_ORG", {})
    cmd = _build_tree_with_leaderboard()
    interaction = _make_interaction(guild_id=999)
    await cmd.callback(interaction)
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "scored mode" in args[0].lower()


@pytest.mark.asyncio
async def test_callback_empty_board_renders_empty_text(monkeypatch):
    monkeypatch.setattr(leaderboard, "GUILD_TO_ORG", {"100": "solstitch"})

    with patch.object(
        leaderboard.discord_fitcheck_scores,
        "list_top_revealed_fits",
        return_value=[],
    ) as mock_helper:
        cmd = _build_tree_with_leaderboard()
        interaction = _make_interaction()
        await cmd.callback(interaction)

    mock_helper.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert args[0] == leaderboard.EMPTY_BOARD_TEXT
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_callback_public_true_makes_non_ephemeral(monkeypatch):
    monkeypatch.setattr(leaderboard, "GUILD_TO_ORG", {"100": "solstitch"})
    rows = [_row("p1", "1234", 87.0)]
    public_choice = app_commands.Choice(name="public", value="public")  # unused
    with patch.object(
        leaderboard.discord_fitcheck_scores,
        "list_top_revealed_fits",
        return_value=rows,
    ):
        cmd = _build_tree_with_leaderboard()
        interaction = _make_interaction()
        await cmd.callback(interaction, public=True)
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is False


@pytest.mark.asyncio
async def test_callback_rate_limit_short_circuits_query(monkeypatch):
    monkeypatch.setattr(leaderboard, "GUILD_TO_ORG", {"100": "solstitch"})
    cmd = _build_tree_with_leaderboard()
    interaction1 = _make_interaction(user_id=42)
    interaction2 = _make_interaction(user_id=42)

    with patch.object(
        leaderboard.discord_fitcheck_scores,
        "list_top_revealed_fits",
        return_value=[],
    ) as mock_helper:
        await cmd.callback(interaction1)
        await cmd.callback(interaction2)

    # First call queried the helper; second call was rate-limited BEFORE
    # the query — helper invoked only once.
    assert mock_helper.call_count == 1
    args2, kwargs2 = interaction2.response.send_message.call_args
    assert "rate limited" in args2[0].lower()


@pytest.mark.asyncio
async def test_callback_best_per_user_routes_to_correct_helper(monkeypatch):
    monkeypatch.setattr(leaderboard, "GUILD_TO_ORG", {"100": "solstitch"})

    with patch.object(
        leaderboard.discord_fitcheck_scores,
        "list_best_per_user_revealed",
        return_value=[],
    ) as mock_bpu, patch.object(
        leaderboard.discord_fitcheck_scores,
        "list_top_revealed_fits",
        return_value=[],
    ) as mock_top:
        cmd = _build_tree_with_leaderboard()
        interaction = _make_interaction()
        board_choice = app_commands.Choice(name="best_per_user", value="best_per_user")
        await cmd.callback(interaction, board=board_choice)

    mock_bpu.assert_called_once()
    mock_top.assert_not_called()


@pytest.mark.asyncio
async def test_callback_window_30d_passes_since_iso(monkeypatch):
    monkeypatch.setattr(leaderboard, "GUILD_TO_ORG", {"100": "solstitch"})

    with patch.object(
        leaderboard.discord_fitcheck_scores,
        "list_top_revealed_fits",
        return_value=[],
    ) as mock_helper:
        cmd = _build_tree_with_leaderboard()
        interaction = _make_interaction()
        window_choice = app_commands.Choice(name="30d", value="30d")
        await cmd.callback(interaction, window=window_choice)

    args, kwargs = mock_helper.call_args
    assert kwargs.get("since_iso") is not None


@pytest.mark.asyncio
async def test_callback_uses_allowed_mentions_none(monkeypatch):
    """Defends against display_name mention-injection on /leaderboard
    output. Mirrors reveal_pipeline's AllowedMentions.none() contract.
    """
    monkeypatch.setattr(leaderboard, "GUILD_TO_ORG", {"100": "solstitch"})
    rows = [_row("p1", "1234", 87.0)]
    with patch.object(
        leaderboard.discord_fitcheck_scores,
        "list_top_revealed_fits",
        return_value=rows,
    ):
        cmd = _build_tree_with_leaderboard()
        interaction = _make_interaction()
        await cmd.callback(interaction)
    args, kwargs = interaction.response.send_message.call_args
    am = kwargs.get("allowed_mentions")
    assert isinstance(am, discord.AllowedMentions)
    assert am.everyone is False
    assert am.users is False
    assert am.roles is False


@pytest.mark.asyncio
async def test_callback_helper_exception_is_user_friendly(monkeypatch):
    monkeypatch.setattr(leaderboard, "GUILD_TO_ORG", {"100": "solstitch"})

    with patch.object(
        leaderboard.discord_fitcheck_scores,
        "list_top_revealed_fits",
        side_effect=RuntimeError("DB broken"),
    ):
        cmd = _build_tree_with_leaderboard()
        interaction = _make_interaction()
        await cmd.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert "couldn't load" in args[0].lower()
    assert kwargs.get("ephemeral") is True
