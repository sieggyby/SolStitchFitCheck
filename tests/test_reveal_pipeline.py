"""Pass C: reveal pipeline tests.

Coverage:
  - schedule_reveal_recompute replacement single-tick atomic (cancel + swap)
  - _build_reveal_text format per design §4.2 (with and without `caught:` line)
  - tone_band thresholds
  - handle_raw_reaction_add / remove filter by fitcheck channel + scoring kill switch
  - handle_thread_message filters OP + bot + non-fitcheck channels
  - recompute body — full pipeline:
      * silent state: milestone + low-age audits land, NO public reveal fires
      * revealed state below threshold: nothing
      * revealed state at threshold: reveal fires + audit + CAS lock
      * one-and-done: second recompute on same post bails (CAS check)
      * already-revealed score row: bails
      * invalidated score row: bails
      * failed score row: bails
      * age below min: bails
      * age past window: bails
      * NotFound on fetch_message: bails (delete handler owns cancel)
  - handle_raw_message_delete: emits HIGH cancelled_deleted audit + CAS lock
  - close() drains pending tasks
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import text

from sable_platform.db.discord_fitcheck_scores import upsert_score_success
from sable_platform.db.discord_scoring_config import set_state
from sable_platform.db.discord_streaks import upsert_streak_event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rp_module(monkeypatch, db_conn):
    """Import reveal_pipeline with config + get_db patched.

    Patches every module-level dict so tests don't leak state. Mirrors the
    `fitcheck_module` fixture's discipline.
    """
    from sable_roles.features import reveal_pipeline as mod

    monkeypatch.setattr(mod, "SCORED_MODE_ENABLED", True)
    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(mod, "_FITCHECK_CHANNEL_IDS", {200})
    monkeypatch.setattr(mod, "_CHANNEL_TO_GUILD", {200: "100"})
    monkeypatch.setattr(mod, "_pending_reveals", {})
    monkeypatch.setattr(mod, "_low_age_audited", set())
    monkeypatch.setattr(mod, "REVEAL_DEBOUNCE_SECONDS", 0.0)  # don't sleep

    class _DBContext:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr(mod, "get_db", lambda: _DBContext())
    bot_user = SimpleNamespace(id=99999)
    monkeypatch.setattr(mod, "_client", SimpleNamespace(user=bot_user))
    return mod


def _success_score_kwargs(*, post_id: str = "900", user_id: str = "555", percentile: float = 65.0) -> dict:
    return {
        "org_id": "solstitch",
        "guild_id": "100",
        "post_id": post_id,
        "user_id": user_id,
        "posted_at": "2026-05-12T12:00:00Z",
        "scored_at": "2026-05-12T12:00:05Z",
        "model_id": "claude-sonnet-4-6",
        "prompt_version": "rubric_v1",
        "axis_cohesion": 8,
        "axis_execution": 7,
        "axis_concept": 6,
        "axis_catch": 5,
        "raw_total": 26,
        "catch_detected": None,
        "catch_naming_class": None,
        "description": "neutral fit",
        "confidence": 0.85,
        "axis_rationales_json": "{}",
        "curve_basis": "absolute",
        "pool_size_at_score_time": 0,
        "percentile": percentile,
    }


def _seed_score(db_conn, **overrides) -> None:
    kwargs = _success_score_kwargs(**overrides)
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", kwargs["post_id"], kwargs["user_id"],
        kwargs["posted_at"], "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)


def _backdate_state_change(db_conn, guild_id: str, hours_ago: int) -> None:
    """Backdate discord_scoring_config.state_changed_at so a test post
    seeded as "1 hour ago" satisfies the design-§8.3 `posted_at >=
    state_changed_at` floor. Without this, set_state() stamps
    state_changed_at to wall-clock-now, which is AFTER any back-dated
    posted_at and the reveal-fire gate (correctly) blocks the reveal.
    """
    import sqlalchemy as _sa
    older = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    db_conn.execute(
        _sa.text(
            "UPDATE discord_scoring_config"
            " SET state_changed_at = :older WHERE guild_id = :gid"
        ),
        {"older": older, "gid": guild_id},
    )
    db_conn.commit()


def _make_reaction(*, emoji: str, reactor_ids: list[int]) -> SimpleNamespace:
    """Build a discord.Reaction stub. reactor_ids are user.id values.

    Returns an object with `emoji` (str) and `users()` (async iterator).
    Reactor user objects expose `.id` and `.created_at` (set to a 5-year-old
    UTC date so they don't trip the low-age audit unless the test overrides).
    """
    old_account = datetime(2021, 1, 1, tzinfo=timezone.utc)
    reactors = [SimpleNamespace(id=uid, created_at=old_account) for uid in reactor_ids]

    class _Users:
        def __init__(self, items):
            self._items = items

        def __aiter__(self):
            self._iter = iter(self._items)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    return SimpleNamespace(emoji=emoji, users=lambda: _Users(reactors))


def _make_message_stub(
    *,
    post_id: int = 900,
    author_id: int = 555,
    author_display_name: str = "tester",
    posted_at: datetime | None = None,
    reactions: list | None = None,
    reply_message_id: int = 9999,
    reply_raises: BaseException | None = None,
) -> MagicMock:
    """A discord.Message stub sufficient for reveal_pipeline tests."""
    msg = MagicMock(spec=discord.Message)
    msg.id = post_id
    author = MagicMock()
    author.id = author_id
    author.display_name = author_display_name
    author.bot = False
    msg.author = author
    msg.guild = MagicMock()
    msg.guild.get_member.return_value = None
    msg.created_at = posted_at or datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    msg.reactions = reactions or []

    if reply_raises is not None:
        msg.reply = AsyncMock(side_effect=reply_raises)
    else:
        reply_msg = MagicMock()
        reply_msg.id = reply_message_id
        msg.reply = AsyncMock(return_value=reply_msg)
    return msg


def _make_client_with_message(message, parent_channel=None) -> SimpleNamespace:
    """Build a discord.Client stub whose fetch_channel + fetch_message return
    the given message. parent_channel is the parent text channel.
    """
    channel = parent_channel or MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    channel.get_thread = MagicMock(return_value=None)
    client = SimpleNamespace(
        user=SimpleNamespace(id=99999),
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
    )
    return client


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_reveal_text_with_caught_line(rp_module):
    score = {
        "percentile": 87.3,
        "axis_cohesion": 8,
        "axis_execution": 7,
        "axis_concept": 9,
        "axis_catch": 9,
        "catch_detected": "late-90s Raf energy in the silhouette",
    }
    text_out = rp_module._build_reveal_text(score, "monasex")
    lines = text_out.split("\n")
    assert lines[0] == "monasex's fit just landed. Stitzy says: 87."
    assert lines[1] == "cohesion 8 · execution 7 · concept 9 · catch 9"
    assert lines[2] == "caught: late-90s Raf energy in the silhouette"
    assert len(lines) == 3


def test_build_reveal_text_omits_caught_when_null(rp_module):
    score = {
        "percentile": 58.0,
        "axis_cohesion": 7,
        "axis_execution": 7,
        "axis_concept": 4,
        "axis_catch": 4,
        "catch_detected": None,
    }
    text_out = rp_module._build_reveal_text(score, "sieggy")
    assert "caught:" not in text_out
    assert "Stitzy says: 58." in text_out


def test_build_reveal_text_clamps_percentile_to_1_100(rp_module):
    score = {"percentile": 0.4, "axis_cohesion": 4, "axis_execution": 5, "axis_concept": 6, "axis_catch": 4, "catch_detected": None}
    assert "Stitzy says: 1." in rp_module._build_reveal_text(score, "x")
    score["percentile"] = 130.0
    assert "Stitzy says: 100." in rp_module._build_reveal_text(score, "x")


def test_tone_band_thresholds(rp_module):
    assert rp_module._tone_band(80.0) == "high"
    assert rp_module._tone_band(95.0) == "high"
    assert rp_module._tone_band(40.0) == "mid"
    assert rp_module._tone_band(79.9) == "mid"
    assert rp_module._tone_band(39.9) == "low"
    assert rp_module._tone_band(1.0) == "low"


# ---------------------------------------------------------------------------
# schedule_reveal_recompute replacement semantics
# ---------------------------------------------------------------------------


async def test_schedule_replaces_existing_task_atomically(rp_module):
    """A second schedule for the same post_id cancels the first."""
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()

    async def _hold():
        first_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            first_cancelled.set()
            raise

    first_task = asyncio.create_task(_hold())
    rp_module._pending_reveals[42] = first_task
    await first_started.wait()
    # Now reschedule — should cancel first and store new.
    rp_module.schedule_reveal_recompute(guild_id="100", post_id=42, channel_id=200)
    await first_cancelled.wait()
    assert first_task.cancelled()
    # Drain the replacement so the test loop doesn't leak it.
    await rp_module.close()


# ---------------------------------------------------------------------------
# Event-handler gates (SCORED_MODE_ENABLED + channel filter)
# ---------------------------------------------------------------------------


async def test_handle_raw_reaction_add_skipped_when_disabled(rp_module, monkeypatch):
    monkeypatch.setattr(rp_module, "SCORED_MODE_ENABLED", False)
    payload = SimpleNamespace(channel_id=200, message_id=900)
    await rp_module.handle_raw_reaction_add(payload)
    assert rp_module._pending_reveals == {}


async def test_handle_raw_reaction_add_skipped_for_non_fitcheck_channel(rp_module):
    payload = SimpleNamespace(channel_id=999, message_id=900)
    await rp_module.handle_raw_reaction_add(payload)
    assert rp_module._pending_reveals == {}


async def test_handle_raw_reaction_add_schedules_for_fitcheck_channel(rp_module):
    payload = SimpleNamespace(channel_id=200, message_id=900)
    await rp_module.handle_raw_reaction_add(payload)
    assert 900 in rp_module._pending_reveals
    await rp_module.close()


async def test_handle_thread_message_schedules_for_op_too_recompute_filters(rp_module):
    """OP-filtering happens in `_count_thread_messages` inside the recompute
    body, NOT in handle_thread_message. The handler over-schedules (cheap)
    and lets the debounce coalesce — moving the per-handler fetch_message
    out avoids rate-limit pressure during thread bursts (H5 fix).
    """
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = 200
    thread.id = 900

    op_message = MagicMock()
    op_message.author = SimpleNamespace(id=555, bot=False)
    op_message.guild = MagicMock()
    op_message.channel = thread

    await rp_module.handle_thread_message(op_message)
    # Handler scheduled regardless of OP identity — debounce + recompute
    # body filters OP from the thread count.
    assert 900 in rp_module._pending_reveals
    await rp_module.close()


async def test_handle_thread_message_schedules_for_non_op(rp_module):
    parent_post = _make_message_stub(post_id=900, author_id=555)
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = 200
    thread.id = 900
    thread.fetch_message = AsyncMock(return_value=parent_post)

    other_message = MagicMock()
    other_message.author = SimpleNamespace(id=888, bot=False)
    other_message.guild = MagicMock()
    other_message.channel = thread

    await rp_module.handle_thread_message(other_message)
    assert 900 in rp_module._pending_reveals
    await rp_module.close()


async def test_handle_thread_message_skips_bot_authors(rp_module):
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = 200
    thread.id = 900

    bot_message = MagicMock()
    bot_message.author = SimpleNamespace(id=88888, bot=True)
    bot_message.guild = MagicMock()
    bot_message.channel = thread

    await rp_module.handle_thread_message(bot_message)
    assert rp_module._pending_reveals == {}


# ---------------------------------------------------------------------------
# Recompute body — silent state runs milestone + low-age audits, no reveal
# ---------------------------------------------------------------------------


async def test_silent_state_runs_milestone_audit_no_public_reveal(
    rp_module, db_conn, monkeypatch
):
    """In silent state: milestones land, low-age audits land, NO reply,
    NO reveal_fired_at on the score row.
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    # Seed score with posted_at 1 hour ago (past min age).
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    # 8 unique reactors on 🔥 → crosses 5 + 8 milestones, NOT 10.
    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(1001, 1009)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    # No reply was sent.
    message.reply.assert_not_called()
    # Score row has NO reveal_fired_at.
    row = db_conn.execute(
        text("SELECT reveal_fired_at FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert row["reveal_fired_at"] is None
    # Milestones 5 + 8 landed.
    milestones = db_conn.execute(
        text(
            "SELECT milestone FROM discord_fitcheck_emoji_milestones"
            " WHERE post_id = '900' ORDER BY milestone"
        )
    ).fetchall()
    assert [m["milestone"] for m in milestones] == [5, 8]


# ---------------------------------------------------------------------------
# Recompute body — revealed state, fires at threshold
# ---------------------------------------------------------------------------


async def test_revealed_state_fires_reveal_at_10_unique_reactors(
    rp_module, db_conn, monkeypatch
):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _backdate_state_change(db_conn, "100", hours_ago=2)
    kwargs = _success_score_kwargs(post_id="900", user_id="555", percentile=87.0)
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    kwargs["catch_detected"] = "late-90s Raf energy"
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    # 10 unique reactors on 🔥 — exactly at threshold.
    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(2001, 2011)))
    message = _make_message_stub(
        post_id=900, author_id=555,
        author_display_name="monasex", reactions=[reaction],
    )
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    # Reply was sent.
    message.reply.assert_awaited_once()
    body = message.reply.await_args.kwargs.get("content") or message.reply.await_args.args[0]
    assert "monasex" in body
    assert "Stitzy says: 87." in body
    assert "caught: late-90s Raf energy" in body
    # mention_author=False per design §4.1.
    assert message.reply.await_args.kwargs.get("mention_author") is False
    # CAS lock recorded the reveal post id.
    row = db_conn.execute(
        text(
            "SELECT reveal_fired_at, reveal_post_id, reveal_trigger"
            " FROM discord_fitcheck_scores WHERE post_id = '900'"
        )
    ).fetchone()
    assert row["reveal_fired_at"] is not None
    assert row["reveal_post_id"] == "9999"
    assert row["reveal_trigger"] == "reactions"


async def test_revealed_state_below_threshold_does_not_fire(
    rp_module, db_conn, monkeypatch
):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    # 9 reactors — under threshold of 10.
    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(2001, 2010)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    # No parent_channel.get_thread → thread count = 0
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    message.reply.assert_not_called()


# ---------------------------------------------------------------------------
# One-and-done
# ---------------------------------------------------------------------------


async def test_already_revealed_score_bails(rp_module, db_conn, monkeypatch):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)
    # Pre-fire reveal so the CAS guard trips.
    db_conn.execute(
        text(
            "UPDATE discord_fitcheck_scores"
            " SET reveal_fired_at = '2026-05-13T00:00:00Z',"
            "     reveal_post_id = 'already_fired',"
            "     reveal_trigger = 'reactions'"
            " WHERE post_id = '900'"
        )
    )
    db_conn.commit()

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(3001, 3011)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    message.reply.assert_not_called()


