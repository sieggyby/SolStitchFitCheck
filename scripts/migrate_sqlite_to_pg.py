"""Copy sable-roles state from local SQLite to production Postgres.

One-time migrator used during the VPS cutover (2026-05-16). Idempotent on
re-run for the 13 sable-roles-owned tables (ON CONFLICT keys); NOT idempotent
for the two shared-table append paths (`audit_log` source='sable-roles',
`cost_events` call_type LIKE 'sable_roles_%') — those skip id preservation
and let the prod sequence assign new ids, so re-running would double-write
those logs. Run them once.

Usage:
    SABLE_DATABASE_URL=postgresql://... python migrate_sqlite_to_pg.py \\
        /tmp/laptop_sable.db [--dry-run] [--only TABLE [TABLE ...]]

Tables migrate in FK-safe order: observations precede vibes, etc.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from typing import Any, Iterable

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:  # pragma: no cover
    sys.exit("psycopg2 is required. Run inside /opt/sable/venv on the VPS.")


# (sqlite_table, postgres_table, conflict_clause, preserve_id, sequence_col_or_None,
#  row_filter_sql_or_None)
#
# - preserve_id=True:   include the laptop's id column in the INSERT so re-runs
#                       collide on (id) and ON CONFLICT (id) DO NOTHING handles it.
#                       After the table copies, the sequence is bumped past the
#                       max id we just inserted.
# - preserve_id=False:  drop the id column from the INSERT so the prod sequence
#                       assigns. Used for the two SHARED tables (audit_log,
#                       cost_events) — those are append-only logs, not 1:1
#                       sable-roles state.
# - row_filter:         a SQL fragment appended after WHERE for the SQLite SELECT
#                       (used to scope the shared tables to sable-roles rows).
TABLES: list[tuple[str, str, str, bool, str | None, str | None]] = [
    # discord_guild_config — PK is guild_id (text). No id column.
    ("discord_guild_config", "discord_guild_config",
     "ON CONFLICT (guild_id) DO NOTHING",
     False, None, None),

    # discord_burn_optins — composite PK (guild_id, user_id). No id column.
    ("discord_burn_optins", "discord_burn_optins",
     "ON CONFLICT (guild_id, user_id) DO NOTHING",
     False, None, None),

    # discord_burn_blocklist — id PK + UNIQUE(guild_id, user_id). Preserve id
    # so a future operator query by id stays stable; conflict-key on the
    # natural unique so re-runs are idempotent even if id sequencing diverged.
    ("discord_burn_blocklist", "discord_burn_blocklist",
     "ON CONFLICT (guild_id, user_id) DO NOTHING",
     True, "discord_burn_blocklist_id_seq", None),

    # discord_burn_random_log — id PK, no natural unique (multiple roasts per
    # (guild_id, user_id) over time). Conflict on id is the only safe key.
    ("discord_burn_random_log", "discord_burn_random_log",
     "ON CONFLICT (id) DO NOTHING",
     True, "discord_burn_random_log_id_seq", None),

    # discord_team_inviters — id PK + UNIQUE(guild_id, user_id). Same pattern
    # as blocklist.
    ("discord_team_inviters", "discord_team_inviters",
     "ON CONFLICT (guild_id, user_id) DO NOTHING",
     True, "discord_team_inviters_id_seq", None),

    # discord_invite_snapshot — id PK, no natural unique (it's a history of
    # invite-state snapshots). Conflict on id.
    ("discord_invite_snapshot", "discord_invite_snapshot",
     "ON CONFLICT (id) DO NOTHING",
     True, "discord_invite_snapshot_id_seq", None),

    # discord_member_admit — id PK + UNIQUE(guild_id, user_id).
    ("discord_member_admit", "discord_member_admit",
     "ON CONFLICT (guild_id, user_id) DO NOTHING",
     True, "discord_member_admit_id_seq", None),

    # discord_peer_roast_tokens — id PK, no natural unique.
    ("discord_peer_roast_tokens", "discord_peer_roast_tokens",
     "ON CONFLICT (id) DO NOTHING",
     True, "discord_peer_roast_tokens_id_seq", None),

    # discord_peer_roast_flags — id PK, no natural unique.
    ("discord_peer_roast_flags", "discord_peer_roast_flags",
     "ON CONFLICT (id) DO NOTHING",
     True, "discord_peer_roast_flags_id_seq", None),

    # discord_message_observations — id PK. Conflict on id.
    ("discord_message_observations", "discord_message_observations",
     "ON CONFLICT (id) DO NOTHING",
     True, "discord_message_observations_id_seq", None),

    # discord_user_observations — id PK. Must precede discord_user_vibes
    # (the vibes table has FK to observations.id).
    ("discord_user_observations", "discord_user_observations",
     "ON CONFLICT (id) DO NOTHING",
     True, "discord_user_observations_id_seq", None),

    # discord_user_vibes — id PK, FK source_observation_id -> observations(id).
    ("discord_user_vibes", "discord_user_vibes",
     "ON CONFLICT (id) DO NOTHING",
     True, "discord_user_vibes_id_seq", None),

    # discord_streak_events — id PK + UNIQUE(guild_id, post_id). Most
    # user-visible table; conflict on natural unique guarantees per-fit
    # idempotency.
    ("discord_streak_events", "discord_streak_events",
     "ON CONFLICT (guild_id, post_id) DO NOTHING",
     True, "discord_streak_events_id_seq", None),

    # SHARED tables — append-only. Drop the laptop id, let prod sequence
    # assign. No ON CONFLICT — running this twice creates duplicate log rows.
    ("audit_log", "audit_log", "", False, None, "source = 'sable-roles'"),
    ("cost_events", "cost_events", "", False, None,
     "call_type LIKE 'sable_roles_%'"),
]


def sqlite_columns(sqlite_conn: sqlite3.Connection, table: str) -> list[str]:
    rows = sqlite_conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def postgres_columns(pg_conn, table: str) -> list[str]:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s "
            "ORDER BY ordinal_position",
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def fetch_rows(
    sqlite_conn: sqlite3.Connection,
    table: str,
    cols: list[str],
    where: str | None,
) -> list[tuple]:
    quoted_cols = ", ".join(cols)
    sql = f"SELECT {quoted_cols} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return sqlite_conn.execute(sql).fetchall()


def pg_count(pg_conn, table: str, where: str | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    with pg_conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def sqlite_count(sqlite_conn: sqlite3.Connection, table: str, where: str | None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return sqlite_conn.execute(sql).fetchone()[0]


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    sqlite_table: str,
    pg_table: str,
    conflict: str,
    preserve_id: bool,
    seq: str | None,
    row_filter: str | None,
    dry_run: bool,
) -> dict:
    """Copy one table. Returns dict with src_count, dst_before, dst_after, inserted."""
    src_cols = sqlite_columns(sqlite_conn, sqlite_table)
    dst_cols = postgres_columns(pg_conn, pg_table)

    # Intersect — defensive against any column drift. Warn if SQLite has
    # something Postgres doesn't (dropped on copy).
    insert_cols = [c for c in src_cols if c in set(dst_cols)]
    dropped = [c for c in src_cols if c not in set(dst_cols)]
    pg_only = [c for c in dst_cols if c not in set(src_cols)]

    # Shared tables: drop the id (let prod seq assign). Detect by absence of
    # preserve_id flag.
    if not preserve_id:
        # cost_events PK is event_id; audit_log PK is id. Either way drop both
        # if present.
        for k in ("id", "event_id"):
            if k in insert_cols:
                insert_cols.remove(k)

    src_count = sqlite_count(sqlite_conn, sqlite_table, row_filter)
    dst_before = pg_count(pg_conn, pg_table)

    rows = fetch_rows(sqlite_conn, sqlite_table, insert_cols, row_filter) if src_count else []

    if dry_run:
        return {
            "src_count": src_count,
            "dst_before": dst_before,
            "dst_after": dst_before,
            "would_insert": len(rows),
            "insert_cols": insert_cols,
            "dropped_cols": dropped,
            "pg_only_cols": pg_only,
            "preview_first_row": rows[0] if rows else None,
        }

    inserted = 0
    if rows:
        col_list = ", ".join(insert_cols)
        placeholders = "(" + ", ".join(["%s"] * len(insert_cols)) + ")"
        sql = f"INSERT INTO {pg_table} ({col_list}) VALUES %s {conflict}".strip()
        with pg_conn.cursor() as cur:
            execute_values(cur, sql, rows, template=placeholders)
            inserted = cur.rowcount  # rows actually inserted (ON CONFLICT skipped not counted)
        pg_conn.commit()

    # Bump sequence past the max id we just inserted, but ONLY for
    # preserve_id tables (where we wrote explicit laptop ids that may exceed
    # the current Postgres seq value). For shared-append tables the seq is
    # already valid because Postgres assigned the ids itself.
    if preserve_id and seq:
        with pg_conn.cursor() as cur:
            cur.execute(
                f"SELECT setval(%s, GREATEST((SELECT COALESCE(MAX(id), 1) FROM {pg_table}), 1))",
                (seq,),
            )
        pg_conn.commit()

    dst_after = pg_count(pg_conn, pg_table)
    return {
        "src_count": src_count,
        "dst_before": dst_before,
        "dst_after": dst_after,
        "inserted": inserted,
        "insert_cols": insert_cols,
        "dropped_cols": dropped,
        "pg_only_cols": pg_only,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_path", help="Path to laptop SQLite (e.g. /tmp/laptop_sable.db)")
    parser.add_argument("--dry-run", action="store_true", help="Print would-insert counts; don't write")
    parser.add_argument("--only", nargs="+", default=None,
                        help="Restrict to a subset of source-table names")
    args = parser.parse_args()

    url = os.environ.get("SABLE_DATABASE_URL")
    if not url:
        sys.exit("SABLE_DATABASE_URL must be set (postgresql://...)")
    if not os.path.exists(args.sqlite_path):
        sys.exit(f"SQLite file not found: {args.sqlite_path}")

    only = set(args.only) if args.only else None

    print(f"Source : {args.sqlite_path}")
    print(f"Target : {url.split('@', 1)[-1] if '@' in url else url}")
    print(f"Mode   : {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    with closing(sqlite3.connect(args.sqlite_path)) as sconn, \
         closing(psycopg2.connect(url)) as pgconn:
        sconn.row_factory = None

        results: list[tuple[str, dict]] = []
        for sqlite_table, pg_table, conflict, preserve_id, seq, row_filter in TABLES:
            if only and sqlite_table not in only:
                continue
            label = (f"{sqlite_table}"
                     + (f" ({row_filter})" if row_filter else ""))
            try:
                stats = migrate_table(
                    sconn, pgconn,
                    sqlite_table, pg_table, conflict,
                    preserve_id, seq, row_filter,
                    args.dry_run,
                )
                results.append((label, stats))
            except Exception as exc:
                # Abort on first error — partial state is recoverable from the
                # pg_dump backup but per-table corruption is worse than full halt.
                pgconn.rollback()
                print(f"FATAL on {label}: {exc!r}", file=sys.stderr)
                return 2

        # Summary table
        print(f"{'TABLE':<48} {'SRC':>6} {'DST_BEFORE':>10} {'DST_AFTER':>10} {'INSERTED':>10}")
        print("-" * 90)
        for label, s in results:
            if args.dry_run:
                print(f"{label[:48]:<48} {s['src_count']:>6} {s['dst_before']:>10} {'(dry)':>10} {s['would_insert']:>10}")
            else:
                print(f"{label[:48]:<48} {s['src_count']:>6} {s['dst_before']:>10} {s['dst_after']:>10} {s['inserted']:>10}")

        # Schema diff callouts (only worth printing if any)
        print()
        for label, s in results:
            if s.get("dropped_cols"):
                print(f"  ! {label}: SQLite-only columns dropped on copy: {s['dropped_cols']}")
            if s.get("pg_only_cols"):
                print(f"  i {label}: Postgres-only columns (use defaults): {s['pg_only_cols']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
