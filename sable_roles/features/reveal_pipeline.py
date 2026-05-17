"""Pass C: reveal pipeline — debounced per-post recompute that surfaces
Stitzy's score back in-channel when a fit crosses the design-§3 reveal
threshold.

Design refs:
  - ~/Projects/SolStitch/internal/fitcheck_scored_mode_plan.md §3, §4, §6.4, §7.3
  - ~/Projects/SolStitch/internal/scored_mode_pass_ab_qa_log.md (Pass C deferred items)

Pipeline:
  1. on_raw_reaction_add/remove on a fitcheck post → schedule a debounced
     recompute. Composes with the existing reaction handlers (fitcheck_streak's
     streak-recompute + vibe_observer's emoji capture + roast's 🚩 flag).
  2. on_message in a fitcheck thread → schedule the same debounce on the
     parent post. Composes with vibe_observer's existing on_message hook.
  3. on_raw_message_delete on a fitcheck post → cancel-and-lock helper
     (mark_reveal_cancelled_deleted CAS); emit HIGH-severity
     `fitcheck_reveal_cancelled_deleted` when the lock takes.

Recompute body:
  - Bail if scoring state is not 'revealed' (silent state still gets the
    milestone audits below — but no public surface).
  - Bail if score row missing / failed / invalidated / already revealed.
  - Bail if post age < reveal_min_age_minutes or > reveal_window_days.
  - Fetch the live message (NotFound → tombstone; delete handler owns cancel).
  - Per-emoji unique-reactor counts (filter bot + OP).
  - Emit `fitcheck_reaction_milestone` for newly-crossed 5/8/10 thresholds
    (per-emoji, durable via discord_fitcheck_emoji_milestones).
  - Emit `fitcheck_low_age_reactor` for first-time-seen reactors whose
    Discord account age < 30 days.
  - Trigger evaluation:
      reactions:       max per-emoji count >= reaction_threshold (default 10)
      thread_messages: paginated count via channel.history() (cap 200,
                       skip bot + OP) >= thread_message_threshold (default 100)
  - On trigger + state == 'revealed': re-read state from the live config
    (defend against mid-recompute mod flip), CAS-lock via mark_reveal_fired
    with placeholder 'pending' reveal_post_id, publish the reply, then
    EITHER update_reveal_post_id (success) OR convert_pending_to_cancelled_deleted
    (404 — post deleted mid-publish) OR mark_reveal_publish_failed (other
    HTTP error). Each terminal branch emits the matching design-§6.4 audit.

The single-task-per-post debounce mirrors V1 fitcheck_streak's reaction
recompute pattern (self-identity-guarded pop, CancelledError re-raise,
close() drain). 5-second debounce window — slightly longer than V1's 2s
to give the 10th reactor's neighbours a chance to land before recompute
publishes, but the CAS lock makes the window cosmetic, not correctness.

Silent → Revealed transition reading: implements design §8.3 strict
("Reveals only on fits posted *after* the transition"). A silent-period
fit's threshold trip post-flip earns milestone + low-age audits but
NEVER fires a public reveal — the calibration period stays private
forever. Implemented via a `posted_at >= state_changed_at` gate in
step 9 of the recompute body. Plan §3 line 94 has been tightened in
the canonical plan doc to match this reading.

Pass C also lands the two Pass A+B deferred items
(fitcheck_reaction_milestone + fitcheck_low_age_reactor — both surfaced
from this recompute since the per-emoji counts are already in hand).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from sqlalchemy import text as _sa_text  # noqa: F401  (kept for future raw-SQL needs)

from sable_platform.db import (
    discord_fitcheck_scores,
    discord_scoring_config,
)
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db

from sable_roles.config import GUILD_TO_ORG, SCORED_MODE_ENABLED

logger = logging.getLogger("sable_roles.reveal_pipeline")

# Terminal `reveal_trigger` values stamped on discord_fitcheck_scores rows.
# Pass D's leaderboard query MUST filter to {"reactions","thread_messages"}
# only — see discord_fitcheck_scores module docstring "Leaderboard query
# notes" for the contract.
TRIGGER_REACTIONS = "reactions"
TRIGGER_THREAD_MESSAGES = "thread_messages"
TRIGGER_PENDING = "pending"  # placeholder reveal_post_id during publish
SUCCESS_TRIGGERS = (TRIGGER_REACTIONS, TRIGGER_THREAD_MESSAGES)

# Debounce window. Slightly longer than V1's 2s so reaction bursts coalesce
# (the 10th and 11th reactor land in the same recompute). Correctness rests
# on the mark_reveal_fired CAS, not this number — tunable without behavior
# change beyond API-call cadence.
REVEAL_DEBOUNCE_SECONDS = 5.0

# Cap on how many thread messages we paginate per recompute. 2× the default
# thread_message_threshold (100) — enough headroom to find 100 non-bot non-OP
# messages even when bot reactions add chrome to most posts.
THREAD_HISTORY_FETCH_CAP = 200

# Per-emoji milestones audited via fitcheck_reaction_milestone. Per design
# §6.4 + §7.3. The 10 here is the design's reveal trigger; the 5 and 8 are
# early-warning crossings for retro analysis.
MILESTONE_LEVELS = (5, 8, 10)

# Reactor account-age threshold for fitcheck_low_age_reactor audit.
LOW_AGE_DAYS = 30

# Hard cap on the per-process low-age dedup set to bound memory at scale.
# When exceeded we clear the set; the rare repeat audit is acceptable.
_LOW_AGE_AUDITED_CAP = 10000

# Cap on _pending_reveals dict before oldest entries are dropped. Each entry
# is a small asyncio.Task wrapper — 1024 fits comfortably inside the typical
# per-process memory budget while still bounding worst-case burst pressure
# (e.g. 100 distinct fits each getting a recompute scheduled in parallel).
_PENDING_REVEALS_CAP = 1024

# Module-level state. Mirrors fitcheck_streak's pattern — tests reset via
# the `rp_module` conftest fixture in tests/test_reveal_pipeline.py.
_client: discord.Client | None = None
_FITCHECK_CHANNEL_IDS: set[int] = set()
_CHANNEL_TO_GUILD: dict[int, str] = {}
_pending_reveals: dict[int, asyncio.Task] = {}
# (post_id, reactor_id) seen-set for low-age audit dedup. In-memory only;
# bot restart re-audits worst-case once per reactor per post. Acceptable
# at V1 scale; durable dedup table is V2 work (see scored_mode_pass_ab_qa_log
# Pass C section for the trade-off discussion).
_low_age_audited: set[tuple[int, int]] = set()


def _is_fitcheck_channel(channel_id: int) -> bool:
    return channel_id in _FITCHECK_CHANNEL_IDS


def _guild_for(channel_id: int) -> str | None:
    return _CHANNEL_TO_GUILD.get(channel_id)


def _resolve_bot_actor() -> str:
    if _client is not None and _client.user is not None:
        return f"discord:bot:{_client.user.id}"
    return "discord:bot:unknown"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime | None:
    """Tolerant ISO-Z parser. Returns None on any parse error so the caller
    can bail without crashing the recompute body.
    """
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


def _tone_band(percentile: float) -> str:
    """Design §4.1 register bands. Returned string is the leading line for
    `_build_reveal_text` — drives the warmth shift without changing voice.
    """
    if percentile >= 80:
        return "high"
    if percentile >= 40:
        return "mid"
    return "low"


def _build_reveal_text(score: dict, display_name: str) -> str:
    """Render the design-§4.2 reveal body. plain text, no @-ping.

    Per design: the `caught:` line is omitted entirely when catch_detected
    is null. Percentile is the headline integer 1-100. Display name is
    Discord's per-guild display_name — caller's responsibility to resolve
    (Member.display_name when in-guild, falls back to global).
    """
    pct = score.get("percentile")
    if pct is None:
        pct_int = 0
    else:
        pct_int = max(1, min(100, int(round(float(pct)))))
    cohesion = int(score.get("axis_cohesion") or 0)
    execution = int(score.get("axis_execution") or 0)
    concept = int(score.get("axis_concept") or 0)
    catch = int(score.get("axis_catch") or 0)
    catch_detected = score.get("catch_detected")

    lines = [
        f"{display_name}'s fit just landed. Stitzy says: {pct_int}.",
        f"cohesion {cohesion} · execution {execution} · concept {concept} · catch {catch}",
    ]
    if catch_detected:
        lines.append(f"caught: {catch_detected}")
    return "\n".join(lines)


def schedule_reveal_recompute(
    *,
    guild_id: str,
    post_id: int,
    channel_id: int,
) -> None:
    """Cancel any existing reveal-recompute task for this post and start
    a fresh debounced one. Single-tick atomic — mirrors fitcheck_streak's
    `_schedule_recompute` pattern.

    Caller responsibility: filter inbound events so this is only invoked
    for posts in a configured fitcheck channel (avoids creating tasks
    for every reaction across every server).

    Caps `_pending_reveals` at `_PENDING_REVEALS_CAP` entries to bound
    burst-scenario memory growth — when full, cancel and evict the oldest
    entry. Python dicts preserve insertion order so `next(iter(dict))`
    is the oldest key.
    """
    existing = _pending_reveals.get(post_id)
    if existing is not None:
        existing.cancel()
    elif len(_pending_reveals) >= _PENDING_REVEALS_CAP:
        # Evict oldest entry to make room. Cancelling its task lets the
        # caller-task drain cleanly via the self-identity-guarded finally.
        try:
            oldest_key = next(iter(_pending_reveals))
            stale = _pending_reveals.pop(oldest_key, None)
            if stale is not None:
                stale.cancel()
        except StopIteration:
            pass
    task = asyncio.create_task(
        _recompute_after_delay(
            guild_id=guild_id,
            post_id=post_id,
            channel_id=channel_id,
        )
    )
    _pending_reveals[post_id] = task


async def _count_per_emoji_reactors(
    message: discord.Message,
    author_id: int,
    bot_ids: set[int],
) -> tuple[dict[str, int], dict[int, datetime]]:
    """Return (per_emoji_counts, reactor_accounts).

    per_emoji_counts: {str(emoji) -> unique reactor count, excluding bots
    and the OP}. Emoji key is the discord.py canonical str() form (unicode
    glyph for stock; <:name:id> for custom).
    reactor_accounts: {reactor_user_id -> account_created_at_utc}. Lets the
    low-age audit run without a second `reaction.users()` iteration.

    Skips bot-only reactions early (`reaction.me` set AND no other reactors)
    so we don't burn a `reaction.users()` Discord API call for the bot's
    own 🔥 confirmation reaction on every recompute.
    """
    per_emoji: dict[str, int] = {}
    accounts: dict[int, datetime] = {}
    for reaction in message.reactions:
        # Fast-skip: bot-only reaction with count==1 means it's just the
        # bot's own confirmation emoji. No need to iterate users.
        if getattr(reaction, "me", False) and getattr(reaction, "count", 0) <= 1:
            continue
        key = str(reaction.emoji)
        count = 0
        async for user in reaction.users():
            if user.id in bot_ids or user.id == author_id:
                continue
            count += 1
            # user.created_at is timezone-aware UTC per discord.py 2.x.
            accounts[user.id] = user.created_at.astimezone(timezone.utc)
        per_emoji[key] = count
    return per_emoji, accounts


async def _count_thread_messages(
    parent_channel: Any,
    post_id: int,
    author_id: int,
    bot_ids: set[int],
) -> int:
    """Count non-bot non-OP messages in the thread whose parent message is
    `post_id`. Capped at THREAD_HISTORY_FETCH_CAP raw fetches; iterates the
    full window so the audit detail can carry the actual count (callers
    that just need the trigger-trip boolean can compare to threshold).

    Returns 0 if the thread doesn't exist yet (no early thread messages
    means we can't be at the 100-msg trigger either way).
    """
    thread = None
    try:
        if hasattr(parent_channel, "get_thread"):
            thread = parent_channel.get_thread(post_id)
        if thread is None and _client is not None:
            try:
                fetched = await _client.fetch_channel(post_id)
                if isinstance(fetched, discord.Thread):
                    thread = fetched
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return 0
    except Exception as exc:  # noqa: BLE001
        logger.info("thread lookup failed for post %s: %s", post_id, exc)
        return 0
    if thread is None:
        return 0

    count = 0
    fetched_so_far = 0
    try:
        async for msg in thread.history(
            limit=THREAD_HISTORY_FETCH_CAP, oldest_first=True
        ):
            fetched_so_far += 1
            if msg.author is None:
                continue
            if msg.author.id in bot_ids:
                continue
            if msg.author.id == author_id:
                continue
            count += 1
    except (discord.HTTPException, discord.Forbidden) as exc:
        logger.info(
            "thread.history failed for post %s after %s fetched: %s",
            post_id, fetched_so_far, exc,
        )
    return count


def _maybe_dedup_low_age(post_id: int, reactor_id: int) -> bool:
    """Return True iff this (post, reactor) pair has not been audited yet
    for low-age. Records the seen pair as a side-effect so the next call
    returns False. Caps the seen-set at _LOW_AGE_AUDITED_CAP entries; on
    overflow it clears (worst-case a few duplicate audit rows post-clear).
    """
    key = (post_id, reactor_id)
    if key in _low_age_audited:
        return False
    if len(_low_age_audited) >= _LOW_AGE_AUDITED_CAP:
        _low_age_audited.clear()
    _low_age_audited.add(key)
    return True


async def _recompute_after_delay(
    *,
    guild_id: str,
    post_id: int,
    channel_id: int,
) -> None:
    """The debounced recompute body. Never raises — last-line try/except.

    Self-identity finally clause prevents a cancelled task from evicting its
    replacement. `CancelledError` re-raises so the cancelling caller (drain
    in close() OR replacement scheduler) sees clean unwind.
    """
    self_task = asyncio.current_task()
    try:
        await asyncio.sleep(REVEAL_DEBOUNCE_SECONDS)
        if not SCORED_MODE_ENABLED:
            return
        if _client is None:
            logger.warning(
                "reveal recompute skipped: _client unset for post_id=%s",
                post_id,
            )
            return
        org_id = GUILD_TO_ORG.get(guild_id)
        if org_id is None:
            return

        # 1) Read config + score.
        with get_db() as conn:
            cfg = discord_scoring_config.get_config(conn, guild_id)
            score = discord_fitcheck_scores.get_score(
                conn, guild_id, str(post_id)
            )

        # No score yet? Reaction arrived before scoring finished, or scoring
        # is off / failed / didn't fit. Nothing to do (Pass A audit
        # captures the post separately).
        if score is None:
            return
        if score.get("score_status") != "success":
            return
        if score.get("invalidated_at"):
            return
        # One-and-done check (also enforced by mark_reveal_fired CAS).
        if score.get("reveal_fired_at"):
            return

        # 2) Floor checks.
        posted_at = _parse_iso(str(score.get("posted_at") or ""))
        if posted_at is None:
            logger.info("reveal recompute: bad posted_at for %s", post_id)
            return
        now = _now_utc()
        age_seconds = (now - posted_at).total_seconds()
        min_age_seconds = int(cfg.get("reveal_min_age_minutes") or 10) * 60
        window_seconds = int(cfg.get("reveal_window_days") or 7) * 86400
        if age_seconds < min_age_seconds:
            return
        if age_seconds > window_seconds:
            return

        # 3) Fetch live message + thread metadata. NotFound → post deleted;
        # delete handler owns cancellation audit.
        try:
            channel = _client.get_channel(channel_id) or await _client.fetch_channel(
                channel_id
            )
            message = await channel.fetch_message(post_id)
        except discord.NotFound:
            return
        except (discord.HTTPException, discord.Forbidden) as exc:
            logger.info(
                "reveal recompute: message fetch failed for %s: %s",
                post_id, exc,
            )
            return

        author_id = int(score.get("user_id") or message.author.id)
        bot_ids: set[int] = (
            {_client.user.id} if _client.user is not None else set()
        )

        # 4) Per-emoji reactor counts + reactor account ages.
        per_emoji, reactor_accounts = await _count_per_emoji_reactors(
            message, author_id, bot_ids
        )

        # 5) Trigger evaluation. Reactions first (cheap — already in hand).
        # Thread message count is potentially expensive — only fetch when
        # reactions didn't already trip.
        reaction_threshold = int(cfg.get("reaction_threshold") or 10)
        thread_threshold = int(cfg.get("thread_message_threshold") or 100)
        max_per_emoji = max(per_emoji.values()) if per_emoji else 0
        trigger: str | None = None
        thread_count_at_trigger: int | None = None
        if max_per_emoji >= reaction_threshold:
            trigger = TRIGGER_REACTIONS
        else:
            parent = _client.get_channel(channel_id)
            if parent is None:
                try:
                    parent = await _client.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    parent = None
            if parent is not None:
                thread_count = await _count_thread_messages(
                    parent, post_id, author_id, bot_ids
                )
                if thread_count >= thread_threshold:
                    trigger = TRIGGER_THREAD_MESSAGES
                    thread_count_at_trigger = thread_count

        # 6) Re-read live state. Single source of truth for the rest of the
        # recompute — milestone + low-age audit `scoring_state` field uses
        # the live value (defends against the early-cfg read at step 1 going
        # stale if a mod flips state during the recompute body's slow-path
        # operations like thread.history()).
        with get_db() as conn:
            live_cfg = discord_scoring_config.get_config(conn, guild_id)
        live_state = live_cfg["state"]

        # 7) Audits — milestones + low-age reactors. Run in BOTH silent and
        # revealed states (calibration mode benefits from the same signals).
        # Batched into a single connection to bound pool checkouts per
        # recompute (per QA-round-1 finding M4). Stamps the LIVE state, not
        # the step-1 cfg state (per QA-round-2 finding M-NEW-2).
        with get_db() as conn:
            for emoji_key, count in per_emoji.items():
                for milestone in MILESTONE_LEVELS:
                    if count < milestone:
                        continue
                    newly = discord_fitcheck_scores.record_emoji_milestone_crossing(
                        conn, org_id, guild_id, str(post_id), emoji_key, milestone
                    )
                    if not newly:
                        continue
                    log_audit(
                        conn,
                        actor=_resolve_bot_actor(),
                        action="fitcheck_reaction_milestone",
                        org_id=org_id,
                        entity_id=None,
                        detail={
                            "guild_id": guild_id,
                            "channel_id": str(channel_id),
                            "post_id": str(post_id),
                            "user_id": str(author_id),
                            "emoji": emoji_key,
                            "milestone": int(milestone),
                            "count": int(count),
                            "scoring_state": live_state,
                        },
                        source="sable-roles",
                    )

            for reactor_id, created_at in reactor_accounts.items():
                account_age_days = (now - created_at).total_seconds() / 86400.0
                if account_age_days >= LOW_AGE_DAYS:
                    continue
                if not _maybe_dedup_low_age(post_id, reactor_id):
                    continue
                log_audit(
                    conn,
                    actor=_resolve_bot_actor(),
                    action="fitcheck_low_age_reactor",
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "channel_id": str(channel_id),
                        "post_id": str(post_id),
                        "user_id": str(author_id),
                        "reactor_user_id": str(reactor_id),
                        "reactor_account_created_at": created_at.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "reactor_account_age_days": round(account_age_days, 2),
                        "scoring_state": live_state,
                    },
                    source="sable-roles",
                )

        # 8) Bail if no trigger (post still grew via audit-only paths above).
        if trigger is None:
            return

        # 9) Reveal-fire gates.
        #   (a) live state must be revealed (re-uses live_state from step 6)
        #   (b) post must have been posted AT OR AFTER the current revealed-
        #       state transition timestamp. Implements design §8.3 strict
        #       reading: "Silent → Revealed: NO historical reveals. Reveals
        #       only on fits posted *after* the transition." Pre-flip
        #       silent-period posts gain milestone audits (above) but never
        #       a public reveal — calibration data stays private forever.
        #       Design §3 has been tightened to match (plan-doc update
        #       landed alongside this code).
        #
        #   state_changed_at SHOULD be non-NULL whenever live_state is
        #   'revealed' (a transition must have happened to reach revealed).
        #   Defensive: if it's somehow NULL or unparseable, fail closed
        #   (skip the reveal) — better than firing on data we can't reason
        #   about.
        if live_state != "revealed":
            return
        state_changed_at_raw = live_cfg.get("state_changed_at")
        if not state_changed_at_raw:
            logger.warning(
                "reveal recompute: live state is 'revealed' but"
                " state_changed_at is NULL for guild %s — fail-closed",
                guild_id,
            )
            return
        state_changed_at = _parse_iso(str(state_changed_at_raw))
        if state_changed_at is None:
            logger.warning(
                "reveal recompute: state_changed_at %r failed parse for"
                " guild %s — fail-closed",
                state_changed_at_raw, guild_id,
            )
            return
        if posted_at < state_changed_at:
            return

        # 10) Fire reveal — CAS-lock FIRST with placeholder reveal_post_id,
        # then publish, then patch the post_id or route to a terminal
        # failure trigger. CAS-before-publish so a lost race burns no
        # Discord-side message.
        with get_db() as conn:
            locked = discord_fitcheck_scores.mark_reveal_fired(
                conn, guild_id, str(post_id), TRIGGER_PENDING, trigger
            )
        if not locked:
            logger.info(
                "reveal CAS lost for post %s (concurrent or repeat fire)",
                post_id,
            )
            return

        display_name = _resolve_display_name(message)
        reveal_text = _build_reveal_text(score, display_name)
        publish_exc: Exception | None = None
        publish_is_404 = False
        reveal_msg = None
        try:
            reveal_msg = await message.reply(
                reveal_text,
                mention_author=False,
                # Hard-pin allowed_mentions to none so a malicious display
                # name containing `@everyone` / `@here` / `<@id>` text can't
                # ping anyone via the bot's authority. mention_author=False
                # alone covers the OP reference, not arbitrary mentions in
                # the body content.
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound as exc:
            # 404 on reply = post was deleted DURING the publish window.
            # This is the design-§6.4 HIGH-severity "yank-before-reveal"
            # gaming vector — the in-process CAS race against the delete
            # handler would otherwise silently swallow it (delete handler
            # sees reveal_fired_at != NULL and bails). Route to the
            # cancelled_deleted audit class instead of publish_failed.
            publish_exc = exc
            publish_is_404 = True
        except (discord.HTTPException, discord.Forbidden) as exc:
            publish_exc = exc

        if publish_exc is not None:
            with get_db() as conn:
                if publish_is_404:
                    converted = discord_fitcheck_scores.convert_pending_to_cancelled_deleted(
                        conn, guild_id, str(post_id)
                    )
                    if converted:
                        log_audit(
                            conn,
                            actor=_resolve_bot_actor(),
                            action="fitcheck_reveal_cancelled_deleted",
                            org_id=org_id,
                            entity_id=None,
                            detail={
                                "guild_id": guild_id,
                                "channel_id": str(channel_id),
                                "post_id": str(post_id),
                                "user_id": str(author_id),
                                "scoring_state": live_state,
                                "percentile": float(score.get("percentile") or 0),
                                "raw_total": int(score.get("raw_total") or 0),
                                "severity": "HIGH",
                                "via": "publish_404",
                            },
                            source="sable-roles",
                        )
                    else:
                        # Defensive log: in single-process operation the only
                        # writer of 'pending' is this same task, so the CAS
                        # cannot legitimately lose here. If it does, the
                        # lock was already converted by another writer
                        # (delete handler, manual SQL, or a forgotten code
                        # path) and the audit row is intentionally skipped.
                        logger.warning(
                            "reveal cancellation CAS lost for %s on 404 "
                            "publish — lock already converted upstream",
                            post_id,
                        )
                else:
                    flipped = discord_fitcheck_scores.mark_reveal_publish_failed(
                        conn, guild_id, str(post_id)
                    )
                    if flipped:
                        log_audit(
                            conn,
                            actor=_resolve_bot_actor(),
                            action="fitcheck_reveal_publish_failed",
                            org_id=org_id,
                            entity_id=None,
                            detail={
                                "guild_id": guild_id,
                                "channel_id": str(channel_id),
                                "post_id": str(post_id),
                                "user_id": str(author_id),
                                "trigger": trigger,
                                "tone_band": _tone_band(
                                    float(score.get("percentile") or 0)
                                ),
                                "error": f"{type(publish_exc).__name__}: {publish_exc}",
                            },
                            source="sable-roles",
                        )
                    else:
                        logger.warning(
                            "reveal publish-failed CAS lost for %s — lock "
                            "already converted upstream",
                            post_id,
                        )
            logger.warning(
                "reveal post failed for %s post-lock: %s", post_id, publish_exc
            )
            return

        # Success — patch the placeholder reveal_post_id with the real id
        # under the same 'pending'-guarded CAS, then write the design-§6.4
        # INFO-severity reveal_fired audit.
        with get_db() as conn:
            patched = discord_fitcheck_scores.update_reveal_post_id(
                conn, guild_id, str(post_id), str(reveal_msg.id)
            )
            if not patched:
                # Should be impossible (we hold the lock, no other path
                # writes 'pending'), but defensive log so an operator can
                # find this if the invariant breaks.
                logger.warning(
                    "reveal post_id patch CAS lost for %s — pending lock"
                    " already converted by another writer",
                    post_id,
                )
            log_audit(
                conn,
                actor=_resolve_bot_actor(),
                action="fitcheck_reveal_fired",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "channel_id": str(channel_id),
                    "post_id": str(post_id),
                    "user_id": str(author_id),
                    "reveal_post_id": str(reveal_msg.id),
                    "trigger": trigger,
                    "tone_band": _tone_band(float(score.get("percentile") or 0)),
                    "percentile": float(score.get("percentile") or 0),
                    "raw_total": int(score.get("raw_total") or 0),
                    "max_per_emoji": int(max_per_emoji),
                    "thread_message_count_at_trigger": thread_count_at_trigger,
                    "scoring_state": live_cfg["state"],
                },
                source="sable-roles",
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — recompute must never crash loop
        logger.warning(
            "reveal recompute failed for post_id=%s", post_id, exc_info=exc
        )
    finally:
        if _pending_reveals.get(post_id) is self_task:
            _pending_reveals.pop(post_id, None)


def _resolve_display_name(message: discord.Message) -> str:
    """Display name preference: member-in-guild > author.display_name.

    Falls back to the message author's display_name attribute which is
    discord.py's documented fall-through; on detached messages it returns
    the username.
    """
    try:
        if message.guild is not None and message.author is not None:
            member = message.guild.get_member(message.author.id)
            if member is not None:
                return member.display_name
        return message.author.display_name if message.author else "someone"
    except Exception:  # noqa: BLE001
        return "someone"


# ---------------------------------------------------------------------------
# Gateway-event handlers
#
# Module-level entry points are deliberately prefixed `handle_*` so the
# register() wrappers (`on_raw_reaction_add` etc. bound via @client.event)
# don't shadow them. The compose wrappers call into these names; tests
# import these as the testable surface.
# ---------------------------------------------------------------------------


async def handle_raw_reaction_add(
    payload: discord.RawReactionActionEvent,
) -> None:
    if not SCORED_MODE_ENABLED:
        return
    channel_id = payload.channel_id
    if not _is_fitcheck_channel(channel_id):
        return
    guild_id = _guild_for(channel_id)
    if guild_id is None:
        return
    schedule_reveal_recompute(
        guild_id=guild_id,
        post_id=payload.message_id,
        channel_id=channel_id,
    )


async def handle_raw_reaction_remove(
    payload: discord.RawReactionActionEvent,
) -> None:
    if not SCORED_MODE_ENABLED:
        return
    channel_id = payload.channel_id
    if not _is_fitcheck_channel(channel_id):
        return
    guild_id = _guild_for(channel_id)
    if guild_id is None:
        return
    schedule_reveal_recompute(
        guild_id=guild_id,
        post_id=payload.message_id,
        channel_id=channel_id,
    )


async def handle_thread_message(message: discord.Message) -> None:
    """Schedule a recompute on the PARENT post when a non-bot message lands
    in a fitcheck thread. discord.py routes thread messages through
    on_message, so the compose wrapper filters here.

    The OP filter is handled inside the recompute body's thread.history()
    walk (`_count_thread_messages` skips author_id == OP). This handler
    deliberately does NOT pre-fetch the starter message to verify OP
    identity — under burst (e.g. 100 thread msgs in 10s), a per-handler
    fetch_message would block the event loop on rate-limited lookups
    before the debounce had a chance to coalesce. Over-scheduling is
    free; the debounce single-task-per-post pattern is the rate-limiter.
    """
    if not SCORED_MODE_ENABLED:
        return
    if message.author is None or message.author.bot:
        return
    if message.guild is None:
        return
    channel = message.channel
    if not isinstance(channel, discord.Thread):
        return
    parent_id = channel.parent_id
    if parent_id is None or not _is_fitcheck_channel(parent_id):
        return
    guild_id = _guild_for(parent_id)
    if guild_id is None:
        return
    # parent thread = thread whose `id` matches the original post message_id.
    # In discord.py, `message.create_thread()` yields a Thread whose id ==
    # the parent message id (load-bearing invariant; standalone-created
    # threads or forum threads break this — fitcheck's create-thread-on-fit
    # pattern preserves it. Documented at register() so a future feature
    # that adds a different thread-creation path knows to add a fallback.)
    post_id = channel.id
    schedule_reveal_recompute(
        guild_id=guild_id,
        post_id=post_id,
        channel_id=parent_id,
    )


async def handle_raw_message_delete(
    payload: discord.RawMessageDeleteEvent,
) -> None:
    """Cancel + lock any pending reveal when a fitcheck post is deleted.

    Composes with delete_monitor's existing handler (which writes the
    severity-classified `fitcheck_post_deleted` audit). This handler adds
    the design-§3 `fitcheck_reveal_cancelled_deleted` HIGH-severity audit
    when the row had a successful score AND no reveal_fired_at — those are
    exactly the "yank before reveal" gaming attempts §6.4 calls out.
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
            score = discord_fitcheck_scores.get_score(conn, guild_id, post_id)
            if score is None:
                return
            if score.get("score_status") != "success":
                return
            if score.get("invalidated_at"):
                return
            if score.get("reveal_fired_at"):
                # Already fired or already cancelled — nothing to do.
                return
            cfg = discord_scoring_config.get_config(conn, guild_id)
            locked = discord_fitcheck_scores.mark_reveal_cancelled_deleted(
                conn, guild_id, post_id
            )
            if not locked:
                return
            log_audit(
                conn,
                actor=_resolve_bot_actor(),
                action="fitcheck_reveal_cancelled_deleted",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "channel_id": str(channel_id),
                    "post_id": post_id,
                    "user_id": str(score.get("user_id")),
                    "scoring_state": cfg["state"],
                    "percentile": float(score.get("percentile") or 0),
                    "raw_total": int(score.get("raw_total") or 0),
                    "severity": "HIGH",
                },
                source="sable-roles",
            )
        # Drop any pending recompute task too — the post is gone, no point
        # racing the recompute body to a NotFound.
        existing = _pending_reveals.pop(payload.message_id, None)
        if existing is not None:
            existing.cancel()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "on_raw_message_delete reveal-cancel failed for %s",
            payload.message_id,
            exc_info=exc,
        )


