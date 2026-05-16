"""Tests for the mod-only "Roast this fit" message context-menu command (R5).

R5 ships ONE new surface — the V1 mod-only context-menu /roast. Peer routing,
token economy, vibe injection, and actor_user_id audit detail all land in
R6/R7/R11. R5 uses the SHIPPED `generate_roast` signature with no new kwargs.

Test surface (~16 cases): tree registration, DM bounce, unconfigured-guild
bounce, non-mod bounce (silent on audit), non-fitcheck-channel bounce,
cooldown bounce, cooldown expiry, SHARED-cooldown contract with /burn-me,
bot-author skip, blocklisted skip, daily-cap skip, no-image skip, image-fetch
fail, happy path (full pipeline + reply + ✓), LLM refusal, reply HTTPException
still ✓, direct seam invocation locking the load-bearing private handler.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_roles.features import burn_me as bm
from sable_roles.features import roast

from tests.conftest import fetch_audit_rows


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """Clear the SHARED cooldown dict between tests without rebinding it.

    Rebinding (`roast._burn_invoke_cooldown = {}`) would break sharing with
    burn_me, defeating the contract this whole chunk is designed to lock.
    Mutating the existing dict in-place preserves the cross-module identity.
    """
    bm._burn_invoke_cooldown.clear()
    yield
    bm._burn_invoke_cooldown.clear()


@pytest.fixture
def patched_roast(monkeypatch, db_conn):
    """Wire roast.py against an in-memory DB + a permissive mod gate + the
    fit-check channel id set. Returns the patched roast module."""
    monkeypatch.setattr(roast, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(roast, "_FITCHECK_CHANNEL_IDS", {200})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(roast, "_is_mod", lambda member, guild_id: True)
    return roast


def _make_member(*, user_id: int) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    return member


def _make_author(*, user_id: int, display_name: str = "tester", bot: bool = False):
    author = MagicMock()
    author.id = user_id
    author.display_name = display_name
    author.bot = bot
    return author


def _make_attachment(
    *,
    filename: str = "fit.png",
    content_type: str | None = "image/png",
    size: int = 1024,
    data: bytes = b"\x89PNG\r\n\x1a\n",
    read_raises: BaseException | None = None,
) -> MagicMock:
    att = MagicMock(spec=discord.Attachment)
    att.filename = filename
    att.content_type = content_type
    att.size = size
    if read_raises is not None:
        att.read = AsyncMock(side_effect=read_raises)
    else:
        att.read = AsyncMock(return_value=data)
    return att


def _make_message(
    *,
    channel_id: int = 200,
    message_id: int = 700,
    author: MagicMock | None = None,
    attachments: list | None = None,
    reply_raises: BaseException | None = None,
) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    channel = MagicMock()
    channel.id = channel_id
    message.channel = channel
    message.id = message_id
    message.author = author or _make_author(user_id=555)
    # `attachments or [...]` would mis-fire on `attachments=[]` — explicit
    # None-check so the "no image" case can pass an empty list intentionally.
    message.attachments = (
        [_make_attachment()] if attachments is None else attachments
    )
    if reply_raises is not None:
        message.reply = AsyncMock(side_effect=reply_raises)
    else:
        message.reply = AsyncMock()
    return message


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
    """Return the lowercased body of the most-recent followup.send call.

    Also asserts EVERY followup.send call in this interaction's history fired
    with `ephemeral=True`. Plan §0.1 mandates every bounce + the success ✓ are
    ephemeral — a future refactor that drops the kwarg would silently leak
    gate state ("you're not a mod", blocklist hits, etc.) into the public
    channel. Pinning per-call (not just on the last one) defends against a
    single missed kwarg slipping through.
    """
    for call in interaction.followup.send.call_args_list:
        assert call.kwargs.get("ephemeral") is True, (
            f"non-ephemeral followup.send call: {call}"
        )
    args, _ = interaction.followup.send.call_args
    return args[0].lower()


def _register_and_get_context_menu(patched_roast):
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    patched_roast.register_commands(tree, client=client)
    cmd = tree.get_command("Roast this fit", type=discord.AppCommandType.message)
    return cmd, tree


# ---------------------------------------------------------------------------
# Tree registration
# ---------------------------------------------------------------------------


def test_register_commands_installs_context_menu(patched_roast):
    """The "Roast this fit" message context menu is registered on the tree."""
    cmd, tree = _register_and_get_context_menu(patched_roast)
    assert cmd is not None
    assert isinstance(cmd, app_commands.ContextMenu)
    assert cmd.name == "Roast this fit"
    msg_cmds = tree.get_commands(type=discord.AppCommandType.message)
    assert any(c.name == "Roast this fit" for c in msg_cmds)


def test_burn_invoke_cooldown_is_shared_module_object(patched_roast):
    """Plan §0.1 shares the 30s cooldown with /burn-me. The dict object
    imported into roast.py MUST be the same object as burn_me's — rebinding
    it would silently break the cross-feature throttle contract."""
    assert roast._burn_invoke_cooldown is bm._burn_invoke_cooldown


# ---------------------------------------------------------------------------
# Bounce gates (no DB writes, no audit rows)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_context_bounces(patched_roast, db_conn):
    interaction = _make_interaction(in_dm=True)
    message = _make_message()
    await roast._handle_mod_roast(interaction, message)
    assert "inside a server" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_unconfigured_guild_bounces(patched_roast, db_conn):
    """Guild present but not in GUILD_TO_ORG → "not configured" reply."""
    interaction = _make_interaction(guild_id=999)
    message = _make_message()
    await roast._handle_mod_roast(interaction, message)
    assert "not configured" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_non_member_user_bounces(patched_roast, db_conn):
    """interaction.user is a discord.User (not Member) — matches /burn-me's
    defense against the cross-guild interaction shape."""
    interaction = _make_interaction(user_is_member=False)
    message = _make_message()
    await roast._handle_mod_roast(interaction, message)
    assert "inside the server" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_context_menu_routes_non_mod_to_peer_handler(
    patched_roast, db_conn, monkeypatch
):
    """R7 dispatch: the context-menu closure routes non-mods to the peer
    handler. Verifies the dispatch by patching `_handle_peer_roast` and
    asserting it was awaited with the (interaction, message) pair while
    `_handle_mod_roast` was NOT touched.

    Replaces the R5 "mod-only bounce" test — R7's peer path now exists so
    non-mods are routed there instead of being silently denied.
    """
    monkeypatch.setattr(roast, "_is_mod", lambda member, guild_id: False)
    peer_calls: list[tuple] = []

    async def _fake_peer(interaction, message):
        peer_calls.append((interaction, message))

    mod_calls: list[tuple] = []

    async def _fake_mod(interaction, message):
        mod_calls.append((interaction, message))

    monkeypatch.setattr(roast, "_handle_peer_roast", _fake_peer)
    monkeypatch.setattr(roast, "_handle_mod_roast", _fake_mod)

    cmd, _ = _register_and_get_context_menu(patched_roast)
    interaction = _make_interaction()
    message = _make_message()
    await cmd.callback(interaction, message)
    assert len(peer_calls) == 1
    assert peer_calls[0][0] is interaction
    assert peer_calls[0][1] is message
    assert mod_calls == []


@pytest.mark.asyncio
async def test_non_fitcheck_channel_bounces(patched_roast, db_conn):
    """Target message's channel id is NOT in _FITCHECK_CHANNEL_IDS → skip."""
    interaction = _make_interaction()
    message = _make_message(channel_id=999)  # not in {200}
    await roast._handle_mod_roast(interaction, message)
    assert "fit-check channel" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