async def test_invalidated_score_bails(rp_module, db_conn, monkeypatch):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)
    db_conn.execute(
        text(
            "UPDATE discord_fitcheck_scores"
            " SET invalidated_at = '2026-05-13T00:00:00Z',"
            "     invalidated_reason = 'mod review'"
            " WHERE post_id = '900'"
        )
    )
    db_conn.commit()

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(4001, 4011)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    message.reply.assert_not_called()


# ---------------------------------------------------------------------------
# Age floor + window
# ---------------------------------------------------------------------------


async def test_age_below_min_age_bails(rp_module, db_conn, monkeypatch):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    # Posted 30s ago → under default 10-min min age.
    just_now = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = just_now
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        just_now, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(5001, 5011)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    message.reply.assert_not_called()


async def test_age_past_window_bails(rp_module, db_conn, monkeypatch):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    # Posted 8 days ago → past default 7-day window.
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = eight_days_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        eight_days_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(6001, 6011)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    message.reply.assert_not_called()


# ---------------------------------------------------------------------------
# NotFound on message fetch (delete-during-recompute)
# ---------------------------------------------------------------------------


async def test_notfound_on_fetch_bails_silently(rp_module, db_conn, monkeypatch):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _seed_score(db_conn)

    # Override posted_at to a past, valid age.
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_conn.execute(
        text("UPDATE discord_fitcheck_scores SET posted_at = :p WHERE post_id = '900'"),
        {"p": one_hour_ago},
    )
    db_conn.commit()

    channel = MagicMock()
    channel.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "not found")
    )
    channel.get_thread = MagicMock(return_value=None)
    client = SimpleNamespace(
        user=SimpleNamespace(id=99999),
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(rp_module, "_client", client)

    # Must not raise.
    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    row = db_conn.execute(
        text("SELECT reveal_fired_at FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert row["reveal_fired_at"] is None


# ---------------------------------------------------------------------------
# handle_raw_message_delete — cancel + lock + HIGH audit
# ---------------------------------------------------------------------------


async def test_delete_during_pending_reveal_emits_cancelled_audit(
    rp_module, db_conn, monkeypatch
):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _seed_score(db_conn)

    payload = SimpleNamespace(channel_id=200, message_id=900)
    await rp_module.handle_raw_message_delete(payload)

    # CAS lock takes; reveal_trigger set to 'cancelled_deleted'.
    row = db_conn.execute(
        text(
            "SELECT reveal_fired_at, reveal_post_id, reveal_trigger"
            " FROM discord_fitcheck_scores WHERE post_id = '900'"
        )
    ).fetchone()
    assert row["reveal_fired_at"] is not None
    assert row["reveal_post_id"] is None
    assert row["reveal_trigger"] == "cancelled_deleted"

    # HIGH severity audit row landed.
    audit = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_reveal_cancelled_deleted' LIMIT 1"
        )
    ).fetchone()
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["severity"] == "HIGH"
    assert detail["post_id"] == "900"


async def test_delete_after_reveal_fired_is_no_op(rp_module, db_conn):
    """Already-fired reveals don't get cancelled_deleted audits — that would
    be a misleading signal (the reveal already shipped).
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _seed_score(db_conn)
    db_conn.execute(
        text(
            "UPDATE discord_fitcheck_scores"
            " SET reveal_fired_at = '2026-05-13T00:00:00Z',"
            "     reveal_post_id = 'already_revealed',"
            "     reveal_trigger = 'reactions'"
            " WHERE post_id = '900'"
        )
    )
    db_conn.commit()

    payload = SimpleNamespace(channel_id=200, message_id=900)
    await rp_module.handle_raw_message_delete(payload)

    audit_n = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action = 'fitcheck_reveal_cancelled_deleted'"
        )
    ).fetchone()
    assert audit_n["n"] == 0


async def test_delete_for_unscored_post_is_no_op(rp_module, db_conn):
    # No score row exists.
    payload = SimpleNamespace(channel_id=200, message_id=900)
    await rp_module.handle_raw_message_delete(payload)
    audit_n = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action = 'fitcheck_reveal_cancelled_deleted'"
        )
    ).fetchone()
    assert audit_n["n"] == 0


async def test_delete_cancels_pending_recompute_task(rp_module, db_conn):
    """When a fitcheck post with a score row is deleted, any in-flight
    reveal-recompute task for that post is cancelled (so it doesn't race
    the delete-handler to a NotFound).
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _seed_score(db_conn)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _hold():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_hold())
    rp_module._pending_reveals[900] = task
    await started.wait()  # let task body run into the sleep

    payload = SimpleNamespace(channel_id=200, message_id=900)
    await rp_module.handle_raw_message_delete(payload)
    # Yield once so the cancellation propagates through the event loop.
    await asyncio.sleep(0)
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    assert cancelled.is_set()
    assert 900 not in rp_module._pending_reveals