def register(client: discord.Client) -> None:
    """Compose with whatever's bound to on_message / on_raw_reaction_add /
    on_raw_reaction_remove / on_raw_message_delete and dispatch our hooks
    AFTER the prior handler runs.

    Must be called AFTER fitcheck_streak.register, roast.register,
    vibe_observer.register, and delete_monitor.register so the wrap-existing
    pattern preserves all of them. Bare `client.event(...)` would CLOBBER
    fitcheck_streak's streak-reaction-debounce + vibe_observer's emoji
    capture + roast's 🚩 flag + delete_monitor's severity audit.
    """
    global _client, _FITCHECK_CHANNEL_IDS, _CHANNEL_TO_GUILD
    _client = client

    # Hold REFERENCES (not copies) to fitcheck_streak's reverse-lookup tables
    # so any future runtime config-reload that mutates the source stays in
    # lockstep. delete_monitor currently copies — that's its own historical
    # quirk to fix separately; reveal_pipeline gets the right pattern here.
    from sable_roles.features import fitcheck_streak as fs
    _FITCHECK_CHANNEL_IDS = fs._FITCHECK_CHANNEL_IDS
    _CHANNEL_TO_GUILD = fs._CHANNEL_TO_GUILD

    existing_on_message = getattr(client, "on_message", None)
    existing_on_reaction_add = getattr(client, "on_raw_reaction_add", None)
    existing_on_reaction_remove = getattr(client, "on_raw_reaction_remove", None)
    existing_on_message_delete = getattr(client, "on_raw_message_delete", None)

    @client.event
    async def on_message(message: discord.Message):
        if existing_on_message is not None:
            await existing_on_message(message)
        await handle_thread_message(message)

    @client.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        if existing_on_reaction_add is not None:
            await existing_on_reaction_add(payload)
        await handle_raw_reaction_add(payload)

    @client.event
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        if existing_on_reaction_remove is not None:
            await existing_on_reaction_remove(payload)
        await handle_raw_reaction_remove(payload)

    @client.event
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        if existing_on_message_delete is not None:
            await existing_on_message_delete(payload)
        await handle_raw_message_delete(payload)


async def close() -> None:
    """Cancel + drain in-flight reveal recompute tasks. Called from
    SableRolesClient.close() before super().close() tears down the
    websocket. Pattern + invariant mirror fitcheck_streak.close().

    Deliberately does NOT clear `_low_age_audited` — the dedup cache is
    bounded and useful across an in-process reset. Tests that want a
    fresh state should monkeypatch `_low_age_audited = set()` (the
    `rp_module` conftest fixture does this).
    """
    tasks = list(_pending_reveals.values())
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending_reveals.clear()
