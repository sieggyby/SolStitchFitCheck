"""Pass A: delete + text-edit monitoring with severity classification.

Listens on `on_raw_message_delete` and `on_raw_message_edit` for posts that
belonged to a configured #fitcheck channel. Severity classification per
~/Projects/SolStitch/internal/fitcheck_scored_mode_plan.md sec 7.2:

  LOW       deleted within 10 min, no reactions yet
  LOW       deleted within 1 hr, <3 reactions
  MEDIUM    deleted while at >=5 reactions OR >=30 thread messages
  CRITICAL  deleted while within 2 of reveal threshold
            (>=8 reactions on one emoji OR >=80 thread messages)

Raw events fire even for uncached messages — critical for post-restart
correctness when the message cache is empty.

`fitcheck_text_edit` (LOW) is the text-edit signal — fired when a
#fitcheck post is edited. Detail records old / new content lengths so
mods can spot suspicious edits without storing message text directly.

Audit log is the ENTIRE surface — no DM, no public callout, no auto-action.
False-positive cost is intolerable per design sec 7 ("log liberally, act
conservatively"). Mods SQL the audit log to investigate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord

from sable_platform.db import discord_fitcheck_scores, discord_scoring_config
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db
from sqlalchemy import text

from sable_roles.config import GUILD_TO_ORG, SCORED_MODE_ENABLED

logger = logging.getLogger("sable_roles.delete_monitor")

# Severity thresholds per design sec 7.2. Kept as module constants so tests
# can patch them and so a future per-guild override (V2) has one place to
# read from.
LOW_AGE_MINUTES = 10
LOW_AGE_HOURS = 1
LOW_REACTIONS = 3
MEDIUM_REACTIONS = 5
MEDIUM_THREAD = 30
CRITICAL_REACTIONS = 8
CRITICAL_THREAD = 80

# Module-level state for fitcheck-channel lookup. Built by `register` from
# fitcheck_streak's reverse-lookup tables so the two stay in lockstep.
_client: discord.Client | None = None
_FITCHECK_CHANNEL_IDS: set[int] = set()
_CHANNEL_TO_GUILD: dict[int, str] = {}


def _is_fitcheck_channel(channel_id: int) -> bool:
    return channel_id in _FITCHECK_CHANNEL_IDS


def _guild_for(channel_id: int) -> str | None:
    return _CHANNEL_TO_GUILD.get(channel_id)


def _resolve_bot_actor() -> str:
    if _client is not None and _client.user is not None:
        return f"discord:bot:{_client.user.id}"
    return "discord:bot:unknown"


def classify_delete_severity(
    *,
    age_seconds: float,
    reaction_count: int,
    thread_message_count: int,
    reaction_threshold: int,
    thread_message_threshold: int,
) -> str:
    """Pure-function severity classification per design sec 7.2.

    Tiebreakers (most severe wins): CRITICAL > MEDIUM > LOW. Defaults to
    LOW for any state we don't explicitly elevate — keeping the bias
    toward log-liberally.
    """
    # CRITICAL: within 2 of either threshold.
    if reaction_count >= max(0, reaction_threshold - 2):
        return "CRITICAL"
    if thread_message_count >= max(0, thread_message_threshold - 2):
        return "CRITICAL"
    # MEDIUM: high engagement.
    if reaction_count >= MEDIUM_REACTIONS or thread_message_count >= MEDIUM_THREAD:
        return "MEDIUM"
    # LOW age windows.
    if age_seconds <= LOW_AGE_MINUTES * 60 and reaction_count == 0:
        return "LOW"
    if age_seconds <= LOW_AGE_HOURS * 3600 and reaction_count < LOW_REACTIONS:
        return "LOW"
    return "LOW"


def _now_iso_seconds() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_streak_event(conn, guild_id: str, post_id: str) -> dict | None:
    """Fetch the streak event row to recover state-at-time-of-delete.

    Bypasses discord_streaks.get_event so we can keep this module's
    SP-helper imports minimal — only fitcheck_scores + scoring_config +
    audit are touched at module-level. The row may be None if the post
    was deleted before the streak upsert (text-only or no image).
    """
    row = conn.execute(
        text(
            "SELECT user_id, channel_id, posted_at, reaction_score, image_phash"
            " FROM discord_streak_events"
            " WHERE guild_id = :guild_id AND post_id = :post_id LIMIT 1"
        ),
        {"guild_id": guild_id, "post_id": post_id},
    ).fetchone()
    if row is None:
        return None
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def _fetch_score_row(conn, guild_id: str, post_id: str) -> dict | None:
    return discord_fitcheck_scores.get_score(conn, guild_id, post_id)


async def on_raw_message_delete(
    payload: discord.RawMessageDeleteEvent,
) -> None:
    """Audit a deletion of a fitcheck post with full state-at-time context.

    No-op when:
      - SCORED_MODE_ENABLED is False (env kill switch)
      - channel isn't a configured #fitcheck channel
      - no streak event row for the post (post never qualified — e.g. text)
    """
    if not SCORED_MODE_ENABLED:
        return
    try:
        channel_id = payload.channel_id
        if not _is_fitcheck_channel(channel_id):
            return
        guild_id = _guild_for(channel_id)
        if guild_id is None:
            return
        org_id = GUILD_TO_ORG.get(guild_id)
        if org_id is None:
            return
        post_id = str(payload.message_id)
        with get_db() as conn:
            event = _fetch_streak_event(conn, guild_id, post_id)
            if event is None:
                return  # post never qualified for the streak — nothing to flag
            cfg = discord_scoring_config.get_config(conn, guild_id)
            score = _fetch_score_row(conn, guild_id, post_id)

        # Best-effort age compute. Posted_at is ISO Z; parse + compare to now.
        try:
            posted_at = datetime.strptime(
                event["posted_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - posted_at).total_seconds()
        except Exception:  # noqa: BLE001
            age_seconds = 0.0

        # Best-effort thread message count. discord.py exposes thread message
        # totals only when the thread is cached / fetchable; we conservatively
        # treat as 0 if we can't get it (severity classification falls back
        # to reactions-only). Real V1 traffic is so low this rarely matters.
        thread_msg_count = 0
        try:
            channel = (
                _client.get_channel(channel_id) if _client is not None else None
            )
            if channel is not None and hasattr(channel, "get_thread"):
                thread = channel.get_thread(int(post_id))
                if thread is not None:
                    # Discord exposes Thread.message_count; bot+OP filtering
                    # happens at reveal-trigger time (Pass C), not here —
                    # we want the raw count for severity.
                    thread_msg_count = getattr(thread, "message_count", 0) or 0
        except Exception as exc:  # noqa: BLE001
            logger.info("thread_msg_count probe failed for %s: %s", post_id, exc)

        reaction_count = int(event.get("reaction_score") or 0)
        severity = classify_delete_severity(
            age_seconds=age_seconds,
            reaction_count=reaction_count,
            thread_message_count=thread_msg_count,
            reaction_threshold=int(cfg["reaction_threshold"]),
            thread_message_threshold=int(cfg["thread_message_threshold"]),
        )

        was_scored = score is not None and score.get("score_status") == "success"
        score_value = float(score["percentile"]) if was_scored and score.get("percentile") is not None else None

        with get_db() as conn:
            log_audit(
                conn,
                actor=_resolve_bot_actor(),
                action="fitcheck_post_deleted",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "channel_id": str(channel_id),
                    "post_id": post_id,
                    "user_id": str(event.get("user_id")),
                    "posted_at": event.get("posted_at"),
                    "deleted_at": _now_iso_seconds(),
                    "age_seconds": int(age_seconds),
                    "reaction_count_at_delete": reaction_count,
                    "thread_message_count_at_delete": thread_msg_count,
                    "severity": severity,
                    "was_scored": was_scored,
                    "score_value_if_scored": score_value,
                    "image_phash": event.get("image_phash"),
                    "scoring_state": cfg["state"],
                },
                source="sable-roles",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_raw_message_delete failed for %s", payload.message_id, exc_info=exc)


async def on_raw_message_edit(
    payload: discord.RawMessageUpdateEvent,
) -> None:
    """Audit a text-edit on a fitcheck post (LOW severity).

    Edit content is NOT stored — only flags + content lengths. The bot
    has no business archiving message text, but the *fact* of an edit
    is part of after-the-fact cheating detection.
    """
    if not SCORED_MODE_ENABLED:
        return
    try:
        channel_id = payload.channel_id
        if not _is_fitcheck_channel(channel_id):
            return
        guild_id = _guild_for(channel_id)
        if guild_id is None:
            return
        org_id = GUILD_TO_ORG.get(guild_id)
        if org_id is None:
            return
        post_id = str(payload.message_id)
        with get_db() as conn:
            event = _fetch_streak_event(conn, guild_id, post_id)
            if event is None:
                return

        new_content = ""
        if isinstance(payload.data, dict):
            new_content = payload.data.get("content") or ""
        # cached_message is None on un-cached posts (post-restart).
        old_content = ""
        if payload.cached_message is not None:
            old_content = payload.cached_message.content or ""

        with get_db() as conn:
            log_audit(
                conn,
                actor=_resolve_bot_actor(),
                action="fitcheck_text_edit",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "channel_id": str(channel_id),
                    "post_id": post_id,
                    "user_id": str(event.get("user_id")),
                    "old_content_length": len(old_content),
                    "new_content_length": len(new_content),
                    "cached": payload.cached_message is not None,
                    "edited_at": _now_iso_seconds(),
                },
                source="sable-roles",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_raw_message_edit failed for %s", payload.message_id, exc_info=exc)


def register(client: discord.Client) -> None:
    """Wire the two raw listeners + import-time channel snapshot.

    Composes-with-existing-handlers per the repo convention: this module
    binds its own coroutines via `client.event(...)`, NOT decorators on
    `Client.on_raw_message_delete` (which would be replaced rather than
    composed). Other features that need to bind the same events do the
    same — discord.py dispatches to ALL registered coroutines.
    """
    global _client, _FITCHECK_CHANNEL_IDS, _CHANNEL_TO_GUILD
    _client = client
    # Snapshot the fitcheck_streak module's reverse-lookup tables. This is
    # the single source of truth — if fitcheck_streak's tests monkeypatch
    # them, this module's tests do the same dance via the conftest fixture.
    from sable_roles.features import fitcheck_streak as fs
    _FITCHECK_CHANNEL_IDS = set(fs._FITCHECK_CHANNEL_IDS)
    _CHANNEL_TO_GUILD = dict(fs._CHANNEL_TO_GUILD)
    client.event(on_raw_message_delete)
    client.event(on_raw_message_edit)
