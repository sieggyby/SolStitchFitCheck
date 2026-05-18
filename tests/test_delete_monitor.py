"""Pass A: delete monitoring with severity classification + text-edit audit."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from sable_platform.db.discord_streaks import upsert_streak_event
from sable_platform.db.discord_fitcheck_scores import upsert_score_success
from sable_platform.db.discord_scoring_config import set_state


# ---------------------------------------------------------------------------
# Pure-function severity classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age_s,reactions,threads,rxn_thresh,thread_thresh,expected",
    [
        # CRITICAL — within 2 of reaction threshold (10 - 2 = 8)
        (3600, 8, 0, 10, 100, "CRITICAL"),
        (3600, 9, 0, 10, 100, "CRITICAL"),
        # CRITICAL — within 2 of thread threshold (100 - 2 = 98)
        (3600, 0, 98, 10, 100, "CRITICAL"),
        # MEDIUM — >=5 reactions but below CRITICAL
        (3600, 5, 0, 10, 100, "MEDIUM"),
        (3600, 7, 0, 10, 100, "MEDIUM"),
        # MEDIUM — >=30 thread but below CRITICAL
        (3600, 0, 30, 10, 100, "MEDIUM"),
        (3600, 0, 50, 10, 100, "MEDIUM"),
        # LOW — quick delete, no reactions
        (60, 0, 0, 10, 100, "LOW"),
        (599, 0, 0, 10, 100, "LOW"),
        # LOW — within hour, < 3 reactions
        (1800, 2, 0, 10, 100, "LOW"),
        # default LOW for "neither of the above"
        (24 * 3600, 0, 0, 10, 100, "LOW"),
    ],
)
def test_classify_delete_severity(
    age_s, reactions, threads, rxn_thresh, thread_thresh, expected
):
    from sable_roles.features.delete_monitor import classify_delete_severity

    sev = classify_delete_severity(
        age_seconds=age_s,
        reaction_count=reactions,
        thread_message_count=threads,
        reaction_threshold=rxn_thresh,
        thread_message_threshold=thread_thresh,
    )
    assert sev == expected


# ---------------------------------------------------------------------------
# on_raw_message_delete integration
# ---------------------------------------------------------------------------


@pytest.fixture
def dm_module(monkeypatch, db_conn):
    """delete_monitor with config patched for the test guild."""
    from sable_roles.features import delete_monitor as mod

    monkeypatch.setattr(mod, "SCORED_MODE_ENABLED", True)
    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(mod, "_FITCHECK_CHANNEL_IDS", {200})
    monkeypatch.setattr(mod, "_CHANNEL_TO_GUILD", {200: "100"})

    class _DBContext:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr(mod, "get_db", lambda: _DBContext())
    monkeypatch.setattr(mod, "_client", SimpleNamespace(user=SimpleNamespace(id=99999)))
    yield mod


def _delete_payload(channel_id: int = 200, message_id: int = 800) -> MagicMock:
    payload = MagicMock()
    payload.channel_id = channel_id
    payload.message_id = message_id
    return payload


async def test_delete_audits_with_severity_and_state_capture(dm_module, db_conn):
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "800", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    db_conn.execute(
        text(
            "UPDATE discord_streak_events"
            " SET reaction_score = 1, image_phash = 'abc123'"
            " WHERE post_id = '800'"
        )
    )
    db_conn.commit()

    await dm_module.on_raw_message_delete(_delete_payload())

    audit = db_conn.execute(
        text(
            "SELECT action, detail_json, source FROM audit_log"
            " WHERE action = 'fitcheck_post_deleted'"
        )
    ).fetchone()
    assert audit is not None
    assert audit["source"] == "sable-roles"
    import json
    detail = json.loads(audit["detail_json"])
    assert detail["post_id"] == "800"
    assert detail["reaction_count_at_delete"] == 1
    assert detail["severity"] in ("LOW", "MEDIUM", "CRITICAL")
    assert detail["image_phash"] == "abc123"
    assert detail["scoring_state"] == "off"  # default


async def test_delete_records_was_scored_when_score_exists(dm_module, db_conn):
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "801", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    upsert_score_success(
        db_conn,
        org_id="solstitch",
        guild_id="100",
        post_id="801",
        user_id="555",
        posted_at="2026-05-12T12:00:00Z",
        scored_at="2026-05-12T12:00:05Z",
        model_id="claude-sonnet-4-6",
        prompt_version="rubric_v1",
        axis_cohesion=7,
        axis_execution=8,
        axis_concept=6,
        axis_catch=5,
        raw_total=26,
        catch_detected=None,
        catch_naming_class=None,
        description=None,
        confidence=None,
        axis_rationales_json=None,
        curve_basis="absolute",
        pool_size_at_score_time=0,
        percentile=72.5,
    )

    await dm_module.on_raw_message_delete(_delete_payload(message_id=801))

    audit = db_conn.execute(
        text(
            "SELECT detail_json FROM audit_log"
            " WHERE action = 'fitcheck_post_deleted' ORDER BY id DESC LIMIT 1"
        )
    ).fetchone()
    import json
    detail = json.loads(audit["detail_json"])
    assert detail["was_scored"] is True
    assert detail["score_value_if_scored"] == 72.5


async def test_delete_noop_when_kill_switch_off(dm_module, db_conn, monkeypatch):
    monkeypatch.setattr(dm_module, "SCORED_MODE_ENABLED", False)
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "802", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )

    await dm_module.on_raw_message_delete(_delete_payload(message_id=802))

    audit = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action = 'fitcheck_post_deleted'"
        )
    ).fetchone()
    assert audit["n"] == 0


async def test_delete_noop_when_post_never_qualified(dm_module, db_conn):
    """No streak row -> nothing to flag (text-only post, etc.)."""
    await dm_module.on_raw_message_delete(_delete_payload(message_id=999))

    audit = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action = 'fitcheck_post_deleted'"
        )
    ).fetchone()
    assert audit["n"] == 0


async def test_delete_noop_when_channel_not_fitcheck(dm_module, db_conn):
    """Delete in some other channel -> ignored entirely."""
    upsert_streak_event(
        db_conn, "solstitch", "100", "999", "803", "555",  # non-fitcheck channel 999
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    payload = _delete_payload(channel_id=999, message_id=803)
    await dm_module.on_raw_message_delete(payload)

    audit = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log"
            " WHERE action = 'fitcheck_post_deleted'"
        )
    ).fetchone()
    assert audit["n"] == 0


# ---------------------------------------------------------------------------
# on_raw_message_edit
# ---------------------------------------------------------------------------


def _edit_payload(channel_id: int = 200, message_id: int = 850, new_content: str = "edited"):
    payload = MagicMock()
    payload.channel_id = channel_id
    payload.message_id = message_id
    payload.data = {"content": new_content}
    cached = MagicMock()
    cached.content = "original"
    payload.cached_message = cached
    return payload


async def test_edit_audits_fitcheck_text_edit_with_lengths_not_content(
    dm_module, db_conn
):
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "850", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )

    await dm_module.on_raw_message_edit(_edit_payload(new_content="brand new caption"))

    audit = db_conn.execute(
        text(
            "SELECT action, detail_json FROM audit_log"
            " WHERE action = 'fitcheck_text_edit'"
        )
    ).fetchone()
    assert audit is not None
    import json
    detail = json.loads(audit["detail_json"])
    assert detail["post_id"] == "850"
    assert detail["new_content_length"] == len("brand new caption")
    assert detail["old_content_length"] == len("original")
    assert detail["cached"] is True
    # Content text must NOT be stored.
    assert "brand new caption" not in audit["detail_json"]
    assert "original" not in audit["detail_json"]


async def test_edit_noop_when_kill_switch_off(dm_module, db_conn, monkeypatch):
    monkeypatch.setattr(dm_module, "SCORED_MODE_ENABLED", False)
    upsert_streak_event(
        db_conn, "solstitch", "100", "200", "851", "555",
        "2026-05-12T12:00:00Z", "2026-05-12", 1, 1,
    )
    await dm_module.on_raw_message_edit(_edit_payload(message_id=851))
    audit = db_conn.execute(
        text(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'fitcheck_text_edit'"
        )
    ).fetchone()
    assert audit["n"] == 0