# ---------------------------------------------------------------------------
# close() drain
# ---------------------------------------------------------------------------


async def test_close_drains_in_flight_tasks(rp_module):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _hold():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_hold())
    rp_module._pending_reveals[7] = task
    await started.wait()
    await rp_module.close()
    assert cancelled.is_set()
    assert rp_module._pending_reveals == {}


# ---------------------------------------------------------------------------
# Low-age reactor audit
# ---------------------------------------------------------------------------


async def test_low_age_reactor_emits_audit_with_account_created_at(
    rp_module, db_conn, monkeypatch
):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    # Single low-age reactor: account created 5 days ago.
    young_account = datetime.now(timezone.utc) - timedelta(days=5)
    young = SimpleNamespace(id=8888, created_at=young_account)

    class _Users:
        def __init__(self):
            self._items = [young]

        def __aiter__(self):
            self._iter = iter(self._items)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    reaction = SimpleNamespace(emoji="🔥", users=lambda: _Users())
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    audit = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_low_age_reactor' LIMIT 1"
        )
    ).fetchone()
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["reactor_user_id"] == "8888"
    assert "reactor_account_created_at" in detail
    assert detail["reactor_account_age_days"] < 30


async def test_low_age_dedup_blocks_second_audit_same_pair(
    rp_module, db_conn, monkeypatch
):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    young_account = datetime.now(timezone.utc) - timedelta(days=5)

    def _make_react():
        young = SimpleNamespace(id=8888, created_at=young_account)

        class _Users:
            def __aiter__(self):
                self._iter = iter([young])
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        return SimpleNamespace(emoji="🔥", users=lambda: _Users())

    msg1 = _make_message_stub(post_id=900, author_id=555, reactions=[_make_react()])
    client = _make_client_with_message(msg1)
    monkeypatch.setattr(rp_module, "_client", client)
    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    # Second recompute with the SAME reactor — dedup must block the second
    # audit row.
    msg2 = _make_message_stub(post_id=900, author_id=555, reactions=[_make_react()])
    client2 = _make_client_with_message(msg2)
    monkeypatch.setattr(rp_module, "_client", client2)
    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    rows = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action = 'fitcheck_low_age_reactor'"
        )
    ).fetchone()
    assert rows["n"] == 1


