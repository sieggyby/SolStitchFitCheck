"""Tests for the peer-/roast path + DM + 🚩 flag handler (R7).

R7 ships:
  * `_handle_peer_roast(interaction, message)` — full peer-token gate chain
  * `_send_peer_roast_dm(...)` — silent DM to target w/ 5min cooldown
  * `_handle_flag_reaction(payload, *, client)` — 🚩 reaction → flag row
  * Context-menu dispatch (closure): mod → `_handle_mod_roast`; non-mod →
    `_handle_peer_roast`.
  * `register(client)` — wires on_raw_reaction_add.
  * Audit actions added: `fitcheck_peer_roast_consumed`,
    `fitcheck_peer_roast_refunded`, `fitcheck_peer_roast_skipped`,
    `fitcheck_roast_replied`, `fitcheck_peer_roast_dm_skipped`,
    `fitcheck_peer_roast_flagged`.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_platform.db import discord_roast
from sable_roles.features import burn_me as bm
from sable_roles.features import roast

from tests.conftest import fetch_audit_rows


# ---------------------------------------------------------------------------
# Fixture + helpers
# ---------------------------------------------------------------------------


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


@pytest.fixture(autouse=True)
def _reset_state():
    bm._burn_invoke_cooldown.clear()
    roast._target_dm_cooldown.clear()
    yield
    bm._burn_invoke_cooldown.clear()
    roast._target_dm_cooldown.clear()


def _make_member(*, user_id: int, role_ids: list[int] | None = None) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    roles = []
    for rid in role_ids or []:
        role = MagicMock(spec=discord.Role)
        role.id = rid
        roles.append(role)
    member.roles = roles
    member.display_name = f"peer{user_id}"
    return member


def _make_author(
    *,
    user_id: int,
    display_name: str = "tester",
    bot: bool = False,
    is_member: bool = True,
    role_ids: list[int] | None = None,
    dm_raises: BaseException | None = None,
) -> MagicMock:
    if is_member:
        author = _make_member(user_id=user_id, role_ids=role_ids)
        author.display_name = display_name
        author.bot = bot
    else:
        author = MagicMock(spec=discord.User)
        author.id = user_id
        author.display_name = display_name
        author.bot = bot
    if dm_raises is not None:
        author.send = AsyncMock(side_effect=dm_raises)
    else:
        author.send = AsyncMock()
    return author


def _make_attachment(
    *,
    filename: str = "fit.png",
    content_type: str | None = "image/png",
    size: int = 1024,
    data: bytes = b"\x89PNG\r\n\x1a\n",
) -> MagicMock:
    att = MagicMock(spec=discord.Attachment)
    att.filename = filename
    att.content_type = content_type
    att.size = size
    att.read = AsyncMock(return_value=data)
    return att


def _make_message(
    *,
    channel_id: int = 200,
    message_id: int = 700,
    author: MagicMock | None = None,
    attachments: list | None = None,
    reply_returns: int | None = 9999,
    reply_raises: BaseException | None = None,
) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    channel = MagicMock()
    channel.id = channel_id
    message.channel = channel
    message.id = message_id
    message.author = author or _make_author(user_id=555)
    message.attachments = (
        [_make_attachment()] if attachments is None else attachments
    )
    if reply_raises is not None:
        message.reply = AsyncMock(side_effect=reply_raises)
    else:
        reply_msg = MagicMock()
        reply_msg.id = reply_returns
        reply_msg.jump_url = f"https://discord.com/channels/100/{channel_id}/{reply_returns}"
        message.reply = AsyncMock(return_value=reply_msg)
    return message


def _make_interaction(
    *,
    guild_id: int | None = 100,
    user_id: int = 999,
    in_dm: bool = False,
    user_is_member: bool = True,
    role_ids: list[int] | None = None,
) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    if in_dm:
        interaction.guild_id = None
        interaction.guild = None
    else:
        interaction.guild_id = guild_id
        interaction.guild = MagicMock()
    if user_is_member:
        user = _make_member(user_id=user_id, role_ids=role_ids or [777])
    else:
        user = MagicMock(spec=discord.User)
        user.id = user_id
        user.display_name = "ghost"
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _last_followup(interaction: MagicMock) -> str:
    for call in interaction.followup.send.call_args_list:
        assert call.kwargs.get("ephemeral") is True, (
            f"non-ephemeral followup.send call: {call}"
        )
    args, _ = interaction.followup.send.call_args
    return args[0].lower()


@pytest.fixture
def patched_roast(monkeypatch, db_conn):
    monkeypatch.setattr(roast, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(roast, "_FITCHECK_CHANNEL_IDS", {200})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(roast, "PEER_ROAST_ROLES", {"100": [777]})
    monkeypatch.setattr(roast, "PERSONALIZE_ADMINS", {})
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    return roast


@pytest.fixture
def patched_roast_with_stub_llm(monkeypatch, patched_roast):
    """patched_roast + stub generate_roast that returns ("roast text", audit_id)
    without touching the LLM. Returns the AsyncMock so tests can inspect calls."""
    stub = AsyncMock(return_value=("burn", 12345))
    monkeypatch.setattr(roast, "generate_roast", stub)
    return stub


# ---------------------------------------------------------------------------
# Context-menu router dispatch
# ---------------------------------------------------------------------------


def _register_and_get_context_menu(patched_roast):
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    patched_roast.register_commands(tree, client=client)
    cmd = tree.get_command("Roast this fit", type=discord.AppCommandType.message)
    return cmd, tree


@pytest.mark.asyncio
async def test_context_menu_routes_mod_to_mod_handler(
    patched_roast, db_conn, monkeypatch
):
    monkeypatch.setattr(roast, "_is_mod", lambda member, guild_id: True)
    mod_calls: list[tuple] = []

    async def _fake_mod(interaction, message):
        mod_calls.append((interaction, message))

    monkeypatch.setattr(roast, "_handle_mod_roast", _fake_mod)
    monkeypatch.setattr(
        roast, "_handle_peer_roast", AsyncMock(side_effect=AssertionError)
    )

    cmd, _ = _register_and_get_context_menu(patched_roast)
    interaction = _make_interaction()
    message = _make_message()
    await cmd.callback(interaction, message)
    assert len(mod_calls) == 1


@pytest.mark.asyncio
async def test_context_menu_routes_non_mod_to_peer_handler(
    patched_roast, db_conn, monkeypatch
):
    monkeypatch.setattr(roast, "_is_mod", lambda member, guild_id: False)
    peer_calls: list[tuple] = []

    async def _fake_peer(interaction, message):
        peer_calls.append((interaction, message))

    monkeypatch.setattr(roast, "_handle_peer_roast", _fake_peer)
    monkeypatch.setattr(
        roast, "_handle_mod_roast", AsyncMock(side_effect=AssertionError)
    )

    cmd, _ = _register_and_get_context_menu(patched_roast)
    interaction = _make_interaction()
    message = _make_message()
    await cmd.callback(interaction, message)
    assert len(peer_calls) == 1


@pytest.mark.asyncio
async def test_context_menu_routes_dm_caller_to_peer(
    patched_roast, db_conn, monkeypatch
):
    """DM-context calls have no guild — must route to peer (which bounces
    on its DM check rather than mod's). Defends the closure's guild_id
    check from collapsing into "DM caller is treated as mod"."""
    monkeypatch.setattr(roast, "_is_mod", lambda member, guild_id: True)
    peer_calls = []

    async def _fake_peer(interaction, message):
        peer_calls.append((interaction, message))

    monkeypatch.setattr(roast, "_handle_peer_roast", _fake_peer)
    monkeypatch.setattr(
        roast, "_handle_mod_roast", AsyncMock(side_effect=AssertionError)
    )

    cmd, _ = _register_and_get_context_menu(patched_roast)
    interaction = _make_interaction(in_dm=True)
    message = _make_message()
    await cmd.callback(interaction, message)
    assert len(peer_calls) == 1


# ---------------------------------------------------------------------------
# Bounce gates — no token charged, no consumed audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_bounces(patched_roast, db_conn):
    interaction = _make_interaction(in_dm=True)
    message = _make_message()
    await roast._handle_peer_roast(interaction, message)
    assert "inside a server" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_unconfigured_guild_bounces(patched_roast, db_conn):
    interaction = _make_interaction(guild_id=999)
    message = _make_message()
    await roast._handle_peer_roast(interaction, message)
    assert "not configured" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_non_member_user_bounces(patched_roast, db_conn):
    interaction = _make_interaction(user_is_member=False)
    message = _make_message()
    await roast._handle_peer_roast(interaction, message)
    assert "inside the server" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_no_peer_role_bounces_with_friendly_message(patched_roast, db_conn):
    """Plan §0.2 — peer eligibility role gate. No audit on bounce (probing
    the surface stays cheap)."""
    interaction = _make_interaction(role_ids=[222])  # role 222 not in {777}
    message = _make_message()
    await roast._handle_peer_roast(interaction, message)
    body = _last_followup(interaction)
    assert "@stitch role" in body
    assert fetch_audit_rows(db_conn) == []
    # NO token granted on a role-gate bounce
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_non_fitcheck_channel_bounces(patched_roast, db_conn):
    interaction = _make_interaction()
    message = _make_message(channel_id=999)
    await roast._handle_peer_roast(interaction, message)
    assert "fit-check channel" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_cooldown_bounces_when_recent(patched_roast):
    bm._burn_invoke_cooldown[999] = datetime.now(timezone.utc)
    interaction = _make_interaction()
    message = _make_message()
    await roast._handle_peer_roast(interaction, message)
    assert "slow down" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_shared_cooldown_with_burn_me_blocks_peer(
    patched_roast_with_stub_llm, db_conn, monkeypatch
):
    """SHARED-cooldown contract: /burn-me seeding the cooldown blocks
    subsequent peer /roast. R5 already tests this for mod /roast; this
    locks the same contract on the peer surface."""
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(bm, "_is_mod", lambda m, g: True)
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    bm.register_commands(tree)
    burn_cmd = tree.get_command("burn-me")
    burn_interaction = _make_interaction(user_id=888)
    await burn_cmd.callback(burn_interaction, None)
    assert 888 in bm._burn_invoke_cooldown

    peer_interaction = _make_interaction(user_id=888)
    message = _make_message()
    await roast._handle_peer_roast(peer_interaction, message)
    assert "slow down" in _last_followup(peer_interaction)


@pytest.mark.asyncio
async def test_bot_author_target_bounces(patched_roast, db_conn):
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=12345, bot=True))
    await roast._handle_peer_roast(interaction, message)
    assert "bot's post" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_self_roast_blocked(patched_roast, db_conn):
    """Peer can't roast self — economy makes no sense (spending your token
    on yourself). Mod path allows self-roast per plan §0.1; peer does not."""
    interaction = _make_interaction(user_id=555)
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert "own fit" in _last_followup(interaction)
    assert fetch_audit_rows(db_conn) == []


# ---------------------------------------------------------------------------
# Pre-token gate chain (skipped audit fires; no token charged)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocklisted_target_skipped_no_token(patched_roast, db_conn):
    discord_roast.insert_blocklist(db_conn, "100", "555")
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert "opted out" in _last_followup(interaction)
    # No token consumed
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
        " WHERE consumed_at IS NOT NULL"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0
    skipped = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_skipped"
    ]
    assert len(skipped) == 1
    assert json.loads(skipped[0]["detail_json"])["reason"] == "target_blocklisted"


@pytest.mark.asyncio
async def test_daily_cap_target_skipped_no_token(patched_roast, db_conn):
    detail_json = json.dumps({"guild_id": "100", "user_id": "555"})
    for _ in range(20):
        db_conn.execute(
            "INSERT INTO audit_log (actor, action, org_id, entity_id, detail_json, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("discord:bot:auto", "fitcheck_roast_generated", "solstitch", None,
             detail_json, "sable-roles"),
        )
    db_conn.commit()
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert "daily cap" in _last_followup(interaction)
    skipped = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_skipped"
    ]
    assert len(skipped) == 1
    assert json.loads(skipped[0]["detail_json"])["reason"] == "target_daily_cap"


@pytest.mark.asyncio
async def test_target_month_cap_skipped(patched_roast, db_conn, monkeypatch):
    """Seed 3 consumed tokens against target → 4th peer /roast bounces."""
    monkeypatch.setattr(
        discord_roast, "count_target_peer_roasts_this_month",
        lambda conn, gid, tid, **kw: 3,
    )
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert "month's peer-roast cap" in _last_followup(interaction)
    skipped = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_skipped"
    ]
    assert json.loads(skipped[0]["detail_json"])["reason"] == "target_month_cap"


@pytest.mark.asyncio
async def test_target_month_cap_bypassed_for_inner_circle(
    patched_roast_with_stub_llm, db_conn, monkeypatch
):
    """Inner-circle target → 3/month cap bypassed even when count >= 3."""
    monkeypatch.setattr(
        discord_roast, "count_target_peer_roasts_this_month",
        lambda conn, gid, tid, **kw: 10,  # way over
    )
    monkeypatch.setattr(roast, "_is_inner_circle", lambda member, guild_id: True)
    interaction = _make_interaction()
    target = _make_author(user_id=555)
    message = _make_message(author=target)
    await roast._handle_peer_roast(interaction, message)
    assert _last_followup(interaction) == "roasted ✓"


@pytest.mark.asyncio
async def test_actor_target_cooldown_skipped(patched_roast, db_conn, monkeypatch):
    monkeypatch.setattr(
        discord_roast, "cooldown_active_between",
        lambda conn, gid, aid, tid, **kw: True,
    )
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert "roasted them recently" in _last_followup(interaction)
    skipped = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_skipped"
    ]
    assert json.loads(skipped[0]["detail_json"])["reason"] == "actor_target_cooldown"


@pytest.mark.asyncio
async def test_actor_target_cooldown_bypassed_for_inner_circle(
    patched_roast_with_stub_llm, db_conn, monkeypatch
):
    monkeypatch.setattr(
        discord_roast, "cooldown_active_between",
        lambda conn, gid, aid, tid, **kw: True,
    )
    monkeypatch.setattr(roast, "_is_inner_circle", lambda m, g: True)
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert _last_followup(interaction) == "roasted ✓"


# ---------------------------------------------------------------------------
# Token economy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tokens_available_bounces_without_audit(
    patched_roast, db_conn
):
    """Pre-grant + pre-consume so monthly + restoration are both spent → bounce."""
    ym = discord_roast._current_year_month()
    discord_roast.grant_monthly_token(db_conn, "100", "999", year_month=ym)
    tok = discord_roast.available_token(db_conn, "100", "999", year_month=ym)
    discord_roast.consume_token(
        db_conn, tok["id"], target_user_id="some_other", post_id="prior"
    )

    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert "no tokens left this month" in _last_followup(interaction)
    # Only the prior consume_audit doesn't exist (we set it via SP, not seam).
    # Our handler must NOT have written a fitcheck_peer_roast_consumed row.
    consumed = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_consumed"
    ]
    assert consumed == []


@pytest.mark.asyncio
async def test_lazy_grant_fires_on_first_peer_roast(
    patched_roast_with_stub_llm, db_conn
):
    """First peer /roast of the month grants the token + consumes it →
    audit_log carries both `fitcheck_peer_roast_token_granted` AND
    `fitcheck_peer_roast_consumed` rows."""
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert _last_followup(interaction) == "roasted ✓"

    audits = fetch_audit_rows(db_conn)
    granted = [a for a in audits if a["action"] == "fitcheck_peer_roast_token_granted"]
    consumed = [a for a in audits if a["action"] == "fitcheck_peer_roast_consumed"]
    assert len(granted) == 1
    assert len(consumed) == 1
    assert json.loads(consumed[0]["detail_json"])["token_source"] == "monthly"


@pytest.mark.asyncio
async def test_consume_token_race_loss_bounces_no_audit_no_refund(
    patched_roast_with_stub_llm, db_conn, monkeypatch
):
    """Two concurrent peer-roasts both pass `available_token` for the same
    row; SP's `consume_token` is atomic so the loser gets False back. The
    handler must bounce with "race condition" ephemeral and write NO
    consumed audit + NO refund audit + NO reply (refund-on-bounce path
    isn't taken because nothing was consumed)."""
    # Pre-grant a token so available_token has something to return.
    ym = discord_roast._current_year_month()
    discord_roast.grant_monthly_token(db_conn, "100", "999", year_month=ym)

    # Force the race-loss branch.
    monkeypatch.setattr(
        discord_roast, "consume_token",
        lambda conn, token_id, **kw: False,
    )

    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert "race condition" in _last_followup(interaction)
    consumed = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_consumed"
    ]
    refunded = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_refunded"
    ]
    assert consumed == []
    assert refunded == []
    # generate_roast never called — token was never consumed.
    patched_roast_with_stub_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_token_writes_audit_with_token_id_in_entity_id(
    patched_roast_with_stub_llm, db_conn
):
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)

    row = db_conn.execute(
        "SELECT entity_id, detail_json FROM audit_log"
        " WHERE action='fitcheck_peer_roast_consumed'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    detail = json.loads(rd["detail_json"])
    assert rd["entity_id"] == str(detail["token_id"])


# ---------------------------------------------------------------------------
# Refund paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_image_refunds_token(patched_roast, db_conn):
    """No-image target → token REFUNDED + fitcheck_peer_roast_refunded audit."""
    interaction = _make_interaction()
    message = _make_message(
        author=_make_author(user_id=555),
        attachments=[],
    )
    await roast._handle_peer_roast(interaction, message)
    assert "refunded" in _last_followup(interaction)

    # Token row exists but consumed_at is back to NULL after refund.
    tok = discord_roast.available_token(db_conn, "100", "999")
    assert tok is not None  # back to unspent
    assert tok["consumed_at"] is None

    refunded = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_refunded"
    ]
    assert len(refunded) == 1
    assert json.loads(refunded[0]["detail_json"])["reason"] == "no_image"


