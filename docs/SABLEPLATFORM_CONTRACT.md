# SablePlatform Dependency Contract

`sable-roles` does not own its database layer. It depends on **SablePlatform** — the shared backbone for the Sable tool stack — which owns `sable.db`, all schema migrations, and the DB helper modules this bot calls.

**SablePlatform is a separate, currently-private Sable repository.** It is *not* included in this GitHub repo. Without it installed (`pip install -e <path-to-SablePlatform>`), this repo is **review-only**: the code reads and reasons fine, but `import sable_roles.main` raises `ModuleNotFoundError: sable_platform` and `pytest` fails at collection.

This document specifies the **entire** `sable_platform` surface `sable-roles` touches — six symbols plus one table. If you are reviewing this repo or building a stub, this is the complete contract. Anything not listed here, `sable-roles` does not use.

---

## 1. Import surface

Every `sable_platform` import in this repo:

```python
# sable_roles/main.py
from sable_platform.db.connection import get_db

# sable_roles/features/fitcheck_streak.py
from sable_platform.db import discord_guild_config, discord_streaks
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db
```

That's it. Four modules: `sable_platform.db.connection`, `sable_platform.db.discord_streaks`, `sable_platform.db.discord_guild_config`, `sable_platform.db.audit`.

---

## 2. `sable_platform.db.connection.get_db`

```python
def get_db(db_path: str | Path | None = None) -> CompatConnection
```

Returns a synchronous SQLAlchemy connection wrapper (`CompatConnection`). Used as a context manager:

```python
with get_db() as conn:
    ...  # conn is a sqlalchemy.engine.Connection-compatible object
```

- **DB target resolution:** explicit `db_path` arg wins; else `SABLE_DATABASE_URL` env var; else `~/.sable/sable.db` (SQLite). `sable-roles` always calls `get_db()` with no argument.
- The wrapper supports both `?`-positional and `:named` SQLAlchemy parameter styles, and `row["col"]` dict-style access on result rows.
- **Blocking:** `get_db()` is synchronous. Every `with get_db() as conn:` inside an async Discord handler blocks the event loop for the duration of the DB call. Acceptable at V1 traffic (single-digit events/min, sub-5ms local SQLite calls). See `CLAUDE.md` for the >50 events/min escape hatch.

---

## 3. `sable_platform.db.discord_streaks`

Four functions. All take a `Connection` as the first positional arg (the `conn` from `get_db()`). All SQL uses `:named` bind params. Write functions call `conn.commit()` internally.

### `upsert_streak_event`

```python
def upsert_streak_event(
    conn: Connection,
    org_id: str,
    guild_id: str,
    channel_id: str,
    post_id: str,
    user_id: str,
    posted_at: str,            # raw ISO timestamp of the Discord message
    counted_for_day: str,      # "YYYY-MM-DD" UTC calendar day
    attachment_count: int,
    image_attachment_count: int,
    ingest_source: str = "gateway",
) -> None
```

`INSERT ... ON CONFLICT (guild_id, post_id) DO UPDATE SET updated_at = excluded.updated_at`.

**Conflict semantics — critical:** on a duplicate `(guild_id, post_id)`, *only* `updated_at` is touched. `reaction_score`, `counts_for_streak`, `invalidated_at`, `invalidated_reason`, `posted_at`, `counted_for_day`, and `user_id` are **never clobbered** by a re-upsert. This is what lets `sable-roles` call `upsert_streak_event` idempotently on `on_message` without destroying reaction state owned by `update_reaction_score`.

### `update_reaction_score`

```python
def update_reaction_score(
    conn: Connection,
    guild_id: str,
    post_id: str,
    reaction_score: int,
    expected_updated_at: str,
) -> bool
```

Optimistic-locked UPDATE: `SET reaction_score = :score, updated_at = <now-ms> WHERE guild_id = :g AND post_id = :p AND updated_at = :expected`.

- Returns `True` if the row was updated (`rowcount == 1`), `False` if stale (another writer moved `updated_at` first — `rowcount == 0`).
- `<now-ms>` is a millisecond-resolution UTC ISO string (`...T...sssZ`). Migration-default rows start at second-resolution; the comparison is exact string equality (`row's-current-string == expected-passed-in`), never lexicographic, so the second→millisecond transition is safe within one lock cycle.
- `sable-roles` reads `expected_updated_at` from `get_event(...)["updated_at"]`, recomputes the score, and writes. On `False`, it logs `"reaction recompute lost race for post_id=..."` and drops — the next reaction event re-fires the debounce with a fresh `expected_updated_at`. No retry loop.

### `get_event`

