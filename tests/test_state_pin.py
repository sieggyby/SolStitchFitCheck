"""State-pin surface tests.

Coverage per state-pin plan §10:

  - _format_body: headline prefix, plain-text mod identity, no
    prompt_version / model_id leak, state line presence.
  - announce_state_change paths:
      * first-ever pin (no prior row, optimistic lock NOT applied)
      * replace-prior-pin (prior exists, message alive)
      * replace-prior-pin (prior exists, message deleted by mod — 404)
      * replace-prior-pin (channel moved — drift audit fires)
      * missing OPS_CHANNELS entry → no-op + LOW audit
      * channel.send raises HTTPException → WARN audit + no DB write
      * channel.pin raises → WARN audit + best-effort delete of new msg
      * optimistic-lock loss → loser self-deletes + LOW audit
      * lock-loss self-cleanup ALSO fails → HIGH audit
  - Coalescing: 2 announces same key, prior cancelled, only last completes.
  - Per-channel lock: 2 announces same channel different characteristics
    serialize (no Discord rate-limit hit).
  - close() drain: cancel + await + clear (G1).
  - Self-identity pop guard (G2).
  - Same-state no-op flips don't call announce (G8 — integration tests
    below cover this in the call-site handlers).
  - sweep_orphan_pins: orphan-in-channel-not-in-DB gets unpinned +
    audited.
  - Entry-side ValueError for unknown characteristic (PR6-L2).

Test runtime style: mocks discord.Client / channel / message and uses
the shared `db_conn` in-memory CompatConnection so DB writes land
through real SP helpers + audit_log queries can verify behavior.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import text

from sable_platform.db.discord_state_pins import (
    get_state_pin,
    upsert_state_pin,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sp_module(monkeypatch, db_conn):
    """Import state_pin with config + get_db patched.

    AGENTS.md: module-level dicts MUST be reset via `.clear()`, not
    rebound. Other features (call sites) hold no reference to these
    dicts, but we follow the convention to keep tests cheap to copy.
    """
    from sable_roles.features import state_pin as mod

    monkeypatch.setitem(mod.OPS_CHANNELS, "100", "300")
    mod._pending_announcements.clear()
    mod._channel_locks.clear()

    class _DBContext:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr(mod, "get_db", lambda: _DBContext())
    # Patch the leaderboard display-name cache + resolver so tests don't
    # touch a real Discord client.
    from sable_roles.features import leaderboard as lb
    lb._DISPLAY_NAME_CACHE.clear()

    async def _stub_resolve(client, user_id):
        return f"user_{user_id}"

    monkeypatch.setattr(mod, "_resolve_display_name", _stub_resolve)

    yield mod
    mod._pending_announcements.clear()
    mod._channel_locks.clear()


def _make_client(*, bot_user_id: int = 99999) -> SimpleNamespace:
    """A discord.Client stub. .get_channel returns a default-configured
    channel for id 300; tests override per-call via channel_factory."""
    bot_user = SimpleNamespace(id=bot_user_id)
    client = SimpleNamespace(user=bot_user, _channels={})

    def get_channel(channel_id):
        return client._channels.get(int(channel_id))

    client.get_channel = get_channel
    return client


def _make_channel(
    *,
    channel_id: int = 300,
    pinned_messages: list | None = None,
    send_raises: BaseException | None = None,
    pin_raises: BaseException | None = None,
    pins_raises: BaseException | None = None,
    sent_message_id: int = 5000,
):
    """A channel stub with the methods used by state_pin._do_announce."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id

    new_msg = MagicMock(spec=discord.Message)
    new_msg.id = sent_message_id
    new_msg.pin = AsyncMock()
    if pin_raises is not None:
        new_msg.pin.side_effect = pin_raises
    new_msg.unpin = AsyncMock()
    new_msg.delete = AsyncMock()

    if send_raises is not None:
        channel.send = AsyncMock(side_effect=send_raises)
    else:
        channel.send = AsyncMock(return_value=new_msg)

    if pins_raises is not None:
        channel.pins = AsyncMock(side_effect=pins_raises)
    else:
        channel.pins = AsyncMock(return_value=list(pinned_messages or []))

    # fetch_message used by _best_effort_unpin + _best_effort_unpin_and_delete.
    fetched = MagicMock(spec=discord.Message)
    fetched.unpin = AsyncMock()
    fetched.delete = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=fetched)

    channel._new_msg = new_msg
    channel._fetched_msg = fetched
    return channel