@pytest.mark.asyncio
async def test_image_fetch_failure_refunds_token(patched_roast, db_conn):
    big = _make_attachment(size=6 * 1024 * 1024)
    interaction = _make_interaction()
    message = _make_message(
        author=_make_author(user_id=555),
        attachments=[big],
    )
    await roast._handle_peer_roast(interaction, message)
    assert "couldn't fetch image" in _last_followup(interaction)

    tok = discord_roast.available_token(db_conn, "100", "999")
    assert tok is not None
    refunded = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_refunded"
    ]
    assert len(refunded) == 1
    assert json.loads(refunded[0]["detail_json"])["reason"] == "image_fetch_failed"


@pytest.mark.asyncio
async def test_llm_refusal_refunds_token(patched_roast, db_conn, monkeypatch):
    """LLM returns None (refused / failed) → token refunded + audit."""
    monkeypatch.setattr(roast, "generate_roast", AsyncMock(return_value=None))
    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    assert "refunded" in _last_followup(interaction)

    tok = discord_roast.available_token(db_conn, "100", "999")
    assert tok is not None  # back to unspent
    refunded = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_refunded"
    ]
    assert len(refunded) == 1
    assert json.loads(refunded[0]["detail_json"])["reason"] == "llm_refused_or_failed"


@pytest.mark.asyncio
async def test_refund_token_failure_writes_refund_failed_audit(
    patched_roast, db_conn, monkeypatch
):
    """If SP `refund_token` raises mid-handler, `_safe_refund_token` MUST
    write a `fitcheck_peer_roast_refund_failed` audit row so an operator
    can manually re-refund — the user still sees the same UX, the token
    can't strand silently with no audit pair.

    This locks the resilience contract: no raw exception leaks to the
    user; the audit trail always has a paired row even on refund failure.
    """
    monkeypatch.setattr(roast, "generate_roast", AsyncMock(return_value=None))

    def _boom(conn, token_id):
        raise RuntimeError("SP connection blip")

    monkeypatch.setattr(discord_roast, "refund_token", _boom)

    interaction = _make_interaction()
    message = _make_message(author=_make_author(user_id=555))
    await roast._handle_peer_roast(interaction, message)
    # User still gets the "token refunded" ephemeral — UX unchanged.
    assert "refunded" in _last_followup(interaction)

    # No success-audit landed (refund did NOT actually succeed).
    refunded = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_refunded"
    ]
    assert refunded == []

    # But the operator-visible failure audit DID land, carrying the token_id
    # + reason so reconciliation is possible.
    failed = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_refund_failed"
    ]
    assert len(failed) == 1
    detail = json.loads(failed[0]["detail_json"])
    assert detail["reason"] == "llm_refused_or_failed"
    assert "refund_error" in detail
    assert "RuntimeError" in detail["refund_error"]


