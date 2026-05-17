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

from sable_roles.config import GUILD_TO_ORG, SABLE_ROLES_DISCORD_TOKEN
from sable_roles.features import (
    airlock,
    burn_me,
    delete_monitor,
    fitcheck_streak,
    roast,
    scoring_pipeline,
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
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Register feature handlers and slash commands BEFORE syncing.
        # Order matters: roast + vibe_observer + airlock COMPOSE with
        # whatever event handlers are already bound (wrap-existing-handler
        # pattern), so the @client.event-binding modules must register first.
        fitcheck_streak.register(self)
        fitcheck_streak.register_commands(self.tree)
        burn_me.register_commands(self.tree)
        roast.register(self)  # R7: 🚩 reaction handler (composes)
        roast.register_commands(self.tree, client=self)
        vibe_observer.register(self)  # R10: msg + reaction observation (composes)
        vibe_observer.start_tasks()    # R10: rollup + GC background loops
        airlock.register(self)  # A3+A4: on_member_join/remove/invite_* (composes)
        airlock.register_commands(self.tree, client=self)  # A5+A6: mod commands
        # Scored Mode V2 Pass A: on_raw_message_delete + on_raw_message_edit
        # (composes — discord.py dispatches to all registered handlers).
        delete_monitor.register(self)
        # Scored Mode V2 Pass B: /scoring slash command. Default state is
        # `off` per migration 051 default — no scoring fires until a mod
        # explicitly runs `/scoring action:set state:silent`.
        scoring_pipeline.register_commands(self.tree, client=self)
        # Per-guild instant sync via copy_global_to (SableTracking pattern).
        for guild_id_str in GUILD_TO_ORG:
            guild = discord.Object(id=int(guild_id_str))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

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
        # otherwise be unattributable.
        try:
            await airlock.bootstrap(self)
        except Exception as exc:  # noqa: BLE001
            logger.warning("airlock bootstrap failed: %s", exc)

    async def close(self) -> None:
        # Graceful drain. Client.close() is discord.py 2.x's documented shutdown hook.
        vibe_observer.stop_tasks()
        await fitcheck_streak.close()
        await super().close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not SABLE_ROLES_DISCORD_TOKEN:
        raise SystemExit(
            "SABLE_ROLES_DISCORD_TOKEN is empty — populate .env before running."
        )
    client = SableRolesClient()
    client.run(SABLE_ROLES_DISCORD_TOKEN)
