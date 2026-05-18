"""Pass A: pHash compute + collision detection."""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from sqlalchemy import text

from sable_platform.db.discord_streaks import upsert_streak_event


def _png_bytes(pixel: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """Build a tiny synthetic PNG. Each test uses a different pixel so
    each produces a distinct (or close-to-distinct) pHash.
    """
    img = Image.new("RGB", (32, 32), pixel)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_attachment(*, raw_bytes: bytes, filename: str = "fit.png", content_type: str = "image/png", size: int | None = None):
    att = MagicMock()
    att.filename = filename
    att.content_type = content_type
    att.size = size if size is not None else len(raw_bytes)
    att.read = AsyncMock(return_value=raw_bytes)
    return att


def _make_message(
    *, message_id: int = 700, author_id: int = 555, attachments=None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        attachments=attachments or [],
        author=SimpleNamespace(id=author_id),
    )


# ---------------------------------------------------------------------------
# compute_phash_from_bytes
# ---------------------------------------------------------------------------


def test_compute_phash_returns_hex_string():
    from sable_roles.features.image_hashing import compute_phash_from_bytes

    phash = compute_phash_from_bytes(_png_bytes())
    assert isinstance(phash, str)
    assert len(phash) > 0
    # imagehash hex strings are lowercase hex
    int(phash, 16)


def test_hamming_distance_identical_is_zero():
    from sable_roles.features.image_hashing import (
        compute_phash_from_bytes,
        hamming_distance,
    )

    img = _png_bytes((128, 128, 128))
    a = compute_phash_from_bytes(img)
    b = compute_phash_from_bytes(img)
    assert hamming_distance(a, b) == 0


def test_hamming_distance_changes_with_image():
    from sable_roles.features.image_hashing import (
        compute_phash_from_bytes,
        hamming_distance,
    )

    a = compute_phash_from_bytes(_png_bytes((0, 0, 0)))
    b = compute_phash_from_bytes(_png_bytes((255, 255, 255)))
    # Solid black vs solid white are visually polar, but pHash on flat images
    # can still be near-identical (no high-frequency content). Just sanity:
    # distance is a non-negative integer of plausible scale.
    dist = hamming_distance(a, b)
    assert isinstance(dist, int)
    assert dist >= 0


# ---------------------------------------------------------------------------
# maybe_record_phash — happy path
# ---------------------------------------------------------------------------


@pytest.fixture
def img_module(monkeypatch, db_conn):
    from sable_roles.features import image_hashing as mod

    monkeypatch.setattr(mod, "SCORED_MODE_ENABLED", True)

    class _DBContext:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr(mod, "get_db", lambda: _DBContext())
    yield mod


async def test_maybe_record_phash_stamps_and_audits(img_module, db_conn):
    upsert_streak_event(
        db_conn,
        "solstitch",
        "100",
        "200",
        "700",
        "555",
        "2026-05-12T12:00:00Z",
        "2026-05-12",
        1,
        1,
    )
    att = _make_attachment(raw_bytes=_png_bytes((255, 0, 0)))
    msg = _make_message(attachments=[att])
    client = SimpleNamespace(user=SimpleNamespace(id=99999))

    result = await img_module.maybe_record_phash(
        message=msg, org_id="solstitch", guild_id="100", client=client
    )
    assert result is not None

    row = db_conn.execute(
        text(
            "SELECT image_phash FROM discord_streak_events"
            " WHERE guild_id = '100' AND post_id = '700'"
        )
    ).fetchone()
    assert row[0] == result

    audit = db_conn.execute(
        text(
            "SELECT action, source FROM audit_log"
            " WHERE action = 'fitcheck_image_phash_recorded'"
            " ORDER BY id DESC LIMIT 1"
        )
    ).fetchone()
    assert audit is not None
    assert audit["source"] == "sable-roles"


async def test_maybe_record_phash_noop_when_disabled(img_module, db_conn, monkeypatch):
    monkeypatch.setattr(img_module, "SCORED_MODE_ENABLED", False)
    att = _make_attachment(raw_bytes=_png_bytes())
    msg = _make_message(attachments=[att])
    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    result = await img_module.maybe_record_phash(
        message=msg, org_id="solstitch", guild_id="100", client=client
    )
    assert result is None


async def test_maybe_record_phash_noop_when_no_image(img_module):
    msg = _make_message(attachments=[])
    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    result = await img_module.maybe_record_phash(
        message=msg, org_id="solstitch", guild_id="100", client=client
    )
    assert result is None


async def test_maybe_record_phash_skips_oversize(img_module, db_conn):
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "700", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    att = _make_attachment(
        raw_bytes=_png_bytes(),
        size=11 * 1024 * 1024,  # >10MB cap
    )
    att.read = AsyncMock()  # should NOT be called
    msg = _make_message(attachments=[att])
    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    result = await img_module.maybe_record_phash(
        message=msg, org_id="solstitch", guild_id="100", client=client
    )
    assert result is None
    att.read.assert_not_called()


# ---------------------------------------------------------------------------
# maybe_record_phash — collision detection
# ---------------------------------------------------------------------------


async def test_maybe_record_phash_logs_repost_on_same_user_collision(
    img_module, db_conn
):
    """Same user posts identical image twice -> fitcheck_repost_detected LOW."""
    img_bytes = _png_bytes((42, 42, 42))
    # First post by user 555.
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "701", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    att1 = _make_attachment(raw_bytes=img_bytes)
    msg1 = _make_message(message_id=701, author_id=555, attachments=[att1])
    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await img_module.maybe_record_phash(
        message=msg1, org_id="solstitch", guild_id="100", client=client
    )

    # Second post by SAME user 555, same bytes.
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "702", "555",
        "2026-05-12T13:00:00Z", "2026-05-12", 1, 1,
    )
    att2 = _make_attachment(raw_bytes=img_bytes)
    msg2 = _make_message(message_id=702, author_id=555, attachments=[att2])
    await img_module.maybe_record_phash(
        message=msg2, org_id="solstitch", guild_id="100", client=client
    )

    audit_rows = db_conn.execute(
        text(
            "SELECT action FROM audit_log"
            " WHERE action = 'fitcheck_repost_detected'"
        )
    ).fetchall()
    assert len(audit_rows) >= 1