# ---------------------------------------------------------------------------
# Cooldown — own + shared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_bounces_when_recent(patched_roast):
    """Seed the shared cooldown dict for the mod → mod /roast bounces with the
    remaining-seconds message that mirrors /burn-me's pattern."""
    mod_id = 999
    bm._burn_invoke_cooldown[mod_id] = datetime.now(timezone.utc)
    interaction = _make_interaction(user_id=mod_id)
    message = _make_message()
    await roast._handle_mod_roast(interaction, message)
    body = _last_followup(interaction)
    assert "slow down" in body
    assert "try again" in body


@pytest.mark.asyncio
async def test_cooldown_expires_after_window(patched_roast, monkeypatch):
    """Cooldown set 60s ago is past the 30s window → invocation proceeds."""
    monkeypatch.setattr(
        roast, "generate_roast", AsyncMock(return_value=("ok", 42))
    )
    mod_id = 999
    bm._burn_invoke_cooldown[mod_id] = datetime.now(timezone.utc) - timedelta(
        seconds=60
    )
    interaction = _make_interaction(user_id=mod_id)
    message = _make_message()
    await roast._handle_mod_roast(interaction, message)
    assert _last_followup(interaction) == "roasted ✓"


@pytest.mark.asyncio
async def test_shared_cooldown_with_burn_me_blocks_mod_roast(
    monkeypatch, db_conn
):
    """Plan §0.1 SHARED-cooldown contract: a /burn-me invocation seeding the
    cooldown dict must block a subsequent mod /roast keyed on the same
    actor_user_id. Drives both surfaces against the SAME dict object and
    verifies the cross-feature throttle."""
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(bm, "_is_mod", lambda member, guild_id: True)
    monkeypatch.setattr(roast, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(roast, "_FITCHECK_CHANNEL_IDS", {200})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(roast, "_is_mod", lambda member, guild_id: True)

    mod_id = 999
    # Drive /burn-me first — registers it on a fresh tree, invokes via callback.
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    bm.register_commands(tree)
    burn_me_cmd = tree.get_command("burn-me")
    burn_interaction = _make_interaction(user_id=mod_id)
    await burn_me_cmd.callback(burn_interaction, None)
    # /burn-me seeded the cooldown.
    assert mod_id in bm._burn_invoke_cooldown

    # Now mod /roast bounces on the SHARED cooldown.
    roast_interaction = _make_interaction(user_id=mod_id)
    message = _make_message()
    await roast._handle_mod_roast(roast_interaction, message)
    assert "slow down" in _last_followup(roast_interaction)


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_author_target_bounces(patched_roast, db_conn):
    """Plan §0.1 says author must be non-bot."""
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=12345, bot=True))
    await roast._handle_mod_roast(interaction, message)
    assert "bot's post" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_blocklisted_target_bounces(patched_roast, db_conn):
    """Pre-seed discord_burn_blocklist via insert_blocklist → /roast skips."""
    from sable_platform.db.discord_roast import insert_blocklist

    inserted = insert_blocklist(db_conn, guild_id="100", user_id="555")
    assert inserted is True

    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_mod_roast(interaction, message)
    assert "opted out" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_daily_cap_target_bounces(patched_roast, db_conn):
    """Seed 20 fitcheck_roast_generated audit rows for the target today →
    next mod /roast attempt against the target bounces with the cap message."""
    detail_json = json.dumps({"guild_id": "100", "user_id": "555"})
    for _ in range(20):
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

    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_mod_roast(interaction, message)
    assert "daily cap" in _last_followup(interaction)


