"""sable-roles entrypoint: gateway client + per-guild slash-command tree.

Subclasses `discord.Client` so `setup_hook` hosts slash-command registration (matches
SableTracking precedent — `on_ready` may fire multiple times on reconnect).

Per plan §4: `Client.close()` is the documented discord.py 2.x shutdown hook; the
override drains pending feature work before super().close() tears down the session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from sqlalchemy import text

from sable_platform.db.connection import get_db

from sable_roles.config import GUILD_TO_ORG, SABLE_ROLES_DISCORD_TOKEN, feature_enabled
from sable_roles.features import (
    airlock,
    burn_me,
    content_deck,
    content_duel,
    delete_monitor,
    fitcheck_streak,
    leaderboard,
    reveal_pipeline,
    roast,
    scoring_pipeline,
    state_pin,
    vibe_observer,
)

logger = logging.getLogger("sable_roles")


def _hours_ago_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class SableRolesClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged — must be enabled in dev portal
        # A0: airlock requires on_member_join + on_member_remove, which
        # need the Members privileged intent. Must also be ON in the
        # Discord developer portal under Bot → Privileged Gateway Intents.
        # Feature-gated: a duel-only client instance (airlock off) must NOT
        # request it — requesting a privileged intent the portal hasn't
        # enabled fails the whole gateway connection (close 4014).
        intents.members = feature_enabled("airlock")
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Register feature handlers and slash commands BEFORE syncing.
        # Order matters: roast + vibe_observer + airlock COMPOSE with
        # whatever event handlers are already bound (wrap-existing-handler
        # pattern), so the @client.event-binding modules must register first.
        # Every block is gated on ENABLED_FEATURES (default "all" — the
        # single-bot SolStitch deployment is byte-identical). A multi-tenant
        # client instance (e.g. the TIG duel-only bot) enables only its
        # groups, so it neither observes events nor pollutes the command
        # picker with another client's features. Skipping groups is
        # compose-safe: each wrapper composes with whatever handlers exist.
        if feature_enabled("fitcheck"):
            fitcheck_streak.register(self)
            fitcheck_streak.register_commands(self.tree)
        if feature_enabled("burn_me"):
            burn_me.register_commands(self.tree)
        if feature_enabled("roast"):
            roast.register(self)  # R7: 🚩 reaction handler (composes)
            roast.register_commands(self.tree, client=self)
        if feature_enabled("vibe_observer"):
            vibe_observer.register(self)  # R10: msg + reaction observation (composes)
            vibe_observer.start_tasks()    # R10: rollup + GC background loops
        if feature_enabled("airlock"):
            airlock.register(self)  # A3+A4: on_member_join/remove/invite_* (composes)
            airlock.register_commands(self.tree, client=self)  # A5+A6: mod commands
        if feature_enabled("fitcheck"):
            # Scored Mode V2 (Pass A/B/C/D — the fitcheck family):
            # delete/edit audit, /scoring, reveal pipeline, /leaderboard.
            # reveal_pipeline composes with all prior reaction / message /
            # delete handlers — MUST register LAST among the wrappers.
            # Scoring default state is 'off' per migration 051.
            delete_monitor.register(self)
            scoring_pipeline.register_commands(self.tree, client=self)
            reveal_pipeline.register(self)
            leaderboard.register_commands(self.tree, client=self)
        if feature_enabled("state_pin"):
            # State-pin surface: slash-command-triggered pinned dashboard
            # in the per-guild #sable-ops channel. Default-invisible when
            # SABLE_ROLES_OPS_CHANNELS_JSON has no entry for a guild.
            state_pin.register(self)
        # Content Deck (Phase 0 spike) — registers /content-deck GUILD-SCOPED to TEST
        # guilds only (SABLE_ROLES_CONTENT_DECK_GUILDS_JSON), refusing any live
        # GUILD_TO_ORG guild. Returns the safe test-guild ids to sync below. Empty by
        # default → no registration (invisible). NEVER touches the global tree, so the
        # copy_global_to loop below can never fan it onto a live client guild.
        content_deck_guilds = (
            content_deck.register_commands(self.tree, client=self)
            if feature_enabled("content_deck") else []
        )
        # Phase-5 community duel (/duel + /tasteboard) — GLOBAL commands fanned onto
        # the live GUILD_TO_ORG guilds by the copy_global_to loop below (the OPPOSITE
        # registration posture from the Phase-0 content_deck spike above, on purpose).
        # Authorization is at RUNTIME: org mapping + the duel-starter allowlist + the
        # FAIL-CLOSED per-org `pairwise_disclosure_signed` gate — an org with no signed
        # disclosure gets a polite refusal, never a duel.
        if feature_enabled("duel"):
            content_duel.register_commands(self.tree)
            # Persistent duel view + durable close sweep (mig 084): a 24h duel survives a
            # restart — the persistent view re-binds button clicks by message_id and the
            # sweep reveals past-deadline duels (incl. a startup pass for ones that expired
            # while the bot was down). Drained in close().
            content_duel.register(self)
        # Per-guild instant sync via copy_global_to (SableTracking pattern). Each guild's
        # sync is FAILURE-ISOLATED (the long-planned Item-2 hardening, SableTracking
        # bot.py precedent): a Forbidden/HTTP error on ONE guild — e.g. a guild staged
        # in GUILD_TO_ORG before the bot is invited (the TIG onboarding order) — logs
        # loudly and skips, instead of crashing the whole bot out of every live guild.
        for guild_id_str in GUILD_TO_ORG:
            guild = discord.Object(id=int(guild_id_str))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.HTTPException as exc:
                logger.error(
                    "command sync FAILED for guild %s (bot not invited yet, or missing "
                    "applications.commands scope?) — skipping; other guilds unaffected: %s",
                    guild_id_str, exc,
                )
        # Sync the guild-scoped /content-deck onto its TEST guilds only (these are NOT in
        # GUILD_TO_ORG by construction — _safe_test_guilds refuses overlap — so the loop
        # above did not touch them; sync pushes ONLY the guild-scoped command, no globals).
        for gid in content_deck_guilds:
            try:
                await self.tree.sync(guild=discord.Object(id=int(gid)))
            except discord.HTTPException as exc:
                logger.error("content-deck test-guild sync FAILED for %s — skipping: %s",
                             gid, exc)

    async def on_ready(self) -> None:
        logger.info(
            "sable-roles connected as %s · fitcheck streak active", self.user
        )
        # Startup activity check — CompatResult has no .scalar(), use fetchone().
        with get_db() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM discord_streak_events"
                    " WHERE created_at > :since"
                ),
                {"since": _hours_ago_iso(24)},
            ).fetchone()
            recent = row[0] if row else 0
            if recent == 0:
                logger.warning("no events in last 24h — was the bot offline?")
        # A3: airlock bootstrap (invite snapshot + team-inviter env seed).
        # Runs on every on_ready (reconnect-safe) — guards against the
        # restart-blackout case where the first joiner after boot would
        # otherwise be unattributable. Feature-gated: a duel-only instance
        # has no airlock handlers and no invite-read permissions to use.
        if feature_enabled("airlock"):
            try:
                await airlock.bootstrap(self)
            except Exception as exc:  # noqa: BLE001
                logger.warning("airlock bootstrap failed: %s", exc)

    async def close(self) -> None:
        # Graceful drain. Client.close() is discord.py 2.x's documented shutdown hook.
        vibe_observer.stop_tasks()
        if feature_enabled("duel"):
            content_duel.stop_tasks()
        await fitcheck_streak.close()
        await reveal_pipeline.close()
        await state_pin.close()
        await super().close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not SABLE_ROLES_DISCORD_TOKEN:
        raise SystemExit(
            "SABLE_ROLES_DISCORD_TOKEN is empty — populate .env before running."
        )
    client = SableRolesClient()
    client.run(SABLE_ROLES_DISCORD_TOKEN)
