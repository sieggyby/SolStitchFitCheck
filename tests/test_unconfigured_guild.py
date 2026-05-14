"""Verify guild/channel/bot/thread filters silently no-op."""
from __future__ import annotations

import pytest

from tests.conftest import fetch_audit_rows, fetch_streak_rows, make_attachment, make_message


@pytest.mark.asyncio
async def test_message_in_unconfigured_guild_is_ignored(fitcheck_module, db_conn) -> None:
    msg = make_message(attachments=[make_attachment()], guild_id=999, message_id=701)
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    assert fetch_audit_rows(db_conn) == []
    msg.delete.assert_not_called()
    msg.author.send.assert_not_called()


@pytest.mark.asyncio
async def test_dm_in_no_guild_is_ignored(fitcheck_module, db_conn) -> None:
    msg = make_message(attachments=[], guild_id=None, message_id=701)
    msg.guild = None
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    assert fetch_audit_rows(db_conn) == []
    msg.delete.assert_not_called()


@pytest.mark.asyncio
async def test_bot_message_is_ignored(fitcheck_module, db_conn) -> None:
    msg = make_message(attachments=[make_attachment()], author_bot=True, message_id=701)
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_message_in_other_channel_in_configured_guild_is_ignored(
    fitcheck_module, db_conn
) -> None:
    """Configured guild but a different channel — neither enforce nor count."""
    msg = make_message(attachments=[], channel_id=300, message_id=701)
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    assert fetch_audit_rows(db_conn) == []
    msg.delete.assert_not_called()


@pytest.mark.asyncio
async def test_message_in_thread_under_fitcheck_channel_is_allowed(
    fitcheck_module, db_conn
) -> None:
    """Threads under #fitcheck → no enforcement, no counting."""
    msg = make_message(
        attachments=[],
        channel_id=400,
        channel_kind="thread",
        parent_id=200,
        message_id=701,
    )
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    assert fetch_audit_rows(db_conn) == []
    msg.delete.assert_not_called()


@pytest.mark.asyncio
async def test_thread_under_other_channel_is_ignored(fitcheck_module, db_conn) -> None:
    msg = make_message(
        attachments=[],
        channel_id=400,
        channel_kind="thread",
        parent_id=999,
        message_id=701,
    )
    await fitcheck_module.on_message(msg)
    assert fetch_streak_rows(db_conn) == []
    assert fetch_audit_rows(db_conn) == []