```python
def get_event(conn: Connection, guild_id: str, post_id: str) -> dict | None
```

`SELECT * FROM discord_streak_events WHERE guild_id = :g AND post_id = :p LIMIT 1`. Returns the row as a plain `dict` (all columns from §5), or `None` if no row. `sable-roles` uses the returned `guild_id`, `post_id`, `user_id`, and `updated_at` keys.

### `compute_streak_state`

```python
def compute_streak_state(
    conn: Connection,
    org_id: str,
    user_id: str,
    as_of_day: str | None = None,   # "YYYY-MM-DD"; defaults to today UTC
) -> dict
```

Computes streak state app-side by iterating distinct `counted_for_day` values. Only counts rows with `counts_for_streak = 1 AND invalidated_at IS NULL`.

**Returned dict shape** (this is exactly what `_format_streak` in `fitcheck_streak.py` consumes):

| Key | Type | Meaning |
|---|---|---|
| `current_streak` | `int` | Consecutive days back from `as_of_day` (or yesterday if no fit today). `0` if no fit today and none yesterday. |
| `longest_streak` | `int` | Max consecutive-day run in the user's full history. |
| `total_fits` | `int` | Count of all streak-eligible rows for the user. |
| `most_reacted_post_id` | `str \| None` | `post_id` of the user's highest-`reaction_score` fit ever (tie-break: newest `posted_at`, then `post_id`). `None` if no fits. |
| `most_reacted_reaction_count` | `int` | That fit's `reaction_score`. `0` if no fits. |
| `most_reacted_channel_id` | `str \| None` | That fit's `channel_id`. Used to build the jump-link. |
| `most_reacted_guild_id` | `str \| None` | That fit's `guild_id`. Used to build the jump-link. |
| `today_post_id` | `str \| None` | `post_id` of today's fit (if any). |
| `today_reaction_count` | `int` | Today's fit's `reaction_score`. `0` if no fit today. |
| `posted_today` | `bool` | Whether `as_of_day` is in the user's distinct-day set. |

---

## 3b. `sable_platform.db.discord_guild_config` (V2 — relax-mode + burn-me)

Three functions. Per-guild config table — one row per configured guild, created lazily by the first mod toggle.

### `get_config`

```python
def get_config(conn: Connection, guild_id: str) -> dict
```

Returns a dict with keys: `guild_id`, `relax_mode_on` (int 0|1), `current_burn_mode` (str, "once"|"persist"), `updated_at` (str|None), `updated_by` (str|None). For unconfigured guilds, returns defaults (`relax_mode_on=0`, `current_burn_mode="once"`, `updated_at=None`, `updated_by=None`) — does not insert a row.

### `set_relax_mode`

```python
def set_relax_mode(conn: Connection, guild_id: str, on: bool, updated_by: str) -> None
```

Upserts `relax_mode_on` for a guild. On conflict, only `relax_mode_on`, `updated_at`, and `updated_by` change; `current_burn_mode` is preserved. Called by `/relax-mode` mod-only slash command.

### `set_burn_mode`

```python
def set_burn_mode(conn: Connection, guild_id: str, mode: str, updated_by: str) -> None
```

Upserts `current_burn_mode` for a guild. `mode` must be `"once"` or `"persist"` (raises `ValueError` otherwise). On conflict, only `current_burn_mode`, `updated_at`, and `updated_by` change; `relax_mode_on` is preserved. Reserved for the V2 burn-me feature; the interface ships now so the schema is stable.

---

## 4. `sable_platform.db.audit.log_audit`

```python
def log_audit(
    conn: Connection,
    actor: str,
    action: str,
    *,                              # keyword-only barrier
    org_id: str | None = None,
    entity_id: str | None = None,
    detail: dict | None = None,
    source: str = "cli",
) -> int
```

Appends a row to the `audit_log` table. Returns the new row id. `detail` is JSON-serialized to `detail_json`.

**How `sable-roles` calls it** — every enforcement action is audited:

```python
log_audit(
    conn,
    actor=f"discord:bot:{client.user.id}",   # the bot's Discord user id
    action="fitcheck_text_message_deleted",  # or "fitcheck_thread_create_failed"
    org_id=org_id,                           # resolved Sable org_id
    entity_id=None,
    detail={"post_id": ..., "user_id": ..., "dm_success": ..., ...},
    source="sable-roles",                    # always "sable-roles" for this bot
)
```

Actions this bot writes (V1): `fitcheck_text_message_deleted`, `fitcheck_thread_create_failed` — both with `source="sable-roles"` and `actor="discord:bot:<bot_user_id>"`.