def _audit_rows(db_conn, action_prefix: str = "fitcheck_state_pin"):
    rows = db_conn.execute(
        text(
            "SELECT actor, action, detail_json FROM audit_log"
            " WHERE action LIKE :prefix ORDER BY id ASC"
        ),
        {"prefix": f"{action_prefix}%"},
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# _format_body
# ---------------------------------------------------------------------------


async def test_format_body_headline_prefix_and_state_line(sp_module):
    client = _make_client()
    body = await sp_module._format_body(
        client, "scoring",
        "state: silent\nthreshold: 10 reactions on one emoji",
        402620324744790017,
    )
    # Headline prefix present (PR2-M5 module constant).
    assert body.startswith(sp_module._STATE_HEADLINE_PREFIX)
    assert "**stitzy state · scoring**" in body
    # state: line preserved verbatim from caller summary.
    assert "state: silent" in body
    # Mod identity is plain-text "@<name> (id: <user_id>)" per P9.
    assert "by @user_402620324744790017 (id: 402620324744790017)" in body
    # `last changed:` line present.
    assert "last changed:" in body


@pytest.mark.parametrize(
    "characteristic",
    ["scoring", "burn_mode", "relax_mode", "personalize_mode"],
)
async def test_format_body_round_trips_through_sweep_parser(
    sp_module, characteristic,
):
    """R1-M6: the formatter's headline shape (with `**` opening AND
    closing) round-trips cleanly through the sweep's parser for every
    valid characteristic. A future format change to either side that
    breaks this round-trip will trip this test rather than silently
    leaving orphans the sweep can't recover."""
    client = _make_client()
    body = await sp_module._format_body(
        client, characteristic, f"state: foo", 555,
    )
    parsed = sp_module._extract_characteristic_from_headline(body)
    assert parsed == characteristic


async def test_format_body_no_leak_of_prompt_or_model_id(sp_module):
    """Plan P10: pinned body excludes prompt_version + model_id. Those
    operational tells stay in audit_log detail."""
    client = _make_client()
    body = await sp_module._format_body(
        client, "scoring",
        "state: silent\nthreshold: 10",
        555,
    )
    assert "rubric_v1" not in body
    assert "claude-sonnet-4-6" not in body
    assert "prompt_version" not in body
    assert "model_id" not in body


# ---------------------------------------------------------------------------
# announce_state_change — entry-side defense
# ---------------------------------------------------------------------------


async def test_announce_state_change_rejects_unknown_characteristic(sp_module):
    """PR6-L2: unknown characteristic raises ValueError at entry rather
    than landing a malformed pin the sweep can't recover."""
    client = _make_client()
    with pytest.raises(ValueError, match="unknown characteristic"):
        await sp_module.announce_state_change(
            client,
            guild_id="100",
            org_id="solstitch",
            characteristic="scoring_state",  # typo — not in whitelist
            new_state_summary="state: silent",
            changed_by_user_id=555,
        )


# ---------------------------------------------------------------------------
# announce_state_change — happy paths
# ---------------------------------------------------------------------------


async def test_announce_first_ever_pin_posts_and_upserts(sp_module, db_conn):
    """No prior pin row → channel.send + new_msg.pin + upsert with
    expected_updated_at=None. Audit row: fitcheck_state_pin_posted."""
    client = _make_client()
    channel = _make_channel(sent_message_id=5000)
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: silent\nthreshold: 10",
        changed_by_user_id=555,
    )

    channel.send.assert_called_once()
    sent_kwargs = channel.send.call_args
    body = sent_kwargs.args[0]
    assert "stitzy state · scoring" in body
    # AllowedMentions.none() contract (matches reveal_pipeline + leaderboard).
    assert sent_kwargs.kwargs["allowed_mentions"].everyone is False
    channel._new_msg.pin.assert_called_once()

    pin_row = get_state_pin(db_conn, "100", "scoring")
    assert pin_row is not None
    assert pin_row["message_id"] == "5000"
    assert pin_row["channel_id"] == "300"

    audits = _audit_rows(db_conn)
    assert any(a["action"] == "fitcheck_state_pin_posted" for a in audits)


