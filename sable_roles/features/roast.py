"""Roast feature module — V1 + V2 + personalization layer.

R3 shipped `/set-personalize-mode`.
R5 added the V1 mod-only context-menu `/roast` (right-click message → Apps →
"Roast this fit").
R6 adds `/my-roasts` (peer-token status surface) + the lazy-grant SEAM
`_maybe_grant_monthly_token` that R7's peer-/roast handler will call as
its first post-gate step. R6 wires the seam ONLY into /my-roasts (so
checking status on the first day of a new month grants the token); R7
wires it into the actual peer-roast invocation path.

The rest of the surface (peer /roast economy + cap + refund + DM + flag,
streak-restoration grants, vibe injection, /peer-roast-report aggregation)
lands in R7-R9 per ~/Projects/SolStitch/internal/roast_build_TODO.md.

The toggle is gated on `config.PERSONALIZE_ADMINS` (user-ID allowlist)
rather than `MOD_ROLES` so a single operator (e.g. Arf) can flip
personalization without granting every mod the keys (plan §0.3).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands

from sable_platform.db import (
    discord_burn,
    discord_guild_config,
    discord_roast,
    discord_streaks,
    discord_user_vibes,
)
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db
# Private to SP but stable — pinning to it ensures R6's year_month format
# can never drift from the UNIQUE-constraint format used by grant_monthly_token.
from sable_platform.db.discord_roast import _current_year_month

from sable_roles.config import (
    BURN_DAILY_CAP_PER_USER,
    BURN_INVOKE_COOLDOWN_SECONDS,
    GUILD_TO_ORG,
    PEER_ROAST_ROLES,
    PERSONALIZE_ADMINS,
    VIBE_OBSERVATION_WINDOW_DAYS,
)
from sable_roles.features.burn_me import (
    REDACTED_MESSAGE,
    _burn_invoke_cooldown,
    _fetch_image_bytes,
    _is_image_for_roast,
    _is_inner_circle,
    generate_roast,
    is_burn_mode_never,
    record_roast_reply,
)
from sable_roles.features.fitcheck_streak import _FITCHECK_CHANNEL_IDS, _is_mod

logger = logging.getLogger("sable_roles.roast")


def _next_month_first_day(today_utc: date) -> str:
    """Return the first day of the calendar month AFTER today_utc as YYYY-MM-DD.

    Used by /my-roasts to render the "monthly reset" line — peer-roast
    tokens reset at month boundary per plan §0.2.
    """
    if today_utc.month == 12:
        return f"{today_utc.year + 1:04d}-01-01"
    return f"{today_utc.year:04d}-{today_utc.month + 1:02d}-01"


def _maybe_grant_monthly_token(
    conn,
    guild_id: str,
    actor_user_id: str,
) -> bool:
    """Lazy-grant the current calendar month's peer-roast token.

    Idempotent — wraps :func:`discord_roast.grant_monthly_token` which
    uses ON CONFLICT DO NOTHING to block the concurrent double-grant
    race (plan §0.2 POST-AUDIT). Writes the audit row only when a fresh
    row actually landed.

    Contract — load-bearing for R7's peer-/roast handler. R7 will call
    this with the SAME signature `(conn, guild_id, actor_user_id) -> bool`
    as the first step after gate validation; do not bake any
    /my-roasts-specific behavior into this seam.

    Caller is responsible for resolving `GUILD_TO_ORG[guild_id]` to a
    real `org_id` BEFORE calling — None org_id would fail at audit time.
    The /my-roasts handler validates this in its unconfigured-guild bounce.

    Returns ``True`` iff a new token row was inserted (i.e. this was the
    first attempt of the calendar month for this actor in this guild).
    Returns ``False`` if a token was already granted for the current
    (year_month, source='monthly') — R7's caller uses this to decide
    whether to DM the user about the fresh token.
    """
    # Capture year_month ONCE so the token row and the audit detail can't
    # disagree if wall-clock crosses month midnight between the grant and
    # the audit write.
    ym = _current_year_month()
    granted = discord_roast.grant_monthly_token(
        conn, guild_id, actor_user_id, year_month=ym
    )
    if granted:
        org_id = GUILD_TO_ORG.get(guild_id)
        log_audit(
            conn,
            actor=f"discord:user:{actor_user_id}",
            action="fitcheck_peer_roast_token_granted",
            org_id=org_id,
            entity_id=None,
            detail={
                "guild_id": guild_id,
                "actor_user_id": actor_user_id,
                "source": "monthly",
                "year_month": ym,
            },
            source="sable-roles",
        )
    return granted


async def _handle_set_personalize_mode(
    interaction: discord.Interaction,
    mode_value: str,
) -> None:
    """Underlying handler for /set-personalize-mode. Split from the tree.command
    closure so the unit tests can drive it without instantiating a CommandTree.

    Gates:
      1) Guild context required (DMs bounced before reading admin allowlist).
      2) Caller user_id must appear in PERSONALIZE_ADMINS[guild_id]. Non-admin
         bounces are silent (no audit row) so probing the gate stays cheap.
    """
    await interaction.response.defer(ephemeral=True)
    # DM-context defense: PERSONALIZE_ADMINS is per-guild; without a guild we
    # have nothing to scope against. Bounce before any allowlist read.
    if interaction.guild is None or interaction.guild_id is None:
        await interaction.followup.send(
            "this command must be run inside a server.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id)
    admins = {str(uid) for uid in PERSONALIZE_ADMINS.get(guild_id, [])}
    if str(interaction.user.id) not in admins:
        await interaction.followup.send(
            "you're not authorized.", ephemeral=True
        )
        return
    on = mode_value == "on"
    with get_db() as conn:
        discord_guild_config.set_personalize_mode(
            conn,
            guild_id=guild_id,
            on=on,
            updated_by=str(interaction.user.id),
        )
    await interaction.followup.send(
        f"personalize-mode is now **{mode_value}**.", ephemeral=True
    )


async def _handle_mod_roast(
    interaction: discord.Interaction,
    message: discord.Message,
) -> None:
    """Underlying handler for the mod-only "Roast this fit" message context
    menu (R5). Split from the @tree.context_menu closure so unit tests can
    drive it without spinning up a CommandTree.

    Gate order (R7 peer path will share this prefix — do not reorder):
      0) defer ephemeral immediately (Discord 3s response cap).
      1) DM-context defense — bounce before reading GUILD_TO_ORG.
      2) Resolve guild_id + org_id.
      3) Member-type defense (matches /burn-me).
      4) Mod gate — explicit ephemeral so the surface is discoverable to
         non-mods clicking the menu (R7 replaces this branch with peer-routing).
      5) Channel restriction — target must live in a configured fit-check channel.
      6) Actor cooldown — SHARED with /burn-me via the imported
         `_burn_invoke_cooldown` dict, keyed on actor_user_id.
      7) Target validation — non-bot author.
      8) Blocklist check (target opted out via /stop-pls).
      9) Daily-cap check (target's existing 20/day cap).
     10) Image gate + fetch.
     11) generate_roast — shipped signature; no actor_user_id / vibe_block (R11).
     12) message.reply (mention_author=False) — HTTPException swallowed.
     13) Ephemeral "roasted ✓" fires WHENEVER the LLM call succeeded, even if
         the reply itself failed (plan §0.1 — the roast was billed; from the
         mod's perspective the roast happened).
    """
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

    if message.channel.id not in _FITCHECK_CHANNEL_IDS:
        await interaction.followup.send(
            "skipped: target message isn't in a fit-check channel.",
            ephemeral=True,
        )
        return

    now = datetime.now(timezone.utc)
    last = _burn_invoke_cooldown.get(interaction.user.id)
    if last is not None and now - last < timedelta(
        seconds=BURN_INVOKE_COOLDOWN_SECONDS
    ):
        remaining = BURN_INVOKE_COOLDOWN_SECONDS - int((now - last).total_seconds())
        await interaction.followup.send(
            f"slow down — try again in {remaining}s.", ephemeral=True
        )
        return
    _burn_invoke_cooldown[interaction.user.id] = now

    target = message.author
    if target.bot:
        await interaction.followup.send(
            "skipped: can't roast a bot's post.", ephemeral=True
        )
        return

    with get_db() as conn:
        if discord_roast.is_blocklisted(conn, guild_id, str(target.id)):
            await interaction.followup.send(
                "skipped: target opted out.", ephemeral=True
            )
            return
        if (
            discord_burn.count_roasts_today(conn, guild_id, str(target.id))
            >= BURN_DAILY_CAP_PER_USER
        ):
            await interaction.followup.send(
                "skipped: target hit daily cap.", ephemeral=True
            )
            return

    attachment = next(
        (a for a in message.attachments if _is_image_for_roast(a)), None
    )
    if attachment is None:
        await interaction.followup.send(
            "skipped: no image attachment on that message.", ephemeral=True
        )
        return

    fetched = await _fetch_image_bytes(attachment)
    if fetched is None:
        await interaction.followup.send(
            "skipped: couldn't fetch image (oversize / unreadable).",
            ephemeral=True,
        )
        return
    image_bytes, media_type = fetched

    # R11 vibe injection — fetch the target's vibe block if personalize-
    # mode is on for this guild and a fresh row exists. Mod + peer paths
    # share the same fetch helper.
    with get_db() as conn:
        vibe_block = _maybe_fetch_vibe_block(
            conn, guild_id=guild_id, target_user_id=str(target.id)
        )

    result = await generate_roast(
        org_id=org_id,
        guild_id=guild_id,
        user_id=str(target.id),
        post_id=str(message.id),
        image_bytes=image_bytes,
        media_type=media_type,
        author_display_name=target.display_name,
        invocation_path="mod_roast",
        actor_user_id=str(interaction.user.id),
        vibe_block=vibe_block,
    )

    if result is None:
        # generate_roast already wrote the skipped/refused audit row.
        await interaction.followup.send(
            "skipped: model refused or call failed.", ephemeral=True
        )
        return
    # Mod path doesn't need the audit_id (no flag tracking — plan §8.2 says
    # only peer/restored roasts produce flag-eligible audit trails).
    roast_text, _audit_id = result

    try:
        await message.reply(roast_text, mention_author=False)
    except discord.HTTPException as exc:
        logger.warning(
            "mod-roast reply failed for post %s", message.id, exc_info=exc
        )

    # Plan §0.1: the "✓" fires whenever the LLM succeeded, even if the reply
    # itself failed. The roast was billed; from the mod's UX the roast happened.
    await interaction.followup.send("roasted ✓", ephemeral=True)


# Per-target DM throttle for the peer-roast "someone roasted you" silent DM.
# 5min per-user matches the fitcheck text-DM cooldown convention so a
# spammy target doesn't get DM'd more than once every 5min even when
# /roast bursts. Module-level dict, single-process (per sable-roles
# CLAUDE.md). Mutated in-place — tests must NOT rebind.
_target_dm_cooldown: dict[int, datetime] = {}

# Per-actor invocation throttle for peer /roast itself. Distinct from
# the SHARED `_burn_invoke_cooldown` because the peer surface is a
# higher-friction surface than /burn-me — but kept on the same key
# space (actor user id) so a peer /roast burst followed immediately by
# /burn-me still trips the SHARED cooldown (and vice versa).
_PEER_ROAST_DM_COOLDOWN_SECONDS = 300


# Per plan §0.2 — per-target volume cap (3/month, inner-circle bypass) and
# per-actor-per-target cooldown (90d, inner-circle bypass).
PEER_ROAST_TARGET_MONTHLY_CAP = 3
PEER_ROAST_ACTOR_TARGET_COOLDOWN_DAYS = 90


def _is_peer_eligible(member: discord.Member, guild_id: str) -> bool:
    """True iff the caller holds any role in PEER_ROAST_ROLES[guild_id].

    Both sides string-coerced so int-in-config vs string-from-JSON both
    work (real `.env` JSON parses to strings; tests often pass ints).
    """
    role_ids = {str(rid) for rid in PEER_ROAST_ROLES.get(guild_id, [])}
    if not role_ids:
        return False
    member_role_ids = {str(r.id) for r in member.roles}
    return bool(member_role_ids & role_ids)


def _maybe_fetch_vibe_block(
    conn,
    *,
    guild_id: str,
    target_user_id: str,
) -> str | None:
    """Resolve the vibe-block text to inject into generate_roast — or None
    if any gate fails.

    Gates (in order):
      1) `personalize_mode_on` for this guild (off → no vibe for anyone)
      2) Target is NOT blocklisted (stop-pls also opts out of vibe injection)
      3) Latest vibe row for target is fresh (within VIBE_OBSERVATION_WINDOW_DAYS;
         stale → no vibe rather than ship a months-old inference)

    Returns the rendered ``<user_vibe>...</user_vibe>`` block text on
    success. Caller passes this directly into generate_roast's
    `vibe_block` kwarg.
    """
    cfg = discord_guild_config.get_config(conn, guild_id)
    if not cfg.get("personalize_mode_on"):
        return None
    if discord_roast.is_blocklisted(conn, guild_id, target_user_id):
        return None
    vibe = discord_user_vibes.get_latest_vibe(
        conn, guild_id, target_user_id,
        max_age_days=VIBE_OBSERVATION_WINDOW_DAYS,
    )
    if vibe is None:
        return None
    block = vibe.get("vibe_block_text")
    return str(block) if block else None


async def _send_peer_roast_dm(
    *,
    target_user: discord.abc.User,
    actor_display_name: str,
    jump_link: str,
    org_id: str,
    guild_id: str,
    actor_user_id: str,
    target_user_id: str,
    post_id: str,
) -> bool:
    """Silent DM to peer-roast target. Returns True iff DM landed.

    Cooldown-suppressed and Forbidden-failure both write an audit row
    (`fitcheck_peer_roast_dm_skipped`) so /peer-roast-report can surface
    DM-deliverability metrics. Happy path writes no audit (lean log).

    Per plan §8.1 — body mirrors the locked text byte-for-byte. The 🚩
    react instruction MUST appear so the reaction-flag handler closes
    the consent loop.
    """
    now = datetime.now(timezone.utc)
    last = _target_dm_cooldown.get(target_user.id)
    cooldown_active = (
        last is not None
        and (now - last).total_seconds() < _PEER_ROAST_DM_COOLDOWN_SECONDS
    )
    if cooldown_active:
        with get_db() as conn:
            log_audit(
                conn,
                actor="discord:bot:auto",
                action="fitcheck_peer_roast_dm_skipped",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "actor_user_id": actor_user_id,
                    "target_user_id": target_user_id,
                    "post_id": post_id,
                    "reason": "cooldown",
                },
                source="sable-roles",
            )
        return False
    body = (
        f"{actor_display_name} roasted your fit in #fitcheck ({jump_link}). "
        "React 🚩 to flag this to mods, or run /stop-pls to permanently opt out "
        "of being roasted."
    )
    try:
        await target_user.send(body)
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.info(
            "peer-roast DM to %s failed: %s", target_user_id, exc
        )
        with get_db() as conn:
            log_audit(
                conn,
                actor="discord:bot:auto",
                action="fitcheck_peer_roast_dm_skipped",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "actor_user_id": actor_user_id,
                    "target_user_id": target_user_id,
                    "post_id": post_id,
                    "reason": f"send_failed:{type(exc).__name__}",
                },
                source="sable-roles",
            )
        return False
    _target_dm_cooldown[target_user.id] = now
    return True


def _peer_audit_consumed(
    conn,
    *,
    org_id: str,
    guild_id: str,
    actor_user_id: str,
    target_user_id: str,
    post_id: str,
    token_id: int,
    source: str,
) -> None:
    """Write `fitcheck_peer_roast_consumed` audit row — fires immediately
    after consume_token succeeds. entity_id carries the token row id so
    a downstream refund row JOINs cleanly on entity_id.
    """
    log_audit(
        conn,
        actor=f"discord:user:{actor_user_id}",
        action="fitcheck_peer_roast_consumed",
        org_id=org_id,
        entity_id=str(token_id),
        detail={
            "guild_id": guild_id,
            "actor_user_id": actor_user_id,
            "target_user_id": target_user_id,
            "post_id": post_id,
            "token_id": token_id,
            "token_source": source,
        },
        source="sable-roles",
    )


def _peer_audit_refunded(
    conn,
    *,
    org_id: str,
    guild_id: str,
    actor_user_id: str,
    target_user_id: str,
    post_id: str,
    token_id: int,
    reason: str,
) -> None:
    log_audit(
        conn,
        actor=f"discord:user:{actor_user_id}",
        action="fitcheck_peer_roast_refunded",
        org_id=org_id,
        entity_id=str(token_id),
        detail={
            "guild_id": guild_id,
            "actor_user_id": actor_user_id,
            "target_user_id": target_user_id,
            "post_id": post_id,
            "token_id": token_id,
            "reason": reason,
        },
        source="sable-roles",
    )


def _safe_refund_token(
    conn,
    *,
    org_id: str,
    guild_id: str,
    actor_user_id: str,
    target_user_id: str,
    post_id: str,
    token_id: int,
    reason: str,
) -> None:
    """Refund a consumed token + write the paired refund audit row.

    Wraps :func:`discord_roast.refund_token` so a transient SP failure
    doesn't leak a raw exception to the user OR strand the token in the
    consumed state with no audit pair. On refund failure: log the error,
    write a `fitcheck_peer_roast_refund_failed` audit row so an operator
    can manually re-refund, and return without raising. Caller still
    sends the user-facing "token refunded" ephemeral — they should
    perceive the same UX either way; reconciliation is operator-side.
    """
    try:
        discord_roast.refund_token(conn, token_id)
    except Exception as exc:  # noqa: BLE001 — never crash handler on refund
        logger.warning(
            "refund_token raised for token_id=%s reason=%s: %s",
            token_id, reason, exc,
        )
        try:
            log_audit(
                conn,
                actor=f"discord:user:{actor_user_id}",
                action="fitcheck_peer_roast_refund_failed",
                org_id=org_id,
                entity_id=str(token_id),
                detail={
                    "guild_id": guild_id,
                    "actor_user_id": actor_user_id,
                    "target_user_id": target_user_id,
                    "post_id": post_id,
                    "token_id": token_id,
                    "reason": reason,
                    "refund_error": f"{type(exc).__name__}:{exc}",
                },
                source="sable-roles",
            )
        except Exception as audit_exc:  # noqa: BLE001
            logger.warning(
                "refund_failed audit also raised for token_id=%s: %s",
                token_id, audit_exc,
            )
        return
    _peer_audit_refunded(
        conn,
        org_id=org_id, guild_id=guild_id,
        actor_user_id=actor_user_id, target_user_id=target_user_id,
        post_id=post_id, token_id=token_id, reason=reason,
    )


def _peer_audit_skipped(
    conn,
    *,
    org_id: str,
    guild_id: str,
    actor_user_id: str,
    target_user_id: str,
    post_id: str,
    reason: str,
) -> None:
    """Pre-token-consume gate bounce audit. Token id is None because
    no token was charged — caller hit a gate before lazy-grant +
    consume. Distinct from `fitcheck_peer_roast_refunded` which fires
    AFTER consume + refund.
    """
    log_audit(
        conn,
        actor=f"discord:user:{actor_user_id}",
        action="fitcheck_peer_roast_skipped",
        org_id=org_id,
        entity_id=None,
        detail={
            "guild_id": guild_id,
            "actor_user_id": actor_user_id,
            "target_user_id": target_user_id,
            "post_id": post_id,
            "reason": reason,
        },
        source="sable-roles",
    )


async def _handle_peer_roast(
    interaction: discord.Interaction,
    message: discord.Message,
) -> None:
    """Peer-/roast context-menu handler (R7).

    Routed to from the context-menu closure when the caller is NOT a mod.
    Implements plan §0.2 + §5.2:

      0) defer ephemeral
      1) DM bounce
      2) unconfigured-guild bounce
      3) Member-type defense
      4) Peer eligibility role gate (no role → friendly bounce; NO audit)
      5) Channel restriction (target message must be in a fit-check channel)
      6) SHARED cooldown w/ /burn-me + /roast mod path
      7) Bot author skip (silent)
      8) Self-roast block (peer can't roast their own fit)
      9) Single DB block — gate chain BEFORE token consumption:
         a) blocklist (target opted out) → bounce + audit (no refund — no token charged)
         b) daily cap → bounce + audit
         c) per-target volume cap (3/month) UNLESS inner-circle bypass → bounce + audit
         d) per-actor-target 90d cooldown UNLESS inner-circle bypass → bounce + audit
         e) lazy grant via _maybe_grant_monthly_token (idempotent)
         f) available_token → None → "no tokens this month" bounce (no audit; pure UX message)
         g) consume_token (atomic) + audit `fitcheck_peer_roast_consumed`
     10) Image fetch → if fail: REFUND + audit + bounce
     11) generate_roast (invocation_path="peer_roast", actor_user_id stamped)
     12) None (refusal/error) → REFUND + audit + bounce
     13) message.reply
     14) record_roast_reply audit (links bot_reply_id → audit_log_id)
     15) Fire-and-forget _send_peer_roast_dm (target gets silent DM + 🚩 instruction)
     16) ephemeral "roasted ✓"

    A refund leaves the token row visible-as-unspent (consumed_at=NULL)
    so /my-roasts immediately reflects it. The original
    `fitcheck_peer_roast_consumed` audit row stays for forensics — the
    refund row is the audit-pair, not a delete.
    """
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

    # never-mode lockdown BEFORE the @Stitch role-gate, so non-peer
    # callers also see REDACTED — the whole peer surface looks dark
    # while the server is locked down (avoid leaking "if you had the
    # role you could probe me" via the friendly bounce).
    with get_db() as conn:
        if is_burn_mode_never(conn, guild_id):
            await interaction.followup.send(
                REDACTED_MESSAGE, ephemeral=True
            )
            return

    actor_user_id = str(interaction.user.id)
    actor_display_name = interaction.user.display_name

    if not _is_peer_eligible(interaction.user, guild_id):
        # Friendly bounce — discoverability + no silent failure.
        await interaction.followup.send(
            "you need the @Stitch role to use /roast.",
            ephemeral=True,
        )
        return

    if message.channel.id not in _FITCHECK_CHANNEL_IDS:
        await interaction.followup.send(
            "skipped: target message isn't in a fit-check channel.",
            ephemeral=True,
        )
        return

    now = datetime.now(timezone.utc)
    last_invoke = _burn_invoke_cooldown.get(interaction.user.id)
    if last_invoke is not None and now - last_invoke < timedelta(
        seconds=BURN_INVOKE_COOLDOWN_SECONDS
    ):
        remaining = BURN_INVOKE_COOLDOWN_SECONDS - int(
            (now - last_invoke).total_seconds()
        )
        await interaction.followup.send(
            f"slow down — try again in {remaining}s.", ephemeral=True
        )
        return
    _burn_invoke_cooldown[interaction.user.id] = now

    target = message.author
    if target.bot:
        await interaction.followup.send(
            "skipped: can't roast a bot's post.", ephemeral=True
        )
        return

    target_user_id = str(target.id)
    if target_user_id == actor_user_id:
        # Peer-self-roast doesn't make sense in the economy (you'd be
        # spending your token on yourself). Allowed in mod path per
        # plan §0.1 ("self-roast allowed, silly, harmless") but not peer.
        await interaction.followup.send(
            "skipped: can't peer-roast your own fit.", ephemeral=True
        )
        return

    post_id = str(message.id)
    target_is_inner_circle = isinstance(
        target, discord.Member
    ) and _is_inner_circle(target, guild_id)

    # ------------------------------------------------------------------
    # Pre-token gate chain (no token charged yet — bounces are free).
    # ------------------------------------------------------------------
    with get_db() as conn:
        if discord_roast.is_blocklisted(conn, guild_id, target_user_id):
            _peer_audit_skipped(
                conn, org_id=org_id, guild_id=guild_id,
                actor_user_id=actor_user_id, target_user_id=target_user_id,
                post_id=post_id, reason="target_blocklisted",
            )
            await interaction.followup.send(
                "skipped: target opted out.", ephemeral=True
            )
            return

        if (
            discord_burn.count_roasts_today(conn, guild_id, target_user_id)
            >= BURN_DAILY_CAP_PER_USER
        ):
            _peer_audit_skipped(
                conn, org_id=org_id, guild_id=guild_id,
                actor_user_id=actor_user_id, target_user_id=target_user_id,
                post_id=post_id, reason="target_daily_cap",
            )
            await interaction.followup.send(
                "skipped: target hit daily cap.", ephemeral=True
            )
            return

        if not target_is_inner_circle:
            target_month_count = (
                discord_roast.count_target_peer_roasts_this_month(
                    conn, guild_id, target_user_id
                )
            )
            if target_month_count >= PEER_ROAST_TARGET_MONTHLY_CAP:
                _peer_audit_skipped(
                    conn, org_id=org_id, guild_id=guild_id,
                    actor_user_id=actor_user_id, target_user_id=target_user_id,
                    post_id=post_id, reason="target_month_cap",
                )
                await interaction.followup.send(
                    "skipped: that user has hit this month's peer-roast cap.",
                    ephemeral=True,
                )
                return

            if discord_roast.cooldown_active_between(
                conn, guild_id, actor_user_id, target_user_id,
                within_days=PEER_ROAST_ACTOR_TARGET_COOLDOWN_DAYS,
            ):
                _peer_audit_skipped(
                    conn, org_id=org_id, guild_id=guild_id,
                    actor_user_id=actor_user_id, target_user_id=target_user_id,
                    post_id=post_id, reason="actor_target_cooldown",
                )
                await interaction.followup.send(
                    "skipped: you already roasted them recently — wait it out.",
                    ephemeral=True,
                )
                return

        # Lazy grant — idempotent. If today's the first call of the month,
        # this lands the token. The granted-audit row is written by the seam.
        _maybe_grant_monthly_token(conn, guild_id, actor_user_id)

        token = discord_roast.available_token(conn, guild_id, actor_user_id)
        if token is None:
            # No audit — pure UX bounce. Token was either already spent
            # this month or restoration grants haven't landed yet.
            await interaction.followup.send(
                "no tokens left this month — wait for the reset or hit a 7-day streak.",
                ephemeral=True,
            )
            return

        token_id = int(token["id"])
        token_source = str(token["source"])
        consumed = discord_roast.consume_token(
            conn, token_id, target_user_id=target_user_id, post_id=post_id
        )
        if not consumed:
            # Race lost — another invocation consumed this token first.
            # Re-fetch to find the next available, OR bounce. Bounce keeps
            # the code simple; the user retries.
            await interaction.followup.send(
                "race condition — try again.", ephemeral=True
            )
            return
        _peer_audit_consumed(
            conn, org_id=org_id, guild_id=guild_id,
            actor_user_id=actor_user_id, target_user_id=target_user_id,
            post_id=post_id, token_id=token_id, source=token_source,
        )

    # ------------------------------------------------------------------
    # Post-consume: image fetch + LLM. Any failure REFUNDS the token.
    # ------------------------------------------------------------------
    attachment = next(
        (a for a in message.attachments if _is_image_for_roast(a)), None
    )
    if attachment is None:
        with get_db() as conn:
            _safe_refund_token(
                conn, org_id=org_id, guild_id=guild_id,
                actor_user_id=actor_user_id, target_user_id=target_user_id,
                post_id=post_id, token_id=token_id, reason="no_image",
            )
        await interaction.followup.send(
            "skipped: no image attachment on that message (token refunded).",
            ephemeral=True,
        )
        return

    fetched = await _fetch_image_bytes(attachment)
    if fetched is None:
        with get_db() as conn:
            _safe_refund_token(
                conn, org_id=org_id, guild_id=guild_id,
                actor_user_id=actor_user_id, target_user_id=target_user_id,
                post_id=post_id, token_id=token_id, reason="image_fetch_failed",
            )
        await interaction.followup.send(
            "skipped: couldn't fetch image (oversize / unreadable). Token refunded.",
            ephemeral=True,
        )
        return
    image_bytes, media_type = fetched

    with get_db() as conn:
        vibe_block = _maybe_fetch_vibe_block(
            conn, guild_id=guild_id, target_user_id=target_user_id
        )

    result = await generate_roast(
        org_id=org_id,
        guild_id=guild_id,
        user_id=target_user_id,
        post_id=post_id,
        image_bytes=image_bytes,
        media_type=media_type,
        author_display_name=target.display_name,
        invocation_path="peer_roast",
        actor_user_id=actor_user_id,
        vibe_block=vibe_block,
    )

    if result is None:
        with get_db() as conn:
            _safe_refund_token(
                conn, org_id=org_id, guild_id=guild_id,
                actor_user_id=actor_user_id, target_user_id=target_user_id,
                post_id=post_id, token_id=token_id, reason="llm_refused_or_failed",
            )
        await interaction.followup.send(
            "skipped: model refused or call failed. Token refunded.",
            ephemeral=True,
        )
        return
    roast_text, audit_log_id = result

    reply_id: int | None = None
    try:
        reply_msg = await message.reply(roast_text, mention_author=False)
        reply_id = reply_msg.id
    except discord.HTTPException as exc:
        logger.warning(
            "peer-roast reply failed for post %s", message.id, exc_info=exc
        )

    if reply_id is not None:
        # Link bot_reply_id → audit_log_id so the 🚩 flag handler can
        # JOIN back to the originating roast event.
        record_roast_reply(
            audit_log_id=audit_log_id,
            bot_reply_id=str(reply_id),
            guild_id=guild_id,
            org_id=org_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            post_id=post_id,
        )
        # Fire-and-forget DM. Don't block the ✓ response on it.
        jump_link = reply_msg.jump_url if hasattr(reply_msg, "jump_url") else ""
        asyncio.create_task(
            _send_peer_roast_dm(
                target_user=target,
                actor_display_name=actor_display_name,
                jump_link=jump_link,
                org_id=org_id,
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                post_id=post_id,
            )
        )

    await interaction.followup.send("roasted ✓", ephemeral=True)


# ---------------------------------------------------------------------------
# 🚩 flag detection — on_raw_reaction_add
# ---------------------------------------------------------------------------


_FLAG_EMOJI = "🚩"


async def _handle_flag_reaction(
    payload: discord.RawReactionActionEvent,
    *,
    client: discord.Client,
) -> None:
    """Handle a 🚩 reaction on a bot message. If the bot message is a peer-
    roast reply (i.e. has a matching `fitcheck_roast_replied` audit row),
    insert a flag row and write `fitcheck_peer_roast_flagged` audit.

    Silently ignores 🚩 on non-bot messages, on bot messages that aren't
    peer-roast replies (opt-in / random / mod-roast), and DM-channel
    reactions (no guild context).
    """
    if str(payload.emoji) != _FLAG_EMOJI:
        return
    if payload.guild_id is None:
        return
    if client.user is None or payload.user_id == client.user.id:
        # Don't react to our own reactions (in case the bot ever reacts 🚩).
        return

    guild_id = str(payload.guild_id)
    bot_reply_id = str(payload.message_id)
    reactor_user_id = str(payload.user_id)

    with get_db() as conn:
        match = discord_roast.find_peer_roast_for_bot_reply(conn, bot_reply_id)
        if match is None:
            return  # not a tracked peer-roast reply
        org_id = GUILD_TO_ORG.get(guild_id)
        if org_id is None:
            return  # bot doesn't manage this guild
        flag_id = discord_roast.insert_flag(
            conn,
            guild_id=guild_id,
            target_user_id=match["target_user_id"],
            actor_user_id=match["actor_user_id"],
            post_id=match["post_id"],
            bot_reply_id=bot_reply_id,
            reactor_user_id=reactor_user_id,
        )
        log_audit(
            conn,
            actor=f"discord:user:{reactor_user_id}",
            action="fitcheck_peer_roast_flagged",
            org_id=org_id,
            entity_id=str(flag_id),
            detail={
                "guild_id": guild_id,
                "target_user_id": match["target_user_id"],
                "actor_user_id": match["actor_user_id"],
                "post_id": match["post_id"],
                "bot_reply_id": bot_reply_id,
                "reactor_user_id": reactor_user_id,
            },
            source="sable-roles",
        )


def register(client: discord.Client) -> None:
    """Wire roast's gateway-event listeners against the client.

    Bare ``discord.Client`` exposes no multi-handler API
    (``add_listener``/``extra_events`` live on ``discord.ext.commands.Bot``
    only) and ``@client.event`` uses ``setattr``, which would CLOBBER
    fitcheck_streak's pre-existing ``on_raw_reaction_add`` debounce
    handler (registered earlier in setup_hook) — breaking streak
    reaction-scoring in prod.

    Compose instead: read whatever ``on_raw_reaction_add`` is already
    attached, then bind a new one that calls the old + our 🚩 handler.
    MUST be called AFTER any other module's reaction registration.
    """
    existing_on_raw_reaction_add = getattr(
        client, "on_raw_reaction_add", None
    )

    @client.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        if existing_on_raw_reaction_add is not None:
            await existing_on_raw_reaction_add(payload)
        await _handle_flag_reaction(payload, client=client)


# ---------------------------------------------------------------------------
# R8 — Streak restoration token grant
# ---------------------------------------------------------------------------


_RESTORATION_DM_TEXT = (
    "you hit 7 days — bonus roast token earned. /my-roasts to check status."
)


async def _handle_peer_roast_report(
    interaction: discord.Interaction,
    days: int,
) -> None:
    """Mod-only ephemeral report on peer-roast activity in the last N days.

    Aggregates via SP `discord_roast.aggregate_peer_roast_report` (shipped
    in R1, fully dialect-aware). Adds a header line showing
    `personalize_mode_on` so mods can see toggle state without a
    separate command. Body renders one row per (actor, target) pair plus
    a blocklist tail.

    Gate order:
      0) defer ephemeral
      1) DM bounce
      2) unconfigured-guild bounce
      3) Member-type defense
      4) Mod gate (friendly "you're not a mod" bounce; NO audit)
      5) days clamping (1..365 — anything outside is operator error)
      6) Single DB block: get_config + aggregate + list_blocklisted
      7) Send ephemeral body
    """
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
            "you're not a mod.", ephemeral=True
        )
        return

    # Clamp days into a sane window; 365 covers a year.
    days = max(1, min(int(days), 365))

    with get_db() as conn:
        cfg = discord_guild_config.get_config(conn, guild_id)
        rows = discord_roast.aggregate_peer_roast_report(
            conn, guild_id, lookback_days=days
        )
        blocklisted = discord_roast.list_blocklisted_users(conn, guild_id)

    personalize_on = bool(cfg.get("personalize_mode_on"))
    body = _format_peer_roast_report(
        days=days,
        personalize_on=personalize_on,
        rows=rows,
        blocklisted=blocklisted,
    )
    await interaction.followup.send(body, ephemeral=True)


def _format_peer_roast_report(
    *,
    days: int,
    personalize_on: bool,
    rows: list[dict],
    blocklisted: list[str],
) -> str:
    """Pure renderer for /peer-roast-report — split for direct testing.

    Returns a code-blocked monospace body Discord renders cleanly in
    ephemeral. Single backtick-fenced block keeps spacing consistent.
    """
    header = (
        f"peer-roast report — last {days} days\n"
        f"personalize: {'on' if personalize_on else 'off'}\n"
    )
    if not rows:
        body_rows = "no peer-roast activity in this window."
    else:
        body_lines = [
            "actor → target: count (flags: X · self-flags: Y)",
            "-" * 50,
        ]
        for r in rows:
            body_lines.append(
                f"{r['actor_user_id']} → {r['target_user_id']}:"
                f" {r['n']}"
                f" (flags: {r['flag_count']} · self-flags: {r['self_flag_count']})"
            )
        body_rows = "\n".join(body_lines)
    block_tail = (
        f"\n\nblocklisted users (all-time): {len(blocklisted)}"
        + (f"\n  {', '.join(blocklisted)}" if blocklisted else "")
    )
    return f"```\n{header}\n{body_rows}{block_tail}\n```"


async def maybe_grant_restoration_token(
    *,
    client: discord.Client | None,
    user_id: str,
    guild_id: str,
    org_id: str,
) -> bool:
    """Grant the streak-restoration bonus token if user JUST hit 7 days.

    Per plan §0.2 + §6: hook fires from the fitcheck image-branch tail on
    every fit-post. Cheap: when current_streak != 7 we return immediately
    without writing anything; when current_streak == 7 the SP
    ``grant_restoration_token`` helper uses ON CONFLICT DO NOTHING, so a
    second grant for the same (guild, user, year_month, 'streak_restoration')
    silently no-ops. The audit row + DM only fire on the True-grant path
    so spam is bounded.

    **Multi-per-month limitation** (mig 047 UNIQUE constraint): the plan
    §0.2 spec says "multi-per-month-OK (break streak, re-hit 7 → another
    token)", but mig 047's UNIQUE(guild_id, actor_user_id, year_month,
    source) blocks a second restoration grant in the same calendar month
    even if the prior one was already consumed. Lifting this requires a
    schema change deferred to a future mig.

    `client` is optional so the grandfathering CLI (which has no Discord
    client handle) can call this with `client=None` to skip the DM.

    Returns True iff a fresh token row was inserted.
    """
    with get_db() as conn:
        state = discord_streaks.compute_streak_state(conn, org_id, user_id)
        if int(state.get("current_streak", 0)) != 7:
            return False
        granted = discord_roast.grant_restoration_token(
            conn, guild_id, user_id
        )
        if not granted:
            return False
        log_audit(
            conn,
            actor="discord:bot:auto",
            action="fitcheck_peer_roast_token_granted",
            org_id=org_id,
            entity_id=None,
            detail={
                "guild_id": guild_id,
                "actor_user_id": user_id,
                "source": "streak_restoration",
                "year_month": _current_year_month(),
            },
            source="sable-roles",
        )

    if client is None:
        return True

    # never-mode lockdown — suppress the "bonus token earned" DM so the
    # user isn't told about a token they can't currently spend (peer
    # /roast is REDACTED while never-mode is on). The grant + audit row
    # already landed above, so the token is preserved for whenever
    # never mode is flipped back to once/persist.
    with get_db() as conn:
        if is_burn_mode_never(conn, guild_id):
            return True

    # Best-effort DM. Failures don't undo the grant — the user will see
    # the token surfaced via /my-roasts on next invocation.
    try:
        user = client.get_user(int(user_id))
        if user is None:
            user = await client.fetch_user(int(user_id))
        await user.send(_RESTORATION_DM_TEXT)
    except (
        discord.HTTPException, discord.Forbidden, discord.NotFound,
        ValueError, AttributeError,
    ) as exc:
        logger.info(
            "restoration-grant DM to user %s failed: %s", user_id, exc
        )
    return True


_MY_ROASTS_RULES_FOOTER = (
    "rules\n"
    "— peer /roast lets you cast 1 burn per calendar month on another fit\n"
    "— hit a 7-day streak to earn a bonus restoration token (1/month max — break + re-streak next month for another)\n"
    "— targets get a silent DM with a 🚩 react to flag mods + /stop-pls to opt out\n"
    "— sticky stop-pls protects EVEN inner-circle members\n"
    "— /my-roasts grants your monthly token on first call of the new month"
)


def _format_my_roasts(
    *,
    tokens_left: int,
    peer_eligible: bool,
    current_streak: int,
    reset_date: str,
    last_consumed: dict | None,
    just_granted: bool,
) -> str:
    """Pure renderer for /my-roasts body. Split from the handler so unit
    tests can drive it without mocking a Discord interaction."""
    progress = min(int(current_streak), 7)
    if last_consumed is None:
        last_line = "none yet"
    else:
        # last_consumed comes from SP's last_consumed_token which filters
        # `consumed_at IS NOT NULL` + sets consumed_target_user_id at consume
        # time — both fields are guaranteed populated for any row this branch
        # ever sees, so no fallbacks needed.
        consumed_at = last_consumed["consumed_at"][:10]
        target_id = last_consumed["consumed_target_user_id"]
        last_line = f"{consumed_at} on user {target_id}"

    just_granted_line = (
        "\n(fresh token granted — happy hunting.)" if just_granted else ""
    )
    # Suppress the @Stitch hint when the user has nothing to cast — telling
    # someone with 0 tokens that they "need a role to cast" is just noise.
    role_gate_line = (
        ""
        if peer_eligible or tokens_left == 0
        else "\nheads-up: you have tokens but need the @Stitch role to cast /roast."
    )

    return (
        "your peer-roast status\n"
        "\n"
        f"tokens left this month: {tokens_left}\n"
        f"streak progress: {progress}/7 days\n"
        f"monthly reset: {reset_date}\n"
        f"last roast cast: {last_line}"
        f"{just_granted_line}"
        f"{role_gate_line}\n"
        "\n"
        f"{_MY_ROASTS_RULES_FOOTER}"
    )


async def _handle_my_roasts(interaction: discord.Interaction) -> None:
    """Underlying handler for /my-roasts. Split from the @tree.command
    closure so unit tests can drive it without spinning up a CommandTree.

    Plan §0.2 lazy-grant is wired here in addition to R7's eventual peer-
    /roast path — operator intent is that "checking status" counts as the
    first-touch for the month so users never see "0 tokens" when they
    actually have one queued up. The grant is idempotent (ON CONFLICT
    DO NOTHING + audit-only-when-row-landed) so repeat invocations don't
    fan out duplicate audit rows.

    Gate order:
      0) defer ephemeral immediately (Discord 3s response cap).
      1) DM-context bounce — before any DB read.
      2) Resolve guild_id + org_id; "not configured" bounce if no mapping.
      3) Member-type defense (matches mod-/roast pattern).
      4) Single get_db() block: lazy-grant → count_available → last_consumed
         → compute_streak_state. One session per invocation.
      5) Build + send the ephemeral body via _format_my_roasts.
    """
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

    # never-mode lockdown — REDACTED bounce before any DB read so the
    # surface looks dark to anyone probing during a server lockdown.
    with get_db() as conn:
        if is_burn_mode_never(conn, guild_id):
            await interaction.followup.send(
                REDACTED_MESSAGE, ephemeral=True
            )
            return

    actor_user_id = str(interaction.user.id)

    with get_db() as conn:
        just_granted = _maybe_grant_monthly_token(
            conn, guild_id, actor_user_id
        )
        tokens_left = discord_roast.count_available_tokens(
            conn, guild_id, actor_user_id
        )
        last_consumed = discord_roast.last_consumed_token(
            conn, guild_id, actor_user_id
        )
        streak_state = discord_streaks.compute_streak_state(
            conn, org_id, actor_user_id
        )

    current_streak = int(streak_state.get("current_streak", 0))
    reset_date = _next_month_first_day(datetime.now(timezone.utc).date())
    peer_role_ids = {str(rid) for rid in PEER_ROAST_ROLES.get(guild_id, [])}
    user_role_ids = {str(r.id) for r in interaction.user.roles}
    peer_eligible = bool(peer_role_ids & user_role_ids)

    body = _format_my_roasts(
        tokens_left=tokens_left,
        peer_eligible=peer_eligible,
        current_streak=current_streak,
        reset_date=reset_date,
        last_consumed=last_consumed,
        just_granted=just_granted,
    )
    await interaction.followup.send(body, ephemeral=True)


def register_commands(
    tree: app_commands.CommandTree,
    *,
    client: discord.Client,
) -> None:
    """Register roast slash + context-menu commands against the command tree.

    Called from `SableRolesClient.setup_hook` AFTER
    `burn_me.register_commands(tree)` so all commands sync together in the
    same `copy_global_to` + `tree.sync` pass per guild.

    `client` is accepted purely as a signature-locking sentinel — the
    @tree.context_menu / @tree.command decorators only capture `tree`,
    not `client`. The kwarg-only requirement keeps R7+'s callers from
    silently positionally drifting if a future chunk needs the client
    handle here (e.g. for register_listener composition).
    """
    del client  # signature-locking sentinel; not consumed by any decorator

    @tree.command(
        name="set-personalize-mode",
        description="(admins) Toggle /roast personalization for this server",
    )
    @app_commands.describe(mode="on or off")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def set_personalize_mode_cmd(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
    ) -> None:
        await _handle_set_personalize_mode(interaction, mode.value)

    @tree.context_menu(name="Roast this fit")
    async def roast_context_menu(
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        # R7 dispatch: mods get the no-token mod path; peers (anyone with
        # the peer-roast role) get the token-economy peer path. Non-Member
        # / DM-context callers fall through to peer which surfaces the
        # friendly bounce (we don't pre-emptively swallow them here).
        is_mod_caller = (
            interaction.guild_id is not None
            and isinstance(interaction.user, discord.Member)
            and _is_mod(interaction.user, str(interaction.guild_id))
        )
        if is_mod_caller:
            await _handle_mod_roast(interaction, message)
        else:
            await _handle_peer_roast(interaction, message)

    @tree.command(
        name="my-roasts",
        description="Your peer-roast tokens and streak status",
    )
    async def my_roasts_cmd(interaction: discord.Interaction) -> None:
        await _handle_my_roasts(interaction)

    @tree.command(
        name="peer-roast-report",
        description="(mods) Summarize peer-roast activity",
    )
    @app_commands.describe(days="lookback days (default 30, 1-365)")
    async def peer_roast_report_cmd(
        interaction: discord.Interaction,
        days: int = 30,
    ) -> None:
        await _handle_peer_roast_report(interaction, days)
