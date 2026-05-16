"""Tests for the airlock feature (A3 + A4 + A5 + A6).

Covers:
  * `_refresh_invite_snapshot` + `_on_invite_create/_delete` — A3 snapshot machinery
  * `bootstrap(client)` — team-inviter env seed + first-boot snapshot
  * `_handle_member_join` — A4 full flow (team auto-admit + non-team hold + DM + mod ping)
  * `_handle_member_remove` — left_during_airlock transition
  * `_handle_admit` / `_handle_ban` / `_handle_kick` / `_handle_airlock_status` — A5 mod commands
  * `_handle_add_team_inviter` / `_handle_list_team_inviters` — A6 team-only commands
  * Tiered perm gates (AIRLOCK_TRIAGE_ROLES for A5, MOD_ROLES for A6)
  * `_can_triage_airlock` helper
  * `_format_mod_ping` pure renderer
  * Composition pattern (register wraps existing handlers)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sable_platform.db import discord_airlock
from sable_roles.features import airlock

from tests.conftest import fetch_audit_rows


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


@pytest.fixture
def patched_airlock(monkeypatch, db_conn):
    monkeypatch.setattr(airlock, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(airlock, "AIRLOCK_ROLES", {"100": "999"})       # @Outsider
    monkeypatch.setattr(airlock, "AIRLOCK_DEFAULT_MEMBER_ROLES", {"100": "888"})  # @Insider
    monkeypatch.setattr(airlock, "AIRLOCK_MOD_CHANNELS", {"100": "777"})  # #triage
    monkeypatch.setattr(airlock, "AIRLOCK_TRIAGE_ROLES", {"100": ["555"]})  # @Mod
    monkeypatch.setattr(airlock, "TEAM_INVITERS_BOOTSTRAP", {})
    monkeypatch.setattr(airlock, "AIRLOCK_ENABLED", True)
    monkeypatch.setattr(airlock, "get_db", lambda: _make_db_context(db_conn)())
    return airlock


def _make_invite(
    *,
    code: str = "abc",
    inviter_id: int | None = 200,
    uses: int = 0,
    max_uses: int = 0,
    expires_at: datetime | None = None,
    guild_id: int = 100,
) -> MagicMock:
    inv = MagicMock(spec=discord.Invite)
    inv.code = code
    inv.uses = uses
    inv.max_uses = max_uses
    inv.expires_at = expires_at
    if inviter_id is None:
        inv.inviter = None
    else:
        inviter = MagicMock()
        inviter.id = inviter_id
        inv.inviter = inviter
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    inv.guild = g
    return inv


def _make_member(
    *,
    user_id: int = 1234,
    bot: bool = False,
    guild_id: int = 100,
    roles: list[int] | None = None,
    dm_raises: BaseException | None = None,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = bot
    member.display_name = f"user{user_id}"
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    member.guild = g
    role_objs = []
    for rid in roles or []:
        r = MagicMock(spec=discord.Role)
        r.id = rid
        role_objs.append(r)
    member.roles = role_objs
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.kick = AsyncMock()
    if dm_raises is not None:
        member.send = AsyncMock(side_effect=dm_raises)
    else:
        member.send = AsyncMock()
    return member


def _make_guild(
    *,
    guild_id: int = 100,
    invites: list | None = None,
    invite_fetch_raises: BaseException | None = None,
    role_lookup: dict | None = None,
    channel_lookup: dict | None = None,
) -> MagicMock:
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    if invite_fetch_raises is not None:
        g.invites = AsyncMock(side_effect=invite_fetch_raises)
    else:
        g.invites = AsyncMock(return_value=invites or [])
    role_lookup = role_lookup or {}

    def _get_role(rid):
        return role_lookup.get(rid)

    g.get_role = MagicMock(side_effect=_get_role)
    channel_lookup = channel_lookup or {}

    def _get_channel(cid):
        return channel_lookup.get(cid)

    g.get_channel = MagicMock(side_effect=_get_channel)
    g.ban = AsyncMock()
    return g


# ---------------------------------------------------------------------------
# A3 — invite snapshot bootstrap + per-event refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_invite_snapshot_upserts_each_invite(patched_airlock, db_conn):
    inv_a = _make_invite(code="aaa", inviter_id=200, uses=5, max_uses=10)
    inv_b = _make_invite(code="bbb", inviter_id=201, uses=0, max_uses=1)
    guild = _make_guild(invites=[inv_a, inv_b])
    rows = await airlock._refresh_invite_snapshot(guild)
    assert len(rows) == 2
    snap = discord_airlock.get_invite_snapshot(db_conn, "100")
    assert set(snap.keys()) == {"aaa", "bbb"}
    assert snap["aaa"]["uses"] == 5
    assert snap["aaa"]["inviter_user_id"] == "200"
    assert snap["bbb"]["max_uses"] == 1


@pytest.mark.asyncio
async def test_refresh_invite_snapshot_swallows_forbidden(patched_airlock, db_conn):
    """Bot lacks Manage Server → guild.invites() raises Forbidden →
    helper returns empty list, doesn't crash on_member_join."""
    guild = _make_guild(invite_fetch_raises=discord.Forbidden(MagicMock(), "no perm"))
    rows = await airlock._refresh_invite_snapshot(guild)
    assert rows == []


