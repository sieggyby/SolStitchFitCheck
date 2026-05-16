"""R4 tests: sable_roles.cli backfill_blocklist subcommand.

The CLI is the grandfathering path for users who ran /stop-pls before R4
landed and therefore have a fitcheck_burn_optout audit row but no
discord_burn_blocklist entry. Each successful insert writes a
fitcheck_burn_blocklist_backfilled audit row attributed to
cli:<SABLE_OPERATOR_ID> so /peer-roast-report can later distinguish
user-driven blocks from operator-grandfathered ones.
"""
from __future__ import annotations

import json
import os

import pytest

from sable_platform.db import discord_roast
from sable_platform.db.audit import log_audit

from sable_roles import cli


def _seed_optout_audit(db_conn, *, guild_id: str, user_id: str, org_id: str = "solstitch") -> int:
    return log_audit(
        db_conn,
        actor=f"discord:user:{user_id}",
        action="fitcheck_burn_optout",
        org_id=org_id,
        entity_id=None,
        detail={"guild_id": guild_id, "user_id": user_id},
        source="sable-roles",
    )


def _patch_get_db(monkeypatch, db_conn) -> None:
    """Route cli.get_db at the in-memory test DB."""
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr(cli, "get_db", lambda: _Ctx())


def _count_blocklist(db_conn, guild_id: str, user_id: str) -> int:
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_burn_blocklist"
        f" WHERE guild_id='{guild_id}' AND user_id='{user_id}'"
    ).fetchone()
    return dict(row._mapping if hasattr(row, "_mapping") else row)["n"]


# --- Happy path ---


def test_backfill_reads_optout_rows_and_inserts_blocklist(
    monkeypatch, db_conn, capsys
):
    monkeypatch.setenv("SABLE_OPERATOR_ID", "test_op")
    _patch_get_db(monkeypatch, db_conn)
    _seed_optout_audit(db_conn, guild_id="100", user_id="555")
    _seed_optout_audit(db_conn, guild_id="100", user_id="777")

    rc = cli.main(["backfill_blocklist"])

    assert rc == 0
    assert _count_blocklist(db_conn, "100", "555") == 1
    assert _count_blocklist(db_conn, "100", "777") == 1

    out = capsys.readouterr().out
    assert "inserted 2" in out
    assert "skipped 0" in out


def test_backfill_is_idempotent_on_rerun(monkeypatch, db_conn, capsys):
    monkeypatch.setenv("SABLE_OPERATOR_ID", "test_op")
    _patch_get_db(monkeypatch, db_conn)
    _seed_optout_audit(db_conn, guild_id="100", user_id="555")

    cli.main(["backfill_blocklist"])
    capsys.readouterr()  # drain first-run output

    # Second invocation: zero new rows.
    rc = cli.main(["backfill_blocklist"])
    assert rc == 0
    assert _count_blocklist(db_conn, "100", "555") == 1

    out = capsys.readouterr().out
    assert "inserted 0" in out
    assert "skipped 1" in out

    # Only ONE backfilled audit row total (from the first run).
    backfilled = db_conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log"
        " WHERE action='fitcheck_burn_blocklist_backfilled'"
    ).fetchone()
    assert dict(backfilled._mapping if hasattr(backfilled, "_mapping") else backfilled)["n"] == 1


def test_backfill_handles_multi_guild_isolated(monkeypatch, db_conn, capsys):
    monkeypatch.setenv("SABLE_OPERATOR_ID", "test_op")
    _patch_get_db(monkeypatch, db_conn)
    _seed_optout_audit(db_conn, guild_id="100", user_id="555")
    _seed_optout_audit(db_conn, guild_id="200", user_id="555")

    rc = cli.main(["backfill_blocklist"])
    assert rc == 0
    assert _count_blocklist(db_conn, "100", "555") == 1
    assert _count_blocklist(db_conn, "200", "555") == 1

    out = capsys.readouterr().out
    assert "inserted 2" in out


def test_backfill_missing_operator_id_exits_with_friendly_error(
    monkeypatch, db_conn, capsys
):
    """SABLE_OPERATOR_ID unset → exit 1 with stderr message, no DB writes."""
    monkeypatch.delenv("SABLE_OPERATOR_ID", raising=False)
    _patch_get_db(monkeypatch, db_conn)
    _seed_optout_audit(db_conn, guild_id="100", user_id="555")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["backfill_blocklist"])
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "SABLE_OPERATOR_ID" in captured.err

    # No blocklist row landed because the CLI exited before the loop.
    assert _count_blocklist(db_conn, "100", "555") == 0


def test_backfill_writes_audit_per_insert_with_cli_actor(
    monkeypatch, db_conn, capsys
):
    """Each newly-landed blocklist row gets a fitcheck_burn_blocklist_backfilled
    audit row attributed to cli:<SABLE_OPERATOR_ID> with source='sable-roles'."""
    monkeypatch.setenv("SABLE_OPERATOR_ID", "sieggy")
    _patch_get_db(monkeypatch, db_conn)
    source_id_1 = _seed_optout_audit(db_conn, guild_id="100", user_id="555")
    source_id_2 = _seed_optout_audit(db_conn, guild_id="100", user_id="777")

    rc = cli.main(["backfill_blocklist"])
    assert rc == 0

    rows = db_conn.execute(
        "SELECT actor, action, org_id, detail_json, source FROM audit_log"
        " WHERE action='fitcheck_burn_blocklist_backfilled'"
        " ORDER BY id ASC"
    ).fetchall()
    rows = [dict(r._mapping if hasattr(r, "_mapping") else r) for r in rows]
    assert len(rows) == 2

    for r in rows:
        assert r["actor"] == "cli:sieggy"
        assert r["source"] == "sable-roles"
        assert r["org_id"] == "solstitch"

    detail_users = sorted(json.loads(r["detail_json"])["user_id"] for r in rows)
    assert detail_users == ["555", "777"]

    source_ids = sorted(json.loads(r["detail_json"])["source_audit_id"] for r in rows)
    assert source_ids == sorted([source_id_1, source_id_2])