# ---------------------------------------------------------------------------
# Image gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_image_attachment_bounces(patched_roast, db_conn):
    interaction = _make_interaction()
    message = _make_message(attachments=[])
    await roast._handle_mod_roast(interaction, message)
    assert "no image attachment" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_image_fetch_failure_bounces(patched_roast, db_conn):
    """Oversize attachment (>5MB) → _fetch_image_bytes returns None → skip."""
    big = _make_attachment(size=6 * 1024 * 1024)
    interaction = _make_interaction()
    message = _make_message(attachments=[big])
    await roast._handle_mod_roast(interaction, message)
    assert "couldn't fetch image" in _last_followup(interaction)


# ---------------------------------------------------------------------------
# Happy path + LLM outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_writes_audit_replies_and_confirms(
    patched_roast, db_conn, monkeypatch
):
    """Full pipeline: cost + audit rows land via generate_roast, the bot
    replies on the target message with mention_author=False, and the mod
    sees the ephemeral "roasted ✓"."""
    # Patch the Anthropic client so generate_roast runs its real cost+audit
    # write path against the in-memory DB.
    from types import SimpleNamespace

    fake_response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=1500,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        content=[SimpleNamespace(text="that drip is offshore.")],
    )
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(bm, "_anthropic_client", fake_client)
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())

    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555, display_name="zoot"))

    await roast._handle_mod_roast(interaction, message)

    # Cost row landed via generate_roast.
    cost_rows = db_conn.execute(
        "SELECT call_type, call_status FROM cost_events"
    ).fetchall()
    cost_rows = [
        dict(r._mapping if hasattr(r, "_mapping") else r) for r in cost_rows
    ]
    assert len(cost_rows) == 1
    assert cost_rows[0]["call_type"] == "sable_roles_burn"
    assert cost_rows[0]["call_status"] == "success"

    # Audit row stamped with invocation_path="mod_roast".
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_roast_generated"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["invocation_path"] == "mod_roast"
    assert detail["user_id"] == "555"
    assert detail["guild_id"] == "100"

    # Inline reply on the target message, ping suppressed.
    message.reply.assert_awaited_once()
    args, kwargs = message.reply.call_args
    assert args[0] == "that drip is offshore."
    assert kwargs.get("mention_author") is False

    # Mod sees the ✓ confirm.
    assert _last_followup(interaction) == "roasted ✓"