@pytest.mark.asyncio
async def test_on_invite_create_upserts(patched_airlock, db_conn):
    inv = _make_invite(code="newcode", inviter_id=200, uses=0)
    await airlock._on_invite_create(inv)
    snap = discord_airlock.get_invite_snapshot(db_conn, "100")
    assert "newcode" in snap


@pytest.mark.asyncio
async def test_on_invite_delete_removes(patched_airlock, db_conn):
    inv = _make_invite(code="oldcode", inviter_id=200, uses=5)
    await airlock._on_invite_create(inv)
    await airlock._on_invite_delete(inv)
    snap = discord_airlock.get_invite_snapshot(db_conn, "100")
    assert "oldcode" not in snap


@pytest.mark.asyncio
async def test_on_invite_create_ignores_unconfigured_guild(patched_airlock, db_conn):
    inv = _make_invite(code="abc", guild_id=999)
    await airlock._on_invite_create(inv)
    snap = discord_airlock.get_invite_snapshot(db_conn, "999")
    assert snap == {}


@pytest.mark.asyncio
async def test_bootstrap_seeds_team_inviters_from_env(
    monkeypatch, patched_airlock, db_conn
):
    monkeypatch.setattr(
        airlock, "TEAM_INVITERS_BOOTSTRAP",
        {"100": ["402620324744790017", "209577624618401802"]},
    )
    guild = _make_guild(invites=[])
    client = MagicMock()
    client.get_guild = MagicMock(return_value=guild)
    await airlock.bootstrap(client)
    users = discord_airlock.list_team_inviters(db_conn, "100")
    ids = {u["user_id"] for u in users}
    assert ids == {"402620324744790017", "209577624618401802"}


@pytest.mark.asyncio
async def test_bootstrap_idempotent(monkeypatch, patched_airlock, db_conn):
    """Re-running bootstrap should not duplicate or re-trigger audits."""
    monkeypatch.setattr(
        airlock, "TEAM_INVITERS_BOOTSTRAP", {"100": ["402620324744790017"]}
    )
    guild = _make_guild(invites=[])
    client = MagicMock()
    client.get_guild = MagicMock(return_value=guild)
    await airlock.bootstrap(client)
    await airlock.bootstrap(client)
    users = discord_airlock.list_team_inviters(db_conn, "100")
    assert len(users) == 1


@pytest.mark.asyncio
async def test_bootstrap_skips_guild_not_visible(
    monkeypatch, patched_airlock, db_conn
):
    """If client.get_guild returns None (bot not in guild yet), bootstrap
    logs + skips that guild without crashing."""
    monkeypatch.setattr(
        airlock, "TEAM_INVITERS_BOOTSTRAP", {"100": ["1"]}
    )
    client = MagicMock()
    client.get_guild = MagicMock(return_value=None)
    await airlock.bootstrap(client)
    # No team-inviter rows landed (the bootstrap exited early)
    users = discord_airlock.list_team_inviters(db_conn, "100")
    assert users == []