V2 adds: `fitcheck_relax_mode_toggled` — written by `/relax-mode`, with `source="sable-roles"` and `actor="discord:user:<invoking_user_id>"` (a real Discord user toggled it, not the bot).

`audit_log` table columns the insert targets: `actor`, `action`, `org_id`, `entity_id`, `detail_json`, `source` (plus an autoincrement `id` and a timestamp default — both owned by SablePlatform's schema).

---

## 5. Tables

### `discord_streak_events` (migration 043)

Owned by SablePlatform — `sable_platform/db/migrations/043_discord_streak_events.sql` + a matching Alembic revision for Postgres. `sable-roles` never issues raw DDL; it only goes through the helpers in §3.

```sql
CREATE TABLE IF NOT EXISTS discord_streak_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id                 TEXT NOT NULL,
    guild_id               TEXT NOT NULL,
    channel_id             TEXT NOT NULL,
    post_id                TEXT NOT NULL,
    user_id                TEXT NOT NULL,
    posted_at              TEXT NOT NULL,
    counted_for_day        TEXT NOT NULL,        -- "YYYY-MM-DD" UTC calendar day
    attachment_count       INTEGER NOT NULL DEFAULT 0,
    image_attachment_count INTEGER NOT NULL DEFAULT 0,
    reaction_score         INTEGER NOT NULL DEFAULT 0,
    counts_for_streak      INTEGER NOT NULL DEFAULT 1,   -- 0 = excluded from streak math
    invalidated_at         TEXT,                          -- non-NULL = moderation-voided
    invalidated_reason     TEXT,
    ingest_source          TEXT NOT NULL DEFAULT 'gateway',
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (guild_id, post_id)
);

CREATE INDEX IF NOT EXISTS idx_discord_streak_events_org_day
    ON discord_streak_events (org_id, counted_for_day);
CREATE INDEX IF NOT EXISTS idx_discord_streak_events_user_day
    ON discord_streak_events (org_id, user_id, counted_for_day);
CREATE INDEX IF NOT EXISTS idx_discord_streak_events_channel_posted
    ON discord_streak_events (org_id, channel_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_discord_streak_events_user_reactions
    ON discord_streak_events (org_id, user_id, reaction_score DESC);
```

**Column ownership / mutation rules:**
- `reaction_score` — owned exclusively by `update_reaction_score`. Never set by `upsert_streak_event`.
- `counts_for_streak`, `invalidated_at`, `invalidated_reason` — moderation-only. Set by an operator (raw SQL or a future admin CLI), never by the bot's runtime path. `compute_streak_state` filters on `counts_for_streak = 1 AND invalidated_at IS NULL`.
- `posted_at`, `counted_for_day`, `user_id` — immutable post facts. Written once on first insert.
- `updated_at` — the optimistic-lock token. Bumped on every `upsert_streak_event` conflict and every `update_reaction_score`.

### `discord_guild_config` (migration 045)

```sql
CREATE TABLE IF NOT EXISTS discord_guild_config (
    guild_id          TEXT PRIMARY KEY,
    relax_mode_on     INTEGER NOT NULL DEFAULT 0,
    current_burn_mode TEXT NOT NULL DEFAULT 'once',
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_by        TEXT NOT NULL
);
```

One row per configured guild, created lazily by the first mod toggle. Rows never deleted; toggles upsert.

**Column ownership / mutation rules:**
- `relax_mode_on` — owned exclusively by `set_relax_mode`. Preserved on `set_burn_mode` conflict.
- `current_burn_mode` — owned exclusively by `set_burn_mode`. Preserved on `set_relax_mode` conflict. Values constrained to `{"once", "persist"}` by the helper (no DB-level CHECK).
- `updated_at`, `updated_by` — bumped on every helper call. `updated_by` is the Discord user ID of the invoking mod.

---

## 6. If you want to actually run this repo

The maintainer's choice (2026-05) is to keep this repo **review-only** on GitHub — the contract above is the substitute for shipping SablePlatform. To run tests or the bot yourself, you would need one of:

1. **The real SablePlatform repo** — `pip install -e <path-to-SablePlatform>`, then `pip install -e .` here. This is how the maintainer runs it.
2. **A stub `sable_platform` package** implementing exactly §2–§5 against in-memory SQLite. Not shipped here; this document is the spec you'd build it from. The four `discord_streaks` functions are pure SQL over one table, `log_audit` is one INSERT, and `get_db()` is a SQLAlchemy connection factory — a faithful stub is on the order of ~150 lines.

For pure code review and "point your Claude at it" understanding, you don't need either — the source plus this contract plus `CLAUDE.md`/`AGENTS.md` is the full picture.
