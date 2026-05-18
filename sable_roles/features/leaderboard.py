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

import asyncio
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
EMPTY_BOARD_TEXT_30D = (
    "no revealed fits in the last 30 days — try `/leaderboard window:all_time`."
)

# VR2-M2: Discord rejects message bodies > 2000 chars with HTTP 400. A
# worst-case 10-row leaderboard (64-char name + 80-char catch + 88-char
# jump link per row) lands around ~2300 chars, occasionally over the
# limit. Budget the body: header + footer reserved, then add rows while
# under MAX_BODY_CHARS, and append a truncation notice if any rows were
# dropped.
MAX_BODY_CHARS = 1900  # safety margin below Discord's 2000 hard cap


# ---------------------------------------------------------------------------
# Pure helpers (testable without a Discord client)
# ---------------------------------------------------------------------------


def _truncate_catch(catch: str | None, max_len: int = CATCH_TRUNCATE_LEN) -> str:
    """Truncate the catch rationale to a fixed display length. Returns
    empty string for None (caller decides whether to render the line).

    M4 (Pass D QA round 1): collapse all whitespace including newlines.
    catch_detected is Sonnet-generated so not directly user-controlled,
    but Sonnet plausibly emits multi-line or em-dashed content that
    would shatter the two-line `header\\n    jump_link` leaderboard
    entry format and offset every subsequent row.
    """
    if not catch:
        return ""
    cleaned = " ".join(catch.split()).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3] + "..."


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
    """Per-row format per design §9.5.

    L7 (Pass D QA round 1): clamp percentile to [1, 100] before display
    to mirror reveal_pipeline._build_reveal_text behavior. Out-of-range
    percentiles shouldn't happen but the reveal text and the leaderboard
    should not disagree on the displayed number for the same fit.
    """
    pct_int = max(1, min(100, int(round(percentile))))
    truncated = _truncate_catch(catch)
    if truncated:
        head = f"{rank:>2}. {display_name} · {pct_int} · caught: {truncated}"
    else:
        head = f"{rank:>2}. {display_name} · {pct_int}"
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
    `rows` is empty. `display_names` is keyed on `user_id` (str).

    L5 (Pass D QA round 1): when rows is empty AND window is 30d, hint
    the caller toward `window:all_time` so they don't assume the whole
    leaderboard is empty when they just don't have recent activity.

    VR2-M2 / VR3-L1: budgets the body under MAX_BODY_CHARS (1900). Two-pass:
    first pass with full budget assumes no truncation; if all rows fit, no
    reserve needed. If a row gets dropped, the 80-char truncation marker is
    appended and counted retroactively — preventing the over-reservation
    that would otherwise truncate a fits-exactly board to N-1 rows.
    Discord rejects messages > 2000 chars with HTTP 400.
    """
    if not rows:
        if window == "30d":
            return EMPTY_BOARD_TEXT_30D
        return EMPTY_BOARD_TEXT

    header_label = "top revealed fits" if board == "top_revealed" else "best per user"
    window_label = "all-time" if window == "all_time" else "last 30 days"
    header = f"**{header_label} — #fitcheck — {window_label}**\n"

    if public:
        footer = "\n\n(/leaderboard window:30d for last 30 days)"
    else:
        footer = (
            "\n\n(ephemeral · /leaderboard window:30d for last 30 days"
            " · /leaderboard public:true to share)"
        )

    # Precompute the truncation marker so we know exactly how much to
    # reserve when it actually fires.
    def _truncation_marker(shown: int, total: int) -> str:
        return (
            f"\n\n(showing {shown} of {total}"
            " — top rows fit Discord's message length)"
        )

    # Render every entry once, then pack against the budget. Two-pass
    # avoids paying the truncation-marker reserve on a board that fits.
    rendered: list[str] = []
    for i, row in enumerate(rows, start=1):
        uid = str(row["user_id"])
        name = display_names.get(uid, f"unknown ({uid[:8]})")
        jump = _build_jump_link(
            str(row["guild_id"]),
            str(row["channel_id"]) if row.get("channel_id") else None,
            str(row["post_id"]),
        )
        rendered.append(
            _format_entry(
                rank=i,
                display_name=name,
                percentile=float(row["percentile"]),
                catch=row.get("catch_detected"),
                jump_link=jump,
            )
        )

    # Pack assuming no truncation first.
    fixed_overhead_no_truncation = len(header) + 1 + len(footer)
    budget_no_truncation = MAX_BODY_CHARS - fixed_overhead_no_truncation

    entries: list[str] = []
    used = 0
    for entry in rendered:
        candidate = used + len(entry) + (1 if entries else 0)
        if candidate > budget_no_truncation:
            break
        entries.append(entry)
        used = candidate

    if len(entries) == len(rendered):
        # Everything fit — no truncation marker needed, return as-is.
        return header + "\n" + "\n".join(entries) + footer

    # Truncation will fire. Re-pack with the marker reserved.
    marker_worst = len(_truncation_marker(len(rendered), len(rendered)))
    budget_with_truncation = budget_no_truncation - marker_worst
    entries = []
    used = 0
    for entry in rendered:
        candidate = used + len(entry) + (1 if entries else 0)
        if candidate > budget_with_truncation:
            break
        entries.append(entry)
        used = candidate

    body = "\n".join(entries)
    marker = _truncation_marker(len(entries), len(rendered))
    return header + "\n" + body + marker + footer


# ---------------------------------------------------------------------------
# Rate-limit + display-name cache helpers
# ---------------------------------------------------------------------------


def _evict_cooldown_if_full() -> None:
    """Bounded dict — drop oldest entry by insertion order when over cap.

    Eviction runs AFTER insert in the caller, so the `>` predicate is
    correct: dict transiently holds CAP+1 inside one call, then drops
    back to CAP after eviction. Using `>=` instead would plateau the
    dict at CAP-1 — every insert past CAP would evict the about-to-be-
    superseded entry. Same pattern as reveal_pipeline._PENDING_REVEALS.

    L2 from Pass D QA round 1: FIFO-not-LRU. A chatty user who keeps
    re-inserting is the OLDEST by insertion order (Python dicts preserve
    insertion order, not access order), making them the wrong eviction
    target under sustained churn near cap. Accepted — same punt as
    reveal_pipeline _pending_reveals (Pass C QA L-NEW-1). At V1 scale
    (single-digit /min invocations across the whole guild) the cap is
    not approached.

    L3 from Pass D QA round 1: bot-user self-throttle is not reachable
    — Discord doesn't deliver INTERACTION_CREATE to bot users invoking
    slash commands, so `interaction.user.id` is always a human.

    VR3-L2: precedent comparison: reveal_pipeline._PENDING_REVEALS uses
    pre-insert eviction with `>=`. Same FIFO-bounded-dict intent, opposite
    phase. Both individually correct (the predicate matches the insertion
    point); the choice here is post-insert + `>` because callers do the
    set unconditionally and the evict helper runs second.
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
    """Same insertion-order FIFO + post-insert eviction pattern as
    _evict_cooldown_if_full. See that docstring for L2/L3 rationale.
    """
    if len(_DISPLAY_NAME_CACHE) > _NAME_CACHE_CAP:
        oldest = next(iter(_DISPLAY_NAME_CACHE))
        del _DISPLAY_NAME_CACHE[oldest]


