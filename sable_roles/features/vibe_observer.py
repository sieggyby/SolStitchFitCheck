"""R10 + R11 — Vibe observation listener + rollup/GC crons + inference cron.

Per plan §7.1 POST-AUDIT (R10):

  (a) on_message listener — every non-bot message in OBSERVATION_CHANNELS
      gets an INSERT into discord_message_observations. Blocklisted users
      skipped at handler entry (re-read per call). Empty per-guild channel
      list = bot observes ALL readable text channels in that guild.

  (b) on_raw_reaction_add — when a user adds a reaction to a message in
      OBSERVATION_CHANNELS, increment that emoji's count in the existing
      observation row's reactions_given_json. SP `merge_reaction_given`
      is a no-op when no observation row exists (reactions on non-observed
      messages are silently dropped — observation-rooted).

  (c) Daily rollup cron — reads recent rows from discord_message_observations
      (last VIBE_OBSERVATION_WINDOW_DAYS), groups by (guild_id, user_id),
      and UPSERTs into discord_user_observations. Cost: free.

  (d) Nightly GC — drops raw rows older than VIBE_OBSERVATION_WINDOW_DAYS + 7.

Per plan §7.2 + §7.3 POST-AUDIT (R11):

  (e) Weekly inference cron — for each guild with personalize_mode_on=True
      AND check_budget OK, calls Anthropic with VIBE_INFER_SYSTEM_PROMPT
      against each user's rollup. Strict JSON output → SP
      validate_inferred_vibe (rejects on schema/length/imperative-guard)
      → upsert_vibe + audit + cost row.

VIBE_OBSERVATION_ENABLED env kill switch short-circuits the listeners,
the rollup pass, the GC pass, the inference pass, AND start_tasks (so
nothing starts in the first place) — flip and restart to silence the
entire observation + inference pipeline. Per-guild personalize_mode_on
gates the inference path independently (observation keeps running so
the warmup window is still building when a guild toggles on).
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import tasks

from anthropic import AsyncAnthropic, BadRequestError

from sable_platform.db import (
    discord_guild_config,
    discord_roast,
    discord_user_vibes,
)
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db
from sable_platform.db.cost import check_budget, log_cost

from sable_roles.config import (
    GUILD_TO_ORG,
    OBSERVATION_CHANNELS,
    VIBE_INFERENCE_INTERVAL_DAYS,
    VIBE_INFERENCE_MODEL,
    VIBE_OBSERVATION_ENABLED,
    VIBE_OBSERVATION_WINDOW_DAYS,
)
from sable_roles.prompts.vibe_infer_system import VIBE_INFER_SYSTEM_PROMPT

logger = logging.getLogger("sable_roles.vibe_observer")

# Content truncation cap per plan §3.7. Keeps raw rows bounded so a
# pasted novel doesn't blow up the table.
_OBSERVATION_CONTENT_MAX_CHARS = 500

# Sample size for the rollup body. Plan §7.1 (b) "up to 20 sampled
# `content_truncated` rows (random sample)".
_ROLLUP_SAMPLE_SIZE = 20

# GC headroom past the observation window — rollups read up to
# WINDOW_DAYS old, so GC of WINDOW + 7 ensures the rollup never reads
# half-deleted history mid-pass.
_GC_HEADROOM_DAYS = 7


def _channel_in_scope(guild_id: str, channel_id: int) -> bool:
    """True iff this guild observes this channel.

    Plan §7.1: empty list / missing guild = observe ALL readable text
    channels (default permissive — operator can restrict later via env).
    """
    allowlist = OBSERVATION_CHANNELS.get(guild_id) or []
    if not allowlist:
        return True
    return str(channel_id) in {str(cid) for cid in allowlist}


def _is_text_channel(channel: Any) -> bool:
    """Filter out voice / category / thread channels. Bot only watches
    TextChannel writes; threads + voice are out of vibe scope for V1.
    """
    return isinstance(channel, discord.TextChannel)


async def _observe_message(message: discord.Message) -> None:
    """Per-message observation entry. Called from the composed on_message
    listener. Drops bot messages, DM-context messages, out-of-scope
    channels, and blocklisted authors.

    Audit-free by design — this is high-volume raw capture. R11's
    inference loop is what writes the personalization audit trail.
    """
    if not VIBE_OBSERVATION_ENABLED:
        return
    if message.author is None or message.author.bot:
        return
    if message.guild is None:
        return
    guild_id = str(message.guild.id)
    if guild_id not in GUILD_TO_ORG:
        return
    if not _is_text_channel(message.channel):
        return
    if not _channel_in_scope(guild_id, message.channel.id):
        return

    user_id = str(message.author.id)
    content = (message.content or "")[:_OBSERVATION_CONTENT_MAX_CHARS]
    posted_at = message.created_at.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        with get_db() as conn:
            if discord_roast.is_blocklisted(conn, guild_id, user_id):
                return
            discord_user_vibes.insert_message_observation(
                conn,
                guild_id=guild_id,
                channel_id=str(message.channel.id),
                message_id=str(message.id),
                user_id=user_id,
                content_truncated=content if content else None,
                posted_at=posted_at,
            )
    except Exception as exc:  # noqa: BLE001 — high-volume listener; log + drop
        logger.warning(
            "vibe observation insert failed for msg %s: %s",
            message.id, exc,
        )


async def _observe_reaction(
    payload: discord.RawReactionActionEvent,
    *,
    client: discord.Client,
) -> None:
    """Per-reaction observation entry. Called from the composed
    on_raw_reaction_add listener. Increments the emoji's count on the
    EXISTING observation row keyed on (guild_id, message_id) — i.e., the
    poster's observation row tracks the reactions their post attracted.

    Silently no-ops when (a) the message wasn't observed, (b) the
    reactor is the bot itself, (c) the reactor is blocklisted, or
    (d) the channel is out of observation scope.
    """
    if not VIBE_OBSERVATION_ENABLED:
        return
    if payload.guild_id is None:
        return
    guild_id = str(payload.guild_id)
    if guild_id not in GUILD_TO_ORG:
        return
    if client.user is not None and payload.user_id == client.user.id:
        return
    if not _channel_in_scope(guild_id, payload.channel_id):
        return
    # Mirror _observe_message's text-channel filter: reactions on threads /
    # voice / category channels can't have an observation row to merge into
    # anyway (the message-side filter blocks them), so this is just symmetry
    # + defense if observations were ever seeded from another source.
    channel = client.get_channel(payload.channel_id)
    if channel is not None and not _is_text_channel(channel):
        return
    try:
        with get_db() as conn:
            if discord_roast.is_blocklisted(
                conn, guild_id, str(payload.user_id)
            ):
                return
            discord_user_vibes.merge_reaction_given(
                conn,
                guild_id=guild_id,
                message_id=str(payload.message_id),
                emoji=str(payload.emoji),
            )
    except Exception as exc:  # noqa: BLE001 — listener; log + drop
        logger.warning(
            "vibe reaction merge failed for msg %s: %s",
            payload.message_id, exc,
        )


async def _rollup_pass() -> None:
    """One pass of the daily rollup cron.

    For each guild in GUILD_TO_ORG, enumerates distinct users with raw
    observations in the last VIBE_OBSERVATION_WINDOW_DAYS, computes a
    per-user rollup, and appends a discord_user_observations row.

    Idempotent in the sense that re-running on the same day appends a
    fresh rollup row — old rows are preserved (append-only by design
    per SP convention; the inference cron reads
    :func:`get_latest_observation` to find the freshest snapshot).
    """
    if not VIBE_OBSERVATION_ENABLED:
        return
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for guild_id in list(GUILD_TO_ORG.keys()):
        with get_db() as conn:
            user_ids = discord_user_vibes.list_recent_observation_users(
                conn, guild_id, within_days=VIBE_OBSERVATION_WINDOW_DAYS,
            )
        for user_id in user_ids:
            try:
                with get_db() as conn:
                    rows = discord_user_vibes.list_recent_message_observations(
                        conn, guild_id, user_id,
                        within_days=VIBE_OBSERVATION_WINDOW_DAYS,
                    )
                    if not rows:
                        continue
                    rollup = _summarize_observations(rows)
                    discord_user_vibes.insert_observation_rollup(
                        conn,
                        guild_id=guild_id,
                        user_id=user_id,
                        window_start=rollup["window_start"],
                        window_end=now_iso,
                        message_count=rollup["message_count"],
                        sample_messages=rollup["sample_messages"],
                        reaction_emojis_given=rollup["reaction_emojis_given"],
                        channels_active_in=rollup["channels_active_in"],
                    )
            except Exception as exc:  # noqa: BLE001 — one bad user shouldn't kill the pass
                logger.warning(
                    "vibe rollup failed for guild %s user %s: %s",
                    guild_id, user_id, exc,
                )


def _summarize_observations(rows: list[dict]) -> dict:
    """Pure helper: collapse raw message-observation rows into a rollup
    dict. Sample is uniform-random (bounded to _ROLLUP_SAMPLE_SIZE) so
    the rollup snapshots message style without bias to the freshest N.
    """
    if not rows:
        return {
            "window_start": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "message_count": 0,
            "sample_messages": [],
            "reaction_emojis_given": {},
            "channels_active_in": [],
        }
    # rows arrive ORDER BY posted_at ASC per SP helper.
    window_start = rows[0]["posted_at"]
    message_count = len(rows)
    contents = [r["content_truncated"] for r in rows if r.get("content_truncated")]
    sample = (
        random.sample(contents, _ROLLUP_SAMPLE_SIZE)
        if len(contents) > _ROLLUP_SAMPLE_SIZE
        else list(contents)
    )
    # Merge reactions_given_json across rows. Each row holds emoji-counts
    # for reactions THIS specific message attracted; the rollup totals
    # them across the window.
    import json
    emoji_total: dict[str, int] = {}
    for r in rows:
        rj = r.get("reactions_given_json")
        if not rj:
            continue
        try:
            parsed = json.loads(rj)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        for k, v in parsed.items():
            try:
                emoji_total[str(k)] = emoji_total.get(str(k), 0) + int(v)
            except (TypeError, ValueError):
                continue
    channels = sorted({str(r["channel_id"]) for r in rows if r.get("channel_id")})
    return {
        "window_start": window_start,
        "message_count": message_count,
        "sample_messages": sample,
        "reaction_emojis_given": emoji_total or None,
        "channels_active_in": channels,
    }


async def _gc_pass() -> None:
    """One pass of the nightly raw-observation GC.

    Drops `discord_message_observations` rows older than
    `VIBE_OBSERVATION_WINDOW_DAYS + _GC_HEADROOM_DAYS`. The headroom
    keeps the rollup cron safe from reading half-deleted history.

    Honors VIBE_OBSERVATION_ENABLED (off → no GC; data is preserved
    in case the operator re-enables the pipeline and wants the history).
    """
    if not VIBE_OBSERVATION_ENABLED:
        return
    age = VIBE_OBSERVATION_WINDOW_DAYS + _GC_HEADROOM_DAYS
    try:
        with get_db() as conn:
            n = discord_user_vibes.gc_old_observations(
                conn, older_than_days=age,
            )
        if n:
            logger.info(
                "vibe observer GC dropped %d raw observation rows older than %d days",
                n, age,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("vibe observer GC failed: %s", exc)


# ---------------------------------------------------------------------------
# R11 — Vibe inference cron + Anthropic call + validate + upsert
# ---------------------------------------------------------------------------

# Minimum observation rows for inference. Plan §7.2 + §7.3 ("If user
# has < 5 messages of data, output insufficient_data"). Defensive cap
# at the SP side too via the helper, but pre-filtering here saves the
# API call entirely for clearly-low-data users.
_INFERENCE_MIN_MESSAGES = 5

# Anthropic SDK client — lazy, single instance (same pattern as
# burn_me._client). Tests monkeypatch `_anthropic_client` directly.
_anthropic_client: AsyncAnthropic | None = None


def _client() -> AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic()
    return _anthropic_client


def _compute_inference_cost(input_tokens: int, output_tokens: int) -> float:
    """USD per inference call. Mirrors burn_me._compute_cost's Sonnet
    rate (no caching expected on per-user inference — different prompt
    body per user) for VIBE_INFERENCE_MODEL == claude-sonnet-4-6.
    """
    if VIBE_INFERENCE_MODEL.startswith("claude-sonnet"):
        return (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000
    if VIBE_INFERENCE_MODEL.startswith("claude-haiku"):
        return (input_tokens * 0.80 + output_tokens * 4.0) / 1_000_000
    return 0.0


def _render_observation_for_inference(observation: dict) -> str:
    """Pure helper: format a rollup row into the user-message payload."""
    import json
    sample = observation.get("sample_messages_json")
    reactions = observation.get("reaction_emojis_given_json")
    channels = observation.get("channels_active_in_json")
    parts = [
        f"message_count: {observation.get('message_count', 0)}",
        f"window: {observation.get('window_start')} to "
        f"{observation.get('window_end')}",
    ]
    if sample:
        try:
            samples = json.loads(sample)
        except (TypeError, ValueError):
            samples = []
        if samples:
            parts.append("sample_messages:")
            for s in samples[:20]:
                parts.append(f"  - {s}")
    if reactions:
        # Re-parse + re-dump with ensure_ascii=False so emoji render as
        # literals (e.g. 🔥 rather than 🔥). The model handles
        # both but literals keep the prompt human-readable.
        try:
            r_parsed = json.loads(reactions)
            parts.append(
                f"reactions_attracted: {json.dumps(r_parsed, ensure_ascii=False, sort_keys=True)}"
            )
        except (TypeError, ValueError):
            parts.append(f"reactions_attracted: {reactions}")
    if channels:
        try:
            c_parsed = json.loads(channels)
            parts.append(
                f"channels: {json.dumps(c_parsed, ensure_ascii=False)}"
            )
        except (TypeError, ValueError):
            parts.append(f"channels: {channels}")
    return "\n".join(parts)


async def _infer_one_user(
    *,
    org_id: str,
    guild_id: str,
    user_id: str,
) -> bool:
    """Run one vibe-inference round for (guild_id, user_id).

    Returns True iff a fresh vibe row was UPSERTed. Skips on:
      * blocklisted user (consent gate; no LLM cost)
      * insufficient observation rows (< _INFERENCE_MIN_MESSAGES)
      * Anthropic API failure (logged, no audit beyond cost-skip)
      * JSON parse / schema / imperative-guard validation failure
        (per plan §7.3 BLOCKER #6 — don't write garbage)

    Writes a `sable_roles_vibe_infer` cost row on every API success
    (refused or accepted), and a `fitcheck_vibe_inferred` audit row on
    every successful upsert.
    """
    with get_db() as conn:
        if discord_roast.is_blocklisted(conn, guild_id, user_id):
            return False
        observation = discord_user_vibes.get_latest_observation(
            conn, guild_id, user_id
        )
    if observation is None:
        return False
    if int(observation.get("message_count", 0)) < _INFERENCE_MIN_MESSAGES:
        return False

    user_payload = _render_observation_for_inference(observation)
    try:
        response = await _client().messages.create(
            model=VIBE_INFERENCE_MODEL,
            max_tokens=400,
            system=[
                {
                    "type": "text",
                    "text": VIBE_INFER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_payload}],
        )
    except BadRequestError as e:
        logger.warning(
            "vibe inference bad-request for %s/%s: %s",
            guild_id, user_id, e,
        )
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "vibe inference call failed for %s/%s",
            guild_id, user_id, exc_info=e,
        )
        return False

    in_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    cost = _compute_inference_cost(in_tok, out_tok)
    raw_text = "".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()
    validated = discord_user_vibes.validate_inferred_vibe(raw_text)

    with get_db() as conn:
        log_cost(
            conn,
            org_id=org_id,
            call_type="sable_roles_vibe_infer",
            cost_usd=cost,
            model=VIBE_INFERENCE_MODEL,
            input_tokens=in_tok,
            output_tokens=out_tok,
            call_status="success" if validated else "refused",
        )
        if validated is None:
            return False
        vibe_id = discord_user_vibes.upsert_vibe(
            conn,
            guild_id=guild_id,
            user_id=user_id,
            fields=validated,
            source_observation_id=observation.get("id"),
        )
        log_audit(
            conn,
            actor="discord:bot:auto",
            action="fitcheck_vibe_inferred",
            org_id=org_id,
            entity_id=str(vibe_id),
            detail={
                "guild_id": guild_id,
                "user_id": user_id,
                "vibe_id": vibe_id,
                "source_observation_id": observation.get("id"),
                "cost_usd": cost,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            },
            source="sable-roles",
        )
    return True


async def _inference_pass() -> None:
    """One pass of the weekly vibe-inference cron.

    Gates (in order):
      * Kill switch VIBE_OBSERVATION_ENABLED off → skip entire pass
      * For each guild: skip if personalize_mode_on is False
      * For each guild: check_budget(org_id) — BUDGET_EXCEEDED → skip
        the guild but continue the loop for other guilds
      * For each user with a recent rollup: call _infer_one_user

    Vibes UPSERT into discord_user_vibes (append-only); the rendered
    block is what R11's generate_roast injection consumes.
    """
    if not VIBE_OBSERVATION_ENABLED:
        return
    for guild_id, org_id in GUILD_TO_ORG.items():
        user_ids: list[str] = []
        try:
            with get_db() as conn:
                cfg = discord_guild_config.get_config(conn, guild_id)
                if not cfg.get("personalize_mode_on"):
                    continue
                try:
                    check_budget(conn, org_id)
                except Exception as exc:  # noqa: BLE001 — BUDGET_EXCEEDED or other
                    logger.info(
                        "vibe inference budget gate tripped for guild %s: %s",
                        guild_id, exc,
                    )
                    continue
                user_ids = discord_user_vibes.list_recent_observation_users(
                    conn, guild_id, within_days=VIBE_OBSERVATION_WINDOW_DAYS,
                )
        except Exception as exc:  # noqa: BLE001 — one bad guild shouldn't kill the pass
            logger.warning(
                "vibe inference setup failed for guild %s: %s",
                guild_id, exc,
            )
            continue
        for user_id in user_ids:
            try:
                await _infer_one_user(
                    org_id=org_id, guild_id=guild_id, user_id=user_id,
                )
            except Exception as exc:  # noqa: BLE001 — one user shouldn't kill the pass
                logger.warning(
                    "vibe inference failed for guild %s user %s: %s",
                    guild_id, user_id, exc,
                )


# discord.ext.tasks.loop instances. Defined at module scope so
# start_tasks() can call .start() without leaking implicit state.
@tasks.loop(hours=24)
async def _rollup_loop() -> None:
    await _rollup_pass()


@tasks.loop(hours=24)
async def _gc_loop() -> None:
    await _gc_pass()


@tasks.loop(hours=24 * max(1, VIBE_INFERENCE_INTERVAL_DAYS))
async def _inference_loop() -> None:
    await _inference_pass()


def start_tasks() -> None:
    """Start the rollup + GC + inference background loops. Called from
    setup_hook AFTER register() so listeners are live by the time the
    first cron tick fires. Honors VIBE_OBSERVATION_ENABLED — when off,
    NO loops are started (sixth surface of the kill switch).
    """
    if not VIBE_OBSERVATION_ENABLED:
        logger.info(
            "VIBE_OBSERVATION_ENABLED=false; observer cron loops not started"
        )
        return
    if not _rollup_loop.is_running():
        _rollup_loop.start()
    if not _gc_loop.is_running():
        _gc_loop.start()
    if not _inference_loop.is_running():
        _inference_loop.start()


def stop_tasks() -> None:
    """Cancel the rollup + GC + inference loops. Called from client.close()
    drain so the bot can shut down cleanly.
    """
    if _rollup_loop.is_running():
        _rollup_loop.cancel()
    if _gc_loop.is_running():
        _gc_loop.cancel()
    if _inference_loop.is_running():
        _inference_loop.cancel()


def register(client: discord.Client) -> None:
    """Wire the on_message + on_raw_reaction_add observation hooks.

    Composes with any pre-existing handlers (fitcheck_streak / roast)
    via the same wrap-existing pattern roast.register uses. MUST be
    called AFTER fitcheck_streak.register and roast.register so its
    wrappers preserve theirs.
    """
    existing_on_message = getattr(client, "on_message", None)
    existing_on_reaction = getattr(client, "on_raw_reaction_add", None)

    @client.event
    async def on_message(message: discord.Message):
        if existing_on_message is not None:
            await existing_on_message(message)
        await _observe_message(message)

    @client.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        if existing_on_reaction is not None:
            await existing_on_reaction(payload)
        await _observe_reaction(payload, client=client)
