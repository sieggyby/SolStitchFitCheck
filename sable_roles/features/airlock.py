"""Airlock — invite-source-aware new-member verification (sable-roles).

Per `~/Projects/SolStitch/internal/airlock_plan.md`.

Flow on every guild member join:

  1. Refresh invite snapshot, diff against the prior captured state to
     attribute the join to a specific invite code → inviter user-id.
  2. Look up the inviter in `discord_team_inviters`. If present →
     auto-admit (grant default member role, record `auto_admitted`).
  3. Otherwise → assign the airlock role, DM the new user with the
     locked "proof of aura" text, post a triage ping in the mod channel,
     record the admit row as `held`.

All ambiguous attributions (vanity-URL joins, concurrent joins,
restart-blackout edge) fall through to airlock per plan §0.4 fail-closed.

Mod surface (gated on AIRLOCK_TRIAGE_ROLES — Team + Mod):
  /admit @user                  → remove airlock role, grant member role, audit
  /ban @user [reason]           → guild.ban, audit
  /kick @user [reason]          → member.kick, audit (rejoinable)
  /airlock-status [@user]       → ephemeral inspect; no-arg lists pending

Team-only surface (gated on _is_mod — Team only):
  /add-team-inviter @user       → UPSERT to discord_team_inviters, audit
  /list-team-inviters           → ephemeral list

Listener composition mirrors the wrap-existing-handler pattern from
[[project_sable_roles_repo]] R7/R10 — `register(client)` reads any
attached on_member_join/on_member_remove/on_invite_create/on_invite_delete
handler and chains. MUST be registered AFTER fitcheck_streak + roast +
vibe_observer so the wrapper picks up the full prior chain.

Privileged-intent requirement: Members intent must be ON in the Discord
developer portal + in `discord.Intents`. Without it, `on_member_join`
never fires and users land in the server with full access. Pre-flight
A0 in the plan doc covers this.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands

from sable_platform.db import discord_airlock
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db

from sable_roles.config import (
    AIRLOCK_DEFAULT_MEMBER_ROLES,
    AIRLOCK_ENABLED,
    AIRLOCK_MOD_CHANNELS,
    AIRLOCK_ROLES,
    AIRLOCK_TRIAGE_ROLES,
    GUILD_TO_ORG,
    TEAM_INVITERS_BOOTSTRAP,
)
from sable_roles.features.fitcheck_streak import _is_mod

logger = logging.getLogger("sable_roles.airlock")


# Locked SolStitch-voice DM text per plan §0.5. Pinned in
# PINNED_WAITING_ROOM_MESSAGE.md.
_AIRLOCK_DM_TEXT = (
    "you're parked in #outside. demonstrate proof of aura — no NPCs allowed"
    " in SolStitch, prove that's not you. ask a friend for an invite link if"
    " you really can't think of something. mods are watching and will admit"
    " you when you've shown up."
)


def _can_triage_airlock(member: discord.Member, guild_id: str) -> bool:
    """True iff member holds any role in AIRLOCK_TRIAGE_ROLES[guild_id]
    OR is a team-mod (`_is_mod` — kept disjoint so team always retains
    everything community-mods can do without manual env duplication).

    Both sides string-coerced (matches `_is_peer_eligible` from roast.py).
    """
    if _is_mod(member, guild_id):
        return True
    triage_role_ids = {str(rid) for rid in AIRLOCK_TRIAGE_ROLES.get(guild_id, [])}
    if not triage_role_ids:
        return False
    member_role_ids = {str(r.id) for r in member.roles}
    return bool(member_role_ids & triage_role_ids)


# ---------------------------------------------------------------------------
# Invite snapshot bootstrap + per-event refresh
# ---------------------------------------------------------------------------


def _invite_to_dict(invite: discord.Invite) -> dict:
    """Coerce a discord.Invite into the dict shape attribute_join expects.

    Inviter can be None for vanity invites + ancient invites whose
    inviter user is no longer cached. `expires_at` is an aware datetime
    on discord.py 2.x — serialize as ISO-Z text or None.
    """
    inviter_id = (
        str(invite.inviter.id) if invite.inviter is not None else None
    )
    expires_at = (
        invite.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if invite.expires_at is not None
        else None
    )
    return {
        "code": invite.code,
        "inviter_user_id": inviter_id,
        "uses": int(invite.uses or 0),
        "max_uses": int(invite.max_uses or 0),
        "expires_at": expires_at,
    }


async def _fetch_live_invites(guild: discord.Guild) -> list[dict]:
    """REST-call live `guild.invites()`, return as a list of dicts.

    Does NOT touch the stored snapshot — caller must explicitly call
    :func:`_persist_invite_snapshot` to update it after consuming the
    diff. This split is load-bearing: if we upserted here, the diff
    would always be empty because the stored snapshot would already
    match the fresh state.

    Silently catches discord.Forbidden (bot lacks Manage Server) and
    returns an empty list — caller treats that as "attribution
    unavailable, fail-closed → airlock."
    """
    try:
        live = await guild.invites()
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning(
            "guild.invites() failed for guild %s: %s — attribution disabled",
            guild.id, exc,
        )
        return []
    return [_invite_to_dict(inv) for inv in live]


def _persist_invite_snapshot(guild_id: str, rows: list[dict]) -> None:
    """Bulk-UPSERT the supplied rows into discord_invite_snapshot.

    Caller is responsible for ordering this AFTER attribute_join — see
    :func:`_fetch_live_invites` docstring.
    """
    with get_db() as conn:
        for row in rows:
            discord_airlock.upsert_invite_snapshot(
                conn,
                guild_id=guild_id, code=row["code"],
                inviter_user_id=row["inviter_user_id"],
                uses=row["uses"], max_uses=row["max_uses"],
                expires_at=row["expires_at"],
            )


async def _refresh_invite_snapshot(guild: discord.Guild) -> list[dict]:
    """Convenience: fetch + persist in one go. Use ONLY when there's no
    diff to compute (bootstrap on first boot, on_invite_create/delete
    events). The on_member_join hot path uses the split fetch/persist
    helpers above so it can diff before updating.
    """
    rows = await _fetch_live_invites(guild)
    _persist_invite_snapshot(str(guild.id), rows)
    return rows


async def _on_invite_create(invite: discord.Invite) -> None:
    if invite.guild is None:
        return
    if str(invite.guild.id) not in GUILD_TO_ORG:
        return
    row = _invite_to_dict(invite)
    with get_db() as conn:
        discord_airlock.upsert_invite_snapshot(
            conn,
            guild_id=str(invite.guild.id), code=row["code"],
            inviter_user_id=row["inviter_user_id"],
            uses=row["uses"], max_uses=row["max_uses"],
            expires_at=row["expires_at"],
        )


async def _on_invite_delete(invite: discord.Invite) -> None:
    if invite.guild is None:
        return
    if str(invite.guild.id) not in GUILD_TO_ORG:
        return
    with get_db() as conn:
        discord_airlock.delete_invite_snapshot(
            conn, guild_id=str(invite.guild.id), code=invite.code
        )


async def bootstrap(client: discord.Client) -> None:
    """One-shot bootstrap called from setup_hook AFTER login.

    Two responsibilities:
      1. Snapshot guild.invites() for every guild in GUILD_TO_ORG so
         the first on_member_join after boot has a baseline to diff against.
         Without this, the first joiner is unattributable and falls through
         to airlock (fail-closed).
      2. UPSERT TEAM_INVITERS_BOOTSTRAP into discord_team_inviters so the
         env-side seed translates into the SP-side allowlist on every boot
         (idempotent — re-running adds nothing new).

    Called manually (NOT on @client.event) so setup_hook can await it
    after the gateway is connected enough to do REST guild.invites() calls.
    Failures per-guild are logged + swallowed; one bad guild doesn't kill
    the bootstrap for the others.
    """
    for guild_id_str, _org in GUILD_TO_ORG.items():
        # Snapshot bootstrap — needs live REST call so the bot must be
        # connected to Discord before this is called.
        guild = client.get_guild(int(guild_id_str))
        if guild is None:
            logger.warning(
                "airlock bootstrap: guild %s not visible to bot — skipping",
                guild_id_str,
            )
            continue
        try:
            await _refresh_invite_snapshot(guild)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "airlock invite-snapshot bootstrap failed for guild %s: %s",
                guild_id_str, exc,
            )
        # Team-inviter env bootstrap.
        bootstrap_user_ids = TEAM_INVITERS_BOOTSTRAP.get(guild_id_str, [])
        if not bootstrap_user_ids:
            continue
        with get_db() as conn:
            for uid in bootstrap_user_ids:
                discord_airlock.add_team_inviter(
                    conn,
                    guild_id=guild_id_str,
                    user_id=str(uid),
                    added_by="env_bootstrap",
                )


# ---------------------------------------------------------------------------
# Member-join handler (A4 — the core flow)
# ---------------------------------------------------------------------------


def _format_mod_ping(
    *,
    member: discord.Member,
    attribution: dict | None,
    is_team_invite: bool,
) -> str:
    """Pure renderer for the #triage mod ping. Split for direct testing."""
    if attribution is None:
        attrib_text = "invite **unknown** (vanity / restart-blackout / concurrent)"
    else:
        code = attribution.get("code", "unknown")
        inviter_id = attribution.get("inviter_user_id")
        if inviter_id:
            attrib_text = f"invite `{code}` (from <@{inviter_id}>)"
        else:
            attrib_text = f"invite `{code}` (no inviter — vanity)"
    if is_team_invite:
        tail = "auto-admitted as team-invite. no triage needed."
    else:
        tail = "use `/admit <user>`, `/ban <user> [reason]`, or `/kick <user> [reason]`."
    return f"🔔 airlock: <@{member.id}> joined via {attrib_text}. {tail}"