# ---------------------------------------------------------------------------
# A4 — _handle_member_join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_no_op(monkeypatch, patched_airlock, db_conn):
    monkeypatch.setattr(airlock, "AIRLOCK_ENABLED", False)
    member = _make_member(user_id=1234)
    client = MagicMock()
    await airlock._handle_member_join(member, client=client)
    member.add_roles.assert_not_awaited()
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_bot_member_skipped(patched_airlock, db_conn):
    member = _make_member(user_id=9999, bot=True)
    client = MagicMock()
    await airlock._handle_member_join(member, client=client)
    member.add_roles.assert_not_awaited()
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_unconfigured_guild_skipped(patched_airlock, db_conn):
    member = _make_member(user_id=1234, guild_id=999)
    client = MagicMock()
    await airlock._handle_member_join(member, client=client)
    member.add_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_invite_auto_admits(patched_airlock, db_conn):
    """Inviter is on team allowlist → @Insider granted, audit
    fitcheck_airlock_auto_admitted, NO DM, NO mod ping."""
    discord_airlock.add_team_inviter(
        db_conn, guild_id="100", user_id="200", added_by="seed"
    )
    discord_airlock.upsert_invite_snapshot(
        db_conn, guild_id="100", code="t1", inviter_user_id="200",
        uses=0, max_uses=0, expires_at=None,
    )
    fresh = _make_invite(code="t1", inviter_id=200, uses=1, max_uses=0)
    insider_role = MagicMock(spec=discord.Role)
    insider_role.id = 888
    guild = _make_guild(invites=[fresh], role_lookup={888: insider_role})
    member = _make_member(user_id=1234)
    member.guild = guild  # ensure member.guild matches the patched guild
    client = MagicMock()

    await airlock._handle_member_join(member, client=client)

    # Granted @Insider (the role object passed to add_roles is exactly insider_role).
    member.add_roles.assert_awaited()
    granted_arg = member.add_roles.call_args.args[0]
    assert granted_arg is insider_role
    # Audit
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_airlock_auto_admitted"
    ]
    assert len(audits) == 1
    # NO DM
    member.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_team_invite_holds_with_dm_and_mod_ping(patched_airlock, db_conn):
    """Inviter is NOT on team allowlist → @Outsider granted, DM sent,
    #triage ping posted, audit fitcheck_airlock_held."""
    discord_airlock.upsert_invite_snapshot(
        db_conn, guild_id="100", code="p1", inviter_user_id="999",
        uses=0, max_uses=0, expires_at=None,
    )
    fresh = _make_invite(code="p1", inviter_id=999, uses=1, max_uses=0)
    outsider_role = MagicMock(spec=discord.Role)
    outsider_role.id = 999
    mod_channel = MagicMock()
    mod_channel.send = AsyncMock()
    guild = _make_guild(
        invites=[fresh],
        role_lookup={999: outsider_role},
        channel_lookup={777: mod_channel},
    )
    member = _make_member(user_id=1234)
    member.guild = guild
    client = MagicMock()

    await airlock._handle_member_join(member, client=client)

    # @Outsider granted
    member.add_roles.assert_any_await(outsider_role, reason="airlock hold (non-team invite)")
    # DM sent w/ locked text
    member.send.assert_awaited_once()
    dm_body = member.send.call_args.args[0]
    assert "proof of aura" in dm_body
    assert "#outside" in dm_body
    # Mod ping posted
    mod_channel.send.assert_awaited_once()
    mod_body = mod_channel.send.call_args.args[0]
    assert "airlock:" in mod_body
    assert "<@1234>" in mod_body
    # Audit
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_airlock_held"
    ]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_ambiguous_attribution_falls_through_to_airlock(
    patched_airlock, db_conn
):
    """Two simultaneous joins → diff returns None → fail-closed: airlock."""
    discord_airlock.upsert_invite_snapshot(
        db_conn, guild_id="100", code="aa", inviter_user_id="111",
        uses=5, max_uses=0, expires_at=None,
    )
    discord_airlock.upsert_invite_snapshot(
        db_conn, guild_id="100", code="bb", inviter_user_id="222",
        uses=3, max_uses=0, expires_at=None,
    )
    a = _make_invite(code="aa", inviter_id=111, uses=6)
    b = _make_invite(code="bb", inviter_id=222, uses=4)
    outsider_role = MagicMock(spec=discord.Role)
    outsider_role.id = 999
    guild = _make_guild(invites=[a, b], role_lookup={999: outsider_role})
    member = _make_member(user_id=1234)
    member.guild = guild
    client = MagicMock()

    await airlock._handle_member_join(member, client=client)
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_airlock_held"]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["attributed_invite_code"] is None