# ---------------------------------------------------------------------------
# Milestone dedup (the SP CAS does the heavy lifting — verify the audit row
# fires exactly once per crossing)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §8.3 strict: silent-period posts never reveal post-flip
# ---------------------------------------------------------------------------


async def test_pre_revealed_flip_post_does_not_reveal_after_flip(
    rp_module, db_conn, monkeypatch
):
    """A fit scored under SILENT whose 10th reactor lands AFTER a
    Silent → Revealed flip MUST NOT reveal. Implements design §8.3
    "Reveals only on fits posted *after* the transition."

    Tested by seeding the post 2 hours ago, flipping to revealed NOW
    (so state_changed_at > posted_at), then triggering a recompute with
    10 reactors. Reveal must NOT fire; milestones DO fire.
    """
    # 1) Seed score in silent mode, posted_at = 2 hours ago.
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    kwargs = _success_score_kwargs(post_id="900", user_id="555", percentile=87.0)
    kwargs["posted_at"] = two_hours_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        two_hours_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    # 2) Flip to revealed NOW — state_changed_at is now (post-posted_at).
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")

    # 3) 10 reactors. Threshold trips; reveal MUST NOT fire (pre-flip post).
    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(11_001, 11_011)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    # Crucial: no reply, no reveal_fired_at on the row.
    message.reply.assert_not_called()
    row = db_conn.execute(
        text("SELECT reveal_fired_at FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert row["reveal_fired_at"] is None
    # But milestones DID fire (calibration signals run in both states).
    milestones = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM discord_fitcheck_emoji_milestones"
            " WHERE post_id = '900'"
        )
    ).fetchone()
    assert milestones["n"] >= 1  # at least the 5-milestone, probably 5+8+10