async def _handle_member_join(
    member: discord.Member,
    *,
    client: discord.Client,
) -> None:
    """Main airlock handler. Called for every on_member_join in a configured
    guild. Plan §0 + §1 architecture diagram.

    Steps:
      0) Kill-switch + guild-configured guard
      1) Bot-self skip (defensive)
      2) Refresh snapshot + attribute
      3) Resolve team-inviter status
      4) Branch:
         (a) team → grant member role, audit `fitcheck_airlock_auto_admitted`,
             record_admit `auto_admitted`. No DM, no mod ping (signal-noise).
         (b) non-team → grant airlock role, DM (best-effort), post mod ping
             (best-effort), audit `fitcheck_airlock_held`, record_admit `held`.

    Refresh-after-decision: snapshot is re-upserted from the fresh
    invite list within `_refresh_invite_snapshot` so the NEXT join's diff
    has the correct baseline.
    """
    if not AIRLOCK_ENABLED:
        return
    if member.bot:
        return
    guild = member.guild
    if guild is None:
        return
    guild_id = str(guild.id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        return

    # 1. Fetch fresh invite state WITHOUT updating the stored snapshot.
    # The stored snapshot is the diff baseline; if we upserted before
    # attribute_join, the diff would always be empty.
    fresh_rows = await _fetch_live_invites(guild)
    with get_db() as conn:
        attribution = discord_airlock.attribute_join(
            conn, guild_id=guild_id, fresh_invites=fresh_rows,
        )
        is_team_invite = False
        inviter_id = (
            attribution.get("inviter_user_id") if attribution else None
        )
        if inviter_id is not None:
            is_team_invite = discord_airlock.is_team_inviter(
                conn, guild_id, str(inviter_id)
            )

    if is_team_invite:
        await _auto_admit(
            member=member, guild=guild, guild_id=guild_id, org_id=org_id,
            attribution=attribution,
        )
    else:
        await _hold(
            member=member, guild=guild, guild_id=guild_id, org_id=org_id,
            attribution=attribution, client=client,
        )

    # Persist the fresh snapshot AFTER the decision so the NEXT join's
    # diff has the now-incremented uses baseline.
    try:
        _persist_invite_snapshot(guild_id, fresh_rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "post-decision snapshot persist failed for guild %s: %s",
            guild_id, exc,
        )


async def _auto_admit(
    *,
    member: discord.Member,
    guild: discord.Guild,
    guild_id: str,
    org_id: str,
    attribution: dict,
) -> None:
    """Team-invite path. Grant the default member role (if configured),
    record `auto_admitted`, audit. No DM + no mod ping — signal/noise
    pruning: team-invited joiners are expected, no triage needed."""
    member_role_id = AIRLOCK_DEFAULT_MEMBER_ROLES.get(guild_id)
    if member_role_id is not None:
        member_role = guild.get_role(int(member_role_id))
        if member_role is not None:
            try:
                await member.add_roles(
                    member_role, reason="airlock auto-admit (team invite)"
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "airlock auto-admit role grant failed for %s: %s",
                    member.id, exc,
                )
    with get_db() as conn:
        admit_id = discord_airlock.record_member_admit(
            conn,
            guild_id=guild_id, user_id=str(member.id),
            attributed_invite_code=attribution.get("code"),
            attributed_inviter_user_id=attribution.get("inviter_user_id"),
            is_team_invite=True,
            airlock_status="auto_admitted",
        )
        log_audit(
            conn,
            actor="discord:bot:auto",
            action="fitcheck_airlock_auto_admitted",
            org_id=org_id,
            entity_id=str(admit_id),
            detail={
                "guild_id": guild_id,
                "user_id": str(member.id),
                "attributed_invite_code": attribution.get("code"),
                "attributed_inviter_user_id": attribution.get("inviter_user_id"),
                "granted_role_id": member_role_id,
            },
            source="sable-roles",
        )


async def _hold(
    *,
    member: discord.Member,
    guild: discord.Guild,
    guild_id: str,
    org_id: str,
    attribution: dict | None,
    client: discord.Client,
) -> None:
    """Non-team path. Grant airlock role, DM (best-effort), mod ping
    (best-effort), audit + record_admit `held`."""
    airlock_role_id = AIRLOCK_ROLES.get(guild_id)
    role_grant_status = "skipped_no_config"
    if airlock_role_id is not None:
        airlock_role = guild.get_role(int(airlock_role_id))
        if airlock_role is None:
            logger.warning(
                "airlock role id %s not resolvable in guild %s",
                airlock_role_id, guild_id,
            )
            role_grant_status = "role_not_found"
        else:
            try:
                await member.add_roles(
                    airlock_role, reason="airlock hold (non-team invite)"
                )
                role_grant_status = "granted"
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "airlock role grant failed for %s: %s", member.id, exc
                )
                role_grant_status = f"failed:{type(exc).__name__}"

    # Best-effort DM. Forbidden = user has DMs disabled; not a failure.
    dm_status = "not_attempted"
    try:
        await member.send(_AIRLOCK_DM_TEXT)
        dm_status = "sent"
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.info(
            "airlock DM to %s skipped (%s)", member.id, type(exc).__name__
        )
        dm_status = f"failed:{type(exc).__name__}"

    # Best-effort mod ping.
    mod_ping_status = "not_attempted"
    mod_channel_id = AIRLOCK_MOD_CHANNELS.get(guild_id)
    if mod_channel_id is not None:
        mod_channel = guild.get_channel(int(mod_channel_id))
        if mod_channel is None:
            mod_ping_status = "channel_not_found"
        else:
            body = _format_mod_ping(
                member=member, attribution=attribution, is_team_invite=False,
            )
            try:
                await mod_channel.send(body)
                mod_ping_status = "sent"
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "airlock mod ping failed in guild %s: %s", guild_id, exc
                )
                mod_ping_status = f"failed:{type(exc).__name__}"

    with get_db() as conn:
        admit_id = discord_airlock.record_member_admit(
            conn,
            guild_id=guild_id, user_id=str(member.id),
            attributed_invite_code=(
                attribution.get("code") if attribution else None
            ),
            attributed_inviter_user_id=(
                attribution.get("inviter_user_id") if attribution else None
            ),
            is_team_invite=False,
            airlock_status="held",
        )
        log_audit(
            conn,
            actor="discord:bot:auto",
            action="fitcheck_airlock_held",
            org_id=org_id,
            entity_id=str(admit_id),
            detail={
                "guild_id": guild_id,
                "user_id": str(member.id),
                "attributed_invite_code": (
                    attribution.get("code") if attribution else None
                ),
                "attributed_inviter_user_id": (
                    attribution.get("inviter_user_id") if attribution else None
                ),
                "role_grant_status": role_grant_status,
                "dm_status": dm_status,
                "mod_ping_status": mod_ping_status,
            },
            source="sable-roles",
        )


