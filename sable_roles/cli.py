"""sable-roles CLI — operator entrypoints for one-shot data tasks.

R4 ships the ``backfill_blocklist`` subcommand which grandfathers existing
``/stop-pls`` audit rows (action ``fitcheck_burn_optout``) into the sticky
``discord_burn_blocklist`` table introduced in mig 047. Idempotent: re-runs
produce zero new rows because :func:`discord_roast.insert_blocklist` uses
``ON CONFLICT DO NOTHING``.

R8 adds ``grandfather_restoration_tokens`` which scans every active streak
holder and grants a streak-restoration peer-roast token to anyone currently
at exactly 7 days. Mirrors the idempotency story of backfill_blocklist —
SP's ``grant_restoration_token`` ON-CONFLICT-DO-NOTHING blocks
double-grants within the same calendar month.

Usage::

    SABLE_OPERATOR_ID=<id> python -m sable_roles.cli backfill_blocklist
    SABLE_OPERATOR_ID=<id> python -m sable_roles.cli grandfather_restoration_tokens

CLI actor convention matches the rest of SablePlatform (``cli:<operator>``);
without ``SABLE_OPERATOR_ID`` the backfill exits 1 with a friendly stderr
message rather than silently writing ``unknown``-attributed audit rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from sable_platform.db import discord_roast, discord_streaks
from sable_platform.db.audit import list_audit_log, log_audit
from sable_platform.db.connection import get_db
from sable_platform.db.discord_roast import _current_year_month

logger = logging.getLogger("sable_roles.cli")

# High sentinel so a single backfill drains every grandfathered optout row
# without pagination. ``list_audit_log`` defaults to 100; for backfill we
# need the full history.
_BACKFILL_AUDIT_LIMIT = 1_000_000


def _resolve_operator_id() -> str:
    op = os.environ.get("SABLE_OPERATOR_ID", "").strip()
    if not op or op == "unknown":
        print(
            "error: SABLE_OPERATOR_ID is required (set it to the operator's"
            " identifier so audit rows attribute correctly).",
            file=sys.stderr,
        )
        sys.exit(1)
    return op


def _cmd_backfill_blocklist(args: argparse.Namespace) -> int:
    del args
    operator_id = _resolve_operator_id()
    actor = f"cli:{operator_id}"
    inserted = 0
    skipped = 0

    with get_db() as conn:
        rows = list_audit_log(
            conn,
            action="fitcheck_burn_optout",
            limit=_BACKFILL_AUDIT_LIMIT,
        )
        for row in rows:
            row_map = dict(row._mapping if hasattr(row, "_mapping") else row)
            if row_map.get("source") != "sable-roles":
                continue
            detail_json = row_map.get("detail_json")
            if not detail_json:
                continue
            try:
                detail = json.loads(detail_json)
            except (TypeError, ValueError):
                continue
            guild_id = detail.get("guild_id")
            user_id = detail.get("user_id")
            if not guild_id or not user_id:
                continue
            org_id = row_map.get("org_id")
            newly = discord_roast.insert_blocklist(conn, guild_id, user_id)
            if newly:
                log_audit(
                    conn,
                    actor=actor,
                    action="fitcheck_burn_blocklist_backfilled",
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "user_id": user_id,
                        "source_audit_id": row_map.get("id"),
                    },
                    source="sable-roles",
                )
                inserted += 1
            else:
                skipped += 1

    print(
        f"backfill_blocklist: inserted {inserted} (skipped {skipped} already-blocked)"
    )
    return 0


def _cmd_grandfather_restoration_tokens(args: argparse.Namespace) -> int:
    """One-shot: for every user currently at a 7-day streak, grant a
    streak-restoration peer-roast token if they don't already have one
    for the current calendar month. Idempotent on re-run.
    """
    del args
    operator_id = _resolve_operator_id()
    actor = f"cli:{operator_id}"
    granted = 0
    skipped_not_7 = 0
    skipped_already_granted = 0

    with get_db() as conn:
        users = discord_streaks.list_active_streak_users(conn)
        for row in users:
            guild_id = row.get("guild_id")
            user_id = row.get("user_id")
            org_id = row.get("org_id")
            if not guild_id or not user_id or not org_id:
                continue
            state = discord_streaks.compute_streak_state(conn, org_id, user_id)
            if int(state.get("current_streak", 0)) != 7:
                skipped_not_7 += 1
                continue
            fresh = discord_roast.grant_restoration_token(
                conn, guild_id, user_id
            )
            if not fresh:
                skipped_already_granted += 1
                continue
            log_audit(
                conn,
                actor=actor,
                action="fitcheck_peer_roast_token_granted",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "actor_user_id": user_id,
                    "source": "streak_restoration",
                    "year_month": _current_year_month(),
                    "grandfathered": True,
                },
                source="sable-roles",
            )
            granted += 1

    print(
        f"grandfather_restoration_tokens: granted {granted}"
        f" (skipped {skipped_not_7} not-at-7, {skipped_already_granted} already-granted)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sable_roles.cli",
        description="sable-roles operator CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    bf = sub.add_parser(
        "backfill_blocklist",
        help=(
            "Grandfather existing /stop-pls audit rows into"
            " discord_burn_blocklist (idempotent)."
        ),
        description=(
            "Reads audit_log rows with action='fitcheck_burn_optout' and"
            " source='sable-roles', then inserts each (guild_id, user_id)"
            " into discord_burn_blocklist via insert_blocklist. ON CONFLICT"
            " DO NOTHING makes re-runs land zero new rows. Each new insert"
            " writes a fitcheck_burn_blocklist_backfilled audit row"
            " attributed to cli:<SABLE_OPERATOR_ID>."
        ),
    )
    bf.set_defaults(func=_cmd_backfill_blocklist)

    gr = sub.add_parser(
        "grandfather_restoration_tokens",
        help=(
            "Grant streak-restoration tokens to every user currently"
            " holding a 7-day streak (idempotent)."
        ),
        description=(
            "Scans discord_streak_events for distinct (guild_id, user_id,"
            " org_id) tuples with active streaks; for each, computes the"
            " current streak and grants a streak_restoration peer-roast"
            " token when the streak is exactly 7. ON CONFLICT DO NOTHING"
            " makes re-runs land zero new rows. Each grant writes a"
            " fitcheck_peer_roast_token_granted audit with"
            " detail.grandfathered=True attributed to cli:<SABLE_OPERATOR_ID>."
        ),
    )
    gr.set_defaults(func=_cmd_grandfather_restoration_tokens)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
