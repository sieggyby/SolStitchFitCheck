"""Tests for on_message conditionals when relax-mode is on vs off."""
from __future__ import annotations

import pytest

from sable_platform.db import discord_guild_config

from tests.conftest import fetch_audit_rows, fetch_streak_rows, make_attachment, make_message


def _set_relax(db_conn, on: bool) -> None:
    discord_guild_config.set_relax_mode(db_conn, "100", on=on, updated_by="setup")


@pytest.mark.asyncio
async def test_relax_off_default_text_post_deleted_with_dm(fitcheck_module, db_conn):
    # Default state (no row in discord_guild_config) is relax_mode_on=0.
    message = make_message(message_id=701)  # text-only

    await fitcheck_module.on_message(message)

    message.delete.assert_awaited_once()
    message.author.send.assert_awaited_once()
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_text_message_deleted"]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_relax_on_text_post_not_deleted_no_dm_no_audit(fitcheck_module, db_conn):
    _set_relax(db_conn, on=True)
    message = make_message(message_id=702)  # text-only

    await fitcheck_module.on_message(message)

    message.delete.assert_not_called()
    message.author.send.assert_not_called()
    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_text_message_deleted"]
    assert audits == []


@pytest.mark.asyncio
async def test_relax_on_image_post_credits_streak_and_reacts_but_no_thread(
    fitcheck_module, db_conn,
):
    _set_relax(db_conn, on=True)
    message = make_message(
        message_id=703,
        attachments=[make_attachment()],  # image
    )

    await fitcheck_module.on_message(message)

    # Streak credit landed
    streak = fetch_streak_rows(db_conn)
    assert len(streak) == 1
    assert streak[0]["post_id"] == "703"
    assert streak[0]["image_attachment_count"] == 1

    # 🔥 fired
    message.add_reaction.assert_awaited_once()

    # auto-thread DID NOT
    message.create_thread.assert_not_called()


@pytest.mark.asyncio
async def test_relax_off_image_post_creates_thread_as_before(fitcheck_module, db_conn):
    # explicitly toggle off to make sure the read path returns 0
    _set_relax(db_conn, on=False)
    message = make_message(
        message_id=704,
        attachments=[make_attachment()],
    )

    await fitcheck_module.on_message(message)

    streak = fetch_streak_rows(db_conn)
    assert len(streak) == 1
    message.add_reaction.assert_awaited_once()
    message.create_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_toggle_relax_on_then_off_image_thread_resumes(fitcheck_module, db_conn):
    # First image while relax on: no thread
    _set_relax(db_conn, on=True)
    m1 = make_message(message_id=710, attachments=[make_attachment()])
    await fitcheck_module.on_message(m1)
    m1.create_thread.assert_not_called()

    # Toggle off, post again: thread fires
    _set_relax(db_conn, on=False)
    m2 = make_message(message_id=711, attachments=[make_attachment()])
    await fitcheck_module.on_message(m2)
    m2.create_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_relax_only_affects_configured_fitcheck_channel(fitcheck_module, db_conn):
    _set_relax(db_conn, on=True)
    # Different channel id; conftest patches FITCHECK_CHANNELS to {"100": {"channel_id": "200"}}.
    # Anything outside channel 200 is ignored entirely regardless of relax-mode.
    message = make_message(message_id=720, channel_id=999)

    await fitcheck_module.on_message(message)

    message.delete.assert_not_called()
    message.author.send.assert_not_called()
    assert fetch_audit_rows(db_conn) == []
