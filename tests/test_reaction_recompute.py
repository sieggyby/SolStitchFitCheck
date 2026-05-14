"""Reaction recompute path: raw events → 2s debounce → refetch + filter + score.

Plan §4 reaction-handling contract:
- `on_raw_reaction_add` / `on_raw_reaction_remove` schedule a debounced recompute
  keyed by `post_id`. Three events in <DEBOUNCE_SECONDS coalesce into one DB write.
- Recompute filters bot reactions and the author's own self-reactions before
  totaling the score.
- A stale `expected_updated_at` (lost optimistic-lock race) is logged at INFO
  and dropped — no retry loop in V1.
- Recompute skipped for posts not in `discord_streak_events` (no backfill in V1).
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from sable_platform.db import discord_streaks
from sable_platform.db.compat_conn import CompatConnection


class _FakeAsyncIter:
    """Minimal `async for`-compatible iterator over a fixed list."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeReaction:
    def __init__(self, users):
        self._users = list(users)

    def users(self):
        return _FakeAsyncIter(self._users)


def _seed_event(
    conn: CompatConnection,
    *,
    post_id: str = "700",
    user_id: str = "555",
    guild_id: str = "100",
    channel_id: str = "200",
) -> None:
    discord_streaks.upsert_streak_event(
        conn,
        org_id="solstitch",
        guild_id=guild_id,
        channel_id=channel_id,
        post_id=post_id,
        user_id=user_id,
        posted_at="2026-05-12T12:00:00Z",
        counted_for_day="2026-05-12",
        attachment_count=1,
        image_attachment_count=1,
    )


def _build_client(*, bot_id: int, fake_message) -> tuple:
    """Build a discord.Client double rich enough for _recompute_after_delay."""
    channel = SimpleNamespace()
    channel.fetch_message = AsyncMock(return_value=fake_message)
    client = MagicMock()
    client.user = SimpleNamespace(id=bot_id)
    client.get_channel = MagicMock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    return client, channel


def _build_message(*, reactions) -> MagicMock:
    msg = MagicMock()
    msg.reactions = reactions
    return msg


@pytest.fixture
def fast_debounce(monkeypatch, fitcheck_module):
    """Squash the 2-second debounce to 20ms so tests stay snappy."""
    monkeypatch.setattr(fitcheck_module, "DEBOUNCE_SECONDS", 0.02)
    return fitcheck_module


def _payload(channel_id: int = 200, message_id: int = 700) -> SimpleNamespace:
    return SimpleNamespace(channel_id=channel_id, message_id=message_id)


@pytest.mark.asyncio
async def test_debounce_coalesces_three_events_into_one_recompute(
    fast_debounce, db_conn, monkeypatch
):
    """Three reaction events inside the debounce window → exactly one DB update."""
    _seed_event(db_conn)
    fake_message = _build_message(
        reactions=[_FakeReaction(users=[SimpleNamespace(id=1001)])]
    )
    client, _channel = _build_client(bot_id=99999, fake_message=fake_message)
    monkeypatch.setattr(fast_debounce, "_client", client)

    call_count = {"n": 0}
    real_update = discord_streaks.update_reaction_score

    def counting_update(*args, **kwargs):
        call_count["n"] += 1
        return real_update(*args, **kwargs)

    monkeypatch.setattr(
        discord_streaks, "update_reaction_score", counting_update
    )
    monkeypatch.setattr(
        fast_debounce.discord_streaks, "update_reaction_score", counting_update
    )

    await fast_debounce.on_raw_reaction_add(_payload())
    await fast_debounce.on_raw_reaction_add(_payload())
    await fast_debounce.on_raw_reaction_remove(_payload())

    # Wait for the surviving debounce task to fire.
    pending = list(fast_debounce._pending_recomputes.values())
    assert len(pending) == 1
    await asyncio.gather(*pending, return_exceptions=True)

    assert call_count["n"] == 1
    row = db_conn.execute(
        text("SELECT reaction_score FROM discord_streak_events WHERE post_id = '700'")
    ).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_bot_and_self_reactions_excluded_from_score(
    fast_debounce, db_conn, monkeypatch
):
    """Bot id and the post author's id must not count toward `reaction_score`."""
    _seed_event(db_conn, user_id="555")  # author id 555
    reactions = [
        _FakeReaction(
            users=[
                SimpleNamespace(id=99999),  # bot — excluded
                SimpleNamespace(id=555),    # self (author) — excluded
                SimpleNamespace(id=1001),   # real user — counts
                SimpleNamespace(id=1002),   # real user — counts
            ]
        ),
        _FakeReaction(users=[SimpleNamespace(id=1003)]),  # one more real user
    ]
    fake_message = _build_message(reactions=reactions)
    client, _channel = _build_client(bot_id=99999, fake_message=fake_message)
    monkeypatch.setattr(fast_debounce, "_client", client)

    await fast_debounce.on_raw_reaction_add(_payload())
    await asyncio.gather(
        *fast_debounce._pending_recomputes.values(), return_exceptions=True
    )

    row = db_conn.execute(
        text("SELECT reaction_score FROM discord_streak_events WHERE post_id = '700'")
    ).fetchone()
    assert row[0] == 3  # 1001, 1002, 1003