async def test_announce_replaces_prior_pin_and_unpins_old(
    sp_module, db_conn,
):
    """Prior pin exists in same channel → upsert with the optimistic
    lock token; channel.fetch_message + msg.unpin on the prior id."""
    upsert_state_pin(db_conn, "100", "scoring", "300", "1234", "2026-05-17T19:00:00Z")
    client = _make_client()
    channel = _make_channel(sent_message_id=5001)
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: revealed\nthreshold: 10",
        changed_by_user_id=555,
    )

    # Old pin fetched + unpinned (best-effort).
    channel.fetch_message.assert_any_call(1234)
    channel._fetched_msg.unpin.assert_called_once()
    # New row in DB.
    row = get_state_pin(db_conn, "100", "scoring")
    assert row["message_id"] == "5001"


async def test_announce_replaces_prior_pin_when_old_message_404s(
    sp_module, db_conn,
):
    """Mod manually deleted the old pin between rotations. _best_effort_unpin
    swallows NotFound; the new pin still lands cleanly."""
    upsert_state_pin(db_conn, "100", "scoring", "300", "1234", "2026-05-17T19:00:00Z")
    client = _make_client()
    channel = _make_channel(sent_message_id=5002)
    channel.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "gone"),
    )
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: revealed",
        changed_by_user_id=555,
    )

    row = get_state_pin(db_conn, "100", "scoring")
    assert row["message_id"] == "5002"


async def test_announce_audits_channel_drift_when_ops_channel_moved(
    sp_module, db_conn,
):
    """Prior pin's channel_id != current OPS channel id → audit
    fitcheck_state_pin_channel_moved (LOW) + skip cleanup of old channel."""
    upsert_state_pin(
        db_conn, "100", "scoring", "999", "1234", "2026-05-17T19:00:00Z",
    )
    client = _make_client()
    channel = _make_channel(sent_message_id=5003)
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: revealed",
        changed_by_user_id=555,
    )

    audits = _audit_rows(db_conn)
    drift_audits = [
        a for a in audits if a["action"] == "fitcheck_state_pin_channel_moved"
    ]
    assert len(drift_audits) == 1
    import json
    detail = json.loads(drift_audits[0]["detail_json"])
    assert detail["prior_channel"] == "999"
    assert detail["new_channel"] == "300"
    assert detail["prior_message_id"] == "1234"  # PR2-M2


# ---------------------------------------------------------------------------
# announce_state_change — degraded paths
# ---------------------------------------------------------------------------


async def test_announce_no_ops_channel_audits_low_and_returns(
    sp_module, db_conn, monkeypatch,
):
    """OPS_CHANNELS empty for the guild → LOW audit + no behavior."""
    sp_module.OPS_CHANNELS.clear()
    client = _make_client()
    channel = _make_channel()
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: silent",
        changed_by_user_id=555,
    )

    channel.send.assert_not_called()
    audits = _audit_rows(db_conn)
    assert any(
        a["action"] == "fitcheck_state_pin_no_ops_channel" for a in audits
    )
    assert get_state_pin(db_conn, "100", "scoring") is None


async def test_announce_channel_unavailable_audits_and_returns(
    sp_module, db_conn,
):
    """client.get_channel returns None (bot doesn't see the ops channel
    in its gateway cache) → WARN audit + no behavior."""
    client = _make_client()
    # No channel registered for id 300 → get_channel returns None.

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: silent",
        changed_by_user_id=555,
    )

    audits = _audit_rows(db_conn)
    assert any(
        a["action"] == "fitcheck_state_pin_channel_unavailable"
        for a in audits
    )


