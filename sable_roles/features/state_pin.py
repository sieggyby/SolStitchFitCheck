"""State-pin surface — system-voice state-summary pin in the per-guild
#sable-ops channel, one pin per Stitzy-managed state dimension.

Design refs:
  - ~/Projects/SolStitch/internal/state_pin_plan.md (rev 7 APPROVED)
  - AGENTS.md sections on CancelledError discipline, close-drain pattern,
    AllowedMentions.none() contract, audit-log shape

One-line: when a mod changes any Stitzy-managed state dimension (scoring,
burn_mode, relax_mode, personalize_mode) AND the new state differs from
the old, this module posts a system-voice summary message in the per-
guild ops channel and pins it, unpinning any prior pin for the same
dimension. #sable-ops's pinned-message list becomes a live dashboard of
current bot state.

Surface:

* :func:`announce_state_change` — fire-and-forget from slash-command
  handlers after they've completed their DB write + audit + ephemeral
  confirmation. Coalesces in-flight announces for the same (guild,
  characteristic) key; last-write-wins per plan §6.1.
* :func:`sweep_orphan_pins` — boot-time orphan-pin cleanup. Lists each
  configured ops channel's pinned messages, parses the Stitzy-state
  headline prefix, and unpins any whose DB pointer doesn't match.
* :func:`register` — wires the boot-time sweep into ``setup_hook``.
* :func:`close` — drains in-flight announce tasks before
  ``super().close()`` tears down the gateway.

Default-invisible: when ``OPS_CHANNELS`` has no entry for a guild,
``announce_state_change`` audits ``fitcheck_state_pin_no_ops_channel``
LOW and returns. No new behavior in production until the operator opts
in by setting ``SABLE_ROLES_OPS_CHANNELS_JSON``.

CancelledError discipline (AGENTS.md): the outer try in
``announce_state_change`` RE-RAISES ``asyncio.CancelledError`` so the
:func:`close` drain's ``asyncio.gather(..., return_exceptions=True)``
sees clean unwind. Inner except blocks catch ``SQLAlchemyError`` +
``discord.HTTPException`` + a final ``Exception`` sink.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from sqlalchemy.exc import SQLAlchemyError

from sable_platform.db import discord_state_pins
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db

from sable_roles.config import GUILD_TO_ORG, OPS_CHANNELS
from sable_roles.features.leaderboard import _resolve_display_name

logger = logging.getLogger("sable_roles.state_pin")


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Coalescing dict — one announce task per (guild_id, characteristic) key.
# Replaces fitcheck_streak / reveal_pipeline's pattern; eviction at cap is
# oldest-insertion (Python dict insertion order) so a flapping mod's repeat
# announces don't unbound the dict.
_pending_announcements: dict[tuple[str, str], asyncio.Task] = {}
_PENDING_ANNOUNCEMENTS_CAP = 256

# Per-channel async lock to serialize pin ops on the same channel. Discord
# rate-limits pin/unpin at ~5/4s per channel; cross-characteristic ops in
# the same ops channel must serialize. Unbounded growth is accepted (PR2-L1
# in the plan) — OPS_CHANNELS_JSON is operator-curated and small.
_channel_locks: dict[str, asyncio.Lock] = {}

# Coupled between the formatter (§5) and the sweep parser (§6.4). Extracting
# as a module constant means a future headline-format change either updates
# both sites or breaks both loudly at code review (PR2-M5). Round-1 R1-M6:
# extracted the SUFFIX as a sibling constant so the formatter and parser
# share both sides of the bold-markup wrapper rather than the formatter
# hardcoding `"**\n"` while the parser strips trailing `*`.
_STATE_HEADLINE_PREFIX = "**stitzy state · "
_STATE_HEADLINE_SUFFIX = "**"

# Whitelist of valid characteristic names. Single source of truth referenced
# in three places: announce_state_change entry-side defense (PR6-L2), the
# sweep parser's recognition check, and the §2 plan-doc table. Adding a 5th
# characteristic = one-line frozenset edit + a plan-doc update.
_KNOWN_CHARACTERISTICS: frozenset[str] = frozenset({
    "scoring", "burn_mode", "relax_mode", "personalize_mode",
})

# Client handle set by register() for the boot-time sweep.
_client: discord.Client | None = None

# R1-H1 + R1-H3: one-shot guard so the orphan sweep doesn't re-fire on every
# gateway reconnect. AGENTS.md explicitly notes on_ready re-fires; we compose
# the wrapper onto on_ready (the documented post-cache-populated hook) but
# guard the sweep body so only the first on_ready of a process invokes it.
# Reset via `.clear()` in the test conftest fixture (AGENTS.md convention).
_sweep_done: dict[str, bool] = {}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _now_iso_minute() -> str:
    """Display timestamp for the `last changed:` line in the pinned body.
    Minute-resolution, human-readable: ``2026-05-17 19:19 UTC``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _now_iso_seconds() -> str:
    """Storage timestamp for ``posted_at`` in the discord_state_pins row.
    Matches the ISO Z format used across SP helpers (mirrors
    :func:`sable_platform.db.discord_streaks._now_iso_seconds`)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_bot_actor(client: discord.Client) -> str:
    """R1-M1 + R1-L2: client.user is Optional and is None during gateway
    reconnect / pre-READY. Return a stable sentinel actor when the bot
    user isn't yet resolved so the audit row still lands.
    """
    if client is not None and client.user is not None:
        return f"discord:bot:{client.user.id}"
    return "discord:bot:unknown"


def _evict_pending_if_full() -> None:
    """Bounded dict — drop oldest entry by insertion order when over cap.

    Eviction runs AFTER the new key is inserted in the caller, so the ``>``
    predicate is correct: dict transiently holds CAP+1 inside one call,
    then drops back to CAP after eviction. Mirrors leaderboard's
    ``_evict_cooldown_if_full`` post-insert + ``>`` precedent.
    """
    if len(_pending_announcements) > _PENDING_ANNOUNCEMENTS_CAP:
        oldest = next(iter(_pending_announcements))
        del _pending_announcements[oldest]


def _extract_characteristic_from_headline(content: str) -> str | None:
    """Parse the characteristic name out of a Stitzy state-pin headline.

    Example::

        >>> _extract_characteristic_from_headline(
        ...     "**stitzy state · scoring**\\nstate: silent"
        ... )
        'scoring'

    Returns ``None`` for any content not matching the prefix or whose
    first whitespace-bounded token (after stripping the matched
    ``_STATE_HEADLINE_SUFFIX``) isn't in ``_KNOWN_CHARACTERISTICS``.
    Defensive: empty-after-prefix returns ``None`` instead of
    IndexError.

    R2-L1: the suffix is consumed via ``removesuffix(_STATE_HEADLINE_SUFFIX)``
    so a future format change to the constant updates BOTH the formatter
    and the parser through the same source-of-truth. The
    `_format_body → _extract_characteristic_from_headline` round-trip
    test (test_state_pin.py) locks the coupling.
    """
    if not content.startswith(_STATE_HEADLINE_PREFIX):
        return None
    tokens = content[len(_STATE_HEADLINE_PREFIX):].split(None, 1)
    if not tokens:
        return None
    char = tokens[0].removesuffix(_STATE_HEADLINE_SUFFIX)
    if char not in _KNOWN_CHARACTERISTICS:
        return None
    return char


# ---------------------------------------------------------------------------
# Body formatter
# ---------------------------------------------------------------------------


async def _format_body(
    client: discord.Client,
    characteristic: str,
    summary: str,
    user_id: int,
) -> str:
    """Render the pinned-message body.

    Caller contract: ``summary`` is a multi-line string whose FIRST line
    is ``state: <value>`` (the canonical state token); subsequent lines
    are characteristic-specific config (thresholds, window, etc.).
    ``_format_body`` does NOT parse ``summary`` — it's pass-through
    between the headline and the ``last changed:`` audit line.

    Display-name resolution is internal via
    :func:`leaderboard._resolve_display_name` (15-min TTL cache, falls
    back to ``unknown (<id-prefix>)`` on a Discord NotFound).
    """
    name = await _resolve_display_name(client, user_id)
    return (
        f"{_STATE_HEADLINE_PREFIX}{characteristic}{_STATE_HEADLINE_SUFFIX}\n"
        f"{summary.rstrip()}\n"
        f"last changed: {_now_iso_minute()} by @{name} (id: {user_id})"
    )


# ---------------------------------------------------------------------------
# Best-effort Discord cleanup helpers
# ---------------------------------------------------------------------------


async def _best_effort_unpin(
    channel: discord.abc.Messageable,
    msg_id_str: str,
) -> None:
    """Try ``channel.fetch_message`` → ``msg.unpin``; swallow all
    ``discord.*`` errors.

    Does NOT delete — plan §6.4 P18 preserves audit history in
    scrollback. Failures here are bot-internal cleanup misfires (404 on
    already-deleted message, 403 on lost Manage Messages, transient
    5xx); operator gets the next sweep as a backstop.
    """
    try:
        msg = await channel.fetch_message(int(msg_id_str))
        await msg.unpin(reason="state_pin_cleanup")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def _best_effort_delete(msg: discord.Message) -> None:
    """Try ``msg.delete``; swallow all ``discord.*`` errors. Used for
    the post-pin cleanup when ``new_msg.pin`` raises and the just-posted
    (unpinned) message would otherwise be channel clutter."""
    try:
        await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def _collect_pins(
    channel: discord.abc.Messageable,
    characteristic: str | None,
    channel_id_str: str,
) -> tuple[list, bool]:
    """List the channel's pinned messages tolerantly across the
    discord.py 2.5 (returns list) and 2.6+ (returns async iterator)
    shapes.

    Returns ``(pinned_list, succeeded)``. ``succeeded=False`` means
    ``channel.pins()`` raised — callers distinguish "genuinely no pins"
    from "Discord 5xx" so the sweep can audit the latter (R2-M1) while
    the opportunistic dup-pin check can keep its swallow-and-continue
    posture (boot-time sweep is the backstop).

    ``characteristic`` is only used in the log line so the operator can
    correlate a sweep failure to the rotation that triggered it; pass
    ``None`` from the boot-time sweep.
    """
    try:
        result = channel.pins()
        if hasattr(result, "__aiter__"):
            return [m async for m in result], True
        return await result, True
    except discord.HTTPException as exc:
        logger.info(
            "channel.pins() failed for %s in %s: %s",
            characteristic or "<sweep>", channel_id_str, exc,
        )
        return [], False


async def _best_effort_unpin_and_delete(
    channel: discord.abc.Messageable,
    msg_id_str: str,
) -> None:
    """Try ``fetch → unpin → delete``; swallow all ``discord.*`` errors.

    Used for the step-g failure-cleanup path where the just-posted pin
    has NO DB pointer and would otherwise be a true orphan with no
    sweep-recovery key (plan §6.1 step g)."""
    try:
        msg = await channel.fetch_message(int(msg_id_str))
        try:
            await msg.unpin(reason="state_pin_lost_race")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


# ---------------------------------------------------------------------------
# Public surface — announce_state_change + the inner _do_announce body
# ---------------------------------------------------------------------------


async def announce_state_change(
    client: discord.Client,
    *,
    guild_id: str,
    org_id: str,
    characteristic: str,
    new_state_summary: str,
    changed_by_user_id: int,
) -> None:
    """Idempotent state-pin replace. Coalesces in-flight announces for
    the same (guild_id, characteristic) key — last-write-wins per plan
    §6.1.

    Fire-and-forget; NEVER raises to the caller (which is a slash-command
    handler's ``asyncio.create_task(...)`` post-confirmation) AFTER the
    entry-side validation. R3-N1: an unknown ``characteristic`` does
    raise ``ValueError`` synchronously at call time per PR6-L2 — call
    sites must pass one of the four whitelisted characteristic strings.

    Internal error handling:

      - Catches ``SQLAlchemyError`` + ``discord.HTTPException`` at the
        operations that can raise them.
      - Catches a final ``Exception`` sink with audit for defense-in-
        depth against bugs in the helpers themselves.
      - RE-RAISES ``asyncio.CancelledError`` per AGENTS.md so
        ``close()``'s ``asyncio.gather`` sees clean unwind.

    Entry-side defense (PR6-L2): unknown ``characteristic`` raises
    ``ValueError`` immediately. The four characteristic strings are
    primary keys in the schema, so a caller typo would land orphans the
    sweep can't recover.
    """
    if characteristic not in _KNOWN_CHARACTERISTICS:
        raise ValueError(
            f"unknown characteristic {characteristic!r}; "
            f"expected one of {sorted(_KNOWN_CHARACTERISTICS)}"
        )

    # Coalesce: cancel any prior pending announce for the same key.
    # PR4-H1: fire-and-forget cancel. We do NOT await the cancelled prior
    # via wait_for — CancelledError is BaseException since Python 3.8 and
    # source-disambiguation between "we got cancelled" vs "prior is
    # finishing its cancellation" is brittle. Accept occasional duplicate
    # pin pair from the prior completing its publish mid-rotation; the
    # opportunistic dup-pin sweep at step d.5 catches the visible case +
    # the boot-time sweep is the long-tail backstop.
    key = (guild_id, characteristic)
    prior_task = _pending_announcements.get(key)
    if prior_task is not None and not prior_task.done():
        prior_task.cancel()
    self_task = asyncio.current_task()
    _pending_announcements[key] = self_task
    _evict_pending_if_full()

    # R1-M1 + R3-L3: actor snapshot at announce entry. The snapshot is
    # acceptable because announces run in seconds; a gateway reconnect
    # mid-announce is rare enough that a stale bot-user-id is a fair
    # trade against re-resolving the actor on every audit emission.
    actor = _resolve_bot_actor(client)

    def _audit(action: str, detail: dict) -> None:
        """Best-effort audit-log write. PR4-L1: surface silent audit-
        write failures via logger so a DB outage that kills audits in
        this module shows up in journalctl rather than vanishing.

        R1-M1: actor resolved via :func:`_resolve_bot_actor` above so
        a reconnect-window AttributeError on ``client.user.id`` doesn't
        sink the audit row.
        """
        try:
            with get_db() as conn:
                log_audit(
                    conn,
                    actor=actor,
                    action=action,
                    org_id=org_id,
                    entity_id=None,
                    detail=detail,
                    source="sable-roles",
                )
        except Exception:  # noqa: BLE001 — audit failure must not crash announce
            logger.warning(
                "state_pin _audit failed for action=%s", action, exc_info=True,
            )

    try:
        await _do_announce(
            client, guild_id, org_id, characteristic,
            new_state_summary, changed_by_user_id, _audit,
        )
    except asyncio.CancelledError:
        # AGENTS.md: CancelledError MUST re-raise so the outer drain in
        # close() sees clean unwind. Never swallow.
        raise
    except Exception as exc:  # noqa: BLE001 — last-line defense
        # PR3-N2: catches bugs in _audit / _format_body / best-effort
        # helpers themselves. The inner SQLAlchemyError +
        # discord.HTTPException blocks handle the expected modes; this
        # is defense-in-depth. Never raise to the caller (slash command
        # response); audit + move on.
        _audit("fitcheck_state_pin_failed", {
            "step": "outer",
            "characteristic": characteristic,
            "exc_class": type(exc).__name__,
            "exc_msg": str(exc)[:200],
        })
    finally:
        # Self-identity-guarded pop (mirrors fitcheck_streak debounce).
        # If we were cancelled and replaced, the replacement task owns
        # this slot and we must not touch it.
        if _pending_announcements.get(key) is self_task:
            _pending_announcements.pop(key, None)


async def _do_announce(
    client: discord.Client,
    guild_id: str,
    org_id: str,
    characteristic: str,
    summary: str,
    user_id: int,
    _audit,
) -> None:
    """Eight-step lifecycle: see plan §6.1.

    (a) resolve OPS channel, (b) fetch channel, (c) per-channel lock,
    (d) read prior pointer, (d.5) opportunistic dup-pin sweep,
    (e) post new + pin, (f) unpin old (or channel-drift audit),
    (g) optimistic-lock upsert, (h) lock-loss self-cleanup.
    """
    # a) Resolve OPS channel from env.
    channel_id_str = OPS_CHANNELS.get(guild_id)
    if not channel_id_str:
        _audit("fitcheck_state_pin_no_ops_channel", {
            "characteristic": characteristic,
            "guild_id": guild_id,
        })
        return

    # b) Fetch the channel. R1-L3: guard the int() coercion so a typo
    # in OPS_CHANNELS_JSON doesn't surface as an unhandled ValueError
    # (which the outer Exception sink would catch, but with a less
    # informative audit row).
    try:
        channel_id_int = int(channel_id_str)
    except (TypeError, ValueError):
        _audit("fitcheck_state_pin_channel_unavailable", {
            "channel_id": channel_id_str,
            "characteristic": characteristic,
            "guild_id": guild_id,
            "reason": "invalid_channel_id",
        })
        return
    channel = client.get_channel(channel_id_int)
    if channel is None or not hasattr(channel, "send"):
        _audit("fitcheck_state_pin_channel_unavailable", {
            "channel_id": channel_id_str,
            "characteristic": characteristic,
            "guild_id": guild_id,
        })
        return

    # c) Per-channel lock — Discord pin-rate-limit serialization.
    lock = _channel_locks.setdefault(channel_id_str, asyncio.Lock())
    async with lock:
        # d) Read prior pointer for THIS (guild, characteristic).
        with get_db() as conn:
            prior = discord_state_pins.get_state_pin(
                conn, guild_id, characteristic,
            )

        # d.5) PR5-M2 + R3-N2 invariant: opportunistic dup-pin check
        # NEVER unpins the live prior pin (see the
        # `str(pinned.id) == prior["message_id"]: continue` guard in the
        # loop below) — only stragglers from a cancelled prior that
        # completed mid-rotation. Sweeping the live prior here would
        # leave the channel pin-less for the duration of step e (post +
        # pin) which is a worse UX than the rare orphan.
        # Opportunistic dup-pin check for THIS characteristic:
        # The PR4-H1 cancel-fire-and-forget design accepts that a cancelled
        # prior may complete its pin mid-rotation, leaving an orphan with
        # no DB pointer. Without this check the orphan sits in #sable-ops
        # until the next bot restart triggers the §6.4 sweep — on the
        # Hetzner VPS that's hours-to-days, which is unacceptable for
        # phase-1 smoke testing per plan §12. Cheap: one channel.pins()
        # call inside the lock we already hold, reuses the sweep parser.
        # R1-L4 + R2-M1: opportunistic-check path swallows HTTPException
        # silently — boot-time sweep is the long-tail backstop, and a
        # noisy audit row here would fan out on every announce during a
        # Discord blip. The sweep path (below) DOES audit the same
        # failure mode because there boot-time silence is the real risk.
        pinned_list, _ok = await _collect_pins(
            channel, characteristic, channel_id_str,
        )
        for pinned in pinned_list:
            pinned_char = _extract_characteristic_from_headline(
                pinned.content,
            )
            if pinned_char != characteristic:
                continue
            if prior is not None and str(pinned.id) == prior["message_id"]:
                continue  # the live current pin — leave alone
            await _best_effort_unpin(channel, str(pinned.id))
            _audit("fitcheck_state_pin_orphan_swept", {
                "channel_id": channel_id_str,
                "message_id": str(pinned.id),
                "characteristic": characteristic,
                "via": "opportunistic_pre_post",
            })

        # e) Post new message + pin it. PR5-M1: _format_body is `async def`
        # (it awaits _resolve_display_name); MUST await — without it
        # `body` would be a coroutine and channel.send would TypeError.
        body = await _format_body(client, characteristic, summary, user_id)
        try:
            new_msg = await channel.send(
                body,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            _audit("fitcheck_state_pin_failed", {
                "step": "send",
                "characteristic": characteristic,
                "exc_class": type(exc).__name__,
                "exc_msg": str(exc)[:200],
            })
            return
        try:
            await new_msg.pin(reason=f"stitzy state · {characteristic}")
        except discord.HTTPException as exc:
            _audit("fitcheck_state_pin_failed", {
                "step": "pin",
                "characteristic": characteristic,
                "new_message_id": str(new_msg.id),
                "exc_class": type(exc).__name__,
                "exc_msg": str(exc)[:200],
            })
            await _best_effort_delete(new_msg)
            return

        # f) Channel-drift check + unpin-old. PR2-M2: include
        # prior_message_id in the drift audit so operators can manually
        # recover the old-channel pin if they care.
        if prior is not None:
            if prior["channel_id"] != channel_id_str:
                _audit("fitcheck_state_pin_channel_moved", {
                    "prior_channel": prior["channel_id"],
                    "new_channel": channel_id_str,
                    "prior_message_id": prior["message_id"],
                    "characteristic": characteristic,
                })
            else:
                await _best_effort_unpin(channel, prior["message_id"])

        # g) Upsert the pointer (commit point). PR2-H2: SQLAlchemyError
        # here writes HIGH `fitcheck_state_pin_upsert_failed` + cleans
        # up the just-posted pin so we don't leave a phantom pin with
        # no DB pointer.
        now = _now_iso_seconds()
        try:
            with get_db() as conn:
                applied = discord_state_pins.upsert_state_pin(
                    conn,
                    guild_id, characteristic, channel_id_str,
                    str(new_msg.id), now,
                    expected_updated_at=(
                        prior["updated_at"] if prior else None
                    ),
                )
        except SQLAlchemyError as exc:
            _audit("fitcheck_state_pin_upsert_failed", {
                "step": "upsert",
                "characteristic": characteristic,
                "new_message_id": str(new_msg.id),
                "prior_message_id": (
                    prior["message_id"] if prior else None
                ),
                "exc_class": type(exc).__name__,
                "exc_msg": str(exc)[:200],
            })
            await _best_effort_unpin_and_delete(channel, str(new_msg.id))
            return

        # h) Optimistic-lock loss path. Another announce raced and won;
        # self-delete our just-posted message to avoid a duplicate pin.
        # Self-delete can also fail (Discord 5xx) — audit explicitly as
        # HIGH so an operator can surface the orphan.
        if not applied:
            try:
                await new_msg.unpin(reason="state_pin_lost_race")
                await new_msg.delete()
                _audit("fitcheck_state_pin_lost_race", {
                    "characteristic": characteristic,
                    "new_message_id": str(new_msg.id),
                })
            except discord.HTTPException as exc:
                _audit("fitcheck_state_pin_orphan_left_by_lock_loss", {
                    "characteristic": characteristic,
                    "new_message_id": str(new_msg.id),
                    "exc_class": type(exc).__name__,
                    "exc_msg": str(exc)[:200],
                })
            return

        _audit("fitcheck_state_pin_posted", {
            "characteristic": characteristic,
            "new_message_id": str(new_msg.id),
            "prior_message_id": prior["message_id"] if prior else None,
            "channel_id": channel_id_str,
        })


# ---------------------------------------------------------------------------
# Boot-time orphan-pin sweep
# ---------------------------------------------------------------------------


def _filter_unique_ops_channels() -> dict[str, str]:
    """R1-C1 + R2-L2 + R2-L3 guard. Discord channels are per-server, so
    a single channel_id can only belong to one guild. If the operator
    typo'd OPS_CHANNELS_JSON and two guild_id keys map to the same
    channel_id, the sweep + opportunistic dup-pin check (which scope
    by guild_id) would mis-classify the OTHER guild's live pin as an
    orphan + unpin it. Skip the duplicated entries + log loudly so the
    operator fixes the config.

    R2-L3: also skips blank / None channel ids at config-load time so
    operators see the misconfig in journalctl on first invocation
    rather than via a `fitcheck_state_pin_channel_unavailable` audit
    only the first time a state changes for that guild.

    Returns the filtered (validated) OPS_CHANNELS mapping.
    """
    seen_channels: dict[str, str] = {}
    safe: dict[str, str] = {}
    for guild_id, channel_id in OPS_CHANNELS.items():
        if not channel_id or not str(channel_id).strip():
            # R3-L4: show the actual offending value so the operator can
            # tell `""` apart from `0` from `null` without re-reading
            # SABLE_ROLES_OPS_CHANNELS_JSON.
            logger.error(
                "state_pin: OPS_CHANNELS misconfig — guild %s has an"
                " unusable channel_id %r. Skipping guild %s. Fix"
                " SABLE_ROLES_OPS_CHANNELS_JSON.",
                guild_id, channel_id, guild_id,
            )
            continue
        if channel_id in seen_channels:
            # R2-L2: name the surviving guild explicitly in the log so
            # the operator can reason about which guild stays wired.
            logger.error(
                "state_pin: OPS_CHANNELS misconfig — channel %s mapped"
                " by both guild %s (surviving) and guild %s (dropped)."
                " Fix SABLE_ROLES_OPS_CHANNELS_JSON to avoid"
                " cross-guild pin-stomp.",
                channel_id, seen_channels[channel_id], guild_id,
            )
            continue
        seen_channels[channel_id] = guild_id
        safe[guild_id] = channel_id
    return safe


async def sweep_orphan_pins(client: discord.Client) -> None:
    """One-shot boot-time sweep. For each configured ops channel, list
    pinned messages, identify Stitzy-state pins via the headline prefix,
    and unpin any whose ``(guild, characteristic)`` row in
    ``discord_state_pins`` doesn't match the message id.

    Idempotent across in-process re-invocations: the ``_sweep_done``
    guard makes a second call a no-op so direct invocation from a
    future feature (e.g. a `/sable-roles-status` slash command, an
    integration test that triggers the sweep without going through
    on_ready) doesn't re-fan ``channel.pins()`` + per-pin DB lookups
    (R2-M2). Tests reset the guard via the autouse ``state_pin_module``
    conftest fixture.

    Best-effort; never raises. Failure on one channel doesn't block
    others (R1-M2: each per-guild iteration is wrapped in its own
    try/except so a typo'd channel_id in OPS_CHANNELS doesn't abort
    the loop). Per plan §6.4 P18 sweeps UNPIN but never DELETE —
    scrollback preservation matters more than channel cleanliness for
    forensic recovery.

    R1-M4: acquires the per-channel ``_channel_locks`` entry around the
    per-channel block so a concurrent ``announce_state_change`` for the
    same channel doesn't race the sweep into double-unpinning the same
    message id.

    R1-L1: resolves ``org_id`` from ``GUILD_TO_ORG`` so the orphan-swept
    audit row is filterable per-org (matches the announce_state_change
    audit shape).
    """
    # R2-M2: idempotent guard. Set BEFORE the work so a re-entrant call
    # during the await of `_sweep_one_channel` (theoretical, but cheap
    # to guard) still sees the flag.
    if _sweep_done.get("done"):
        return
    _sweep_done["done"] = True

    summary = {"channels": 0, "orphans": 0, "errors": 0}
    actor = _resolve_bot_actor(client)
    safe_ops = _filter_unique_ops_channels()

    for guild_id_str, channel_id_str in safe_ops.items():
        summary["channels"] += 1
        try:
            await _sweep_one_channel(
                client, guild_id_str, channel_id_str, actor, summary,
            )
        except Exception as exc:  # noqa: BLE001 — never block other guilds
            logger.warning(
                "state_pin sweep: per-channel iteration failed for"
                " guild=%s channel=%s: %s",
                guild_id_str, channel_id_str, exc, exc_info=True,
            )
            summary["errors"] += 1

    logger.info(
        "state_pin sweep complete: channels=%d orphans=%d errors=%d",
        summary["channels"], summary["orphans"], summary["errors"],
    )


async def _sweep_one_channel(
    client: discord.Client,
    guild_id_str: str,
    channel_id_str: str,
    actor: str,
    summary: dict,
) -> None:
    """Per-channel sweep body. Acquires the per-channel lock so it can't
    race a concurrent ``_do_announce`` for the same channel. R1-M4 +
    R1-L1 + R1-M2.
    """
    try:
        channel_id_int = int(channel_id_str)
    except (TypeError, ValueError):
        logger.info(
            "state_pin sweep: channel_id %r invalid for guild %s",
            channel_id_str, guild_id_str,
        )
        summary["errors"] += 1
        return
    channel = client.get_channel(channel_id_int)
    if channel is None or not hasattr(channel, "pins"):
        logger.info(
            "state_pin sweep: ops channel %s unavailable for guild %s",
            channel_id_str, guild_id_str,
        )
        summary["errors"] += 1
        return

    org_id = GUILD_TO_ORG.get(guild_id_str)

    lock = _channel_locks.setdefault(channel_id_str, asyncio.Lock())
    async with lock:
        pinned_list, ok = await _collect_pins(channel, None, channel_id_str)
        if not ok:
            # R2-M1: distinguish "no pins" from "HTTPException" so a
            # silent boot-time sweep failure is operator-visible in
            # audit_log + journalctl, not just journalctl.
            summary["errors"] += 1
            try:
                with get_db() as conn:
                    log_audit(
                        conn,
                        actor=actor,
                        action="fitcheck_state_pin_sweep_pins_unavailable",
                        org_id=org_id,
                        entity_id=None,
                        detail={
                            "channel_id": channel_id_str,
                            "guild_id": guild_id_str,
                        },
                        source="sable-roles",
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "state_pin sweep: pins-unavailable audit failed for"
                    " channel=%s", channel_id_str, exc_info=True,
                )
            return
        if not pinned_list:
            # Genuinely no pins. Nothing to sweep.
            return
        for pinned in pinned_list:
            char = _extract_characteristic_from_headline(pinned.content)
            if char is None:
                continue  # not a Stitzy state pin
            try:
                with get_db() as conn:
                    row = discord_state_pins.get_state_pin(
                        conn, guild_id_str, char,
                    )
            except SQLAlchemyError as exc:
                logger.warning(
                    "state_pin sweep: get_state_pin failed for"
                    " (%s, %s): %s",
                    guild_id_str, char, exc,
                )
                summary["errors"] += 1
                continue

            if row is not None and str(pinned.id) == row["message_id"]:
                continue  # live pin — leave alone

            await _best_effort_unpin(channel, str(pinned.id))
            summary["orphans"] += 1
            try:
                with get_db() as conn:
                    log_audit(
                        conn,
                        actor=actor,
                        action="fitcheck_state_pin_orphan_swept",
                        org_id=org_id,
                        entity_id=None,
                        detail={
                            "channel_id": channel_id_str,
                            "guild_id": guild_id_str,
                            "message_id": str(pinned.id),
                            "characteristic": char,
                            "via": "restart_sweep",
                        },
                        source="sable-roles",
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "state_pin sweep: orphan-audit failed for"
                    " channel=%s message=%s", channel_id_str, pinned.id,
                    exc_info=True,
                )


# ---------------------------------------------------------------------------
# Registration + close-drain
# ---------------------------------------------------------------------------


def register(client: discord.Client) -> None:
    """Wire the boot-time orphan sweep into the first on_ready of the
    process.

    R1-H1 + R1-H3: the sweep is one-shot per process boot. We can't host
    it in setup_hook because the gateway-side channel cache isn't
    populated yet there (client.get_channel would return None for every
    ops channel). Instead we compose the wrapper onto on_ready — which
    AGENTS.md notes re-fires on every gateway reconnect — and gate the
    sweep body with ``_sweep_done`` so subsequent reconnects only re-run
    the upstream ``existing_on_ready`` and skip a redundant ``channel.pins()``
    fan-out + per-pin DB lookup.

    The state-pin module does NOT compose any reaction or message
    handlers — it's slash-command-triggered only. So no other
    @client.event bindings.
    """
    global _client
    _client = client

    existing_on_ready = getattr(client, "on_ready", None)

    @client.event
    async def on_ready():
        if existing_on_ready is not None:
            await existing_on_ready()
        try:
            # `sweep_orphan_pins` is idempotent via its internal
            # `_sweep_done` guard (R2-M2), so the on_ready reconnect-
            # safety contract is satisfied even though we re-enter here
            # on every reconnect.
            await sweep_orphan_pins(client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("state_pin sweep raised: %s", exc, exc_info=True)


async def close() -> None:
    """Cancel + drain in-flight announce tasks. Called from
    ``SableRolesClient.close()`` BEFORE ``super().close()`` tears down
    the websocket. Mirrors :func:`fitcheck_streak.close` +
    :func:`reveal_pipeline.close` precedents.
    """
    tasks = list(_pending_announcements.values())
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending_announcements.clear()
