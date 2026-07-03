"""Item-2 hardening: a failed per-guild command sync must never crash the bot.

The load-bearing scenario is the TIG onboarding order — a guild staged in
GUILD_TO_ORG before the bot is invited raises Forbidden on `tree.sync`; that guild
must be logged + skipped while every OTHER guild still syncs and setup_hook
completes (one bad guild previously took the whole bot out of every live guild).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord

from sable_roles.main import SableRolesClient


async def test_one_forbidden_guild_does_not_crash_setup_hook(monkeypatch, caplog):
    monkeypatch.setattr(
        "sable_roles.main.GUILD_TO_ORG", {"100": "solstitch", "200": "tig"}
    )

    client = SableRolesClient.__new__(SableRolesClient)  # skip gateway __init__
    client.tree = MagicMock()
    client.tree.copy_global_to = MagicMock()

    synced: list[int] = []

    async def _sync(*, guild):
        if guild.id == 200:  # the not-yet-invited guild
            raise discord.Forbidden(MagicMock(status=403), "Missing Access")
        synced.append(guild.id)

    client.tree.sync = AsyncMock(side_effect=_sync)

    # neutralize every feature registration — this test is ONLY about the sync loop
    feature_mods = [
        "fitcheck_streak", "burn_me", "roast", "vibe_observer", "airlock",
        "delete_monitor", "scoring_pipeline", "reveal_pipeline", "leaderboard",
        "state_pin", "content_duel",
    ]
    patches = []
    for name in feature_mods:
        p = patch(f"sable_roles.main.{name}", MagicMock(
            register=MagicMock(), register_commands=MagicMock(return_value=None),
            start_tasks=MagicMock(),
        ))
        patches.append(p)
        p.start()
    cd_patch = patch("sable_roles.main.content_deck", MagicMock(
        register_commands=MagicMock(return_value=[]),
    ))
    patches.append(cd_patch)
    cd_patch.start()
    try:
        await client.setup_hook()  # must NOT raise
    finally:
        for p in patches:
            p.stop()

    assert synced == [100]  # the healthy guild still synced
    assert any("command sync FAILED for guild 200" in r.message for r in caplog.records)