# ---------------------------------------------------------------------------
# Reply + record_roast_reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_writes_replied_audit_with_join_keys(
    patched_roast_with_stub_llm, db_conn
):
    """The reply audit row carries audit_log_id (= the generate_roast id)
    + bot_reply_id so the 🚩 handler can JOIN back."""
    patched_roast_with_stub_llm.return_value = ("roast text", 12345)
    interaction = _make_interaction()
    message = _make_message(
        author=_make_author(user_id=555), reply_returns=98765
    )
    await roast._handle_peer_roast(interaction, message)

    replied = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_roast_replied"
    ]
    assert len(replied) == 1
    detail = json.loads(replied[0]["detail_json"])
    assert detail["audit_log_id"] == 12345
    assert detail["bot_reply_id"] == "98765"
    assert detail["target_user_id"] == "555"
    assert detail["actor_user_id"] == "999"


@pytest.mark.asyncio
async def test_reply_http_exception_skips_replied_audit_and_dm(
    patched_roast_with_stub_llm, db_conn
):
    """When reply HTTPs out, NO record_roast_reply audit lands (no
    bot_reply_id to record) and NO DM fires (no jump link). Critically,
    the consumed audit row + token consumption stand — the LLM was billed."""
    interaction = _make_interaction()
    message = _make_message(
        author=_make_author(user_id=555),
        reply_raises=discord.HTTPException(MagicMock(), "boom"),
    )
    await roast._handle_peer_roast(interaction, message)

    replied = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_roast_replied"
    ]
    assert replied == []

    # Consumed audit + token consumption both stand.
    consumed = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_consumed"
    ]
    assert len(consumed) == 1
    tok = discord_roast.available_token(db_conn, "100", "999")
    # Should be None — token is spent, not refunded (reply failure isn't
    # a refund event; the user got a roast attempt, charge stands).
    assert tok is None


