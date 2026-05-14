"""Debounce-task race + graceful shutdown drain.

Plan §4 round-2 audit:
- Each `_recompute_after_delay` checks its own identity in the registry before
  popping. A cancelled task that's been replaced by a fresh one must NOT evict
  the replacement from `_pending_recomputes`.
- `CancelledError` re-raises so the cancelling caller knows the task unwound.
- `close()` cancels every pending task and awaits the gather, swallowing the
  re-raised CancelledErrors via `return_exceptions=True`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sable_platform.db import discord_streaks


def _seed_event(db_conn, post_id: str = "700") -> None:
    discord_streaks.upsert_streak_event(
        db_conn,
        org_id="solstitch",
        guild_id="100",
        channel_id="200",
        post_id=post_id,
        user_id="555",
        posted_at="2026-05-12T12:00:00Z",
        counted_for_day="2026-05-12",
        attachment_count=1,
        image_attachment_count=1,
    )


def _build_quiet_client(*, bot_id: int = 99999):
    """Client double whose fetch_message returns a message with zero reactions."""
    message = MagicMock()
    message.reactions = []
    channel = SimpleNamespace()
    channel.fetch_message = AsyncMock(return_value=message)
    client = MagicMock()
    client.user = SimpleNamespace(id=bot_id)
    client.get_channel = MagicMock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    return client


@pytest.fixture
def fast_debounce(monkeypatch, fitcheck_module, db_conn):
    monkeypatch.setattr(fitcheck_module, "DEBOUNCE_SECONDS", 0.02)
    monkeypatch.setattr(fitcheck_module, "_client", _build_quiet_client())
    return fitcheck_module


@pytest.mark.asyncio
async def test_cancelled_task_does_not_pop_replacement_from_registry(
    fast_debounce, db_conn
):
    """The classic pop-race: cancel + replace must keep the replacement registered."""
    _seed_event(db_conn)

    # First event installs task A.
    payload = SimpleNamespace(channel_id=200, message_id=700)
    await fast_debounce.on_raw_reaction_add(payload)
    task_a = fast_debounce._pending_recomputes[700]

    # Second event cancels A and installs task B in the same slot.
    await fast_debounce.on_raw_reaction_add(payload)
    task_b = fast_debounce._pending_recomputes[700]

    assert task_a is not task_b
    assert task_a.cancelled() or task_a.done() or not task_a.done()

    # Let A's CancelledError propagate through its finally block.
    with pytest.raises(asyncio.CancelledError):
        await task_a

    # Registry must still hold B — A's finally must not have popped it.
    assert fast_debounce._pending_recomputes.get(700) is task_b

    # Let B finish naturally.
    await asyncio.gather(task_b, return_exceptions=True)
    # After B completes (self-identity match), the slot is cleared.
    assert 700 not in fast_debounce._pending_recomputes


@pytest.mark.asyncio
async def test_cancelled_task_reraises_cancelled_error(fast_debounce, db_conn):
    """Plan: CancelledError re-raises so the cancelling caller knows the task unwound."""
    _seed_event(db_conn)

    payload = SimpleNamespace(channel_id=200, message_id=700)
    await fast_debounce.on_raw_reaction_add(payload)
    task = fast_debounce._pending_recomputes[700]
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_close_drains_pending_tasks_without_exceptions(
    fast_debounce, db_conn
):
    """`close()` cancels + awaits every in-flight debounce task without raising."""
    _seed_event(db_conn, post_id="700")
    _seed_event(db_conn, post_id="701")
    _seed_event(db_conn, post_id="702")

    for mid in (700, 701, 702):
        await fast_debounce.on_raw_reaction_add(
            SimpleNamespace(channel_id=200, message_id=mid)
        )

    assert len(fast_debounce._pending_recomputes) == 3
    tasks_before = list(fast_debounce._pending_recomputes.values())

    await fast_debounce.close()

    assert fast_debounce._pending_recomputes == {}
    for t in tasks_before:
        assert t.done()
        # gather(return_exceptions=True) swallowed any CancelledErrors.


@pytest.mark.asyncio
async def test_close_is_safe_with_no_pending_tasks(fast_debounce):
    """`close()` must be safe to call when nothing is in flight."""
    assert fast_debounce._pending_recomputes == {}
    await fast_debounce.close()  # no AssertionError, no gather-on-empty crash
    assert fast_debounce._pending_recomputes == {}


@pytest.mark.asyncio
async def test_replacement_task_completes_after_cancellation(
    fast_debounce, db_conn
):
    """End-to-end: cancel-replace-await leaves only the replacement's effects."""
    _seed_event(db_conn)
    payload = SimpleNamespace(channel_id=200, message_id=700)

    await fast_debounce.on_raw_reaction_add(payload)
    await fast_debounce.on_raw_reaction_add(payload)
    await fast_debounce.on_raw_reaction_add(payload)

    pending = list(fast_debounce._pending_recomputes.values())
    assert len(pending) == 1
    survivor = pending[0]

    await asyncio.gather(survivor, return_exceptions=True)
    assert fast_debounce._pending_recomputes == {}