async def test_announce_send_raises_audits_failed_no_db_write(
    sp_module, db_conn,
):
    """channel.send raises HTTPException → fitcheck_state_pin_failed
    (step=send) WARN + no DB row written."""
    client = _make_client()
    channel = _make_channel(
        send_raises=discord.HTTPException(MagicMock(status=429), "rate limit"),
    )
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: silent",
        changed_by_user_id=555,
    )

    audits = _audit_rows(db_conn)
    fails = [
        a for a in audits if a["action"] == "fitcheck_state_pin_failed"
    ]
    assert len(fails) == 1
    import json
    assert json.loads(fails[0]["detail_json"])["step"] == "send"
    assert get_state_pin(db_conn, "100", "scoring") is None


async def test_announce_pin_raises_audits_failed_and_deletes_unpinned(
    sp_module, db_conn,
):
    """new_msg.pin raises → audit (step=pin) WARN + best-effort delete
    of the just-posted-but-unpinned message + no DB row written."""
    client = _make_client()
    channel = _make_channel(
        pin_raises=discord.HTTPException(MagicMock(status=500), "boom"),
        sent_message_id=5010,
    )
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: silent",
        changed_by_user_id=555,
    )

    audits = _audit_rows(db_conn)
    fails = [
        a for a in audits if a["action"] == "fitcheck_state_pin_failed"
    ]
    assert len(fails) == 1
    import json
    assert json.loads(fails[0]["detail_json"])["step"] == "pin"
    # Best-effort delete of the unpinned new message.
    channel._new_msg.delete.assert_called_once()
    assert get_state_pin(db_conn, "100", "scoring") is None


# ---------------------------------------------------------------------------
# Optimistic-lock loss
# ---------------------------------------------------------------------------


async def test_announce_optimistic_lock_loss_self_deletes(
    sp_module, db_conn, monkeypatch,
):
    """Simulate another writer winning the race: monkeypatch upsert to
    return False. The loser unpins + deletes its own message + audits
    fitcheck_state_pin_lost_race."""
    upsert_state_pin(
        db_conn, "100", "scoring", "300", "1234", "2026-05-17T19:00:00Z",
    )
    client = _make_client()
    channel = _make_channel(sent_message_id=5020)
    client._channels[300] = channel

    def _fake_upsert(conn, guild_id, characteristic, channel_id, message_id,
                     posted_at, *, expected_updated_at=None):
        return False  # always lose

    from sable_roles.features import state_pin as sp_mod
    monkeypatch.setattr(
        sp_mod.discord_state_pins, "upsert_state_pin", _fake_upsert,
    )

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: revealed",
        changed_by_user_id=555,
    )

    audits = _audit_rows(db_conn)
    assert any(
        a["action"] == "fitcheck_state_pin_lost_race" for a in audits
    )
    # Loser self-deletes the just-posted message.
    channel._new_msg.delete.assert_called_once()


async def test_announce_lock_loss_self_cleanup_also_fails_audits_high(
    sp_module, db_conn, monkeypatch,
):
    """Loser's own unpin/delete ALSO fails (Discord 5xx) →
    fitcheck_state_pin_orphan_left_by_lock_loss HIGH audit so operators
    can find and clean up manually."""
    upsert_state_pin(
        db_conn, "100", "scoring", "300", "1234", "2026-05-17T19:00:00Z",
    )
    client = _make_client()
    channel = _make_channel(sent_message_id=5021)
    channel._new_msg.unpin = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom"),
    )
    channel._new_msg.delete = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom"),
    )
    client._channels[300] = channel

    def _fake_upsert(conn, guild_id, characteristic, channel_id, message_id,
                     posted_at, *, expected_updated_at=None):
        return False

    from sable_roles.features import state_pin as sp_mod
    monkeypatch.setattr(
        sp_mod.discord_state_pins, "upsert_state_pin", _fake_upsert,
    )

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: revealed",
        changed_by_user_id=555,
    )

    audits = _audit_rows(db_conn)
    assert any(
        a["action"] == "fitcheck_state_pin_orphan_left_by_lock_loss"
        for a in audits
    )