# ---------------------------------------------------------------------------
# DM helper (_send_peer_roast_dm)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_dm_happy_path(patched_roast, db_conn):
    target = _make_author(user_id=555)
    sent = await roast._send_peer_roast_dm(
        target_user=target,
        actor_display_name="actor",
        jump_link="https://example.com/jump",
        org_id="solstitch", guild_id="100",
        actor_user_id="999", target_user_id="555",
        post_id="post_x",
    )
    assert sent is True
    target.send.assert_awaited_once()
    body = target.send.call_args.args[0]
    assert "actor roasted your fit" in body
    assert "🚩" in body
    assert "/stop-pls" in body
    # Cooldown set
    assert target.id in roast._target_dm_cooldown


@pytest.mark.asyncio
async def test_send_dm_cooldown_blocks_and_audits(patched_roast, db_conn):
    target = _make_author(user_id=555)
    roast._target_dm_cooldown[target.id] = datetime.now(timezone.utc)
    sent = await roast._send_peer_roast_dm(
        target_user=target,
        actor_display_name="actor",
        jump_link="https://example.com/jump",
        org_id="solstitch", guild_id="100",
        actor_user_id="999", target_user_id="555",
        post_id="post_x",
    )
    assert sent is False
    target.send.assert_not_awaited()
    skipped = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_dm_skipped"
    ]
    assert len(skipped) == 1
    assert json.loads(skipped[0]["detail_json"])["reason"] == "cooldown"


