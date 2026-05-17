"""Fit-check streak feature: image-only enforcement, streak tracking, /streak slash command.

C3 lands `on_message` + image detection + DM rotation + 5-minute per-user DM cooldown.
C4 adds the bot 🔥 reaction + auto-thread on counted fits (each wrapped in try/except so a
single Discord-side failure doesn't crash the handler or revoke streak credit).
C5 lands reaction handling: `on_raw_reaction_add` / `on_raw_reaction_remove` coalesce
per-`post_id` recomputes through a 2-second debounce, then refetch the message,
filter bot + self reactions, and write the new score via the optimistic-locked
`update_reaction_score` helper. `close()` drains in-flight debounce tasks during
shutdown so we never leak a pending task into super().close() teardown.

C6 lands the `/streak` ephemeral slash command and the `_format_streak` helper —
both branch on `posted_today` and `most_reacted_post_id` per plan §4.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import random
from datetime import datetime, timezone

import discord
from discord import app_commands

from sable_platform.db import discord_guild_config, discord_streaks
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db

from sable_roles.config import (
    CONFIRMATION_EMOJI,
    DEBOUNCE_SECONDS,
    DM_BANK,
    DM_COOLDOWN_SECONDS,
    FITCHECK_CHANNELS,
    GUILD_TO_ORG,
    IMAGE_EXT_ALLOWLIST,
    MOD_ROLES,
)

logger = logging.getLogger("sable_roles.fitcheck_streak")

_client: discord.Client | None = None
_dm_cooldown: dict[int, datetime] = {}
_pending_recomputes: dict[int, asyncio.Task] = {}

# Reverse-lookup tables built at module load from FITCHECK_CHANNELS (plan §4).
# Tests that monkeypatch FITCHECK_CHANNELS must also patch these two structures
# so reverse lookups stay consistent — see the `fitcheck_module` conftest fixture.
_FITCHECK_CHANNEL_IDS: set[int] = {
    int(cfg["channel_id"]) for cfg in FITCHECK_CHANNELS.values()
}
_CHANNEL_TO_GUILD: dict[int, str] = {
    int(cfg["channel_id"]): guild_id for guild_id, cfg in FITCHECK_CHANNELS.items()
}


def _is_fitcheck_channel(channel_id: int) -> bool:
    return channel_id in _FITCHECK_CHANNEL_IDS


def _guild_for(channel_id: int) -> str | None:
    return _CHANNEL_TO_GUILD.get(channel_id)


def _is_mod(member: discord.Member, guild_id: str) -> bool:
    """True if the member holds any role in MOD_ROLES[guild_id].

    Discord's Administrator permission does NOT auto-grant mod status — the
    role must be explicitly listed in `SABLE_ROLES_MOD_ROLES_JSON` for the
    member to pass this check. Decoupled from Discord role hierarchy on
    purpose so Brian-as-@Atelier-admin isn't an automatic mod for V2 ops.
    """
    mod_role_ids = {str(rid) for rid in MOD_ROLES.get(guild_id, [])}
    if not mod_role_ids:
        return False
    member_role_ids = {str(role.id) for role in member.roles}
    return bool(member_role_ids & mod_role_ids)


def is_image(att: discord.Attachment) -> bool:
    """True if attachment is a renderable image per allowlist.

    Content-type takes precedence (`image/*` minus `image/svg+xml`); falls back to
    extension allowlist when content_type is missing or generic (e.g. `application/
    octet-stream`). SVG excluded — Discord doesn't render + sandbox risk. Filename
    extension is spoofable; accepted for V1.
    """
    ctype = (att.content_type or "").lower()
    if ctype.startswith("image/") and ctype != "image/svg+xml":
        return True
    ext = pathlib.Path(att.filename or "").suffix.lower()
    return ext in IMAGE_EXT_ALLOWLIST


async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if message.guild is None:
        return

    guild_id = str(message.guild.id)
    org_id = GUILD_TO_ORG.get(guild_id)
    if org_id is None:
        return

    fitcheck_cfg = FITCHECK_CHANNELS.get(guild_id)
    if fitcheck_cfg is None:
        return
    fitcheck_channel_id = int(fitcheck_cfg["channel_id"])

    channel = message.channel
    if isinstance(channel, discord.Thread) and channel.parent_id == fitcheck_channel_id:
        return
    if channel.id != fitcheck_channel_id:
        return

    # Read the per-guild relax-mode toggle once. When on: image branch still
    # credits the streak + reacts 🔥 but skips auto-threading; text branch
    # skips delete+DM entirely. Off (default) preserves V1 enforcement.
    with get_db() as conn:
        guild_cfg = discord_guild_config.get_config(conn, guild_id)
    relax_mode_on = bool(guild_cfg["relax_mode_on"])

    has_image = any(is_image(att) for att in message.attachments)

    if has_image:
        posted_at_utc = message.created_at.astimezone(timezone.utc)
        counted_for_day = posted_at_utc.strftime("%Y-%m-%d")
        posted_at = posted_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        attachment_count = len(message.attachments)
        image_attachment_count = sum(1 for att in message.attachments if is_image(att))
        with get_db() as conn:
            discord_streaks.upsert_streak_event(
                conn,
                org_id=org_id,
                guild_id=guild_id,
                channel_id=str(channel.id),
                post_id=str(message.id),
                user_id=str(message.author.id),
                posted_at=posted_at,
                counted_for_day=counted_for_day,
                attachment_count=attachment_count,
                image_attachment_count=image_attachment_count,
            )

        try:
            await message.add_reaction(CONFIRMATION_EMOJI)
        except discord.HTTPException as exc:
            logger.warning(
                "add_reaction failed for post %s", message.id, exc_info=exc
            )

        if not relax_mode_on:
            thread_name = f"{message.author.display_name} · {counted_for_day}"[:100]
            try:
                await message.create_thread(name=thread_name)
            except discord.HTTPException as exc:
                logger.warning(
                    "create_thread failed for post %s", message.id, exc_info=exc
                )
                bot_user = _client.user if _client is not None else None
                actor = (
                    f"discord:bot:{bot_user.id}"
                    if bot_user is not None
                    else "discord:bot:unknown"
                )
                with get_db() as conn:
                    log_audit(
                        conn,
                        actor=actor,
                        action="fitcheck_thread_create_failed",
                        org_id=org_id,
                        entity_id=None,
                        detail={
                            "guild_id": guild_id,
                            "channel_id": str(channel.id),
                            "post_id": str(message.id),
                            "error": str(exc),
                        },
                        source="sable-roles",
                    )

        # Burn-me hook: orthogonal to relax-mode — fires in both branches.
        # asyncio.create_task so the Anthropic vision call doesn't block
        # on_message return. Inline import avoids the circular dep (burn_me
        # imports _is_mod + is_image from this module).
        from sable_roles.features import burn_me as bm
        asyncio.create_task(
            bm.maybe_roast(message=message, org_id=org_id, guild_id=guild_id)
        )

        # R8: streak-restoration hook. Fires per-image-post (cheap — only
        # one SP read + one SP write when current_streak==7 AND no row for
        # this (guild,user,year_month,'streak_restoration')). Inline import
        # mirrors the burn_me dispatch to keep import order resilient.
        from sable_roles.features import roast as _roast
        asyncio.create_task(
            _roast.maybe_grant_restoration_token(
                client=_client,
                user_id=str(message.author.id),
                guild_id=guild_id,
                org_id=org_id,
            )
        )

        # Scored Mode V2 Pass A: pHash + collision detection. Fires regardless
        # of scoring state — pHash + repost/theft signals are valuable even
        # when scoring is Off. Inline import to keep ordering resilient
        # against future feature reshuffling.
        from sable_roles.features import image_hashing as _ih
        asyncio.create_task(
            _ih.maybe_record_phash(
                message=message,
                org_id=org_id,
                guild_id=guild_id,
                client=_client,
            )
        )

        # Scored Mode V2 Pass B: vision scoring. NO-OP when per-guild
        # scoring state is 'off' (the default). Read happens first inside
        # maybe_score_fit — no API call when off.
        from sable_roles.features import scoring_pipeline as _sp
        asyncio.create_task(
            _sp.maybe_score_fit(
                message=message,
                org_id=org_id,
                guild_id=guild_id,
                client=_client,
            )
        )
        return

    # text branch
    if relax_mode_on:
        return  # text allowed; no delete + no DM

    user_id = message.author.id
    now = datetime.now(timezone.utc)
    last_dm = _dm_cooldown.get(user_id)
    dm_suppressed = (
        last_dm is not None
        and (now - last_dm).total_seconds() < DM_COOLDOWN_SECONDS
    )

    try:
        await message.delete()
    except discord.HTTPException as exc:
        logger.warning("delete failed for post %s", message.id, exc_info=exc)

    dm_success = False
    if not dm_suppressed:
        try:
            await message.author.send(random.choice(DM_BANK))
            dm_success = True
            _dm_cooldown[user_id] = now
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info("dm send failed for user %s", user_id, exc_info=exc)

    bot_user = _client.user if _client is not None else None
    actor = f"discord:bot:{bot_user.id}" if bot_user is not None else "discord:bot:unknown"
    with get_db() as conn:
        log_audit(
            conn,
            actor=actor,
            action="fitcheck_text_message_deleted",
            org_id=org_id,
            entity_id=None,
            detail={
                "guild_id": guild_id,
                "channel_id": str(channel.id),
                "post_id": str(message.id),
                "user_id": str(message.author.id),
                "dm_success": dm_success,
                "dm_suppressed_for_cooldown": dm_suppressed,
            },
            source="sable-roles",
        )


async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    _schedule_recompute(payload.channel_id, payload.message_id)


async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    _schedule_recompute(payload.channel_id, payload.message_id)


def _schedule_recompute(channel_id: int, post_id: int) -> None:
    """Cancel any existing debounce task for this post and start a fresh one.

    Replacement is single-tick atomic: cancellation + dict swap happen before the
    event loop returns control, so no second task can race in between.
    """
    existing = _pending_recomputes.get(post_id)
    if existing is not None:
        existing.cancel()
    task = asyncio.create_task(_recompute_after_delay(channel_id, post_id))
    _pending_recomputes[post_id] = task


async def _recompute_after_delay(channel_id: int, post_id: int) -> None:
    """Sleep for DEBOUNCE_SECONDS then refetch reactions + update score.

    Self-identity check in `finally` prevents a cancelled task from evicting its
    replacement from `_pending_recomputes`. `CancelledError` is re-raised so the
    cancelling caller knows we unwound cleanly.
    """
    self_task = asyncio.current_task()
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        if not _is_fitcheck_channel(channel_id):
            return
        guild_id = _guild_for(channel_id)
        if guild_id is None:
            return
        if _client is None:
            logger.warning(
                "reaction recompute skipped: _client unset for post_id=%s", post_id
            )
            return
        with get_db() as conn:
            event = discord_streaks.get_event(
                conn, guild_id=guild_id, post_id=str(post_id)
            )
            if event is None:
                return  # no backfill in V1 — skip uncounted posts
            channel = _client.get_channel(channel_id) or await _client.fetch_channel(
                channel_id
            )
            message = await channel.fetch_message(post_id)
            author_id = int(event["user_id"])
            bot_ids = {_client.user.id} if _client.user is not None else set()
            score = 0
            for reaction in message.reactions:
                async for user in reaction.users():
                    if user.id in bot_ids or user.id == author_id:
                        continue
                    score += 1
            ok = discord_streaks.update_reaction_score(
                conn,
                event["guild_id"],
                event["post_id"],
                score,
                event["updated_at"],
            )
            if not ok:
                logger.info(
                    "reaction recompute lost race for post_id=%s", post_id
                )
    except asyncio.CancelledError:
        raise  # re-raise so the cancelling caller knows we unwound cleanly
    except Exception as exc:  # noqa: BLE001 — recompute must never crash the loop
        logger.warning(
            "reaction recompute failed for post_id=%s", post_id, exc_info=exc
        )
    finally:
        # Only pop if we're still the registered task. If we were cancelled and
        # replaced, the new task owns this slot and we must not touch it.
        if _pending_recomputes.get(post_id) is self_task:
            _pending_recomputes.pop(post_id, None)


def register(client: discord.Client) -> None:
    """Wire on_message + raw reaction handlers to the client."""
    global _client
    _client = client
    client.event(on_message)
    client.event(on_raw_reaction_add)
    client.event(on_raw_reaction_remove)


def _format_streak(state: dict, guild_id: int | str | None) -> str:
    """Render `/streak` body. Branches on `posted_today` and `most_reacted_post_id`.

    `guild_id` is the interaction guild fallback; the state's stored
    `most_reacted_guild_id` wins when set so the jump-link points at the post's
    actual server even if `/streak` is somehow run cross-guild.
    """
    current = state.get("current_streak", 0)
    longest = state.get("longest_streak", 0)
    total = state.get("total_fits", 0)

    if state.get("posted_today"):
        today_line = f"posted · {state.get('today_reaction_count', 0)} reaction(s)"
    else:
        today_line = "no fit yet today"

    best_post_id = state.get("most_reacted_post_id")
    if best_post_id:
        best_count = state.get("most_reacted_reaction_count", 0)
        best_channel = state.get("most_reacted_channel_id")
        best_guild = state.get("most_reacted_guild_id") or guild_id
        best_line = (
            f"<https://discord.com/channels/{best_guild}/{best_channel}/{best_post_id}>"
            f" · {best_count} reaction(s)"
        )
    else:
        best_line = "none yet"

    return (
        "your fit-check streak\n\n"
        f"current: {current} day(s)\n"
        f"longest: {longest} day(s)\n"
        f"total fits: {total}\n\n"
        f"today: {today_line}\n"
        f"best fit ever: {best_line}"
    )


def register_commands(tree: app_commands.CommandTree) -> None:
    """Register the ephemeral `/streak` slash command against the command tree.

    Called from `SableRolesClient.setup_hook` BEFORE per-guild `copy_global_to`
    + `sync` so the global definition is in place when sync ships it to each
    guild (SableTracking pattern — see main.py).
    """

    @tree.command(name="streak", description="Your private fit-check streak")
    async def streak(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        org_id = GUILD_TO_ORG.get(str(interaction.guild_id))
        if not org_id:
            await interaction.followup.send(
                "not configured for this server.", ephemeral=True
            )
            return
        with get_db() as conn:
            state = discord_streaks.compute_streak_state(
                conn, org_id, str(interaction.user.id)
            )
        await interaction.followup.send(
            _format_streak(state, interaction.guild_id), ephemeral=True
        )

    @tree.command(
        name="relax-mode",
        description="(mods) Toggle fit-check enforcement relaxation on/off",
    )
    @app_commands.describe(mode="on or off")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def relax_mode(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        org_id = GUILD_TO_ORG.get(guild_id) if guild_id else None
        if guild_id is None or org_id is None:
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
        on = mode.value == "on"
        with get_db() as conn:
            discord_guild_config.set_relax_mode(
                conn, guild_id, on=on, updated_by=str(interaction.user.id)
            )
            log_audit(
                conn,
                actor=f"discord:user:{interaction.user.id}",
                action="fitcheck_relax_mode_toggled",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "on": on,
                    "by_user_id": str(interaction.user.id),
                },
                source="sable-roles",
            )
        body = (
            "relax-mode **on** — text allowed, no auto-threading. enforcement paused."
            if on
            else "relax-mode **off** — normal enforcement restored."
        )
        await interaction.followup.send(body, ephemeral=True)


async def close() -> None:
    """Graceful shutdown drain — cancel + await every in-flight debounce task.

    Called from SableRolesClient.close() before super().close() tears down the
    websocket. `return_exceptions=True` swallows the re-raised CancelledErrors so
    we never propagate them into super().close().
    """
    tasks = list(_pending_recomputes.values())
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending_recomputes.clear()