@pytest.mark.asyncio
async def test_stale_write_logged_and_dropped(
    fast_debounce, db_conn, monkeypatch, caplog
):
    """If update_reaction_score returns False, log at INFO and move on (no retry)."""
    _seed_event(db_conn)
    fake_message = _build_message(
        reactions=[_FakeReaction(users=[SimpleNamespace(id=1001)])]
    )
    client, _channel = _build_client(bot_id=99999, fake_message=fake_message)
    monkeypatch.setattr(fast_debounce, "_client", client)

    monkeypatch.setattr(
        fast_debounce.discord_streaks,
        "update_reaction_score",
        lambda *a, **kw: False,
    )

    with caplog.at_level(logging.INFO, logger="sable_roles.fitcheck_streak"):
        await fast_debounce.on_raw_reaction_add(_payload())
        await asyncio.gather(
            *fast_debounce._pending_recomputes.values(), return_exceptions=True
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("lost race" in m and "700" in m for m in messages), messages


@pytest.mark.asyncio
async def test_recompute_skipped_when_post_not_in_db(
    fast_debounce, db_conn, monkeypatch
):
    """No backfill in V1: reactions on an unknown post short-circuit silently."""
    # No _seed_event call — DB has zero rows.
    fake_message = _build_message(reactions=[])
    client, channel = _build_client(bot_id=99999, fake_message=fake_message)
    monkeypatch.setattr(fast_debounce, "_client", client)

    update_calls = {"n": 0}

    def counting_update(*a, **kw):
        update_calls["n"] += 1
        return True

    monkeypatch.setattr(
        fast_debounce.discord_streaks, "update_reaction_score", counting_update
    )

    await fast_debounce.on_raw_reaction_add(_payload())
    await asyncio.gather(
        *fast_debounce._pending_recomputes.values(), return_exceptions=True
    )

    assert update_calls["n"] == 0
    channel.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_recompute_skipped_when_channel_not_a_fitcheck_channel(
    fast_debounce, db_conn, monkeypatch
):
    """Reactions on non-fitcheck channels short-circuit before the DB read."""
    _seed_event(db_conn)
    fake_message = _build_message(reactions=[])
    client, channel = _build_client(bot_id=99999, fake_message=fake_message)
    monkeypatch.setattr(fast_debounce, "_client", client)

    await fast_debounce.on_raw_reaction_add(
        _payload(channel_id=999, message_id=700)  # 999 not in _FITCHECK_CHANNEL_IDS
    )
    await asyncio.gather(
        *fast_debounce._pending_recomputes.values(), return_exceptions=True
    )

    channel.fetch_message.assert_not_awaited()
    row = db_conn.execute(
        text("SELECT reaction_score FROM discord_streak_events WHERE post_id = '700'")
    ).fetchone()
    assert row[0] == 0  # untouched


@pytest.mark.asyncio
async def test_recompute_uses_get_channel_when_cached(
    fast_debounce, db_conn, monkeypatch
):
    """When client.get_channel returns a hit, fetch_channel is not awaited."""
    _seed_event(db_conn)
    fake_message = _build_message(reactions=[])
    client, channel = _build_client(bot_id=99999, fake_message=fake_message)
    monkeypatch.setattr(fast_debounce, "_client", client)

    await fast_debounce.on_raw_reaction_add(_payload())
    await asyncio.gather(
        *fast_debounce._pending_recomputes.values(), return_exceptions=True
    )

    client.get_channel.assert_called_once_with(200)
    client.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_recompute_falls_back_to_fetch_channel_when_cache_miss(
    fast_debounce, db_conn, monkeypatch
):
    """`get_channel` returning None forces `fetch_channel` — covers post-restart path."""
    _seed_event(db_conn)
    fake_message = _build_message(reactions=[])
    client, channel = _build_client(bot_id=99999, fake_message=fake_message)
    client.get_channel = MagicMock(return_value=None)
    monkeypatch.setattr(fast_debounce, "_client", client)

    await fast_debounce.on_raw_reaction_add(_payload())
    await asyncio.gather(
        *fast_debounce._pending_recomputes.values(), return_exceptions=True
    )

    client.get_channel.assert_called_once_with(200)
    client.fetch_channel.assert_awaited_once_with(200)


@pytest.mark.asyncio
async def test_recompute_failure_logs_warning_does_not_crash(
    fast_debounce, db_conn, monkeypatch, caplog
):
    """A discord-side fetch_message exception is logged + swallowed."""
    _seed_event(db_conn)
    client, channel = _build_client(bot_id=99999, fake_message=None)
    channel.fetch_message = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(fast_debounce, "_client", client)

    with caplog.at_level(logging.WARNING, logger="sable_roles.fitcheck_streak"):
        await fast_debounce.on_raw_reaction_add(_payload())
        await asyncio.gather(
            *fast_debounce._pending_recomputes.values(), return_exceptions=True
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("recompute failed" in m for m in messages), messages