@pytest.mark.asyncio
async def test_send_dm_forbidden_audits_skip(patched_roast, db_conn):
    target = _make_author(
        user_id=555,
        dm_raises=discord.Forbidden(MagicMock(), "DMs disabled"),
    )
    sent = await roast._send_peer_roast_dm(
        target_user=target,
        actor_display_name="actor",
        jump_link="https://example.com/jump",
        org_id="solstitch", guild_id="100",
        actor_user_id="999", target_user_id="555",
        post_id="post_x",
    )
    assert sent is False
    skipped = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_dm_skipped"
    ]
    assert len(skipped) == 1
    reason = json.loads(skipped[0]["detail_json"])["reason"]
    assert reason.startswith("send_failed:")


@pytest.mark.asyncio
async def test_happy_path_schedules_dm_task(
    patched_roast_with_stub_llm, db_conn, monkeypatch
):
    """The full peer-/roast handler fires _send_peer_roast_dm as a
    fire-and-forget task. We don't care about the timing — just that
    the function is invoked at all when reply succeeds."""
    calls: list[dict] = []

    async def _capture_dm(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(roast, "_send_peer_roast_dm", _capture_dm)
    interaction = _make_interaction()
    message = _make_message(
        author=_make_author(user_id=555), reply_returns=98765
    )
    await roast._handle_peer_roast(interaction, message)
    # Give the create_task'd coroutine a tick to land.
    await asyncio.sleep(0)
    assert len(calls) == 1
    assert calls[0]["target_user_id"] == "555"
    assert calls[0]["actor_user_id"] == "999"


# ---------------------------------------------------------------------------
# 🚩 flag handler
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    emoji: str = "🚩",
    guild_id: int | None = 100,
    user_id: int = 555,
    message_id: int = 98765,
) -> SimpleNamespace:
    emoji_obj = MagicMock()
    emoji_obj.__str__ = lambda self: emoji
    return SimpleNamespace(
        emoji=emoji_obj,
        guild_id=guild_id,
        user_id=user_id,
        message_id=message_id,
    )


