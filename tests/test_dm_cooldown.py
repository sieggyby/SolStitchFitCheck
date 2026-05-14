"""Per-user 5-minute DM cooldown tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import fetch_audit_rows, make_message


@pytest.mark.asyncio
async def test_first_text_post_sends_dm_and_audits_no_suppression(
    fitcheck_module, db_conn
) -> None:
    msg = make_message(attachments=[], message_id=701)
    await fitcheck_module.on_message(msg)

    msg.delete.assert_awaited_once()
    msg.author.send.assert_awaited_once()
    audits = fetch_audit_rows(db_conn)
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["dm_success"] is True
    assert detail["dm_suppressed_for_cooldown"] is False
    assert detail["post_id"] == "701"


@pytest.mark.asyncio
async def test_second_text_post_within_cooldown_suppresses_dm_but_still_deletes_and_audits(
    fitcheck_module, db_conn
) -> None:
    first = make_message(attachments=[], author_id=555, message_id=701)
    await fitcheck_module.on_message(first)

    second = make_message(attachments=[], author_id=555, message_id=702)
    await fitcheck_module.on_message(second)

    second.delete.assert_awaited_once()
    second.author.send.assert_not_called()

    audits = fetch_audit_rows(db_conn)
    assert len(audits) == 2
    second_detail = json.loads(audits[1]["detail_json"])
    assert second_detail["dm_success"] is False
    assert second_detail["dm_suppressed_for_cooldown"] is True
    assert second_detail["post_id"] == "702"


@pytest.mark.asyncio
async def test_cooldown_is_per_user_not_global(fitcheck_module) -> None:
    user_a = make_message(attachments=[], author_id=555, message_id=701)
    await fitcheck_module.on_message(user_a)
    user_b = make_message(attachments=[], author_id=666, message_id=702)
    await fitcheck_module.on_message(user_b)
    user_b.author.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cooldown_expires_after_window(fitcheck_module) -> None:
    """Cooldown entries older than DM_COOLDOWN_SECONDS no longer suppress."""
    fitcheck_module._dm_cooldown[555] = datetime.now(timezone.utc) - timedelta(
        seconds=fitcheck_module.DM_COOLDOWN_SECONDS + 5
    )
    msg = make_message(attachments=[], author_id=555, message_id=701)
    await fitcheck_module.on_message(msg)
    msg.author.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dm_failure_audited_with_dm_success_false(fitcheck_module, db_conn) -> None:
    import discord

    msg = make_message(
        attachments=[],
        message_id=701,
        dm_raises=discord.Forbidden(response=type("R", (), {"status": 403, "reason": "Forbidden"})(), message="closed dms"),
    )
    await fitcheck_module.on_message(msg)
    audits = fetch_audit_rows(db_conn)
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["dm_success"] is False
    assert detail["dm_suppressed_for_cooldown"] is False


@pytest.mark.asyncio
async def test_dm_failure_does_not_set_cooldown_so_next_attempt_retries(
    fitcheck_module,
) -> None:
    import discord

    first = make_message(
        attachments=[],
        author_id=555,
        message_id=701,
        dm_raises=discord.Forbidden(response=type("R", (), {"status": 403, "reason": "Forbidden"})(), message="closed dms"),
    )
    await fitcheck_module.on_message(first)
    assert 555 not in fitcheck_module._dm_cooldown

    second = make_message(attachments=[], author_id=555, message_id=702)
    await fitcheck_module.on_message(second)
    second.author.send.assert_awaited_once()