def _sanitize_display_name(raw: str | None, user_id: int) -> str:
    """Strip control chars + collapse whitespace + bound length so a
    crafted Discord display_name can't inject newlines (which would
    shatter the two-line leaderboard format) or render unbounded text.
    """
    fallback = f"unknown ({str(user_id)[:8]})"
    if not raw:
        return fallback
    # Collapse all whitespace including newlines into single spaces.
    cleaned = " ".join(raw.split()).strip()
    if not cleaned:
        return fallback
    # Discord display names are capped at 32; defend against larger.
    return cleaned[:64]


async def _resolve_display_name(client: discord.Client, user_id: int) -> str:
    """Resolve a Discord user_id to a display_name string.

    Cache policy (H2 from Pass D QA round 1):
      - NotFound (user really gone): cache the fallback for full TTL —
        no point re-querying a dead account every minute.
      - HTTPException / unexpected: do NOT cache. A transient 5xx during
        a 30-second Discord blip must not poison the leaderboard for
        the full 15-minute TTL window.
      - Success: cache the resolved + sanitized name for full TTL.
    """
    now = datetime.now(timezone.utc)
    cached = _DISPLAY_NAME_CACHE.get(user_id)
    if cached is not None:
        name, fetched_at = cached
        if (now - fetched_at).total_seconds() < _NAME_CACHE_TTL_SECONDS:
            return name

    try:
        user = await client.fetch_user(user_id)
        raw = user.display_name or user.name
        name = _sanitize_display_name(raw, user_id)
        # Persist success — full TTL.
        _DISPLAY_NAME_CACHE[user_id] = (name, now)
        _evict_name_cache_if_full()
        return name
    except discord.NotFound as exc:
        # User truly doesn't exist; cache the fallback so we don't burn
        # a Discord call on the next /leaderboard hit.
        logger.info("display_name resolve NotFound for %s: %s", user_id, exc)
        name = f"unknown ({str(user_id)[:8]})"
        _DISPLAY_NAME_CACHE[user_id] = (name, now)
        _evict_name_cache_if_full()
        return name
    except discord.HTTPException as exc:
        # Transient — DO NOT cache. Next /leaderboard call will re-try
        # and pick up the real name as soon as Discord recovers.
        logger.warning("display_name resolve transient HTTPException for %s: %s", user_id, exc)
        return f"unknown ({str(user_id)[:8]})"
    except Exception as exc:  # noqa: BLE001 — last-line defense
        # Unknown failure mode — also treat as transient.
        logger.warning("display_name resolve raised %s for %s", type(exc).__name__, user_id)
        return f"unknown ({str(user_id)[:8]})"


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
        ephemeral = not public

        # VR2-L3: guild-gate FIRST so accidental DM or unconfigured-guild
        # invocations don't burn the 60s rate-limit slot. Rate-limit is
        # for "user is spamming the leaderboard" pressure, not "user
        # mistyped where to invoke".
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

        remaining = _check_and_set_rate_limit(interaction.user.id)
        if remaining is not None:
            await interaction.response.send_message(
                f"rate limited — try again in {remaining}s.",
                ephemeral=True,
            )
            return

        # M1: defer before the DB query + user resolves. A cache-cold
        # board does up to 10 sequential fetch_user calls; at ~250 ms
        # per call that's 2.5 s consumed before the response sends, and
        # Discord's interaction-response window is 3 s. Defer first, then
        # do work, then followup.send. Gives 15 minutes of headroom.
        #
        # VR2-M1: defer ephemeral MUST match the followup ephemeral or
        # Discord renders a stuck "thinking…" state on the wrong audience.
        # Single `ephemeral` local feeds both defer + every followup.
        await interaction.response.defer(ephemeral=ephemeral)

        # VR2-L1: since_iso is second-precision (strftime omits sub-second
        # fields), so the 30-day window is right-half-open at second
        # granularity. A reveal that fired EXACTLY 30d ago + 500ms could
        # be included or excluded depending on the wall-clock second at
        # query time. Impact bounded to at-most-one row on the boundary.
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
            # VR2-M1: error followup matches the deferred ephemeral state.
            # A public defer-then-ephemeral-error leaves the "thinking…"
            # state visible to everyone with no resolution; matching the
            # ephemeral flag avoids that. Trade-off: public requests that
            # fail show the error publicly — honest, the operator opted
            # into public.
            await interaction.followup.send(
                "couldn't load the leaderboard right now — try again shortly.",
                ephemeral=ephemeral,
            )
            return

        # L6: drop rows where channel_id is NULL (LEFT JOIN miss against
        # discord_streak_events). Jump-link would otherwise drop the
        # user at the guild root with no channel context — confusing UX.
        # In practice this shouldn't happen (every scored fit was a
        # counted streak event); the drop is defensive.
        filtered_rows = []
        for row in rows:
            if row.get("channel_id") is None:
                logger.warning(
                    "leaderboard row missing channel_id (orphan score?) post=%s",
                    row.get("post_id"),
                )
                continue
            filtered_rows.append(row)

        # M1: parallelize display-name resolves with asyncio.gather. 10
        # sequential awaits become 1×max-latency instead of 10×avg.
        # _resolve_display_name uses a 15min cache so repeat hits within
        # a leaderboard burst are free.
        unique_uids: list[str] = []
        seen: set[str] = set()
        for row in filtered_rows:
            uid = str(row["user_id"])
            if uid not in seen:
                seen.add(uid)
                unique_uids.append(uid)

        async def _resolve_one(uid: str) -> tuple[str, str]:
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                return uid, f"unknown ({uid[:8]})"
            name = await _resolve_display_name(interaction.client, uid_int)
            return uid, name

        resolved = await asyncio.gather(
            *(_resolve_one(uid) for uid in unique_uids),
            return_exceptions=False,
        )
        display_names: dict[str, str] = dict(resolved)

        text_body = _format_leaderboard(
            filtered_rows,
            display_names,
            board=board_val,
            window=window_val,
            public=public,
        )
        await interaction.followup.send(
            text_body,
            ephemeral=ephemeral,
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