def _seed_peer_roast_replied(
    db_conn,
    *,
    target_user_id: str,
    actor_user_id: str,
    post_id: str,
    bot_reply_id: str,
    guild_id: str = "100",
    invocation_path: str = "peer_roast",
) -> int:
    """Seed a complete generate→replied pair so find_peer_roast_for_bot_reply
    resolves. Returns the generate audit_log_id."""
    detail_gen = json.dumps({
        "guild_id": guild_id, "user_id": target_user_id,
        "actor_user_id": actor_user_id, "post_id": post_id,
        "invocation_path": invocation_path,
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
        "guild_id": guild_id, "target_user_id": target_user_id,
        "actor_user_id": actor_user_id, "post_id": post_id,
    })
    db_conn.execute(
        "INSERT INTO audit_log (actor, action, org_id, detail_json, source)"
        " VALUES (?, ?, ?, ?, ?)",
        ("discord:bot:auto", "fitcheck_roast_replied", "solstitch",
         detail_rep, "sable-roles"),
    )
    db_conn.commit()
    return int(gen_id)


@pytest.mark.asyncio
async def test_flag_reaction_on_peer_roast_inserts_row_and_audits(
    patched_roast, db_conn
):
    _seed_peer_roast_replied(
        db_conn,
        target_user_id="555", actor_user_id="999",
        post_id="p1", bot_reply_id="98765",
    )

    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 11111
    payload = _make_payload(
        emoji="🚩", guild_id=100, user_id=777, message_id=98765
    )
    await roast._handle_flag_reaction(payload, client=fake_client)

    flag_rows = db_conn.execute(
        "SELECT * FROM discord_peer_roast_flags"
    ).fetchall()
    flags = [dict(r._mapping) for r in flag_rows]
    assert len(flags) == 1
    assert flags[0]["reactor_user_id"] == "777"
    assert flags[0]["bot_reply_id"] == "98765"
    assert flags[0]["target_user_id"] == "555"
    assert flags[0]["actor_user_id"] == "999"

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_flagged"
    ]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_flag_reaction_non_peer_path_is_silent(patched_roast, db_conn):
    """🚩 on an opt-in / random / mod-roast reply must NOT insert a flag row
    (only peer/restored paths produce flag-eligible audit trails)."""
    _seed_peer_roast_replied(
        db_conn,
        target_user_id="555", actor_user_id="mod_a",
        post_id="p1", bot_reply_id="98765",
        invocation_path="mod_roast",
    )

    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 11111
    payload = _make_payload(emoji="🚩", message_id=98765)
    await roast._handle_flag_reaction(payload, client=fake_client)

    flags = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_flags"
    ).fetchone()
    assert dict(flags._mapping if hasattr(flags, "_mapping") else flags)["n"] == 0


