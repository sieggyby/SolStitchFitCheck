"""Tests for the `never` burn-mode lockdown.

When a guild's `current_burn_mode == 'never'`:
  * `maybe_roast` (auto-fire on image posts) returns immediately — no
    opt-in consumption, no random inner-circle bypass
  * `/burn-me` slash command bounces with REDACTED_MESSAGE
  * peer `/roast` right-click bounces with REDACTED_MESSAGE
  * `/my-roasts` bounces with REDACTED_MESSAGE
  * Streak-restoration grant still fires (token + audit), but the DM is
    suppressed (operator decision — preserve continuity for when mode
    flips back without notifying users about an unspendable token)
  * Mod `/roast` (right-click → "Roast this fit" by team-mod) is
    UNAFFECTED — team retains manual roast control during lockdown
  * `/set-burn-mode` is UNAFFECTED — team can flip back to once/persist
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_platform.db import discord_roast
from sable_platform.db.discord_guild_config import set_burn_mode
from sable_roles.features import burn_me as bm
from sable_roles.features import roast

from tests.conftest import fetch_audit_rows


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _set_never(db_conn, guild_id="100"):
    set_burn_mode(db_conn, guild_id, "never", updated_by="seed_admin")


def _set_once(db_conn, guild_id="100"):
    set_burn_mode(db_conn, guild_id, "once", updated_by="seed_admin")


# ---------------------------------------------------------------------------
# is_burn_mode_never SP-side helper
# ---------------------------------------------------------------------------


def test_is_burn_mode_never_default_false(db_conn):
    """No row in discord_guild_config → default current_burn_mode='once'
    → is_burn_mode_never returns False."""
    assert bm.is_burn_mode_never(db_conn, "100") is False


def test_is_burn_mode_never_true_after_set(db_conn):
    _set_never(db_conn)
    assert bm.is_burn_mode_never(db_conn, "100") is True


def test_is_burn_mode_never_false_after_flip_back(db_conn):
    _set_never(db_conn)
    _set_once(db_conn)
    assert bm.is_burn_mode_never(db_conn, "100") is False


def test_is_burn_mode_never_per_guild_isolated(db_conn):
    _set_never(db_conn, guild_id="100")
    set_burn_mode(db_conn, "200", "once", updated_by="seed_admin")
    assert bm.is_burn_mode_never(db_conn, "100") is True
    assert bm.is_burn_mode_never(db_conn, "200") is False


# ---------------------------------------------------------------------------
# /set-burn-mode accepts 'never' as a Choice
# ---------------------------------------------------------------------------


def test_set_burn_mode_choices_include_never(monkeypatch, db_conn):
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    bm.register_commands(tree)
    cmd = tree.get_command("set-burn-mode")
    assert cmd is not None
    # discord.py stores choices on the param object
    mode_param = cmd._params["mode"] if hasattr(cmd, "_params") else None
    if mode_param is not None:
        values = {c.value for c in mode_param.choices}
        assert values == {"once", "persist", "never"}


# ---------------------------------------------------------------------------
# maybe_roast short-circuits in never mode
# ---------------------------------------------------------------------------


def _make_image_message(*, author_id=555, channel_id=200, message_id=700):
    msg = MagicMock(spec=discord.Message)
    author = MagicMock()
    author.id = author_id
    author.bot = False
    author.display_name = "tester"
    msg.author = author
    g = MagicMock()
    g.id = 100
    msg.guild = g
    channel = MagicMock()
    channel.id = channel_id
    msg.channel = channel
    msg.id = message_id
    att = MagicMock(spec=discord.Attachment)
    att.filename = "fit.png"
    att.content_type = "image/png"
    att.size = 1024
    msg.attachments = [att]
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_maybe_roast_never_mode_short_circuits(monkeypatch, db_conn):
    _set_never(db_conn)
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    # Patch generate_roast so we'd see if maybe_roast erroneously called it
    monkeypatch.setattr(
        bm, "generate_roast",
        AsyncMock(side_effect=AssertionError("must not call LLM in never mode")),
    )
    # Seed an opt-in so we know maybe_roast WOULD have fired otherwise
    from sable_platform.db.discord_burn import opt_in
    opt_in(db_conn, "100", "555", "once", "self")

    msg = _make_image_message(author_id=555)
    await bm.maybe_roast(message=msg, org_id="solstitch", guild_id="100")
    msg.reply.assert_not_awaited()
    # Opt-in row preserved (not auto-consumed in never mode)
    rows = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_burn_optins"
        " WHERE guild_id='100' AND user_id='555'"
    ).fetchone()
    assert dict(rows._mapping if hasattr(rows, "_mapping") else rows)["n"] == 1


# ---------------------------------------------------------------------------
# /burn-me bounces in never mode
# ---------------------------------------------------------------------------


def _make_interaction(
    *,
    guild_id=100,
    user_id=555,
    in_dm=False,
):
    interaction = MagicMock(spec=discord.Interaction)
    if in_dm:
        interaction.guild_id = None
        interaction.guild = None
    else:
        interaction.guild_id = guild_id
        interaction.guild = MagicMock()
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.display_name = "u"
    user.roles = []
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _last_followup(interaction):
    args, _ = interaction.followup.send.call_args
    return args[0]


@pytest.mark.asyncio
async def test_burn_me_cmd_bounces_in_never_mode(monkeypatch, db_conn):
    _set_never(db_conn)
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    bm.register_commands(tree)
    cmd = tree.get_command("burn-me")

    interaction = _make_interaction()
    await cmd.callback(interaction, None)
    assert _last_followup(interaction) == bm.REDACTED_MESSAGE
    # No opt-in row landed
    rows = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_burn_optins WHERE guild_id='100'"
    ).fetchone()
    assert dict(rows._mapping if hasattr(rows, "_mapping") else rows)["n"] == 0


# ---------------------------------------------------------------------------
# /my-roasts bounces in never mode
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_roast(monkeypatch, db_conn):
    monkeypatch.setattr(roast, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(roast, "PEER_ROAST_ROLES", {"100": [777]})
    monkeypatch.setattr(roast, "PERSONALIZE_ADMINS", {})
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    return roast


@pytest.mark.asyncio
async def test_my_roasts_bounces_redacted_in_never_mode(patched_roast, db_conn):
    _set_never(db_conn)
    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    assert _last_followup(interaction) == bm.REDACTED_MESSAGE
    # No token granted (lazy-grant didn't fire)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_my_roasts_normal_mode_still_works(patched_roast, db_conn):
    """Sanity — non-never mode still grants tokens + shows status body."""
    _set_once(db_conn)
    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    body = _last_followup(interaction)
    assert body != bm.REDACTED_MESSAGE
    assert "tokens left" in body.lower()


# ---------------------------------------------------------------------------
# Peer /roast bounces in never mode
# ---------------------------------------------------------------------------


def _make_message_for_peer(*, author_id=555, message_id=700):
    msg = MagicMock(spec=discord.Message)
    author = MagicMock()
    author.id = author_id
    author.bot = False
    author.display_name = "target"
    msg.author = author
    channel = MagicMock()
    channel.id = 200
    msg.channel = channel
    msg.id = message_id
    msg.attachments = []
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_peer_roast_bounces_redacted_in_never_mode_even_for_peer(
    patched_roast, db_conn
):
    """A peer-eligible caller (has @Stitch role) still gets REDACTED in
    never mode. Gates the whole surface dark."""
    _set_never(db_conn)
    # User has the peer-roast role 777
    interaction = _make_interaction()
    interaction.user.roles = [MagicMock(id=777)]
    message = _make_message_for_peer()
    await roast._handle_peer_roast(interaction, message)
    assert _last_followup(interaction) == bm.REDACTED_MESSAGE
    # No token consumed
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens WHERE consumed_at IS NOT NULL"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_peer_roast_bounces_redacted_for_non_peer_too_in_never_mode(
    patched_roast, db_conn
):
    """Non-peer (no @Stitch role) caller in never mode ALSO sees REDACTED
    — the lockdown bounce must precede the role-gate friendly bounce so
    the whole surface looks dark + we don't leak "if you had the role..."
    """
    _set_never(db_conn)
    interaction = _make_interaction()  # no roles
    message = _make_message_for_peer()
    await roast._handle_peer_roast(interaction, message)
    body = _last_followup(interaction)
    assert body == bm.REDACTED_MESSAGE
    assert "@stitch" not in body.lower()  # friendly bounce did NOT fire


# ---------------------------------------------------------------------------
# Mod /roast still works in never mode (team retains manual control)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mod_roast_still_fires_in_never_mode(
    patched_roast, db_conn, monkeypatch
):
    """Team mod's right-click → "Roast this fit" path is UNAFFECTED by
    never mode. Sieggy retains the ability to manually burn during a
    lockdown."""
    _set_never(db_conn)
    monkeypatch.setattr(roast, "_FITCHECK_CHANNEL_IDS", {200})
    monkeypatch.setattr(roast, "_is_mod", lambda member, gid: True)
    monkeypatch.setattr(
        roast, "generate_roast",
        AsyncMock(return_value=("manual mod burn", 12345)),
    )
    interaction = _make_interaction(user_id=999)
    message = _make_message_for_peer()
    # Mod path needs an image attachment to reach LLM call
    att = MagicMock(spec=discord.Attachment)
    att.filename = "fit.png"
    att.content_type = "image/png"
    att.size = 1024
    att.read = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")
    message.attachments = [att]
    await roast._handle_mod_roast(interaction, message)
    body = _last_followup(interaction)
    assert body == "roasted ✓"
    message.reply.assert_awaited_once()


# ---------------------------------------------------------------------------
# Streak-restoration grant: token + audit fire, DM suppressed
# ---------------------------------------------------------------------------


def _seed_7_day_streak(db_conn, user_id="555"):
    from datetime import timedelta as td
    today = datetime.now(timezone.utc).date()
    for i in range(7):
        day = today - td(days=i)
        db_conn.execute(
            "INSERT INTO discord_streak_events"
            " (org_id, guild_id, channel_id, post_id, user_id, posted_at,"
            "  counted_for_day, attachment_count, image_attachment_count,"
            "  ingest_source, counts_for_streak)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("solstitch", "100", "200", f"p_{user_id}_{i}", user_id,
             f"{day.isoformat()}T12:00:00Z", day.isoformat(),
             1, 1, "gateway", 1),
        )
    db_conn.commit()


@pytest.mark.asyncio
async def test_restoration_grant_in_never_mode_skips_dm(patched_roast, db_conn):
    """In never mode: grant + audit row land, DM is NOT called."""
    _set_never(db_conn)
    _seed_7_day_streak(db_conn)

    user_double = MagicMock()
    user_double.send = AsyncMock()
    client = MagicMock()
    client.get_user = MagicMock(return_value=user_double)
    client.fetch_user = AsyncMock(return_value=user_double)

    granted = await roast.maybe_grant_restoration_token(
        client=client, user_id="555", guild_id="100", org_id="solstitch",
    )
    assert granted is True
    # Token row landed
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
        " WHERE actor_user_id='555' AND source='streak_restoration'"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 1
    # Audit row landed
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 1
    # DM was NOT called
    user_double.send.assert_not_awaited()
    client.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_restoration_grant_in_once_mode_does_dm(patched_roast, db_conn):
    """Sanity: once mode (default) → DM fires as normal."""
    _set_once(db_conn)
    _seed_7_day_streak(db_conn)

    user_double = MagicMock()
    user_double.send = AsyncMock()
    client = MagicMock()
    client.get_user = MagicMock(return_value=user_double)

    await roast.maybe_grant_restoration_token(
        client=client, user_id="555", guild_id="100", org_id="solstitch",
    )
    user_double.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# /set-burn-mode itself still works in never mode (flip back)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_burn_mode_can_flip_never_back_to_once(
    monkeypatch, db_conn
):
    """In never mode, mod can still run /set-burn-mode mode:once to
    flip back. Critical — otherwise we'd be stuck."""
    _set_never(db_conn)
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(bm, "_is_mod", lambda m, g: True)
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    bm.register_commands(tree)
    cmd = tree.get_command("set-burn-mode")
    interaction = _make_interaction(user_id=999)
    await cmd.callback(interaction, app_commands.Choice(name="once", value="once"))
    # Mode flipped back
    from sable_platform.db.discord_guild_config import get_config
    assert get_config(db_conn, "100")["current_burn_mode"] == "once"
    assert bm.is_burn_mode_never(db_conn, "100") is False