async def test_post_flip_post_does_reveal(rp_module, db_conn, monkeypatch):
    """The symmetric positive case: a post made AFTER the Silent→Revealed
    transition DOES reveal at threshold.
    """
    # 1) Flip to revealed FIRST + backdate state_changed_at so the test's
    # back-dated posted_at lands AFTER the transition.
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _backdate_state_change(db_conn, "100", hours_ago=1)

    thirty_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    kwargs = _success_score_kwargs(post_id="901", user_id="555", percentile=87.0)
    kwargs["posted_at"] = thirty_min_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "901", "555",
        thirty_min_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(12_001, 12_011)))
    message = _make_message_stub(post_id=901, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=901, channel_id=200,
    )

    # Post-flip post: reveal MUST fire.
    message.reply.assert_awaited_once()
    row = db_conn.execute(
        text("SELECT reveal_fired_at, reveal_trigger FROM discord_fitcheck_scores WHERE post_id = '901'")
    ).fetchone()
    assert row["reveal_fired_at"] is not None
    assert row["reveal_trigger"] == "reactions"


async def test_revealed_state_with_null_state_changed_at_fails_closed(
    rp_module, db_conn, monkeypatch
):
    """Defensive case: live state is 'revealed' but state_changed_at is
    NULL (data inconsistency — shouldn't happen via the supported state-
    transition path). Reveal must fail-closed; no publish, log a warning.
    """
    # Hand-craft an inconsistent config row: state='revealed' but
    # state_changed_at NULL. Bypasses set_state which always populates it.
    import sqlalchemy as _sa
    db_conn.execute(
        _sa.text(
            "INSERT INTO discord_scoring_config"
            " (org_id, guild_id, state, state_changed_at, state_changed_by)"
            " VALUES ('solstitch', '100', 'revealed', NULL, NULL)"
        )
    )
    db_conn.commit()

    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(13_001, 13_011)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    # Fail-closed: no reply, no reveal_fired_at.
    message.reply.assert_not_called()
    row = db_conn.execute(
        text("SELECT reveal_fired_at FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert row["reveal_fired_at"] is None


# ---------------------------------------------------------------------------
# Publish-failure paths — C1 + C2 + H1 + M7 coverage
# ---------------------------------------------------------------------------


async def test_reveal_publish_404_routes_to_cancelled_deleted_HIGH_audit(
    rp_module, db_conn, monkeypatch
):
    """The in-process CAS race where reveal-fire wins but the post got
    deleted DURING the publish window: reply raises 404. The recompute
    body must route to `fitcheck_reveal_cancelled_deleted` HIGH-severity
    (NOT `fitcheck_reveal_publish_failed`) — preserving the design-§6.4
    "yank-before-reveal" gaming-vector signal that would otherwise be
    silently swallowed by the CAS racing the delete handler.
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _backdate_state_change(db_conn, "100", hours_ago=2)
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(7001, 7011)))
    message = _make_message_stub(
        post_id=900, author_id=555, reactions=[reaction],
        reply_raises=discord.NotFound(MagicMock(status=404), "msg gone"),
    )
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    # Score row: lock converted from 'pending' to 'cancelled_deleted'.
    row = db_conn.execute(
        text(
            "SELECT reveal_trigger, reveal_post_id, reveal_fired_at"
            " FROM discord_fitcheck_scores WHERE post_id = '900'"
        )
    ).fetchone()
    assert row["reveal_trigger"] == "cancelled_deleted"
    assert row["reveal_post_id"] is None
    assert row["reveal_fired_at"] is not None  # lock preserved

    # Audit: HIGH severity cancelled_deleted with via=publish_404.
    audit = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_reveal_cancelled_deleted' LIMIT 1"
        )
    ).fetchone()
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["severity"] == "HIGH"
    assert detail["via"] == "publish_404"

    # Crucially: NO publish_failed audit row (publish_404 is a delete-vector,
    # not a transport failure).
    pf_n = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action = 'fitcheck_reveal_publish_failed'"
        )
    ).fetchone()
    assert pf_n["n"] == 0


async def test_reveal_publish_5xx_routes_to_publish_failed(
    rp_module, db_conn, monkeypatch
):
    """Non-404 HTTPException on publish → fitcheck_reveal_publish_failed
    audit + lock converted to 'publish_failed' trigger. The lock stays
    in place (no retry) so the next eligible recompute short-circuits.
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _backdate_state_change(db_conn, "100", hours_ago=2)
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(7101, 7111)))
    message = _make_message_stub(
        post_id=900, author_id=555, reactions=[reaction],
        reply_raises=discord.HTTPException(MagicMock(status=503), "internal error"),
    )
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    row = db_conn.execute(
        text(
            "SELECT reveal_trigger, reveal_post_id, reveal_fired_at"
            " FROM discord_fitcheck_scores WHERE post_id = '900'"
        )
    ).fetchone()
    assert row["reveal_trigger"] == "publish_failed"
    assert row["reveal_post_id"] is None
    assert row["reveal_fired_at"] is not None  # lock preserved

    audit = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_reveal_publish_failed' LIMIT 1"
        )
    ).fetchone()
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["trigger"] == "reactions"
    assert detail["tone_band"] in ("high", "mid", "low")

    # Crucially: NO cancelled_deleted audit row (transport failure isn't
    # a delete-vector — those are HIGH-severity gaming signals).
    cd_n = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action = 'fitcheck_reveal_cancelled_deleted'"
        )
    ).fetchone()
    assert cd_n["n"] == 0


