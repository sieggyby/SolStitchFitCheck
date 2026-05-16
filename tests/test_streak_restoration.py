"""Tests for the R8 streak-restoration grant + grandfathering CLI.

R8 ships:
  * `roast.maybe_grant_restoration_token(client, *, user_id, guild_id, org_id)`
    — called from fitcheck_streak.py image-branch tail per image-post.
    Grants on current_streak == 7 only; idempotent within calendar month
    via SP UNIQUE constraint.
  * SP helper `discord_streaks.list_active_streak_users(conn)` — distinct
    (guild_id, user_id, org_id) tuples with active streaks.
  * CLI subcommand `grandfather_restoration_tokens` — one-shot grant pass
    for every user currently at 7-day streak.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sable_platform.db import discord_roast, discord_streaks
from sable_roles import cli
from sable_roles.features import roast

from tests.conftest import fetch_audit_rows


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


@pytest.fixture
def patched_roast(monkeypatch, db_conn):
    monkeypatch.setattr(roast, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    return roast


def _seed_streak_days(
    db_conn,
    *,
    user_id: str,
    guild_id: str = "100",
    org_id: str = "solstitch",
    days_back: int,
) -> None:
    """Seed `days_back` consecutive fit-events ending TODAY (UTC) so
    compute_streak_state returns current_streak == days_back."""
    from datetime import timedelta

    today_utc = datetime.now(timezone.utc).date()
    for i in range(days_back):
        day = today_utc - timedelta(days=i)
        day_iso = day.isoformat()
        posted_at = f"{day_iso}T12:00:00Z"
        db_conn.execute(
            "INSERT INTO discord_streak_events"
            " (org_id, guild_id, channel_id, post_id, user_id, posted_at,"
            "  counted_for_day, attachment_count, image_attachment_count,"
            "  ingest_source, counts_for_streak)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (org_id, guild_id, "200", f"p_{user_id}_{i}", user_id,
             posted_at, day_iso, 1, 1, "gateway", 1),
        )
    db_conn.commit()


# ---------------------------------------------------------------------------
# maybe_grant_restoration_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_fires_on_exactly_7(patched_roast, db_conn):
    _seed_streak_days(db_conn, user_id="555", days_back=7)

    granted = await roast.maybe_grant_restoration_token(
        client=None,  # skip DM
        user_id="555",
        guild_id="100",
        org_id="solstitch",
    )
    assert granted is True

    # Token row landed under streak_restoration source
    rows = db_conn.execute(
        "SELECT source, year_month FROM discord_peer_roast_tokens"
        " WHERE actor_user_id='555' AND guild_id='100'"
    ).fetchall()
    rs = [dict(r._mapping if hasattr(r, "_mapping") else r) for r in rows]
    assert len(rs) == 1
    assert rs[0]["source"] == "streak_restoration"

    # Audit row landed
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["source"] == "streak_restoration"
    assert detail["actor_user_id"] == "555"


@pytest.mark.asyncio
async def test_no_grant_when_streak_below_7(patched_roast, db_conn):
    _seed_streak_days(db_conn, user_id="555", days_back=6)
    granted = await roast.maybe_grant_restoration_token(
        client=None, user_id="555", guild_id="100", org_id="solstitch",
    )
    assert granted is False
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_no_grant_when_streak_above_7(patched_roast, db_conn):
    """Plan §6: hook fires per-post; only the 6→7 transition grants.
    8-day streak (already past 7) gets NO grant."""
    _seed_streak_days(db_conn, user_id="555", days_back=8)
    granted = await roast.maybe_grant_restoration_token(
        client=None, user_id="555", guild_id="100", org_id="solstitch",
    )
    assert granted is False
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_idempotent_within_month(patched_roast, db_conn):
    """Two grants in same month → second is no-op (SP UNIQUE)."""
    _seed_streak_days(db_conn, user_id="555", days_back=7)
    g1 = await roast.maybe_grant_restoration_token(
        client=None, user_id="555", guild_id="100", org_id="solstitch",
    )
    g2 = await roast.maybe_grant_restoration_token(
        client=None, user_id="555", guild_id="100", org_id="solstitch",
    )
    assert g1 is True
    assert g2 is False
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 1
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 1  # second call wrote no audit


@pytest.mark.asyncio
async def test_grant_fires_dm_when_client_provided(patched_roast, db_conn):
    """When a Discord client is provided, the bonus DM lands on the
    user once the grant succeeds."""
    _seed_streak_days(db_conn, user_id="555", days_back=7)
    user_double = MagicMock()
    user_double.send = AsyncMock()
    client = MagicMock()
    client.get_user = MagicMock(return_value=user_double)
    client.fetch_user = AsyncMock(return_value=user_double)

    granted = await roast.maybe_grant_restoration_token(
        client=client, user_id="555", guild_id="100", org_id="solstitch",
    )
    assert granted is True
    user_double.send.assert_awaited_once()
    body = user_double.send.call_args.args[0]
    assert "7 days" in body
    assert "/my-roasts" in body


@pytest.mark.asyncio
async def test_dm_failure_doesnt_undo_grant(patched_roast, db_conn):
    _seed_streak_days(db_conn, user_id="555", days_back=7)
    user_double = MagicMock()
    user_double.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(), "DMs disabled")
    )
    client = MagicMock()
    client.get_user = MagicMock(return_value=user_double)

    granted = await roast.maybe_grant_restoration_token(
        client=client, user_id="555", guild_id="100", org_id="solstitch",
    )
    assert granted is True  # grant stands even though DM failed
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 1


@pytest.mark.asyncio
async def test_dm_fetch_user_fallback_when_get_user_returns_none(
    patched_roast, db_conn
):
    """get_user can return None (user not in cache); the helper falls
    back to fetch_user."""
    _seed_streak_days(db_conn, user_id="555", days_back=7)
    user_double = MagicMock()
    user_double.send = AsyncMock()
    client = MagicMock()
    client.get_user = MagicMock(return_value=None)
    client.fetch_user = AsyncMock(return_value=user_double)

    await roast.maybe_grant_restoration_token(
        client=client, user_id="555", guild_id="100", org_id="solstitch",
    )
    client.fetch_user.assert_awaited_once_with(555)
    user_double.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_grant_isolated_per_user(patched_roast, db_conn):
    """Two users at 7-day streak: each gets their own grant."""
    _seed_streak_days(db_conn, user_id="555", days_back=7)
    _seed_streak_days(db_conn, user_id="666", days_back=7)
    g1 = await roast.maybe_grant_restoration_token(
        client=None, user_id="555", guild_id="100", org_id="solstitch",
    )
    g2 = await roast.maybe_grant_restoration_token(
        client=None, user_id="666", guild_id="100", org_id="solstitch",
    )
    assert g1 is True
    assert g2 is True
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 2


# ---------------------------------------------------------------------------
# SP helper: list_active_streak_users
# ---------------------------------------------------------------------------


def test_list_active_streak_users_dedups(db_conn):
    """5 events for same (guild, user, org) → 1 enumeration entry."""
    _seed_streak_days(db_conn, user_id="555", days_back=5)
    users = discord_streaks.list_active_streak_users(db_conn)
    assert len(users) == 1
    u = users[0]
    assert u["user_id"] == "555"
    assert u["guild_id"] == "100"
    assert u["org_id"] == "solstitch"


def test_list_active_streak_users_skips_invalidated(db_conn):
    _seed_streak_days(db_conn, user_id="555", days_back=3)
    db_conn.execute(
        "UPDATE discord_streak_events SET invalidated_at = '2026-01-01'"
        " WHERE user_id='555'"
    )
    db_conn.commit()
    users = discord_streaks.list_active_streak_users(db_conn)
    assert users == []


def test_list_active_streak_users_skips_non_counting(db_conn):
    _seed_streak_days(db_conn, user_id="555", days_back=3)
    db_conn.execute(
        "UPDATE discord_streak_events SET counts_for_streak = 0"
        " WHERE user_id='555'"
    )
    db_conn.commit()
    users = discord_streaks.list_active_streak_users(db_conn)
    assert users == []


# ---------------------------------------------------------------------------
# CLI: grandfather_restoration_tokens
# ---------------------------------------------------------------------------


def test_cli_grandfather_grants_at_7(monkeypatch, db_conn):
    monkeypatch.setenv("SABLE_OPERATOR_ID", "test_op")
    monkeypatch.setattr(cli, "get_db", lambda: _make_db_context(db_conn)())

    _seed_streak_days(db_conn, user_id="555", days_back=7)
    _seed_streak_days(db_conn, user_id="666", days_back=4)  # not at 7
    _seed_streak_days(db_conn, user_id="777", days_back=7)

    rc = cli.main(["grandfather_restoration_tokens"])
    assert rc == 0

    tokens = db_conn.execute(
        "SELECT actor_user_id, source FROM discord_peer_roast_tokens"
        " ORDER BY actor_user_id"
    ).fetchall()
    rs = [dict(r._mapping if hasattr(r, "_mapping") else r) for r in tokens]
    assert len(rs) == 2
    assert {r["actor_user_id"] for r in rs} == {"555", "777"}
    assert all(r["source"] == "streak_restoration" for r in rs)

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 2
    for a in audits:
        d = json.loads(a["detail_json"])
        assert d["source"] == "streak_restoration"
        assert d["grandfathered"] is True
        assert a["actor"] == "cli:test_op"


def test_cli_grandfather_idempotent(monkeypatch, db_conn):
    monkeypatch.setenv("SABLE_OPERATOR_ID", "test_op")
    monkeypatch.setattr(cli, "get_db", lambda: _make_db_context(db_conn)())
    _seed_streak_days(db_conn, user_id="555", days_back=7)

    rc1 = cli.main(["grandfather_restoration_tokens"])
    rc2 = cli.main(["grandfather_restoration_tokens"])
    assert rc1 == rc2 == 0

    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 1
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 1


def test_cli_grandfather_requires_operator_id(monkeypatch, db_conn, capsys):
    monkeypatch.delenv("SABLE_OPERATOR_ID", raising=False)
    monkeypatch.setattr(cli, "get_db", lambda: _make_db_context(db_conn)())
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["grandfather_restoration_tokens"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "SABLE_OPERATOR_ID" in err


def test_cli_grandfather_subcommand_registered():
    parser = cli.build_parser()
    # Spelling check + help-doesn't-crash
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["grandfather_restoration_tokens", "--help"])
    assert exc_info.value.code == 0
