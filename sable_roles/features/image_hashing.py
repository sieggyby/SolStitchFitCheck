"""Pass A: image pHash computation + repost/theft collision detection.

Hooks into the image branch of on_message AFTER the streak upsert. Runs
regardless of scoring state — pHash + collision detection are valuable
even when scored mode is Off (the audit log is the entire point of Pass A).

Surface:

* `maybe_record_phash` — coroutine invoked by fitcheck_streak.on_message
  image branch via asyncio.create_task. Fetches the first image attachment,
  computes pHash, stamps it on the streak event row, then runs collision
  detection in the 90-day window. Emits one of:
    - `fitcheck_image_phash_recorded` (always on success)
    - `fitcheck_image_phash_failed`   (INFO; PIL decode or read error)
    - `fitcheck_repost_detected`      (same user, LOW severity)
    - `fitcheck_image_theft_detected` (different user, HIGH severity)
  (`fitcheck_image_phash_failed` is the implementation-emitted action;
  design §6.4 catalog should pick it up in the next plan revision.)
* `compute_phash_from_bytes` — synchronous helper, exposed for tests and
  for the scoring pipeline to reuse (single attachment download per fit).

Image-bytes fetch is bounded: only the first image attachment is hashed,
size cap 10MB (Anthropic vision cap). Any fetch / decode failure is
logged and audited as `fitcheck_image_phash_failed` (INFO) — never raises.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

import discord
import imagehash
from PIL import Image

from sable_platform.db import discord_fitcheck_scores
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db

from sable_roles.config import (
    PHASH_COLLISION_DISTANCE,
    PHASH_COLLISION_WINDOW_DAYS,
    SCORED_MODE_ENABLED,
)
from sable_roles.features.fitcheck_streak import is_image

logger = logging.getLogger("sable_roles.image_hashing")

# 10 MB cap mirrors Anthropic vision endpoint upper bound (sec 5.1).
_IMAGE_BYTE_CAP = 10 * 1024 * 1024


def compute_phash_from_bytes(image_bytes: bytes) -> str:
    """Return pHash hex string for image_bytes.

    Raises PIL.UnidentifiedImageError or OSError on decode failure.
    Caller is responsible for catching — this is a pure transform.
    """
    img = Image.open(io.BytesIO(image_bytes))
    return str(imagehash.phash(img))


def hamming_distance(a_hex: str, b_hex: str) -> int:
    """Hamming distance between two pHash hex strings.

    Both must be the same length; mismatched lengths raise ValueError.
    Uses imagehash.hex_to_hash so we hit the library-validated path.
    Coerces to Python int — imagehash returns numpy.int64, which fails
    `isinstance(x, int)` checks in downstream code and audit serialisation.
    """
    a = imagehash.hex_to_hash(a_hex)
    b = imagehash.hex_to_hash(b_hex)
    return int(a - b)


def _first_image_attachment(
    attachments: Iterable[discord.Attachment],
) -> discord.Attachment | None:
    for att in attachments:
        if is_image(att):
            return att
    return None


def _now_iso_seconds() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


async def _read_image_bytes(att: discord.Attachment) -> bytes | None:
    """Read attachment bytes, capped at _IMAGE_BYTE_CAP. Returns None on any
    failure (HTTP, oversize, network). Never raises.
    """
    if att.size and att.size > _IMAGE_BYTE_CAP:
        logger.info(
            "phash skip: attachment %s is %s bytes (cap %s)",
            att.filename,
            att.size,
            _IMAGE_BYTE_CAP,
        )
        return None
    try:
        data = await att.read()
    except (discord.HTTPException, discord.NotFound) as exc:
        logger.warning("phash skip: attachment read failed: %s", exc)
        return None
    if len(data) > _IMAGE_BYTE_CAP:
        logger.info("phash skip: attachment read %s bytes exceeds cap", len(data))
        return None
    return data


def _resolve_bot_actor(client: discord.Client | None) -> str:
    if client is not None and client.user is not None:
        return f"discord:bot:{client.user.id}"
    return "discord:bot:unknown"


async def maybe_record_phash(
    *,
    message: discord.Message,
    org_id: str,
    guild_id: str,
    client: discord.Client | None,
) -> str | None:
    """Compute pHash for the first image attachment, stamp it on the
    streak event, run collision detection. Returns the phash hex string
    on success, None on any skip/failure path.

    Called from fitcheck_streak.on_message image branch tail via
    asyncio.create_task — must NEVER raise; all errors are swallowed +
    logged + audited so they can't tear down the message handler.

    No-op when SCORED_MODE_ENABLED is False (env kill switch).
    """
    if not SCORED_MODE_ENABLED:
        return None
    try:
        att = _first_image_attachment(message.attachments)
        if att is None:
            return None
        image_bytes = await _read_image_bytes(att)
        if image_bytes is None:
            return None
        try:
            phash = compute_phash_from_bytes(image_bytes)
        except Exception as exc:  # noqa: BLE001 — Pillow has many failure modes
            logger.warning(
                "phash compute failed for post %s: %s", message.id, exc
            )
            with get_db() as conn:
                log_audit(
                    conn,
                    actor=_resolve_bot_actor(client),
                    action="fitcheck_image_phash_failed",
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "post_id": str(message.id),
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                    source="sable-roles",
                )
            return None

        post_id_str = str(message.id)
        with get_db() as conn:
            stamped = discord_fitcheck_scores.set_phash_on_streak_event(
                conn, guild_id, post_id_str, phash
            )
            if stamped:
                log_audit(
                    conn,
                    actor=_resolve_bot_actor(client),
                    action="fitcheck_image_phash_recorded",
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "post_id": post_id_str,
                        "user_id": str(message.author.id),
                        "phash": phash,
                    },
                    source="sable-roles",
                )
            since = _since_iso(PHASH_COLLISION_WINDOW_DAYS)
            candidates = discord_fitcheck_scores.list_recent_phashes_for_collision(
                conn, org_id, since, exclude_post_id=post_id_str
            )
            author_id_str = str(message.author.id)
            for cand in candidates:
                cand_phash = cand.get("image_phash")
                if not cand_phash:
                    continue
                try:
                    dist = hamming_distance(phash, cand_phash)
                except Exception:  # noqa: BLE001 — hex_to_hash may fail on legacy
                    continue
                if dist > PHASH_COLLISION_DISTANCE:
                    continue
                same_user = str(cand.get("user_id")) == author_id_str
                action = (
                    "fitcheck_repost_detected"
                    if same_user
                    else "fitcheck_image_theft_detected"
                )
                log_audit(
                    conn,
                    actor=_resolve_bot_actor(client),
                    action=action,
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "post_id": post_id_str,
                        "user_id": author_id_str,
                        "phash": phash,
                        "matched_post_id": cand.get("post_id"),
                        "matched_user_id": cand.get("user_id"),
                        "matched_posted_at": cand.get("posted_at"),
                        "hamming_distance": dist,
                    },
                    source="sable-roles",
                )
                # Only log the first match per post — one "this is a dupe of
                # something" signal is enough; mods can grep the audit log
                # for the matched_post_id chain.
                break
        return phash
    except Exception as exc:  # noqa: BLE001 — last-line defense
        logger.warning(
            "maybe_record_phash failed for post %s", message.id, exc_info=exc
        )
        return None