@pytest.mark.asyncio
async def test_flag_reaction_with_unknown_bot_reply_is_silent(
    patched_roast, db_conn
):
    """🚩 on a bot message that isn't a tracked peer-roast reply (no
    audit trail) → silent ignore."""
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 11111
    payload = _make_payload(emoji="🚩", message_id=99999)
    await roast._handle_flag_reaction(payload, client=fake_client)

    flags = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_flags"
    ).fetchone()
    assert dict(flags._mapping if hasattr(flags, "_mapping") else flags)["n"] == 0


@pytest.mark.asyncio
async def test_flag_reaction_dm_context_is_silent(patched_roast, db_conn):
    """🚩 in a DM (no guild_id) → silent ignore."""
    _seed_peer_roast_replied(
        db_conn,
        target_user_id="555", actor_user_id="999",
        post_id="p1", bot_reply_id="98765",
    )
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 11111
    payload = _make_payload(emoji="🚩", guild_id=None, message_id=98765)
    await roast._handle_flag_reaction(payload, client=fake_client)
    flags = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_flags"
    ).fetchone()
    assert dict(flags._mapping if hasattr(flags, "_mapping") else flags)["n"] == 0


@pytest.mark.asyncio
async def test_flag_reaction_non_flag_emoji_is_silent(patched_roast, db_conn):
    _seed_peer_roast_replied(
        db_conn,
        target_user_id="555", actor_user_id="999",
        post_id="p1", bot_reply_id="98765",
    )
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 11111
    payload = _make_payload(emoji="🔥", message_id=98765)
    await roast._handle_flag_reaction(payload, client=fake_client)
    flags = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_flags"
    ).fetchone()
    assert dict(flags._mapping if hasattr(flags, "_mapping") else flags)["n"] == 0


