"""Image-detection tests for the fit-check enforcement branch."""
from __future__ import annotations

import pytest

from sable_roles.config import IMAGE_EXT_ALLOWLIST
from sable_roles.features.fitcheck_streak import is_image
from tests.conftest import make_attachment


@pytest.mark.parametrize("ext", sorted(IMAGE_EXT_ALLOWLIST))
def test_extension_allowlist_accepts_each_extension(ext: str) -> None:
    att = make_attachment(filename=f"fit{ext}", content_type=None)
    assert is_image(att) is True


@pytest.mark.parametrize(
    "content_type",
    ["image/png", "image/jpeg", "image/gif", "image/webp", "image/heic"],
)
def test_image_content_type_accepted(content_type: str) -> None:
    att = make_attachment(filename="anything.dat", content_type=content_type)
    assert is_image(att) is True


def test_svg_content_type_rejected() -> None:
    att = make_attachment(filename="logo.svg", content_type="image/svg+xml")
    assert is_image(att) is False


def test_svg_extension_rejected_when_no_content_type() -> None:
    att = make_attachment(filename="logo.svg", content_type=None)
    assert is_image(att) is False


def test_text_filename_no_content_type_rejected() -> None:
    att = make_attachment(filename="notes.txt", content_type=None)
    assert is_image(att) is False


def test_pdf_content_type_with_image_filename_falls_through_to_extension() -> None:
    """Plan §5: documented spoof — extension allowlist still accepts."""
    att = make_attachment(filename="fake.png", content_type="application/pdf")
    assert is_image(att) is True


def test_octet_stream_with_image_filename_accepted_via_fallback() -> None:
    att = make_attachment(filename="fit.png", content_type="application/octet-stream")
    assert is_image(att) is True


def test_missing_filename_rejected_when_no_content_type() -> None:
    att = make_attachment(filename="", content_type=None)
    assert is_image(att) is False


def test_uppercase_extension_normalized() -> None:
    att = make_attachment(filename="FIT.PNG", content_type=None)
    assert is_image(att) is True


@pytest.mark.asyncio
async def test_message_with_no_attachments_treated_as_text(fitcheck_module, db_conn) -> None:
    from tests.conftest import fetch_streak_rows, make_message

    msg = make_message(attachments=[])
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    msg.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_with_image_records_streak(fitcheck_module, db_conn) -> None:
    from tests.conftest import fetch_streak_rows, make_message

    msg = make_message(attachments=[make_attachment()])
    await fitcheck_module.on_message(msg)
    rows = fetch_streak_rows(db_conn)
    assert len(rows) == 1
    assert rows[0]["org_id"] == "solstitch"
    assert rows[0]["guild_id"] == "100"
    assert rows[0]["channel_id"] == "200"
    assert rows[0]["post_id"] == "700"
    assert rows[0]["user_id"] == "555"
    assert rows[0]["counted_for_day"] == "2026-05-12"
    assert rows[0]["attachment_count"] == 1
    assert rows[0]["image_attachment_count"] == 1
    msg.delete.assert_not_called()


@pytest.mark.asyncio
async def test_image_with_caption_text_still_records_streak(fitcheck_module, db_conn) -> None:
    """Image + text caption: still has an image attachment, so it counts."""
    from tests.conftest import fetch_streak_rows, make_message

    msg = make_message(attachments=[make_attachment()])
    msg.content = "look at this fit"
    await fitcheck_module.on_message(msg)
    assert len(fetch_streak_rows(db_conn)) == 1


@pytest.mark.asyncio
async def test_gif_picker_embed_only_treated_as_text(fitcheck_module, db_conn) -> None:
    """GIF-picker GIFs arrive as embeds, not attachments; treated as text."""
    from tests.conftest import fetch_streak_rows, make_message

    msg = make_message(attachments=[])
    msg.embeds = [object()]
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    msg.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_emoji_only_message_treated_as_text(fitcheck_module, db_conn) -> None:
    from tests.conftest import fetch_streak_rows, make_message

    msg = make_message(attachments=[])
    msg.content = "🔥🔥🔥"
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    msg.delete.assert_awaited_once()