@pytest.mark.asyncio
async def test_dm_forbidden_does_not_block_hold(patched_airlock, db_conn):
    """User has DMs off → DM fails Forbidden → hold + audit still proceed."""
    discord_airlock.upsert_invite_snapshot(
        db_conn, guild_id="100", code="p1", inviter_user_id="999",
        uses=0, max_uses=0, expires_at=None,
    )
    fresh = _make_invite(code="p1", inviter_id=999, uses=1)
    outsider_role = MagicMock(spec=discord.Role)
    outsider_role.id = 999
    guild = _make_guild(invites=[fresh], role_lookup={999: outsider_role})
    member = _make_member(
        user_id=1234,
        dm_raises=discord.Forbidden(MagicMock(), "DMs disabled"),
    )
    member.guild = guild
    client = MagicMock()

    await airlock._handle_member_join(member, client=client)
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_airlock_held"]
    detail = json.loads(audits[0]["detail_json"])
    assert detail["dm_status"].startswith("failed:")
    assert detail["role_grant_status"] == "granted"


@pytest.mark.asyncio
async def test_mod_channel_missing_audits_status(patched_airlock, db_conn):
    """AIRLOCK_MOD_CHANNELS points at a channel the bot can't see → still
    audit, but with mod_ping_status=channel_not_found."""
    discord_airlock.upsert_invite_snapshot(
        db_conn, guild_id="100", code="p1", inviter_user_id="999",
        uses=0, max_uses=0, expires_at=None,
    )
    fresh = _make_invite(code="p1", inviter_id=999, uses=1)
    outsider_role = MagicMock(spec=discord.Role)
    outsider_role.id = 999
    guild = _make_guild(
        invites=[fresh],
        role_lookup={999: outsider_role},
        channel_lookup={},  # 777 not present
    )
    member = _make_member(user_id=1234)
    member.guild = guild
    client = MagicMock()
    await airlock._handle_member_join(member, client=client)
    detail = json.loads(
        [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_airlock_held"][0]["detail_json"]
    )
    assert detail["mod_ping_status"] == "channel_not_found"


@pytest.mark.asyncio
async def test_left_during_airlock_transition(patched_airlock, db_conn):
    """User leaves while held → transition to left_during_airlock."""
    discord_airlock.record_member_admit(
        db_conn,
        guild_id="100", user_id="1234",
        attributed_invite_code=None, attributed_inviter_user_id=None,
        is_team_invite=False, airlock_status="held",
    )
    member = _make_member(user_id=1234)
    await airlock._handle_member_remove(member)
    row = discord_airlock.get_admit(db_conn, "100", "1234")
    assert row["airlock_status"] == "left_during_airlock"


@pytest.mark.asyncio
async def test_left_after_admit_does_not_transition(patched_airlock, db_conn):
    """User who was already admitted then leaves → DON'T overwrite their
    admit row."""
    discord_airlock.record_member_admit(
        db_conn,
        guild_id="100", user_id="1234",
        attributed_invite_code="abc", attributed_inviter_user_id="200",
        is_team_invite=True, airlock_status="auto_admitted",
    )
    member = _make_member(user_id=1234)
    await airlock._handle_member_remove(member)
    row = discord_airlock.get_admit(db_conn, "100", "1234")
    assert row["airlock_status"] == "auto_admitted"


# ---------------------------------------------------------------------------
# A5 — mod commands (admit / ban / kick / airlock-status)
# ---------------------------------------------------------------------------


def _make_interaction(
    *,
    guild_id: int | None = 100,
    user_id: int = 5555,
    in_dm: bool = False,
    user_is_member: bool = True,
    user_roles: list[int] | None = None,
    guild: MagicMock | None = None,
) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    if in_dm:
        interaction.guild_id = None
        interaction.guild = None
    else:
        interaction.guild_id = guild_id
        interaction.guild = guild or MagicMock()
    if user_is_member:
        user = _make_member(user_id=user_id, roles=user_roles or [555])
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
    return args[0]


@pytest.mark.asyncio
async def test_admit_happy_path(patched_airlock, db_conn, monkeypatch):
    # Bypass _is_mod by also forcing AIRLOCK_TRIAGE_ROLES to include user's role
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    discord_airlock.record_member_admit(
        db_conn, guild_id="100", user_id="1234",
        attributed_invite_code="abc", attributed_inviter_user_id="999",
        is_team_invite=False, airlock_status="held",
    )
    outsider_role = MagicMock(spec=discord.Role)
    outsider_role.id = 999
    insider_role = MagicMock(spec=discord.Role)
    insider_role.id = 888
    guild = _make_guild(role_lookup={999: outsider_role, 888: insider_role})
    interaction = _make_interaction(guild=guild)
    # Target member must have @Outsider as the SAME object guild.get_role returns,
    # otherwise the `airlock_role in member.roles` membership check fails.
    target_member = MagicMock(spec=discord.Member)
    target_member.id = 1234
    target_member.roles = [outsider_role]
    target_member.add_roles = AsyncMock()
    target_member.remove_roles = AsyncMock()
    guild.get_member = MagicMock(return_value=target_member)
    target_user = MagicMock(spec=discord.User)
    target_user.id = 1234

    await airlock._handle_admit(interaction, target_user)
    body = _last_followup(interaction)
    assert "admitted" in body.lower()
    # @Outsider removed
    target_member.remove_roles.assert_awaited()
    # @Insider granted
    target_member.add_roles.assert_awaited()
    # Admit row transitioned
    row = discord_airlock.get_admit(db_conn, "100", "1234")
    assert row["airlock_status"] == "admitted"
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_airlock_admitted"]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_admit_non_mod_bounces(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    # User has role 222, not 555 (the AIRLOCK_TRIAGE_ROLES role)
    interaction = _make_interaction(user_roles=[222])
    target = MagicMock(spec=discord.User)
    target.id = 1234
    await airlock._handle_admit(interaction, target)
    assert "not authorized" in _last_followup(interaction).lower()
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_airlock_admitted"]
    assert audits == []


@pytest.mark.asyncio
async def test_admit_dm_context_bounces(patched_airlock, db_conn):
    interaction = _make_interaction(in_dm=True)
    target = MagicMock(spec=discord.User)
    target.id = 1234
    await airlock._handle_admit(interaction, target)
    assert "inside a server" in _last_followup(interaction).lower()


@pytest.mark.asyncio
async def test_ban_happy_path(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    discord_airlock.record_member_admit(
        db_conn, guild_id="100", user_id="1234",
        attributed_invite_code=None, attributed_inviter_user_id=None,
        is_team_invite=False, airlock_status="held",
    )
    guild = _make_guild()
    interaction = _make_interaction(guild=guild)
    target = MagicMock(spec=discord.User)
    target.id = 1234

    await airlock._handle_ban(interaction, target, "scam profile")
    guild.ban.assert_awaited_once()
    kwargs = guild.ban.call_args.kwargs
    assert "scam profile" in kwargs["reason"]
    assert kwargs["delete_message_seconds"] == 0
    row = discord_airlock.get_admit(db_conn, "100", "1234")
    assert row["airlock_status"] == "banned"
    assert row["decision_reason"] == "scam profile"
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_airlock_banned"]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_ban_non_mod_bounces(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    interaction = _make_interaction(user_roles=[222])
    target = MagicMock(spec=discord.User)
    target.id = 1234
    await airlock._handle_ban(interaction, target, "reason")
    assert "not authorized" in _last_followup(interaction).lower()


@pytest.mark.asyncio
async def test_ban_forbidden_still_records_state(patched_airlock, db_conn, monkeypatch):
    """guild.ban() raises Forbidden → audit + state still recorded."""
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    discord_airlock.record_member_admit(
        db_conn, guild_id="100", user_id="1234",
        attributed_invite_code=None, attributed_inviter_user_id=None,
        is_team_invite=False, airlock_status="held",
    )
    guild = _make_guild()
    guild.ban.side_effect = discord.Forbidden(MagicMock(), "no perm")
    interaction = _make_interaction(guild=guild)
    target = MagicMock(spec=discord.User)
    target.id = 1234
    await airlock._handle_ban(interaction, target, "reason")
    row = discord_airlock.get_admit(db_conn, "100", "1234")
    assert row["airlock_status"] == "banned"
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_airlock_banned"]
    detail = json.loads(audits[0]["detail_json"])
    assert detail["discord_ban_result"].startswith("forbidden")


@pytest.mark.asyncio
async def test_kick_happy_path(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    discord_airlock.record_member_admit(
        db_conn, guild_id="100", user_id="1234",
        attributed_invite_code=None, attributed_inviter_user_id=None,
        is_team_invite=False, airlock_status="held",
    )
    target_member = _make_member(user_id=1234)
    guild = _make_guild()
    guild.get_member = MagicMock(return_value=target_member)
    interaction = _make_interaction(guild=guild)
    target = MagicMock(spec=discord.User)
    target.id = 1234
    await airlock._handle_kick(interaction, target, "low effort intro")
    target_member.kick.assert_awaited_once()
    row = discord_airlock.get_admit(db_conn, "100", "1234")
    assert row["airlock_status"] == "kicked"
    assert row["decision_reason"] == "low effort intro"


@pytest.mark.asyncio
async def test_airlock_status_lists_pending_when_no_target(
    patched_airlock, db_conn, monkeypatch
):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    discord_airlock.record_member_admit(
        db_conn, guild_id="100", user_id="111",
        attributed_invite_code="c1", attributed_inviter_user_id="999",
        is_team_invite=False, airlock_status="held",
    )
    discord_airlock.record_member_admit(
        db_conn, guild_id="100", user_id="222",
        attributed_invite_code="c2", attributed_inviter_user_id=None,
        is_team_invite=False, airlock_status="held",
    )
    interaction = _make_interaction()
    await airlock._handle_airlock_status(interaction, None)
    body = _last_followup(interaction)
    assert "pending airlock holds" in body
    assert "<@111>" in body
    assert "<@222>" in body


@pytest.mark.asyncio
async def test_airlock_status_per_user_inspect(
    patched_airlock, db_conn, monkeypatch
):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    discord_airlock.record_member_admit(
        db_conn, guild_id="100", user_id="1234",
        attributed_invite_code="abc", attributed_inviter_user_id="999",
        is_team_invite=False, airlock_status="held",
    )
    interaction = _make_interaction()
    target = MagicMock(spec=discord.User)
    target.id = 1234
    await airlock._handle_airlock_status(interaction, target)
    body = _last_followup(interaction)
    assert "<@1234>" in body
    assert "held" in body
    assert "abc" in body


@pytest.mark.asyncio
async def test_airlock_status_missing_row(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    interaction = _make_interaction()
    target = MagicMock(spec=discord.User)
    target.id = 9999
    await airlock._handle_airlock_status(interaction, target)
    body = _last_followup(interaction)
    assert "no admit record" in body.lower()


# ---------------------------------------------------------------------------
# A6 — team-only commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_team_inviter_team_only_gate(patched_airlock, db_conn, monkeypatch):
    """Community-mod role (AIRLOCK_TRIAGE_ROLES) is NOT enough for
    /add-team-inviter — must be MOD_ROLES (team).
    """
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    interaction = _make_interaction(user_roles=[555])  # community mod
    target = MagicMock(spec=discord.User)
    target.id = 999
    await airlock._handle_add_team_inviter(interaction, target)
    assert "team-only" in _last_followup(interaction).lower()
    users = discord_airlock.list_team_inviters(db_conn, "100")
    assert users == []


@pytest.mark.asyncio
async def test_add_team_inviter_team_role_succeeds(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: True)
    interaction = _make_interaction()
    target = MagicMock(spec=discord.User)
    target.id = 209577624618401802
    await airlock._handle_add_team_inviter(interaction, target)
    body = _last_followup(interaction)
    assert "added" in body.lower()
    assert discord_airlock.is_team_inviter(db_conn, "100", "209577624618401802") is True


@pytest.mark.asyncio
async def test_add_team_inviter_idempotent(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: True)
    discord_airlock.add_team_inviter(
        db_conn, guild_id="100", user_id="200", added_by="seed"
    )
    interaction = _make_interaction()
    target = MagicMock(spec=discord.User)
    target.id = 200
    await airlock._handle_add_team_inviter(interaction, target)
    body = _last_followup(interaction).lower()
    assert "already" in body


@pytest.mark.asyncio
async def test_list_team_inviters_team_only(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    interaction = _make_interaction(user_roles=[555])  # community mod
    await airlock._handle_list_team_inviters(interaction)
    assert "team-only" in _last_followup(interaction).lower()


@pytest.mark.asyncio
async def test_list_team_inviters_empty(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: True)
    interaction = _make_interaction()
    await airlock._handle_list_team_inviters(interaction)
    assert "empty" in _last_followup(interaction).lower()


@pytest.mark.asyncio
async def test_list_team_inviters_renders_rows(patched_airlock, db_conn, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: True)
    discord_airlock.add_team_inviter(
        db_conn, guild_id="100", user_id="200", added_by="seed"
    )
    discord_airlock.add_team_inviter(
        db_conn, guild_id="100", user_id="300", added_by="seed"
    )
    interaction = _make_interaction()
    await airlock._handle_list_team_inviters(interaction)
    body = _last_followup(interaction)
    assert "<@200>" in body
    assert "<@300>" in body


# ---------------------------------------------------------------------------
# _can_triage_airlock + _format_mod_ping (pure helpers)
# ---------------------------------------------------------------------------


def test_can_triage_team_mod_always_true(patched_airlock, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: True)
    member = _make_member(user_id=1, roles=[])  # no roles at all
    assert airlock._can_triage_airlock(member, "100") is True


def test_can_triage_community_mod_with_triage_role(patched_airlock, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    member = _make_member(user_id=1, roles=[555])  # community mod role
    assert airlock._can_triage_airlock(member, "100") is True


def test_can_triage_random_user_false(patched_airlock, monkeypatch):
    monkeypatch.setattr(airlock, "_is_mod", lambda m, g: False)
    member = _make_member(user_id=1, roles=[222])  # no relevant role
    assert airlock._can_triage_airlock(member, "100") is False


def test_format_mod_ping_team_invite():
    member = MagicMock()
    member.id = 1234
    body = airlock._format_mod_ping(
        member=member,
        attribution={"code": "abc", "inviter_user_id": "200"},
        is_team_invite=True,
    )
    assert "auto-admitted" in body
    assert "<@1234>" in body
    assert "abc" in body
    assert "<@200>" in body


def test_format_mod_ping_attribution_unknown():
    member = MagicMock()
    member.id = 1234
    body = airlock._format_mod_ping(
        member=member, attribution=None, is_team_invite=False,
    )
    assert "unknown" in body
    assert "/admit" in body or "admit" in body


def test_format_mod_ping_no_inviter():
    member = MagicMock()
    member.id = 1234
    body = airlock._format_mod_ping(
        member=member,
        attribution={"code": "vanity", "inviter_user_id": None},
        is_team_invite=False,
    )
    assert "vanity" in body
    assert "no inviter" in body


# ---------------------------------------------------------------------------
# Composition / register
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composes_with_existing_handlers(patched_airlock, monkeypatch):
    """register(client) must preserve pre-existing on_member_join etc.
    via the wrap-existing pattern (same as roast/vibe_observer)."""
    client = discord.Client(intents=discord.Intents.default())
    join_calls: list = []

    @client.event
    async def on_member_join(member):
        join_calls.append(member)

    # Stub the handler we just registered above so we can ASSERT it survives.
    pre = client.on_member_join

    airlock.register(client)

    assert client.on_member_join is not pre  # wrapper is now bound
    # Stub the airlock-side handler so we don't need full member/guild state
    monkeypatch.setattr(airlock, "_handle_member_join", AsyncMock())
    member = _make_member(user_id=1234)
    await client.on_member_join(member)
    assert len(join_calls) == 1
    assert join_calls[0] is member


def test_register_commands_installs_all_six_slash_commands(patched_airlock):
    from discord import app_commands as ac
    client = discord.Client(intents=discord.Intents.default())
    tree = ac.CommandTree(client)
    airlock.register_commands(tree, client=client)
    names = {c.name for c in tree.get_commands()}
    assert {
        "admit", "ban", "kick", "airlock-status",
        "add-team-inviter", "list-team-inviters",
    } <= names
