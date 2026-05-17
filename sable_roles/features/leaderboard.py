"""Pass D: /leaderboard slash command — two boards, ephemeral default,
window:30d toggle, per-user 1/min rate limit.

Design refs:
  - ~/Projects/SolStitch/internal/fitcheck_scored_mode_plan.md §9
  - SP discord_fitcheck_scores.list_top_revealed_fits / list_best_per_user_revealed

Surface:
  /leaderboard [board:top_revealed|best_per_user]
               [window:all_time|30d]
               [public:true|false]

Defaults (design §9.3): board=top_revealed, window=all_time, public=false.
Display is ephemeral unless `public:true`. Rate-limited to 1 invocation
per user per minute (per design §9.3); rate-limited callers receive an
ephemeral "try again in Ns" message rather than silent drop.

Pre-Phase-3 (community gates per design §10.6) this command ships
INVISIBLE in effect — guilds with zero revealed fits get the empty-board
message regardless of who queries. The first reveal seeds the board;
gating is operator/community decision, not code.

Per-row format (§9.5):
  N. {display_name} · {percentile} · caught: {truncated catch_rationale}
     {jump_link to original fit}

The reveal text in #fitcheck is the ranking surface; the leaderboard is
a recall mechanism, not the announcement channel.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

from sable_platform.db import discord_fitcheck_scores
from sable_platform.db.connection import get_db

from sable_roles.config import GUILD_TO_ORG

logger = logging.getLogger("sable_roles.leaderboard")


# Per-user 1/min rate limit (design §9.3). In-memory bounded dict — mirrors
# the DM_COOLDOWN pattern in fitcheck_streak (single-bot-process assumption).
_INVOKE_COOLDOWN: dict[int, datetime] = {}
_COOLDOWN_SECONDS = 60
_COOLDOWN_CAP = 1024

# Display-name cache: Discord display_name lookups are HTTP calls, so cache
# them. TTL 15min lets you see same-day handle changes within reason.
_DISPLAY_NAME_CACHE: dict[int, tuple[str, datetime]] = {}
_NAME_CACHE_TTL_SECONDS = 900
_NAME_CACHE_CAP = 256

CATCH_TRUNCATE_LEN = 80
EMPTY_BOARD_TEXT = "no revealed fits yet — scored mode is still finding its footing."


# ---------------------------------------------------------------------------
# Pure helpers (testable without a Discord client)
# ---------------------------------------------------------------------------


def _truncate_catch(catch: str | None, max_len: int = CATCH_TRUNCATE_LEN) -> str:
    """Truncate the catch rationale to a fixed display length. Returns
    empty string for None (caller decides whether to render the line).
    """
    if not catch:
        return ""
    if len(catch) <= max_len:
        return catch
    return catch[: max_len - 3] + "..."


def _build_jump_link(guild_id: str, channel_id: str | None, post_id: str) -> str:
    """Construct a Discord deep-link to the original fit. `channel_id`
    can be None when the discord_streak_events row is missing (shouldn't
    happen — every scored fit had a streak event — but defensive).
    """
    if not channel_id:
        # Fallback that lands somewhere reasonable in the guild even if
        # the channel resolution failed. User-visible but rare.
        return f"https://discord.com/channels/{guild_id}"
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{post_id}"


def _format_entry(
    rank: int,
    display_name: str,
    percentile: float,
    catch: str | None,
    jump_link: str,
) -> str:
    """Per-row format per design §9.5."""
    truncated = _truncate_catch(catch)
    if truncated:
        head = f"{rank:>2}. {display_name} · {int(round(percentile))} · caught: {truncated}"
    else:
        head = f"{rank:>2}. {display_name} · {int(round(percentile))}"
    return f"{head}\n    {jump_link}"


def _format_leaderboard(
    rows: list[dict],
    display_names: dict[str, str],
    *,
    board: str,
    window: str,
    public: bool,
) -> str:
    """Build the full message body. Returns the empty-board text when
    `rows` is empty. `display_names` is keyed on `user_id` (str)."""
    if not rows:
        return EMPTY_BOARD_TEXT

    header_label = "top revealed fits" if board == "top_revealed" else "best per user"
    window_label = "all-time" if window == "all_time" else "last 30 days"
    header = f"**{header_label} — #fitcheck — {window_label}**\n"

    entries = []
    for i, row in enumerate(rows, start=1):
        uid = str(row["user_id"])
        name = display_names.get(uid, f"unknown ({uid[:8]})")
        jump = _build_jump_link(
            str(row["guild_id"]),
            str(row["channel_id"]) if row.get("channel_id") else None,
            str(row["post_id"]),
        )
        entries.append(
            _format_entry(
                rank=i,
                display_name=name,
                percentile=float(row["percentile"]),
                catch=row.get("catch_detected"),
                jump_link=jump,
            )
        )

    body = "\n".join(entries)

    if public:
        footer = "\n\n(/leaderboard window:30d for last 30 days)"
    else:
        footer = (
            "\n\n(ephemeral · /leaderboard window:30d for last 30 days"
            " · /leaderboard public:true to share)"
        )
    return header + "\n" + body + footer


# ---------------------------------------------------------------------------
# Rate-limit + display-name cache helpers
# ---------------------------------------------------------------------------


def _evict_cooldown_if_full() -> None:
    """Bounded dict — drop oldest entry by insertion order when at cap.
    Same pattern as reveal_pipeline._PENDING_REVEALS cap eviction.
    """
    if len(_INVOKE_COOLDOWN) > _COOLDOWN_CAP:
        oldest = next(iter(_INVOKE_COOLDOWN))
        del _INVOKE_COOLDOWN[oldest]


def _check_and_set_rate_limit(user_id: int) -> int | None:
    """Returns None if the call is allowed (and records the invocation),
    or the integer seconds remaining if rate-limited.
    """
    now = datetime.now(timezone.utc)
    last = _INVOKE_COOLDOWN.get(user_id)
    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < _COOLDOWN_SECONDS:
            return max(1, int(_COOLDOWN_SECONDS - elapsed))
    _INVOKE_COOLDOWN[user_id] = now
    _evict_cooldown_if_full()
    return None


def _evict_name_cache_if_full() -> None:
    if len(_DISPLAY_NAME_CACHE) > _NAME_CACHE_CAP:
        oldest = next(iter(_DISPLAY_NAME_CACHE))
        del _DISPLAY_NAME_CACHE[oldest]


async def _resolve_display_name(client: discord.Client, user_id: int) -> str:
    """Resolve a Discord user_id to a display_name string. 15min TTL
    cache. On Discord API failure (NotFound / HTTPException / network),
    returns a stable fallback so a transient failure doesn't poison the
    leaderboard rendering.
    """
    now = datetime.now(timezone.utc)
    cached = _DISPLAY_NAME_CACHE.get(user_id)
    if cached is not None:
        name, fetched_at = cached
        if (now - fetched_at).total_seconds() < _NAME_CACHE_TTL_SECONDS:
            return name

    try:
        user = await client.fetch_user(user_id)
        # display_name is the global Discord display name (or username
        # fallback). guild-specific nicks live on Member; for cross-
        # leaderboard consistency the global name is the right call.
        name = user.display_name or user.name or f"unknown ({str(user_id)[:8]})"
    except (discord.NotFound, discord.HTTPException) as exc:
        logger.info("display_name resolve failed for %s: %s", user_id, exc)
        name = f"unknown ({str(user_id)[:8]})"
    except Exception as exc:  # noqa: BLE001 — last-line defense
        logger.warning("display_name resolve raised %s for %s", type(exc).__name__, user_id)
        name = f"unknown ({str(user_id)[:8]})"

    _DISPLAY_NAME_CACHE[user_id] = (name, now)
    _evict_name_cache_if_full()
    return name


# ---------------------------------------------------------------------------
# /leaderboard slash command
# ---------------------------------------------------------------------------


def register_commands(
    tree: app_commands.CommandTree,
    *,
    client: discord.Client | None = None,
) -> None:
    """Register /leaderboard against the command tree.

    Public command (NOT mod-gated). Per-guild scope via the same
    setup_hook copy_global_to + sync pattern as /streak and /scoring.
    Default response is ephemeral; `public:true` opts into in-channel
    broadcast.
    """

    @tree.command(
        name="leaderboard",
        description="Show the #fitcheck top-fits leaderboard.",
    )
    @app_commands.guild_only()
    @app_commands.choices(
        board=[
            app_commands.Choice(name="top_revealed", value="top_revealed"),
            app_commands.Choice(name="best_per_user", value="best_per_user"),
        ],
        window=[
            app_commands.Choice(name="all_time", value="all_time"),
            app_commands.Choice(name="30d", value="30d"),
        ],
    )
    @app_commands.describe(
        board="Which leaderboard view (default: top_revealed)",
        window="Time window (default: all_time)",
        public="Show in-channel instead of ephemeral (default: false)",
    )
    async def leaderboard(
        interaction: discord.Interaction,
        board: app_commands.Choice[str] | None = None,
        window: app_commands.Choice[str] | None = None,
        public: bool = False,
    ) -> None:
        board_val = board.value if board is not None else "top_revealed"
        window_val = window.value if window is not None else "all_time"

        # Rate-limit gate first — even cheap queries should respect the
        # 1/min user cooldown so noisy users don't degrade everyone.
        remaining = _check_and_set_rate_limit(interaction.user.id)
        if remaining is not None:
            await interaction.response.send_message(
                f"rate limited — try again in {remaining}s.",
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        if guild_id is None:
            await interaction.response.send_message(
                "must be invoked in a guild.", ephemeral=True
            )
            return
        org_id = GUILD_TO_ORG.get(guild_id)
        if org_id is None:
            await interaction.response.send_message(
                "this guild isn't configured for scored mode.",
                ephemeral=True,
            )
            return

        since_iso: str | None = None
        if window_val == "30d":
            since = datetime.now(timezone.utc) - timedelta(days=30)
            since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            with get_db() as conn:
                if board_val == "top_revealed":
                    rows = discord_fitcheck_scores.list_top_revealed_fits(
                        conn, org_id, since_iso=since_iso, limit=10
                    )
                else:
                    rows = discord_fitcheck_scores.list_best_per_user_revealed(
                        conn, org_id, since_iso=since_iso, limit=10
                    )
        except Exception as exc:  # noqa: BLE001 — leaderboard read must not crash
            logger.warning("leaderboard fetch failed", exc_info=exc)
            await interaction.response.send_message(
                "couldn't load the leaderboard right now — try again shortly.",
                ephemeral=True,
            )
            return

        # Resolve display names (with cache). Sequential fetches are
        # acceptable at limit=10 + 15min cache.
        display_names: dict[str, str] = {}
        for row in rows:
            uid = str(row["user_id"])
            if uid in display_names:
                continue
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                display_names[uid] = f"unknown ({uid[:8]})"
                continue
            display_names[uid] = await _resolve_display_name(
                interaction.client, uid_int
            )

        text_body = _format_leaderboard(
            rows,
            display_names,
            board=board_val,
            window=window_val,
            public=public,
        )
        await interaction.response.send_message(
            text_body,
            ephemeral=(not public),
            allowed_mentions=discord.AllowedMentions.none(),
        )


# Exported for tests + (future) ops surface.
__all__ = [
    "register_commands",
    "_truncate_catch",
    "_build_jump_link",
    "_format_entry",
    "_format_leaderboard",
    "_check_and_set_rate_limit",
    "_resolve_display_name",
    "_INVOKE_COOLDOWN",
    "_DISPLAY_NAME_CACHE",
    "_COOLDOWN_SECONDS",
    "_COOLDOWN_CAP",
    "EMPTY_BOARD_TEXT",
    "CATCH_TRUNCATE_LEN",
]