async def test_reveal_publish_success_uses_allowed_mentions_none(
    rp_module, db_conn, monkeypatch
):
    """The reply call MUST pass allowed_mentions=discord.AllowedMentions.none()
    to defend against a malicious display_name embedding @everyone / <@id>
    text. mention_author=False alone doesn't cover arbitrary mentions in
    the body content.
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _backdate_state_change(db_conn, "100", hours_ago=2)
    kwargs = _success_score_kwargs(post_id="900", user_id="555", percentile=87.0)
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(8001, 8011)))
    # Display name with an @everyone payload — must not actually ping.
    message = _make_message_stub(
        post_id=900, author_id=555,
        author_display_name="@everyone <@&12345>",
        reactions=[reaction],
    )
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    message.reply.assert_awaited_once()
    kwargs_passed = message.reply.await_args.kwargs
    am = kwargs_passed.get("allowed_mentions")
    assert am is not None
    # discord.AllowedMentions.none() sets ALL four fields to False (the
    # contract is "no mentions of any kind"); a partial assertion would
    # let a regression that flipped replied_user back to True slip
    # through silently.
    assert am.everyone is False
    assert am.users is False
    assert am.roles is False
    assert am.replied_user is False


async def test_reveal_finalises_post_id_via_update_helper(
    rp_module, db_conn, monkeypatch
):
    """Happy path: after a successful reply, the placeholder 'pending'
    reveal_post_id MUST be swapped for the real reply message id via the
    guarded `update_reveal_post_id` helper. Test ensures the swap lands
    AND that the trigger stays at 'reactions' (not flipped to a failure
    terminal).
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    _backdate_state_change(db_conn, "100", hours_ago=2)
    kwargs = _success_score_kwargs(post_id="900", user_id="555", percentile=87.0)
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(9001, 9011)))
    message = _make_message_stub(
        post_id=900, author_id=555, reactions=[reaction],
        reply_message_id=4242,
    )
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    row = db_conn.execute(
        text(
            "SELECT reveal_trigger, reveal_post_id FROM discord_fitcheck_scores"
            " WHERE post_id = '900'"
        )
    ).fetchone()
    assert row["reveal_post_id"] == "4242"
    assert row["reveal_trigger"] == "reactions"
    # No stray 'pending' anywhere.
    assert row["reveal_post_id"] != "pending"