# ---------------------------------------------------------------------------
# DB-side upsert failure (SQLAlchemyError)
# ---------------------------------------------------------------------------


async def test_announce_upsert_sqlalchemy_error_cleans_up(
    sp_module, db_conn, monkeypatch,
):
    """PR2-H2: SQLAlchemyError at step-g upsert → HIGH
    fitcheck_state_pin_upsert_failed audit + best-effort unpin+delete
    of the just-posted pin."""
    from sqlalchemy.exc import SQLAlchemyError

    client = _make_client()
    channel = _make_channel(sent_message_id=5030)
    client._channels[300] = channel

    def _raises(conn, *args, **kwargs):
        raise SQLAlchemyError("simulated DB outage")

    from sable_roles.features import state_pin as sp_mod
    monkeypatch.setattr(sp_mod.discord_state_pins, "upsert_state_pin", _raises)

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: silent",
        changed_by_user_id=555,
    )

    audits = _audit_rows(db_conn)
    assert any(
        a["action"] == "fitcheck_state_pin_upsert_failed" for a in audits
    )
    # Best-effort unpin-and-delete of the orphan pin.
    channel._fetched_msg.delete.assert_called()


# ---------------------------------------------------------------------------
# Coalescing + per-channel lock
# ---------------------------------------------------------------------------


async def test_coalescing_cancels_prior_pending_announce(sp_module):
    """Two announces for the same (guild, characteristic) key: the first
    is cancelled, the second runs to completion. _pending_announcements
    self-identity guard keeps the slot consistent."""
    client = _make_client()
    channel = _make_channel(sent_message_id=5040)
    client._channels[300] = channel

    # Block the first announce inside channel.send so we can race the
    # second one against it.
    send_event = asyncio.Event()

    async def slow_send(*a, **k):
        await send_event.wait()
        return channel._new_msg

    channel.send = AsyncMock(side_effect=slow_send)

    first = asyncio.create_task(
        sp_module.announce_state_change(
            client,
            guild_id="100",
            org_id="solstitch",
            characteristic="scoring",
            new_state_summary="state: silent",
            changed_by_user_id=555,
        )
    )
    await asyncio.sleep(0)  # let first land in _pending_announcements
    assert ("100", "scoring") in sp_module._pending_announcements

    # Reset channel.send to a normal AsyncMock for the second announce.
    new_msg2 = MagicMock(spec=discord.Message)
    new_msg2.id = 5041
    new_msg2.pin = AsyncMock()
    new_msg2.unpin = AsyncMock()
    new_msg2.delete = AsyncMock()
    channel.send = AsyncMock(return_value=new_msg2)

    second = asyncio.create_task(
        sp_module.announce_state_change(
            client,
            guild_id="100",
            org_id="solstitch",
            characteristic="scoring",
            new_state_summary="state: revealed",
            changed_by_user_id=555,
        )
    )
    # Wait for first to be cancelled and second to complete.
    send_event.set()
    await asyncio.gather(first, second, return_exceptions=True)

    # second's task should no longer be in the dict (cleared by finally).
    assert ("100", "scoring") not in sp_module._pending_announcements


async def test_per_channel_lock_serializes_cross_characteristic_ops(sp_module):
    """Different characteristics on the SAME ops channel use the same
    `_channel_locks` entry — verify the lock is shared not per-key."""
    client = _make_client()
    channel = _make_channel(sent_message_id=5050)
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: silent",
        changed_by_user_id=555,
    )
    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="relax_mode",
        new_state_summary="state: on",
        changed_by_user_id=555,
    )
    # Same channel id → same lock object.
    assert "300" in sp_module._channel_locks
    assert len(sp_module._channel_locks) == 1


# ---------------------------------------------------------------------------
# close() drain
# ---------------------------------------------------------------------------