async def _handle_member_remove(member: discord.Member) -> None:
    """Transition a pending airlock row to `left_during_airlock` if the
    user leaves voluntarily while held. Forensic value only — `/admit`
    won't fire on a rejoin against a left row because rejoin creates a
    fresh admit row via the UNIQUE+UPSERT path."""
    if member.guild is None:
        return
    guild_id = str(member.guild.id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        return
    with get_db() as conn:
        admit = discord_airlock.get_admit(
            conn, guild_id, str(member.id)
        )
        if admit is None or admit["airlock_status"] != "held":
            return
        discord_airlock.set_airlock_status(
            conn, guild_id=guild_id, user_id=str(member.id),
            new_status="left_during_airlock",
            decision_by="discord:bot:auto",
        )
        log_audit(
            conn,
            actor="discord:bot:auto",
            action="fitcheck_airlock_left_during_hold",
            org_id=org_id,
            entity_id=str(admit["id"]),
            detail={
                "guild_id": guild_id,
                "user_id": str(member.id),
            },
            source="sable-roles",
        )


# ---------------------------------------------------------------------------
# Mod surface — A5 commands (admit / ban / kick / airlock-status)
# ---------------------------------------------------------------------------


async def _resolve_target_member(
    interaction: discord.Interaction,
    target_user: discord.User | discord.Member,
) -> discord.Member | None:
    """Get the target as a Member (vs User). Returns None if they're
    not in the guild (e.g. they left already)."""
    if isinstance(target_user, discord.Member):
        return target_user
    if interaction.guild is None:
        return None
    return interaction.guild.get_member(target_user.id)


async def _handle_admit(
    interaction: discord.Interaction,
    target: discord.User,
) -> None:
    """`/admit @user` — remove airlock role, grant default member role,
    record `admitted`. Mod-gated via AIRLOCK_TRIAGE_ROLES."""
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None or interaction.guild_id is None:
        await interaction.followup.send(
            "this command must be run inside a server.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        await interaction.followup.send(
            "not configured for this server.", ephemeral=True
        )
        return
    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send(
            "this command must be run inside the server.", ephemeral=True
        )
        return
    if not _can_triage_airlock(interaction.user, guild_id):
        await interaction.followup.send(
            "you're not authorized to triage airlock.", ephemeral=True
        )
        return

    member = await _resolve_target_member(interaction, target)
    if member is None:
        await interaction.followup.send(
            "target user isn't in this server.", ephemeral=True
        )
        return

    # Role swap — remove airlock, grant member (if configured).
    airlock_role_id = AIRLOCK_ROLES.get(guild_id)
    member_role_id = AIRLOCK_DEFAULT_MEMBER_ROLES.get(guild_id)
    role_status: list[str] = []
    if airlock_role_id is not None:
        airlock_role = interaction.guild.get_role(int(airlock_role_id))
        if airlock_role is not None and airlock_role in member.roles:
            try:
                await member.remove_roles(
                    airlock_role, reason=f"/admit by {interaction.user.id}"
                )
                role_status.append("airlock_removed")
            except (discord.Forbidden, discord.HTTPException) as exc:
                role_status.append(f"airlock_remove_failed:{type(exc).__name__}")
    if member_role_id is not None:
        member_role = interaction.guild.get_role(int(member_role_id))
        if member_role is not None and member_role not in member.roles:
            try:
                await member.add_roles(
                    member_role, reason=f"/admit by {interaction.user.id}"
                )
                role_status.append("member_granted")
            except (discord.Forbidden, discord.HTTPException) as exc:
                role_status.append(f"member_grant_failed:{type(exc).__name__}")

    with get_db() as conn:
        updated = discord_airlock.set_airlock_status(
            conn, guild_id=guild_id, user_id=str(member.id),
            new_status="admitted", decision_by=str(interaction.user.id),
        )
        admit_id_row = discord_airlock.get_admit(conn, guild_id, str(member.id))
        log_audit(
            conn,
            actor=f"discord:user:{interaction.user.id}",
            action="fitcheck_airlock_admitted",
            org_id=org_id,
            entity_id=str(admit_id_row["id"]) if admit_id_row else None,
            detail={
                "guild_id": guild_id,
                "user_id": str(member.id),
                "had_admit_row": updated,
                "role_actions": role_status,
            },
            source="sable-roles",
        )

    if not updated:
        await interaction.followup.send(
            f"admitted <@{member.id}> (no prior airlock row — manual override).",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"admitted <@{member.id}>.", ephemeral=True
        )


async def _handle_ban(
    interaction: discord.Interaction,
    target: discord.User,
    reason: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None or interaction.guild_id is None:
        await interaction.followup.send(
            "this command must be run inside a server.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        await interaction.followup.send(
            "not configured for this server.", ephemeral=True
        )
        return
    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send(
            "this command must be run inside the server.", ephemeral=True
        )
        return
    if not _can_triage_airlock(interaction.user, guild_id):
        await interaction.followup.send(
            "you're not authorized to triage airlock.", ephemeral=True
        )
        return

    # Discord ban: works on User OR Member; the User shape suffices.
    try:
        await interaction.guild.ban(
            target,
            reason=f"airlock /ban by {interaction.user.id}: {reason}",
            delete_message_seconds=0,
        )
        ban_result = "banned"
    except discord.NotFound:
        ban_result = "user_not_found"
    except discord.Forbidden as exc:
        ban_result = f"forbidden:{exc}"
    except discord.HTTPException as exc:
        ban_result = f"http_error:{exc}"

    with get_db() as conn:
        updated = discord_airlock.set_airlock_status(
            conn, guild_id=guild_id, user_id=str(target.id),
            new_status="banned", decision_by=str(interaction.user.id),
            decision_reason=reason,
        )
        admit_row = discord_airlock.get_admit(conn, guild_id, str(target.id))
        log_audit(
            conn,
            actor=f"discord:user:{interaction.user.id}",
            action="fitcheck_airlock_banned",
            org_id=org_id,
            entity_id=str(admit_row["id"]) if admit_row else None,
            detail={
                "guild_id": guild_id,
                "user_id": str(target.id),
                "reason": reason,
                "had_admit_row": updated,
                "discord_ban_result": ban_result,
            },
            source="sable-roles",
        )

    if ban_result == "banned":
        await interaction.followup.send(
            f"banned <@{target.id}>. reason: {reason}", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"ban issued (discord result: {ban_result}). audit + state recorded.",
            ephemeral=True,
        )


async def _handle_kick(
    interaction: discord.Interaction,
    target: discord.User,
    reason: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None or interaction.guild_id is None:
        await interaction.followup.send(
            "this command must be run inside a server.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        await interaction.followup.send(
            "not configured for this server.", ephemeral=True
        )
        return
    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send(
            "this command must be run inside the server.", ephemeral=True
        )
        return
    if not _can_triage_airlock(interaction.user, guild_id):
        await interaction.followup.send(
            "you're not authorized to triage airlock.", ephemeral=True
        )
        return

    member = await _resolve_target_member(interaction, target)
    if member is None:
        await interaction.followup.send(
            "target user isn't in this server.", ephemeral=True
        )
        return

    try:
        await member.kick(reason=f"airlock /kick by {interaction.user.id}: {reason}")
        kick_result = "kicked"
    except discord.Forbidden as exc:
        kick_result = f"forbidden:{exc}"
    except discord.HTTPException as exc:
        kick_result = f"http_error:{exc}"

    with get_db() as conn:
        updated = discord_airlock.set_airlock_status(
            conn, guild_id=guild_id, user_id=str(member.id),
            new_status="kicked", decision_by=str(interaction.user.id),
            decision_reason=reason,
        )
        admit_row = discord_airlock.get_admit(conn, guild_id, str(member.id))
        log_audit(
            conn,
            actor=f"discord:user:{interaction.user.id}",
            action="fitcheck_airlock_kicked",
            org_id=org_id,
            entity_id=str(admit_row["id"]) if admit_row else None,
            detail={
                "guild_id": guild_id,
                "user_id": str(member.id),
                "reason": reason,
                "had_admit_row": updated,
                "discord_kick_result": kick_result,
            },
            source="sable-roles",
        )

    if kick_result == "kicked":
        await interaction.followup.send(
            f"kicked <@{member.id}>. reason: {reason}", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"kick issued (discord result: {kick_result}). audit + state recorded.",
            ephemeral=True,
        )


async def _handle_airlock_status(
    interaction: discord.Interaction,
    target: discord.User | None,
) -> None:
    """`/airlock-status [@user]` — ephemeral inspect. No-target version
    lists pending airlock holds. Mod-gated."""
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None or interaction.guild_id is None:
        await interaction.followup.send(
            "this command must be run inside a server.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        await interaction.followup.send(
            "not configured for this server.", ephemeral=True
        )
        return
    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send(
            "this command must be run inside the server.", ephemeral=True
        )
        return
    if not _can_triage_airlock(interaction.user, guild_id):
        await interaction.followup.send(
            "you're not authorized.", ephemeral=True
        )
        return

    with get_db() as conn:
        if target is None:
            pending = discord_airlock.list_pending_airlock(conn, guild_id)
        else:
            pending = None
            row = discord_airlock.get_admit(conn, guild_id, str(target.id))

    if target is None:
        if not pending:
            await interaction.followup.send(
                "no pending airlock holds.", ephemeral=True
            )
            return
        lines = ["pending airlock holds:"]
        for r in pending:
            attrib = r["attributed_invite_code"] or "unknown"
            inv = r["attributed_inviter_user_id"]
            inv_text = f" from <@{inv}>" if inv else " no inviter"
            lines.append(
                f"  • <@{r['user_id']}> — joined {r['joined_at']} via `{attrib}`{inv_text}"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    if row is None:
        await interaction.followup.send(
            f"<@{target.id}>: no admit record. either pre-feature member"
            " or never joined post-feature.",
            ephemeral=True,
        )
        return
    inviter_id = row["attributed_inviter_user_id"]
    inviter_text = f" from <@{inviter_id}>" if inviter_id else " (no inviter)"
    body = (
        f"<@{target.id}> — status: **{row['airlock_status']}**\n"
        f"joined: {row['joined_at']}\n"
        f"invite: `{row['attributed_invite_code'] or 'unknown'}`{inviter_text}\n"
        f"team_invite: {'yes' if row['is_team_invite'] else 'no'}\n"
        f"decision: by {row['decision_by'] or 'n/a'} at {row['decision_at'] or 'n/a'}\n"
        f"reason: {row['decision_reason'] or 'n/a'}"
    )
    await interaction.followup.send(body, ephemeral=True)


# ---------------------------------------------------------------------------
# Team-only commands (A6)
# ---------------------------------------------------------------------------


async def _handle_add_team_inviter(
    interaction: discord.Interaction,
    target: discord.User,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None or interaction.guild_id is None:
        await interaction.followup.send(
            "this command must be run inside a server.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        await interaction.followup.send(
            "not configured for this server.", ephemeral=True
        )
        return
    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send(
            "this command must be run inside the server.", ephemeral=True
        )
        return
    # Team-only — uses MOD_ROLES via _is_mod, NOT AIRLOCK_TRIAGE_ROLES.
    if not _is_mod(interaction.user, guild_id):
        await interaction.followup.send(
            "team-only command.", ephemeral=True
        )
        return

    with get_db() as conn:
        landed = discord_airlock.add_team_inviter(
            conn, guild_id=guild_id, user_id=str(target.id),
            added_by=str(interaction.user.id),
        )
        if landed:
            log_audit(
                conn,
                actor=f"discord:user:{interaction.user.id}",
                action="fitcheck_airlock_team_inviter_added",
                org_id=org_id, entity_id=None,
                detail={"guild_id": guild_id, "user_id": str(target.id)},
                source="sable-roles",
            )
    if landed:
        await interaction.followup.send(
            f"<@{target.id}> added to team-inviter allowlist.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"<@{target.id}> was already on the allowlist.", ephemeral=True
        )


async def _handle_list_team_inviters(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None or interaction.guild_id is None:
        await interaction.followup.send(
            "this command must be run inside a server.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        await interaction.followup.send(
            "not configured for this server.", ephemeral=True
        )
        return
    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send(
            "this command must be run inside the server.", ephemeral=True
        )
        return
    if not _is_mod(interaction.user, guild_id):
        await interaction.followup.send(
            "team-only command.", ephemeral=True
        )
        return

    with get_db() as conn:
        users = discord_airlock.list_team_inviters(conn, guild_id)
    if not users:
        await interaction.followup.send(
            "team-inviter allowlist is empty.", ephemeral=True
        )
        return
    lines = ["team-inviter allowlist:"]
    for u in users:
        lines.append(f"  • <@{u['user_id']}> — added {u['added_at']} by {u['added_by']}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(client: discord.Client) -> None:
    """Wire airlock gateway listeners + slash commands.

    Composes via wrap-existing-handler pattern with any prior
    on_member_join / on_member_remove / on_invite_create / on_invite_delete
    handlers (none today, but defensive against future feature additions).

    Slash commands are wired by register_commands(tree, client) below.
    """
    existing_on_member_join = getattr(client, "on_member_join", None)
    existing_on_member_remove = getattr(client, "on_member_remove", None)
    existing_on_invite_create = getattr(client, "on_invite_create", None)
    existing_on_invite_delete = getattr(client, "on_invite_delete", None)

    @client.event
    async def on_member_join(member: discord.Member):
        if existing_on_member_join is not None:
            await existing_on_member_join(member)
        await _handle_member_join(member, client=client)

    @client.event
    async def on_member_remove(member: discord.Member):
        if existing_on_member_remove is not None:
            await existing_on_member_remove(member)
        await _handle_member_remove(member)

    @client.event
    async def on_invite_create(invite: discord.Invite):
        if existing_on_invite_create is not None:
            await existing_on_invite_create(invite)
        await _on_invite_create(invite)

    @client.event
    async def on_invite_delete(invite: discord.Invite):
        if existing_on_invite_delete is not None:
            await existing_on_invite_delete(invite)
        await _on_invite_delete(invite)


def register_commands(
    tree: app_commands.CommandTree,
    *,
    client: discord.Client,
) -> None:
    """Register airlock slash commands. Called from setup_hook AFTER the
    rest of the command-tree wiring so the per-guild sync ships everything
    at once."""
    del client  # signature-locking sentinel; closures bind through tree

    @tree.command(
        name="admit",
        description="(mods) Admit an airlocked user — remove airlock role, grant member",
    )
    @app_commands.describe(user="The user to admit")
    async def admit_cmd(
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        await _handle_admit(interaction, user)

    @tree.command(
        name="ban",
        description="(mods) Ban a user from the server (permanent)",
    )
    @app_commands.describe(user="The user to ban", reason="Reason for the ban")
    async def ban_cmd(
        interaction: discord.Interaction,
        user: discord.User,
        reason: str,
    ) -> None:
        await _handle_ban(interaction, user, reason)

    @tree.command(
        name="kick",
        description="(mods) Kick a user from the server (rejoinable)",
    )
    @app_commands.describe(user="The user to kick", reason="Reason for the kick")
    async def kick_cmd(
        interaction: discord.Interaction,
        user: discord.User,
        reason: str,
    ) -> None:
        await _handle_kick(interaction, user, reason)

    @tree.command(
        name="airlock-status",
        description="(mods) Inspect airlock state for a user, or list pending holds",
    )
    @app_commands.describe(user="(optional) target user; omit to list pending holds")
    async def airlock_status_cmd(
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        await _handle_airlock_status(interaction, user)

    @tree.command(
        name="add-team-inviter",
        description="(team) Add a user to the team-inviter allowlist (invites bypass airlock)",
    )
    @app_commands.describe(user="The user to add to the team allowlist")
    async def add_team_inviter_cmd(
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        await _handle_add_team_inviter(interaction, user)

    @tree.command(
        name="list-team-inviters",
        description="(team) List the team-inviter allowlist for this server",
    )
    async def list_team_inviters_cmd(
        interaction: discord.Interaction,
    ) -> None:
        await _handle_list_team_inviters(interaction)
