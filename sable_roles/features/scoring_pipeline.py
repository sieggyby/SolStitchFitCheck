"""Pass B: Sonnet 4.6 vision scoring pipeline + /scoring slash command.

Design refs:
  - ~/Projects/SolStitch/internal/fitcheck_scored_mode_plan.md sec 5-8
  - claude-api memory: prompt caching MANDATORY on rubric system prompt

Surface:

* `maybe_score_fit(message, org_id, guild_id, client)` — coroutine invoked
  from fitcheck_streak.on_message image branch tail. NO-OP when scoring
  state is 'off'. Otherwise: read image bytes, call vision API once, retry
  once after 5s on transient failure, then either upsert_score_success
  or record_score_failure. Curve basis decided from pool size (absolute
  below cold_start_min_pool, rolling_30d at/above).
* `register_commands(tree, client)` — registers /scoring with two
  subcommands (status, set). Manage Guild permission gate.

The state='off' check happens FIRST in maybe_score_fit, before any API
call. There is no code path that scores when state is 'off' — that's the
safety floor.

Failure modes:
  - state != 'off' but no image bytes → silent no-op (Pass A handles the
    audit row via maybe_record_phash; no point double-logging here).
  - API call fails twice → record_score_failure, audit fitcheck_score_failed,
    return.
  - JSON parse fails → treated as a failure (no partial score row).

Cost note: prompt caching is set on the system block per
~/Projects/SolStitch/internal/fitcheck_scored_mode_plan.md sec 5.1 — the
rubric is static across all calls so cache hits drive cost ~$0.008/fit.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from anthropic import APIError, AsyncAnthropic, BadRequestError
from discord import app_commands
from PIL import Image

from sable_platform.db import (
    discord_fitcheck_scores,
    discord_guild_config,
    discord_scoring_config,
)
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db
from sable_platform.db.cost import log_cost

from sable_roles.config import (
    GUILD_TO_ORG,
    SCORED_MODE_ENABLED,
    SCORING_MODEL,
    SCORING_PROMPT_VERSION,
    SCORING_RETRY_DELAY_SECONDS,
)
from sable_roles.features.fitcheck_streak import is_image
from sable_roles.features.image_hashing import compute_phash_from_bytes
from sable_roles.prompts.scoring_system import SYSTEM_PROMPT

logger = logging.getLogger("sable_roles.scoring_pipeline")

# Sentinel for "no client cached yet" — set on first lazy-init.
_anthropic_client: AsyncAnthropic | None = None


def _client() -> AsyncAnthropic:
    """Lazy-init async Anthropic client. Reused across calls so the SDK's
    underlying httpx pool stays warm. Tests monkeypatch the module-level
    `_anthropic_client` directly to bypass.
    """
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic()
    return _anthropic_client


# 10 MB cap mirrors Anthropic vision endpoint upper bound.
_IMAGE_BYTE_CAP = 10 * 1024 * 1024
_MEDIA_TYPE_MAP = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",  # WEBP starts with RIFF....WEBP
}


def _detect_media_type(image_bytes: bytes) -> str | None:
    """Magic-byte detection for the 4 Anthropic-accepted vision formats.

    Mirrors the burn_me.py approach — content_type from Discord can be
    wrong (e.g. declares image/webp on PNG bytes). Anthropic rejects
    mismatched media_type, so this is the truth.
    """
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:32]:
        return "image/webp"
    return None


def _first_image_attachment(
    attachments,
) -> discord.Attachment | None:
    for att in attachments:
        if is_image(att):
            return att
    return None


async def _read_image_bytes(att: discord.Attachment) -> bytes | None:
    if att.size and att.size > _IMAGE_BYTE_CAP:
        logger.info(
            "scoring skip: attachment %s is %s bytes (cap %s)",
            att.filename,
            att.size,
            _IMAGE_BYTE_CAP,
        )
        return None
    try:
        data = await att.read()
    except (discord.HTTPException, discord.NotFound) as exc:
        logger.warning("scoring skip: attachment read failed: %s", exc)
        return None
    if len(data) > _IMAGE_BYTE_CAP:
        logger.info("scoring skip: attachment read %s bytes exceeds cap", len(data))
        return None
    return data


def _now_iso_seconds() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _resolve_bot_actor(client: discord.Client | None) -> str:
    if client is not None and client.user is not None:
        return f"discord:bot:{client.user.id}"
    return "discord:bot:unknown"


# Stripped-down JSON-fence remover — Sonnet sometimes wraps despite the
# rubric's "no markdown" instruction. We tolerate it.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_response_text(text: str) -> dict | None:
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _validate_response(parsed: dict) -> str | None:
    """Return None if the response is structurally valid, else a short reason.

    Defends against the worst-case where Sonnet ignores the format and
    returns nonsense — we'd rather record a failure than write a half-row.
    """
    axes = parsed.get("axis_scores")
    if not isinstance(axes, dict):
        return "axis_scores not dict"
    for axis in ("cohesion", "execution", "concept", "catch"):
        val = axes.get(axis)
        if not isinstance(val, int):
            return f"axis_scores.{axis} not int"
        if axis == "catch":
            if val < 3 or val > 10:
                return f"axis_scores.catch out of [3, 10]"
        else:
            if val < 1 or val > 10:
                return f"axis_scores.{axis} out of [1, 10]"
    rats = parsed.get("axis_rationales")
    if not isinstance(rats, dict):
        return "axis_rationales not dict"
    for axis in ("cohesion", "execution", "concept", "catch"):
        if not isinstance(rats.get(axis), str):
            return f"axis_rationales.{axis} not str"
    # Optional fields — accept None or correct type.
    catch = parsed.get("catch_detected", None)
    if catch is not None and not isinstance(catch, str):
        return "catch_detected not str or null"
    catch_class = parsed.get("catch_naming_class", None)
    if catch_class is not None and catch_class not in ("family_only", "specific_piece"):
        return "catch_naming_class invalid"
    desc = parsed.get("description", None)
    if desc is not None and not isinstance(desc, str):
        return "description not str"
    conf = parsed.get("confidence", None)
    if conf is not None and not isinstance(conf, (int, float)):
        return "confidence not number"
    return None


def _compute_percentile_from_pool(
    pool: list[int],
    raw_total: int,
) -> float:
    """Percentile (1-100) of raw_total in pool.

    "Percentile of X" = fraction of pool strictly less than X PLUS half
    the fraction equal to X (mid-rank convention), scaled to 1-100. Floors
    at 1, ceils at 100 to match the design's "headline 1-100" surface.

    Empty pool falls back to absolute mapping (raw_total / 40 * 100).
    """
    if not pool:
        absolute = (raw_total / 40.0) * 100.0
        return max(1.0, min(100.0, absolute))
    n = len(pool)
    below = sum(1 for v in pool if v < raw_total)
    equal = sum(1 for v in pool if v == raw_total)
    pct = ((below + 0.5 * equal) / n) * 100.0
    return max(1.0, min(100.0, pct))


def _compute_percentile_absolute(raw_total: int) -> float:
    """Cold-start: raw_total / 40 -> percentile 1-100 directly."""
    absolute = (raw_total / 40.0) * 100.0
    return max(1.0, min(100.0, absolute))


async def _call_vision_with_retry(
    *,
    model_id: str,
    image_b64: str,
    media_type: str,
    context_text: str,
) -> tuple[Any, str] | tuple[None, str]:
    """Call Anthropic vision. Retry once after SCORING_RETRY_DELAY_SECONDS
    on transient APIError. Returns (response, "") on success or
    (None, error_reason) on terminal failure.

    BadRequestError is terminal (no retry) — usually media type or content
    policy. Any other exception is also terminal on the second attempt.
    """
    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_b64,
            },
        },
        {"type": "text", "text": context_text},
    ]
    last_error_reason = "unknown"
    for attempt in (1, 2):
        try:
            response = await _client().messages.create(
                model=model_id,
                max_tokens=800,
                temperature=0,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            return response, ""
        except BadRequestError as exc:
            return None, f"bad_request:{exc}"
        except APIError as exc:
            last_error_reason = f"api_error:{type(exc).__name__}:{exc}"
            if attempt == 1:
                await asyncio.sleep(SCORING_RETRY_DELAY_SECONDS)
                continue
            return None, last_error_reason
        except Exception as exc:  # noqa: BLE001
            last_error_reason = f"exception:{type(exc).__name__}"
            if attempt == 1:
                await asyncio.sleep(SCORING_RETRY_DELAY_SECONDS)
                continue
            return None, last_error_reason
    return None, last_error_reason


def _compute_cost_per_million(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> float:
    """Best-effort cost USD for Sonnet 4.6. Mirrors burn_me._compute_cost.
    Rates are deliberately conservative; precision isn't load-bearing —
    log_cost stores it for budget tracking, not invoicing.
    """
    if model_id.startswith("claude-sonnet"):
        in_per_m = 3.0
        out_per_m = 15.0
        cache_write_per_m = 3.75
        cache_read_per_m = 0.30
    else:
        in_per_m = 3.0
        out_per_m = 15.0
        cache_write_per_m = 3.75
        cache_read_per_m = 0.30
    fresh_input = max(0, input_tokens - cache_read_tokens - cache_creation_tokens)
    return (
        (fresh_input / 1_000_000.0) * in_per_m
        + (output_tokens / 1_000_000.0) * out_per_m
        + (cache_creation_tokens / 1_000_000.0) * cache_write_per_m
        + (cache_read_tokens / 1_000_000.0) * cache_read_per_m
    )


async def maybe_score_fit(
    *,
    message: discord.Message,
    org_id: str,
    guild_id: str,
    client: discord.Client | None,
) -> None:
    """Score a fit if scoring state is 'silent' or 'revealed'. NO-OP on 'off'.

    Called from fitcheck_streak.on_message image branch tail via
    asyncio.create_task — must NEVER raise; all errors swallowed + logged.

    Hard env kill switch (SCORED_MODE_ENABLED=false) bypasses this entirely
    before any DB read or API call.
    """
    if not SCORED_MODE_ENABLED:
        return
    post_id_str = str(message.id)
    user_id_str = str(message.author.id)
    posted_at_iso = message.created_at.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    actor = _resolve_bot_actor(client)

    try:
        # 1) Gate on per-guild state. Default config is state='off'.
        with get_db() as conn:
            cfg = discord_scoring_config.get_config(conn, guild_id)
        if cfg["state"] == "off":
            return

        # 2) Image bytes. Bail if no readable image — Pass A's hash path
        # will have already logged the failure if applicable.
        att = _first_image_attachment(message.attachments)
        if att is None:
            return
        image_bytes = await _read_image_bytes(att)
        if image_bytes is None:
            return
        media_type = _detect_media_type(image_bytes)
        if media_type is None:
            # Force a successful PIL decode + re-encode to PNG so Anthropic
            # gets a known-good payload. If even THIS fails, we have a
            # non-image file masquerading and should skip.
            try:
                img = Image.open(io.BytesIO(image_bytes))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                image_bytes = buf.getvalue()
                media_type = "image/png"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "scoring skip: media_type detect + reencode failed: %s",
                    exc,
                )
                return

        # 3) Model + prompt version. Per-guild config wins.
        model_id = cfg.get("model_id") or SCORING_MODEL
        prompt_version = cfg.get("prompt_version") or SCORING_PROMPT_VERSION

        # 4) Call vision (with retry).
        b64 = base64.b64encode(image_bytes).decode("ascii")
        context_text = (
            f"poster: {message.author.display_name}\n"
            f"posted_at: {posted_at_iso}\n"
            f"return strict JSON per the schema."
        )
        scored_at_iso = _now_iso_seconds()
        response, error_reason = await _call_vision_with_retry(
            model_id=model_id,
            image_b64=b64,
            media_type=media_type,
            context_text=context_text,
        )
        if response is None:
            with get_db() as conn:
                discord_fitcheck_scores.record_score_failure(
                    conn,
                    org_id,
                    guild_id,
                    post_id_str,
                    user_id_str,
                    posted_at_iso,
                    scored_at_iso,
                    model_id,
                    prompt_version,
                    error_reason,
                )
                log_audit(
                    conn,
                    actor=actor,
                    action="fitcheck_score_failed",
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "post_id": post_id_str,
                        "user_id": user_id_str,
                        "model_id": model_id,
                        "prompt_version": prompt_version,
                        "score_error": error_reason,
                    },
                    source="sable-roles",
                )
            return

        # 5) Parse + validate.
        raw_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()
        parsed = _parse_response_text(raw_text)
        if parsed is None:
            error_reason = "json_parse_failed"
        else:
            invalid = _validate_response(parsed)
            if invalid:
                error_reason = f"schema_invalid:{invalid}"
                parsed = None
        if parsed is None:
            with get_db() as conn:
                discord_fitcheck_scores.record_score_failure(
                    conn,
                    org_id,
                    guild_id,
                    post_id_str,
                    user_id_str,
                    posted_at_iso,
                    scored_at_iso,
                    model_id,
                    prompt_version,
                    error_reason,
                )
                log_audit(
                    conn,
                    actor=actor,
                    action="fitcheck_score_failed",
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "post_id": post_id_str,
                        "user_id": user_id_str,
                        "model_id": model_id,
                        "prompt_version": prompt_version,
                        "score_error": error_reason,
                    },
                    source="sable-roles",
                )
            # Cost still logged on a failed parse — the tokens burned.
            _log_cost_safe(
                model_id=model_id,
                response=response,
                org_id=org_id,
                guild_id=guild_id,
                post_id_str=post_id_str,
            )
            return

        # 6) Curve basis + percentile.
        axes = parsed["axis_scores"]
        raw_total = (
            int(axes["cohesion"])
            + int(axes["execution"])
            + int(axes["concept"])
            + int(axes["catch"])
        )
        curve_window_days = int(cfg.get("curve_window_days") or 30)
        cold_start_min_pool = int(cfg.get("cold_start_min_pool") or 20)
        with get_db() as conn:
            pool_size = discord_fitcheck_scores.count_pool_size(
                conn, org_id, _since_iso(curve_window_days)
            )
        if pool_size < cold_start_min_pool:
            curve_basis = "absolute"
            percentile = _compute_percentile_absolute(raw_total)
            pool_for_record = pool_size
        else:
            with get_db() as conn:
                pool_values = discord_fitcheck_scores.fetch_curve_pool_raw_totals(
                    conn, org_id, _since_iso(curve_window_days)
                )
            curve_basis = "rolling_30d"
            percentile = _compute_percentile_from_pool(pool_values, raw_total)
            pool_for_record = len(pool_values)

        # 7) Upsert success + audit + cost.
        catch_detected = parsed.get("catch_detected")
        catch_naming_class = parsed.get("catch_naming_class")
        description = parsed.get("description")
        confidence_raw = parsed.get("confidence")
        confidence = (
            float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None
        )
        rationales_json = json.dumps(parsed["axis_rationales"])

        with get_db() as conn:
            discord_fitcheck_scores.upsert_score_success(
                conn,
                org_id,
                guild_id,
                post_id_str,
                user_id_str,
                posted_at_iso,
                scored_at_iso,
                model_id,
                prompt_version,
                int(axes["cohesion"]),
                int(axes["execution"]),
                int(axes["concept"]),
                int(axes["catch"]),
                raw_total,
                catch_detected,
                catch_naming_class,
                description,
                confidence,
                rationales_json,
                curve_basis,
                pool_for_record,
                percentile,
            )
            log_audit(
                conn,
                actor=actor,
                action="fitcheck_score_recorded",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "post_id": post_id_str,
                    "user_id": user_id_str,
                    "model_id": model_id,
                    "prompt_version": prompt_version,
                    "raw_total": raw_total,
                    "percentile": percentile,
                    "curve_basis": curve_basis,
                    "pool_size": pool_for_record,
                    "axes": {
                        "cohesion": int(axes["cohesion"]),
                        "execution": int(axes["execution"]),
                        "concept": int(axes["concept"]),
                        "catch": int(axes["catch"]),
                    },
                    "catch_naming_class": catch_naming_class,
                    "confidence": confidence,
                    "state": cfg["state"],
                },
                source="sable-roles",
            )

        _log_cost_safe(
            model_id=model_id,
            response=response,
            org_id=org_id,
            guild_id=guild_id,
            post_id_str=post_id_str,
        )
    except Exception as exc:  # noqa: BLE001 — last-line defense
        logger.warning(
            "maybe_score_fit failed for post %s", message.id, exc_info=exc
        )


def _log_cost_safe(
    *,
    model_id: str,
    response: Any,
    org_id: str,
    guild_id: str,
    post_id_str: str,
) -> None:
    """Best-effort cost logging. Cost telemetry can never break scoring."""
    try:
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        cost = _compute_cost_per_million(
            model_id, in_tok, out_tok, cache_read, cache_write
        )
        with get_db() as conn:
            log_cost(
                conn,
                org_id=org_id,
                call_type="sable_roles_fitcheck_score",
                cost_usd=cost,
                detail={
                    "guild_id": guild_id,
                    "post_id": post_id_str,
                    "model_id": model_id,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cache_read_tokens": cache_read,
                    "cache_creation_tokens": cache_write,
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.info("scoring cost log failed: %s", exc)


def _is_manage_guild(interaction: discord.Interaction) -> bool:
    """True iff the invoking user has Manage Guild permission.

    Discord's slash-command default permission is enforced server-side
    too — `@app_commands.default_permissions(manage_guild=True)` on the
    decorator hides the command from non-mods in most clients. This
    in-handler check is defense-in-depth in case a guild has overridden
    the default visibility.
    """
    if not isinstance(interaction.user, discord.Member):
        return False
    perms = interaction.user.guild_permissions
    return bool(perms.manage_guild)


def register_commands(
    tree: app_commands.CommandTree,
    *,
    client: discord.Client | None = None,
) -> None:
    """Register /scoring (status | set <state>) against the command tree.

    Mod-only via @default_permissions(manage_guild=True). Per-guild scope
    (same as /streak / /relax-mode pattern).
    """

    @tree.command(
        name="scoring",
        description="(mods) Scored-mode controls for #fitcheck.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.describe(
        action="status or set",
        state="off | silent | revealed (required when action=set)",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="status", value="status"),
            app_commands.Choice(name="set", value="set"),
        ],
        state=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="silent", value="silent"),
            app_commands.Choice(name="revealed", value="revealed"),
        ],
    )
    async def scoring(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        state: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        if guild_id is None:
            await interaction.followup.send(
                "this command must be run inside a server.", ephemeral=True
            )
            return
        org_id = GUILD_TO_ORG.get(guild_id)
        if org_id is None:
            await interaction.followup.send(
                "not configured for this server.", ephemeral=True
            )
            return
        # Defense-in-depth permission gate.
        if not _is_manage_guild(interaction):
            await interaction.followup.send(
                "you need Manage Server to run this.", ephemeral=True
            )
            return

        if action.value == "status":
            with get_db() as conn:
                cfg = discord_scoring_config.get_config(conn, guild_id)
                breakdown = discord_scoring_config.count_status_breakdown(
                    conn, org_id, guild_id
                )
            cold = (
                f"cold-start pool: <not started>"
                if breakdown["success"] == 0
                else f"cold-start pool: {breakdown['success']} / {cfg['cold_start_min_pool']}"
                f" {'(graduated)' if breakdown['success'] >= int(cfg['cold_start_min_pool']) else ''}"
            ).rstrip()
            body = (
                f"scored mode for this guild\n\n"
                f"state: **{cfg['state']}**\n"
                f"model: {cfg['model_id']} · prompt: {cfg['prompt_version']}\n"
                f"successes: {breakdown['success']}"
                f" · failures: {breakdown['failed']}"
                f" · invalidated: {breakdown['invalidated']}\n"
                f"{cold}\n"
                f"thresholds: {cfg['reaction_threshold']} reactions"
                f" / {cfg['thread_message_threshold']} thread msgs"
                f" (window {cfg['reveal_window_days']}d, age ≥ {cfg['reveal_min_age_minutes']}m)\n"
                f"last state change: {cfg.get('state_changed_at') or 'never'}"
                f" by {cfg.get('state_changed_by') or 'never'}"
            )
            await interaction.followup.send(body, ephemeral=True)
            return

        # action.value == "set"
        if state is None:
            await interaction.followup.send(
                "pass a state — `/scoring action:set state:silent` (etc).",
                ephemeral=True,
            )
            return
        target_state = state.value
        with get_db() as conn:
            cfg = discord_scoring_config.set_state(
                conn,
                org_id=org_id,
                guild_id=guild_id,
                state=target_state,
                updated_by=str(interaction.user.id),
            )
        body = (
            f"scored mode is now **{cfg['state']}** for this guild.\n"
            f"audit row written. no public announcement was made."
        )
        await interaction.followup.send(body, ephemeral=True)