async def test_close_drains_mid_flight_sweep_task(sp_module):
    """R3-L1: close() cancels + gathers the boot-time sweep task so a
    Ctrl-C during boot doesn't leak a partial-sweep audit row past
    event-loop teardown."""
    client = _make_client()
    channel = _make_channel(pinned_messages=[])
    client._channels[300] = channel

    # Stall the sweep mid-flight by making channel.pins() block on an
    # event we control.
    sweep_started = asyncio.Event()
    sweep_release = asyncio.Event()

    async def slow_pins():
        sweep_started.set()
        await sweep_release.wait()
        return []

    channel.pins = AsyncMock(side_effect=slow_pins)

    # Mirror the on_ready wrapper: launch the tracked task.
    sp_module._sweep_task = asyncio.create_task(
        sp_module._run_sweep(client)
    )
    await sweep_started.wait()
    assert sp_module._sweep_task is not None
    assert not sp_module._sweep_task.done()

    # close() should cancel + drain the sweep without re-raising.
    await sp_module.close()
    assert sp_module._sweep_task is None
    # Release the stub so any straggler can settle.
    sweep_release.set()


async def test_close_drains_pending_and_clears_dict(sp_module):
    """close() cancels every pending task, awaits drain, clears the
    dict. CancelledError re-raise discipline keeps gather clean."""
    client = _make_client()
    channel = _make_channel()
    client._channels[300] = channel

    send_event = asyncio.Event()

    async def slow_send(*a, **k):
        await send_event.wait()
        return channel._new_msg

    channel.send = AsyncMock(side_effect=slow_send)

    task = asyncio.create_task(
        sp_module.announce_state_change(
            client,
            guild_id="100",
            org_id="solstitch",
            characteristic="scoring",
            new_state_summary="state: silent",
            changed_by_user_id=555,
        )
    )
    await asyncio.sleep(0)
    assert ("100", "scoring") in sp_module._pending_announcements

    # close() should cancel + drain.
    await sp_module.close()
    assert len(sp_module._pending_announcements) == 0
    # Task is unwound.
    assert task.done() or task.cancelled()
    # Release the slow_send fixture so any straggler can settle.
    send_event.set()


# ---------------------------------------------------------------------------
# sweep_orphan_pins
# ---------------------------------------------------------------------------


async def test_sweep_unpins_pinned_state_message_not_in_db(
    sp_module, db_conn,
):
    """Orphan pin in #sable-ops with no matching DB row → unpin +
    fitcheck_state_pin_orphan_swept INFO audit with via=restart_sweep."""
    client = _make_client()
    # No row in discord_state_pins → every Stitzy-prefixed pin is orphan.
    orphan = MagicMock(spec=discord.Message)
    orphan.id = 9999
    orphan.content = f"{sp_module._STATE_HEADLINE_PREFIX}scoring**\nstate: off"
    orphan.unpin = AsyncMock()
    orphan.delete = AsyncMock()
    channel = _make_channel(pinned_messages=[orphan])
    channel.fetch_message = AsyncMock(return_value=orphan)
    client._channels[300] = channel

    await sp_module.sweep_orphan_pins(client)

    orphan.unpin.assert_called_once()
    # Sweep does NOT delete — P18 preserves scrollback.
    orphan.delete.assert_not_called()
    audits = _audit_rows(db_conn)
    orphan_audits = [
        a for a in audits if a["action"] == "fitcheck_state_pin_orphan_swept"
    ]
    assert len(orphan_audits) == 1
    import json
    detail = json.loads(orphan_audits[0]["detail_json"])
    assert detail["via"] == "restart_sweep"
    assert detail["characteristic"] == "scoring"