@pytest.mark.asyncio
async def test_llm_refusal_sends_skipped_no_reply(patched_roast, monkeypatch):
    """When generate_roast returns None (model refused), the target message
    is NOT replied to and the mod sees the model-refused skip message."""
    monkeypatch.setattr(roast, "generate_roast", AsyncMock(return_value=None))
    interaction = _make_interaction()
    message = _make_message()
    await roast._handle_mod_roast(interaction, message)
    message.reply.assert_not_awaited()
    assert "model refused" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_reply_http_exception_still_confirms_roasted(
    patched_roast, monkeypatch
):
    """Plan §0.1: when the LLM call succeeds but the reply HTTPs out, the
    mod STILL sees "roasted ✓" — the roast was billed and from the mod's
    UX the roast happened. Locks the "✓ on bill-and-fail-to-deliver" contract."""
    monkeypatch.setattr(
        roast, "generate_roast", AsyncMock(return_value=("a roast", 42))
    )
    interaction = _make_interaction()
    message = _make_message(
        reply_raises=discord.HTTPException(MagicMock(), "reply blocked")
    )
    await roast._handle_mod_roast(interaction, message)
    message.reply.assert_awaited_once()
    assert _last_followup(interaction) == "roasted ✓"


# ---------------------------------------------------------------------------
# Seam load-bearing test (addresses R3 follow-up #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_mod_roast_seam_invokable_directly(patched_roast, monkeypatch):
    """The private `_handle_mod_roast` seam is invoked directly here (not via
    the context-menu callback) so a future refactor that dead-code-removes
    the seam will fail this test. Closes the R3 follow-up #2 gap (R3's
    `_handle_set_personalize_mode` seam was never exercised directly)."""
    monkeypatch.setattr(
        roast, "generate_roast", AsyncMock(return_value=("r", 7))
    )
    interaction = _make_interaction()
    message = _make_message()
    # Direct module-level attribute reference — no tree.get_command path.
    await roast._handle_mod_roast(interaction, message)
    assert _last_followup(interaction) == "roasted ✓"
