"""Handler resilience: Discord-side failures must not crash the handler or
revoke streak credit.

C4 contract per plan §4 image-found branch:
- `add_reaction` failure → warning log only; streak credit (DB row) survives.
- `create_thread` failure → warning log + `fitcheck_thread_create_failed`
  audit row; streak credit survives; handler completes.
- Text-branch `delete` failure → warning log; DM + audit still happen; no crash.
"""
from __future__ import annotations

import json

import discord
import pytest

from tests.conftest import (
    fetch_audit_rows,
    fetch_streak_rows,
    make_attachment,
    make_message,
)


def _http_exc(status: int = 500, reason: str = "Internal") -> discord.HTTPException:
    response = type("R", (), {"status": status, "reason": reason})()
    return discord.HTTPException(response=response, message="boom")


@pytest.mark.asyncio
async def test_add_reaction_failure_preserves_streak_credit(
    fitcheck_module, db_conn
) -> None:
    """Reaction-emoji failure must not roll back the upserted streak row."""
    msg = make_message(
        attachments=[make_attachment()],
        add_reaction_raises=_http_exc(),
    )

    await fitcheck_module.on_message(msg)

    rows = fetch_streak_rows(db_conn)
    assert len(rows) == 1
    assert rows[0]["post_id"] == "700"
    msg.add_reaction.assert_awaited_once()
    msg.create_thread.assert_awaited_once()
    # No audit row written for add_reaction failures (plan §4: warning log only).
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_create_thread_failure_logs_audit_and_preserves_streak_credit(
    fitcheck_module, db_conn
) -> None:
    msg = make_message(
        attachments=[make_attachment()],
        create_thread_raises=_http_exc(status=400, reason="Bad Request"),
    )

    await fitcheck_module.on_message(msg)

    rows = fetch_streak_rows(db_conn)
    assert len(rows) == 1
    assert rows[0]["post_id"] == "700"

    audits = fetch_audit_rows(db_conn)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["action"] == "fitcheck_thread_create_failed"
    assert audit["org_id"] == "solstitch"
    assert audit["source"] == "sable-roles"
    assert audit["actor"] == "discord:bot:99999"
    detail = json.loads(audit["detail_json"])
    assert detail["post_id"] == "700"
    assert detail["guild_id"] == "100"
    assert detail["channel_id"] == "200"
    assert "error" in detail and detail["error"]


@pytest.mark.asyncio
async def test_create_thread_failure_does_not_block_add_reaction(
    fitcheck_module,
) -> None:
    """Reaction fires before thread creation; thread failure mustn't skip the emoji."""
    msg = make_message(
        attachments=[make_attachment()],
        create_thread_raises=_http_exc(),
    )

    await fitcheck_module.on_message(msg)

    msg.add_reaction.assert_awaited_once()
    msg.create_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_both_add_reaction_and_create_thread_fail_streak_survives(
    fitcheck_module, db_conn
) -> None:
    """Worst case: both Discord calls fail. Streak row stays; only thread audit logged."""
    msg = make_message(
        attachments=[make_attachment()],
        add_reaction_raises=_http_exc(),
        create_thread_raises=_http_exc(),
    )

    await fitcheck_module.on_message(msg)

    rows = fetch_streak_rows(db_conn)
    assert len(rows) == 1

    audits = fetch_audit_rows(db_conn)
    actions = [a["action"] for a in audits]
    assert actions == ["fitcheck_thread_create_failed"]


@pytest.mark.asyncio
async def test_delete_failure_on_text_post_does_not_crash_or_skip_dm(
    fitcheck_module, db_conn
) -> None:
    """Plan §4 text-branch: delete failure logs warning; DM + audit still happen."""
    msg = make_message(
        attachments=[],
        delete_raises=_http_exc(status=403, reason="Forbidden"),
        message_id=701,
    )

    await fitcheck_module.on_message(msg)

    msg.delete.assert_awaited_once()
    msg.author.send.assert_awaited_once()

    audits = fetch_audit_rows(db_conn)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["action"] == "fitcheck_text_message_deleted"
    detail = json.loads(audit["detail_json"])
    assert detail["dm_success"] is True
    assert detail["dm_suppressed_for_cooldown"] is False
    assert detail["post_id"] == "701"


@pytest.mark.asyncio
async def test_thread_name_truncated_to_100_chars(fitcheck_module) -> None:
    """Plan §4 round-2 audit #18: thread name capped at Discord's 100-char limit."""
    very_long = "x" * 200
    msg = make_message(
        attachments=[make_attachment()],
        author_display_name=very_long,
    )

    await fitcheck_module.on_message(msg)

    msg.create_thread.assert_awaited_once()
    kwargs = msg.create_thread.await_args.kwargs
    name = kwargs.get("name") if "name" in kwargs else msg.create_thread.await_args.args[0]
    assert len(name) == 100
    assert name.startswith("xxxxx")


@pytest.mark.asyncio
async def test_confirmation_emoji_is_the_configured_value(fitcheck_module) -> None:
    """add_reaction must be called with config.CONFIRMATION_EMOJI (🔥)."""
    from sable_roles.config import CONFIRMATION_EMOJI

    msg = make_message(attachments=[make_attachment()])

    await fitcheck_module.on_message(msg)

    msg.add_reaction.assert_awaited_once_with(CONFIRMATION_EMOJI)