async def test_sweep_leaves_live_pin_alone(sp_module, db_conn):
    """Pinned message matches the DB row → not an orphan, no unpin."""
    upsert_state_pin(db_conn, "100", "scoring", "300", "9999", "2026-05-17T19:00:00Z")
    client = _make_client()
    live = MagicMock(spec=discord.Message)
    live.id = 9999
    live.content = f"{sp_module._STATE_HEADLINE_PREFIX}scoring**\nstate: silent"
    live.unpin = AsyncMock()
    channel = _make_channel(pinned_messages=[live])
    channel.fetch_message = AsyncMock(return_value=live)
    client._channels[300] = channel

    await sp_module.sweep_orphan_pins(client)

    live.unpin.assert_not_called()


async def test_sweep_ignores_non_stitzy_pins(sp_module):
    """Pinned message that doesn't start with the headline prefix is not
    a Stitzy state pin; sweep leaves it alone."""
    client = _make_client()
    mod_pinned = MagicMock(spec=discord.Message)
    mod_pinned.id = 7777
    mod_pinned.content = "important mod note — read this"
    mod_pinned.unpin = AsyncMock()
    channel = _make_channel(pinned_messages=[mod_pinned])
    client._channels[300] = channel

    await sp_module.sweep_orphan_pins(client)

    mod_pinned.unpin.assert_not_called()


async def test_sweep_extract_characteristic_rejects_unknown_token(sp_module):
    """PR4-L2 + PR5-L2: a pinned message whose headline prefix matches
    but whose first token isn't in _KNOWN_CHARACTERISTICS is treated as
    not-a-state-pin (returns None)."""
    bad = (
        f"{sp_module._STATE_HEADLINE_PREFIX}scoring_state**\nstate: silent"
    )
    assert sp_module._extract_characteristic_from_headline(bad) is None
    good = f"{sp_module._STATE_HEADLINE_PREFIX}scoring**\nstate: silent"
    assert sp_module._extract_characteristic_from_headline(good) == "scoring"
    # Empty-after-prefix: defensive None instead of IndexError.
    assert sp_module._extract_characteristic_from_headline(
        sp_module._STATE_HEADLINE_PREFIX,
    ) is None


# ---------------------------------------------------------------------------
# Opportunistic dup-pin sweep at step d.5
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# R2-M3 — fresh coverage for the R1 fixes
# ---------------------------------------------------------------------------


def test_filter_unique_ops_channels_skips_duplicate(
    sp_module, monkeypatch, caplog,
):
    """R1-C1 + R2-L2: a duplicate channel_id across two guilds → second
    is dropped, surviving guild is named in the error log.

    R3-L2: use monkeypatch.setattr so teardown is structural, not
    coincidental on the sp_module fixture's setitem reversal.
    """
    import logging
    monkeypatch.setattr(
        sp_module, "OPS_CHANNELS",
        {"100": "300", "200": "300"},  # duplicate channel id
    )
    with caplog.at_level(logging.ERROR, logger="sable_roles.state_pin"):
        safe = sp_module._filter_unique_ops_channels()
    assert safe == {"100": "300"}
    # Error log cites both guilds + identifies surviving.
    err_lines = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("100 (surviving)" in m and "200 (dropped)" in m for m in err_lines)


def test_filter_unique_ops_channels_skips_blank(
    sp_module, monkeypatch, caplog,
):
    """R2-L3 + R3-L4: blank / whitespace / integer 0 channel_id values
    are rejected at config-load time with the actual value rendered in
    the error message."""
    import logging
    monkeypatch.setattr(
        sp_module, "OPS_CHANNELS",
        {"100": "", "200": "   ", "300": "555", "400": 0},
    )
    with caplog.at_level(logging.ERROR, logger="sable_roles.state_pin"):
        safe = sp_module._filter_unique_ops_channels()
    assert safe == {"300": "555"}
    err_lines = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    # All three blank-ish values logged with their actual repr.
    assert any("unusable channel_id ''" in m for m in err_lines)
    assert any("unusable channel_id '   '" in m for m in err_lines)
    assert any("unusable channel_id 0" in m for m in err_lines)