def test_backfill_skips_rows_already_blocklisted_no_new_audit(
    monkeypatch, db_conn, capsys
):
    """If a user is already in discord_burn_blocklist (e.g. ran /stop-pls
    post-R4), the backfill must NOT write a duplicate fitcheck_burn_blocklist_backfilled
    audit row for them. insert_blocklist returns False → skip."""
    monkeypatch.setenv("SABLE_OPERATOR_ID", "test_op")
    _patch_get_db(monkeypatch, db_conn)
    _seed_optout_audit(db_conn, guild_id="100", user_id="555")
    # Pre-existing blocklist row simulates a post-R4 /stop-pls invocation.
    landed = discord_roast.insert_blocklist(db_conn, "100", "555")
    assert landed is True

    # Also seed a fresh user that DOES need backfill, to make sure the loop
    # doesn't bail on the first skip.
    _seed_optout_audit(db_conn, guild_id="100", user_id="999")

    rc = cli.main(["backfill_blocklist"])
    assert rc == 0

    backfilled = db_conn.execute(
        "SELECT detail_json FROM audit_log"
        " WHERE action='fitcheck_burn_blocklist_backfilled'"
    ).fetchall()
    backfilled = [dict(r._mapping if hasattr(r, "_mapping") else r) for r in backfilled]
    # Only the 999 insert produces a backfilled audit row; 555 was already
    # blocked and is silently skipped.
    assert len(backfilled) == 1
    assert json.loads(backfilled[0]["detail_json"])["user_id"] == "999"

    out = capsys.readouterr().out
    assert "inserted 1" in out
    assert "skipped 1" in out


# --- Argparse + entrypoint smoke ---


def test_build_parser_has_backfill_blocklist_subcommand():
    """Schema-locks the subcommand name so docs/runbooks stay aligned."""
    parser = cli.build_parser()
    # SystemExit on --help is the documented argparse behavior.
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["backfill_blocklist", "--help"])
    assert exc_info.value.code == 0


def test_backfill_help_exits_zero_without_operator_id(monkeypatch, capsys):
    """--help must NOT require SABLE_OPERATOR_ID — it bails out in argparse
    before the handler runs. Pins the operator-id-only-on-execution contract."""
    monkeypatch.delenv("SABLE_OPERATOR_ID", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["backfill_blocklist", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "backfill_blocklist" in out.lower() or "grandfather" in out.lower()


# --- Defensive-branch coverage: filter / malformed-row handling ---


def test_backfill_skips_rows_with_non_sable_roles_source(monkeypatch, db_conn, capsys):
    """Defense-in-depth: even if a future caller writes an audit row with
    action='fitcheck_burn_optout' from a different source, the backfill MUST
    skip it. Locks the source-filter contract in cli.py."""
    monkeypatch.setenv("SABLE_OPERATOR_ID", "test_op")
    _patch_get_db(monkeypatch, db_conn)
    # Legitimate sable-roles row — should be backfilled.
    _seed_optout_audit(db_conn, guild_id="100", user_id="555")
    # Rogue row from a different source — must be ignored.
    log_audit(
        db_conn,
        actor="cli:other",
        action="fitcheck_burn_optout",
        org_id="solstitch",
        entity_id=None,
        detail={"guild_id": "100", "user_id": "999"},
        source="cli",  # NOT sable-roles
    )

    rc = cli.main(["backfill_blocklist"])
    assert rc == 0

    # Only the sable-roles user got blocklisted.
    assert _count_blocklist(db_conn, "100", "555") == 1
    assert _count_blocklist(db_conn, "100", "999") == 0

    out = capsys.readouterr().out
    assert "inserted 1" in out


def test_backfill_skips_rows_with_malformed_or_missing_detail(
    monkeypatch, db_conn, capsys
):
    """The CLI guards against bad audit rows (malformed JSON, missing keys,
    NULL detail_json). Each pathological row must NOT crash the run or leak
    into the blocklist; the well-formed row in the same batch must still
    land. Locks the json-parse / .get() defenses in _cmd_backfill_blocklist."""
    monkeypatch.setenv("SABLE_OPERATOR_ID", "test_op")
    _patch_get_db(monkeypatch, db_conn)

    # Well-formed row.
    _seed_optout_audit(db_conn, guild_id="100", user_id="555")
    # NULL detail_json (log_audit writes NULL when detail is None or falsy).
    log_audit(
        db_conn,
        actor="discord:user:111",
        action="fitcheck_burn_optout",
        org_id="solstitch",
        entity_id=None,
        detail=None,
        source="sable-roles",
    )
    # Detail missing user_id.
    log_audit(
        db_conn,
        actor="discord:user:222",
        action="fitcheck_burn_optout",
        org_id="solstitch",
        entity_id=None,
        detail={"guild_id": "100"},
        source="sable-roles",
    )
    # Detail missing guild_id.
    log_audit(
        db_conn,
        actor="discord:user:333",
        action="fitcheck_burn_optout",
        org_id="solstitch",
        entity_id=None,
        detail={"user_id": "333"},
        source="sable-roles",
    )

    rc = cli.main(["backfill_blocklist"])
    assert rc == 0

    # Only the well-formed row landed.
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_burn_blocklist"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 1
    assert _count_blocklist(db_conn, "100", "555") == 1

    out = capsys.readouterr().out
    assert "inserted 1" in out