# ---------------------------------------------------------------------------
# State-flip race (H4) — re-read live cfg right before reveal-fire CAS
# ---------------------------------------------------------------------------


async def test_mid_recompute_revealed_to_silent_flip_aborts_publish(
    rp_module, db_conn, monkeypatch
):
    """A mod flipping Revealed → Silent mid-recompute (e.g. emergency
    rollback) must prevent the in-flight recompute from firing its reveal,
    even though the original `cfg` read returned 'revealed'.

    Simulated by patching discord_scoring_config.get_config so the FIRST
    call (early-body) returns 'revealed' and the SECOND call (pre-CAS
    re-read) returns 'silent'. The reveal must NOT publish.
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="revealed", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    from sable_platform.db import discord_scoring_config as dsc

    real_get_config = dsc.get_config
    call_counter = {"n": 0}

    def _flipping(conn, guild_id):
        call_counter["n"] += 1
        cfg = real_get_config(conn, guild_id)
        if call_counter["n"] >= 2:
            # The second call (the live re-read) returns silent.
            cfg = dict(cfg)
            cfg["state"] = "silent"
        return cfg

    monkeypatch.setattr(rp_module.discord_scoring_config, "get_config", _flipping)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(10_001, 10_011)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    message.reply.assert_not_called()
    row = db_conn.execute(
        text("SELECT reveal_fired_at FROM discord_fitcheck_scores WHERE post_id = '900'")
    ).fetchone()
    assert row["reveal_fired_at"] is None


# ---------------------------------------------------------------------------
# Silent state low-age audit (M8 coverage gap)
# ---------------------------------------------------------------------------


async def test_silent_state_emits_low_age_reactor_audit(
    rp_module, db_conn, monkeypatch
):
    """Explicit coverage: silent state runs BOTH milestone AND low-age
    audits. Pass A+B's deferred items both land in silent + revealed.
    """
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    young_account = datetime.now(timezone.utc) - timedelta(days=3)

    class _Users:
        def __init__(self, items):
            self._items = items

        def __aiter__(self):
            self._iter = iter(self._items)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    young = SimpleNamespace(id=7777, created_at=young_account)
    reaction = SimpleNamespace(emoji="🔥", users=lambda: _Users([young]))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )

    audit = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_low_age_reactor' LIMIT 1"
        )
    ).fetchone()
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["scoring_state"] == "silent"


# ---------------------------------------------------------------------------
# _pending_reveals cap (M2)
# ---------------------------------------------------------------------------


async def test_pending_reveals_dict_capped_evicts_oldest(rp_module, monkeypatch):
    """When _pending_reveals hits the cap, scheduling a new distinct
    post_id evicts the oldest entry (cancels its task and removes from
    dict). Mirrors V1 fitcheck_streak's dict pattern with an explicit
    cap to bound burst-scenario memory growth.
    """
    monkeypatch.setattr(rp_module, "_PENDING_REVEALS_CAP", 3)
    # Fill the cap with sentinel tasks (must be real awaitables so cancel()
    # is meaningful).
    async def _hold():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    for pid in (101, 102, 103):
        rp_module._pending_reveals[pid] = asyncio.create_task(_hold())

    rp_module.schedule_reveal_recompute(guild_id="100", post_id=104, channel_id=200)
    # Oldest (101) should be evicted.
    assert 101 not in rp_module._pending_reveals
    assert 104 in rp_module._pending_reveals
    await rp_module.close()


async def test_milestone_audit_emitted_exactly_once_per_crossing(
    rp_module, db_conn, monkeypatch
):
    set_state(db_conn, org_id="solstitch", guild_id="100", state="silent", updated_by="ADMIN")
    kwargs = _success_score_kwargs(post_id="900", user_id="555")
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs["posted_at"] = one_hour_ago
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "900", "555",
        one_hour_ago, "2026-05-12", 1, 1,
    )
    upsert_score_success(db_conn, **kwargs)

    reaction = _make_reaction(emoji="🔥", reactor_ids=list(range(1001, 1006)))
    message = _make_message_stub(post_id=900, author_id=555, reactions=[reaction])
    client = _make_client_with_message(message)
    monkeypatch.setattr(rp_module, "_client", client)

    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    await rp_module._recompute_after_delay(
        guild_id="100", post_id=900, channel_id=200,
    )
    # Two recomputes; one milestone (5) crossing → exactly one audit row.
    # Parse JSON in Python (not LIKE-substring) so the assertion isn't
    # brittle to formatter whitespace changes.
    rows = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_reaction_milestone'"
        )
    ).fetchall()
    milestone_5_rows = [
        r for r in rows if json.loads(r["detail_json"]).get("milestone") == 5
    ]
    assert len(milestone_5_rows) == 1


# ---------------------------------------------------------------------------
# H2 verification — production-path reference-identity (NIT-N4 from QA r2)
# ---------------------------------------------------------------------------


def test_register_binds_fitcheck_tables_by_reference(monkeypatch):
    """Round-2 QA NIT-N4: round-1 H2 fix replaced `set(...)`/`dict(...)`
    snapshots with reference binds, but tests previously only exercised
    monkeypatched module attributes. This test calls register() against a
    stub client and confirms reveal_pipeline holds IDENTITY references to
    fitcheck_streak's reverse-lookup tables.
    """
    from sable_roles.features import fitcheck_streak as fs
    from sable_roles.features import reveal_pipeline as rp

    # Use real (non-monkeypatched) fitcheck_streak tables for this test.
    # Both modules' fields must point at the SAME underlying set/dict.
    stub_client = MagicMock()
    stub_client.event = lambda func: func  # @client.event is set-attribute
    rp.register(stub_client)
    assert rp._FITCHECK_CHANNEL_IDS is fs._FITCHECK_CHANNEL_IDS
    assert rp._CHANNEL_TO_GUILD is fs._CHANNEL_TO_GUILD