async def test_sweep_done_guard_blocks_second_invocation(sp_module, db_conn):
    """R1-H1 + R2-M2: second `sweep_orphan_pins` call within the same
    process is a no-op via the `_sweep_done` guard. on_ready reconnects
    must not re-fan channel.pins() across every ops channel."""
    client = _make_client()
    channel = _make_channel(pinned_messages=[])
    client._channels[300] = channel

    await sp_module.sweep_orphan_pins(client)
    assert sp_module._sweep_done.get("done") is True
    # First call hit channel.pins() exactly once.
    assert channel.pins.call_count == 1

    # Second call: short-circuits, no additional pins() fanout.
    await sp_module.sweep_orphan_pins(client)
    assert channel.pins.call_count == 1


async def test_sweep_pins_unavailable_audits_low(sp_module, db_conn):
    """R2-M1: a Discord HTTPException during channel.pins() in the
    boot-time sweep writes a `fitcheck_state_pin_sweep_pins_unavailable`
    audit row (was silently swallowed pre-R2-M1)."""
    client = _make_client()
    channel = _make_channel(
        pins_raises=discord.HTTPException(MagicMock(status=503), "down"),
    )
    client._channels[300] = channel

    await sp_module.sweep_orphan_pins(client)

    audits = _audit_rows(db_conn, action_prefix="fitcheck_state_pin_sweep")
    assert any(
        a["action"] == "fitcheck_state_pin_sweep_pins_unavailable"
        for a in audits
    )


async def test_sweep_acquires_channel_lock_serializing_with_announce(
    sp_module, db_conn,
):
    """R1-M4: sweep_orphan_pins acquires the same `_channel_locks`
    entry as announce_state_change. If announce holds the lock, sweep
    waits — proving they serialize."""
    client = _make_client()
    channel = _make_channel(pinned_messages=[], sent_message_id=5070)
    client._channels[300] = channel

    # Pre-acquire the per-channel lock to simulate an in-flight announce.
    lock = sp_module._channel_locks.setdefault("300", asyncio.Lock())
    await lock.acquire()
    try:
        sweep_task = asyncio.create_task(sp_module.sweep_orphan_pins(client))
        # Let sweep start, then yield repeatedly: it should be blocked
        # on the lock acquisition inside `_sweep_one_channel`.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not sweep_task.done()
        # channel.pins() should NOT have been called yet — sweep is
        # parked on the lock waiting for the (simulated) announce.
        assert channel.pins.call_count == 0
    finally:
        lock.release()
    # After release, sweep proceeds + completes.
    await sweep_task
    assert channel.pins.call_count == 1


async def test_opportunistic_sweep_unpins_orphan_for_same_characteristic(
    sp_module, db_conn,
):
    """PR5-M2: an orphan Stitzy pin for the same characteristic with a
    different message id from the DB pointer gets unpinned inside the
    lock — this is the "cancelled prior left a phantom pin" recovery
    path the boot-time sweep alone would leave visible for hours/days
    on the VPS."""
    upsert_state_pin(
        db_conn, "100", "scoring", "300", "1234", "2026-05-17T19:00:00Z",
    )
    client = _make_client()
    orphan = MagicMock(spec=discord.Message)
    orphan.id = 8888  # different from prior 1234
    orphan.content = f"{sp_module._STATE_HEADLINE_PREFIX}scoring**\nstate: silent"
    orphan.unpin = AsyncMock()
    channel = _make_channel(
        pinned_messages=[orphan], sent_message_id=5060,
    )
    channel.fetch_message = AsyncMock(return_value=orphan)
    client._channels[300] = channel

    await sp_module.announce_state_change(
        client,
        guild_id="100",
        org_id="solstitch",
        characteristic="scoring",
        new_state_summary="state: revealed",
        changed_by_user_id=555,
    )

    # The orphan (id=8888, not the prior 1234 or the new 5060) gets
    # unpinned via the opportunistic sweep.
    audits = _audit_rows(db_conn)
    opp_audits = [
        a for a in audits
        if a["action"] == "fitcheck_state_pin_orphan_swept"
    ]
    import json
    via_values = {json.loads(a["detail_json"]).get("via") for a in opp_audits}
    assert "opportunistic_pre_post" in via_values