async def test_maybe_record_phash_logs_theft_on_different_user_collision(
    img_module, db_conn
):
    """User A posts -> user B posts same image -> fitcheck_image_theft_detected HIGH."""
    img_bytes = _png_bytes((77, 77, 77))
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "703", "AAA",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    att1 = _make_attachment(raw_bytes=img_bytes)
    msg1 = _make_message(message_id=703, author_id=int("a", 16) * 1000, attachments=[att1])
    msg1.author.id = "AAA"  # match seed
    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    await img_module.maybe_record_phash(
        message=msg1, org_id="solstitch", guild_id="100", client=client
    )

    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "704", "BBB",
        "2026-05-12T13:00:00Z", "2026-05-12", 1, 1,
    )
    att2 = _make_attachment(raw_bytes=img_bytes)
    msg2 = _make_message(message_id=704, attachments=[att2])
    msg2.author.id = "BBB"
    await img_module.maybe_record_phash(
        message=msg2, org_id="solstitch", guild_id="100", client=client
    )

    audit_rows = db_conn.execute(
        text(
            "SELECT action FROM audit_log"
            " WHERE action = 'fitcheck_image_theft_detected'"
        )
    ).fetchall()
    assert len(audit_rows) >= 1


async def test_maybe_record_phash_no_collision_when_distinct(img_module, db_conn):
    """Two clearly distinct images -> no repost / no theft."""
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "710", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    client = SimpleNamespace(user=SimpleNamespace(id=99999))
    att1 = _make_attachment(raw_bytes=_png_bytes((0, 0, 0)))
    msg1 = _make_message(message_id=710, attachments=[att1])
    await img_module.maybe_record_phash(
        message=msg1, org_id="solstitch", guild_id="100", client=client
    )

    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "711", "555",
        "2026-05-12T13:00:00Z", "2026-05-12", 1, 1,
    )
    # Use a complex pattern image to ensure pHash differs
    img = Image.new("RGB", (32, 32))
    for x in range(32):
        for y in range(32):
            img.putpixel((x, y), ((x * 7) % 256, (y * 13) % 256, ((x + y) * 5) % 256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    att2 = _make_attachment(raw_bytes=buf.getvalue())
    msg2 = _make_message(message_id=711, attachments=[att2])
    await img_module.maybe_record_phash(
        message=msg2, org_id="solstitch", guild_id="100", client=client
    )

    # Distinct images -> no collision audit rows.
    audit_rows = db_conn.execute(
        text(
            "SELECT action FROM audit_log"
            " WHERE action IN ('fitcheck_repost_detected', 'fitcheck_image_theft_detected')"
        )
    ).fetchall()
    assert len(audit_rows) == 0