@pytest.mark.asyncio
async def test_flag_reaction_self_bot_reaction_ignored(patched_roast, db_conn):
    """If the bot itself reacted 🚩 (unlikely but defensive), ignore."""
    _seed_peer_roast_replied(
        db_conn,
        target_user_id="555", actor_user_id="999",
        post_id="p1", bot_reply_id="98765",
    )
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 11111
    payload = _make_payload(emoji="🚩", user_id=11111, message_id=98765)
    await roast._handle_flag_reaction(payload, client=fake_client)
    flags = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_flags"
    ).fetchone()
    assert dict(flags._mapping if hasattr(flags, "_mapping") else flags)["n"] == 0


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_is_peer_eligible_with_matching_role(patched_roast):
    member = _make_member(user_id=999, role_ids=[777])
    assert roast._is_peer_eligible(member, "100") is True


def test_is_peer_eligible_no_role(patched_roast):
    member = _make_member(user_id=999, role_ids=[222])
    assert roast._is_peer_eligible(member, "100") is False


def test_is_peer_eligible_empty_config(patched_roast):
    """Guild not in PEER_ROAST_ROLES → no one is eligible."""
    member = _make_member(user_id=999, role_ids=[777])
    assert roast._is_peer_eligible(member, "999") is False


def test_is_peer_eligible_string_coercion():
    """Both sides string-coerced — role 777 as int matches "777" in config."""
    import sable_roles.features.roast as roast_mod
    # Save + restore PEER_ROAST_ROLES
    orig = roast_mod.PEER_ROAST_ROLES
    try:
        # String role id in config + int role id on member
        roast_mod.PEER_ROAST_ROLES = {"100": ["777"]}
        member = _make_member(user_id=999, role_ids=[777])
        assert roast_mod._is_peer_eligible(member, "100") is True
    finally:
        roast_mod.PEER_ROAST_ROLES = orig


# ---------------------------------------------------------------------------
# register() — gateway-event wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composes_with_existing_reaction_handler(
    patched_roast, db_conn
):
    """register() MUST preserve any pre-existing on_raw_reaction_add so
    fitcheck_streak's debounce keeps firing alongside the 🚩 handler.

    Without composition, R7's register would clobber fitcheck via
    @client.event setattr — breaking streak reaction-scoring in prod.
    Bare discord.Client lacks add_listener/extra_events, so this is
    enforced by reading the existing attr and wrapping it.
    """
    client = discord.Client(intents=discord.Intents.default())
    sentinel_calls: list = []

    @client.event
    async def on_raw_reaction_add(payload):
        sentinel_calls.append(payload)

    roast.register(client)

    # The bound attribute is now the wrapper, NOT the original sentinel.
    assert client.on_raw_reaction_add is not on_raw_reaction_add
    # Dispatch a non-🚩 reaction → sentinel fires; 🚩 handler is a no-op
    # because no audit-row match exists for this synthetic message id.
    payload = _make_payload(emoji="🔥", message_id=12345)
    await client.on_raw_reaction_add(payload)
    assert len(sentinel_calls) == 1
    assert sentinel_calls[0] is payload
