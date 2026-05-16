# sable-roles — Claude Context

This file captures project context, architectural decisions, and active plans so they survive conversation restarts. Mirrors `AGENTS.md` — keep them in sync.

---

## Repository layout note (read first if you're external)

`sable-roles` is one of several repos in the Sable tool stack. This file was written for the maintainer's local environment and references absolute paths like `~/Projects/SablePlatform/...` and `~/Projects/SolStitch/internal/...`. **Those are Sable-internal repos and documents — they are not part of this GitHub repository.**

The one dependency that genuinely matters for understanding the code is **SablePlatform** (the bot imports `sable_platform.db.*`). Its full surface — the six symbols and one table this bot uses — is specified self-contained in [`docs/SABLEPLATFORM_CONTRACT.md`](docs/SABLEPLATFORM_CONTRACT.md). When this file points at a `~/Projects/SablePlatform/...` file, that contract doc is the in-repo substitute. The other `~/Projects/...` references (build plan, ship runbook) are design-history context that lives outside this repo by design; the code plus this file plus the contract doc is the complete picture for review.

---

## What this is

A dedicated Discord bot for **Sable's community-role automation across client servers**. V1 ships fit-check streak tracking + image-only enforcement for SolStitch's `#fitcheck`. Future features (e.g. `@influenza` monthly rotation, role-tier grants tied to points) plug into the same bot process.

**Repo root:** `~/Projects/sable-roles/`
**Status:** V1 live in SolStitch since 2026-05-13. Bot running locally on Sieggy's machine; VPS deploy targeted within 24-48h of go-live.

**Build plan (source of truth):** `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md`
**Chunked build TODO + audit history:** `~/Projects/SolStitch/internal/fitcheck_build_TODO.md` (C1-C9 all `[x]`)
**Ship runbook (live-ops):** `~/Projects/SolStitch/internal/ship_dms.md`

---

## Architecture

```
Discord gateway (one connection, multi-guild)
    ↓
SableRolesClient(discord.Client)        sable_roles/main.py
    ├─ setup_hook
    │   • register feature handlers (fitcheck_streak.register)
    │   • register slash commands (fitcheck_streak.register_commands → /streak)
    │   • per-guild copy_global_to + tree.sync from GUILD_TO_ORG
    ├─ on_ready  (logs "sable-roles connected as <bot_user> · fitcheck streak active",
    │             warns on 24h-empty discord_streak_events)
    └─ close()   (drains in-flight reaction-recompute debounce tasks before super().close())

features/fitcheck_streak.py
    on_message            → image branch:  upsert discord_streak_events
                                          → react 🔥 (try/except discord.HTTPException)
                                          → create_thread (try/except + audit row on failure)
                          → text branch:   delete + DM (5-min per-user cooldown)
                                          → audit row "fitcheck_text_message_deleted"
    on_raw_reaction_add   → _schedule_recompute (2s debounce, post_id-keyed dict)
    on_raw_reaction_remove → _schedule_recompute
    _recompute_after_delay → asyncio.sleep(2)
                          → fetch message, filter bot+self reactions
                          → discord_streaks.update_reaction_score (optimistic-locked)
                          → on stale (rowcount=0): log "lost race", drop
    register_commands(tree)
        /streak (ephemeral) → compute_streak_state → _format_streak

SablePlatform integration (NOT in this repo — owned by SP):
    sable_platform.db.discord_streaks
        upsert_streak_event         INSERT ... ON CONFLICT DO UPDATE updated_at only
                                    (never clobbers reaction_score / counts_for_streak / invalidated_*)
        update_reaction_score       UPDATE ... WHERE updated_at = :expected  (optimistic lock)
        get_event                   SELECT * by (guild_id, post_id)
        compute_streak_state        SELECT DISTINCT counted_for_day → app-side iteration
    sable_platform.db.audit.log_audit  (source="sable-roles", actor="discord:bot:<bot_user_id>")
    Migration 043 + Alembic revision b2da0d6b1be1
```

Multi-client: one bot process serves all client servers. `GUILD_TO_ORG` (from `SABLE_ROLES_GUILD_TO_ORG_JSON` env var) maps guild_id → SablePlatform `org_id`. `FITCHECK_CHANNELS` (from `SABLE_ROLES_FITCHECK_CHANNELS_JSON`) maps guild_id → {org_id, channel_id}. Same shape conventions as SableTracking's `DISCORD_GUILD_TO_CLIENT`.

**Single-process constraint:** module-level reverse-lookup dicts (`_FITCHECK_CHANNEL_IDS`, `_CHANNEL_TO_GUILD`) are built at import time from `FITCHECK_CHANNELS`. `_dm_cooldown` and `_pending_recomputes` are module-level dicts with no cross-process invalidation. Running two bot processes against the same guild will double-fire reactions/deletes and race on optimistic-locked writes (one will lose, log, drop — but the duplicate DM still sends). Do not introduce a second replica without first moving routing/cooldown/debounce state to SablePlatform.

---

## Key design decisions

**Discord intents — `members` removed (plan §1, audit round 2):**
`Intents.default()` + `message_content` only. The `members` privileged intent is NOT enabled. Reaction recompute fetches reactors directly via `reaction.users()` per message, no member cache needed. Re-enabling `members` requires a privileged-intent toggle in the developer portal + behavioral review for cache-invalidation correctness.

**Message Content is privileged:**
Must be enabled in the Discord developer portal under Bot → Privileged Gateway Intents. Without it, gateway connection fails with close code `4014`. See `INVITE_SETUP.md` §3.

**Raw reaction events (not cached):**
`on_raw_reaction_add` / `on_raw_reaction_remove` are used (not `on_reaction_add` / `on_reaction_remove`) because raw events fire for any message regardless of cache membership. After a bot restart, the in-memory message cache is empty; cached-only events would silently drop reactions on pre-restart posts.

**2-second debounce + optimistic lock for reaction scoring:**
`_pending_recomputes: dict[post_id, asyncio.Task]` coalesces rapid reaction add/remove churn into one recompute per post. The recompute reads `event["updated_at"]`, re-counts reactors via `reaction.users()`, then `update_reaction_score(... expected_updated_at=...)` — the SQL `WHERE updated_at = :expected` clause is the optimistic gate. If another recompute landed first (`rowcount=0`), the loser logs "lost race" and drops; the next reaction event re-fires the debounce with a fresh `expected_updated_at`. No retry loop in V1 — by design (a fresh reaction will trigger fresh recompute).

**Debounce pop-race safety:**
Each `_recompute_after_delay` captures `self_task = asyncio.current_task()` at entry. The `finally` clause pops from `_pending_recomputes` **only if** the registered task is still `self_task` — protects against a replacement task being clobbered when an earlier cancelled task unwinds. `CancelledError` re-raises (never swallowed) so the cancelling caller's `asyncio.gather(..., return_exceptions=True)` in `close()` sees clean unwind.

**`close()` drains in-flight debounces:**
`SableRolesClient.close()` calls `fitcheck_streak.close()` BEFORE `super().close()`. `fitcheck_streak.close()` cancels all pending tasks then awaits them with `return_exceptions=True`. Without this drain, `super().close()` tears down the event loop while debounce tasks still hold open handles → `Task was destroyed but it is pending!` warnings on shutdown.

**`setup_hook`, not `on_ready`, hosts slash-command sync:**
`on_ready` can fire multiple times on gateway reconnect, which would re-sync commands and trip Discord rate limits. `setup_hook` runs once before login (matches SableTracking precedent). Per-guild registration uses `copy_global_to(guild=...)` + `await tree.sync(guild=...)` for instant per-guild availability vs the 1-hour global propagation window.

**Image detection — content_type first, extension fallback:**
`is_image(att)` returns True if `att.content_type.startswith("image/")` AND `content_type != "image/svg+xml"` (SVG excluded — Discord doesn't render + sandbox risk). Falls back to extension allowlist (`.png/.jpg/.jpeg/.gif/.webp/.heic/.heif/.avif/.bmp`) when content_type is missing/generic (e.g. `application/octet-stream`). Extension is spoofable — accepted for V1; document the spoof risk in any future hardening review.

**GIF-picker GIFs are NOT images:**
Discord's GIF picker (Tenor/Giphy) sends an embed with no attachment. Embeds are not iterated; only `message.attachments` is. By design — animated reaction GIFs in `#fitcheck` are treated as text-only spam and deleted.

**DM cooldown is per-user, in-memory:**
`_dm_cooldown: dict[user_id, datetime]` — 5-minute window. Suppresses DM but still deletes the message + writes audit row with `dm_suppressed_for_cooldown=True`. The dict has no LRU cap; at scale this grows unbounded but at V1 traffic (single-digit events/min) the cost is negligible. Add LRU before second-tenant scale (see C3 minor follow-up (c)).

**DM bank rotates random per offense:**
`DM_BANK` is 4 lines in `config.py`. `random.choice` per text-only message. No per-user state — repeat offenses can hit the same line back-to-back, intentional (varied feels organic; deterministic rotation would feel mechanical).

**Streak day-bucket = calendar UTC:**
`counted_for_day = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")`. Geo-neutral, simplest, swappable later because raw `posted_at` is preserved separately. Hard reset on miss (V1). No freeze. No backfill (streaks start at gateway-connect). All decided in grill 2026-05-11.

**Reaction filters: exclude bot reactions + self-reactions:**
`bot_ids = {client.user.id}` plus `u.id != author_id`. Algorithm: raw count (no tier weighting in V1). Surfaced in `/streak` as today's reaction count + jump-link to most-reacted-ever fit.

**`compute_streak_state` is app-side iteration, not SQL aggregate:**
`SELECT DISTINCT counted_for_day FROM discord_streak_events WHERE org_id = :o AND user_id = :u AND counts_for_streak = 1 AND invalidated_at IS NULL ORDER BY counted_for_day DESC` → Python iterates: `current_streak` = consecutive days back from today UTC, `longest_streak` = max run in full history. Simpler than recursive CTE, sub-1ms at any plausible V1 row count.

**Save BEFORE thread/reaction calls:**
On `on_message` image branch, the DB upsert lands FIRST, then 🔥 reaction + thread creation run inside try/except. If Discord-side calls fail (rate limit, missing perms, channel deleted mid-handler), streak credit survives. Inverse ordering would lose credit on transient failures.

**Audit log every enforcement action:**
Text-only delete → `fitcheck_text_message_deleted` audit row with full `dm_success` + `dm_suppressed_for_cooldown` + post_id detail. Thread-create failure → `fitcheck_thread_create_failed` audit row. Lets us answer "did the bot delete X's message?" / "why didn't a thread spawn?" from SQL alone.

**Operator-allowlist NOT implemented in V1:**
Bot deletes any text-only post, including from `@Atelier` (admins). Discord role hierarchy does NOT protect messages from Manage-Messages deletion (hierarchy gates kick/ban/role-edit, not message moderation). The Brian DM in `ship_dms.md` §1 makes this explicit so consent is up-front. If Brian loses patience: see SablePlatform TODO §SolStitch follow-up #3 (config-driven allowlist).

---

## Working conventions

- **Small patches over rewrites.** Don't refactor `fitcheck_streak.py` cosmetically — it was audited byte-for-byte against the build plan across 5 chunks.
- **Tests use `pytest-asyncio` in `asyncio_mode=auto`.** Don't add explicit `@pytest.mark.asyncio` decorators — `pyproject.toml` sets the mode globally.
- **`conftest.py` fixture `fitcheck_module` patches the three module-level dicts** (`FITCHECK_CHANNELS`, `_FITCHECK_CHANNEL_IDS`, `_CHANNEL_TO_GUILD`, `_pending_recomputes`) per test. Any new module-level state needs to be added there or tests will leak state across runs.
- **Module-level dicts shared cross-feature MUST be reset via `.clear()`, NOT rebound.** `burn_me._burn_invoke_cooldown` is imported by reference into `roast.py`; rebinding it (`monkeypatch.setattr(bm, "_burn_invoke_cooldown", {})`) silently severs the identity that `roast.py` sees, so a cross-feature test would stop sharing the cooldown. Use the autouse `.clear()` pattern in `tests/test_roast_peer_path.py` as the template.
- **Don't change `_format_streak` output without updating the SableWeb / future-API consumer expectations** — the angle-bracket embed suppression on the best-fit URL is load-bearing.
- **DB writes go through SablePlatform helpers, not raw SQL.** `discord_streaks.upsert_streak_event` / `update_reaction_score` / `get_event` / `compute_streak_state` are the only surface. Match the SableTracking pattern of strict layering.
- **Audit-log every enforcement action.** `actor="discord:bot:<bot_user_id>"`, `source="sable-roles"`, `org_id=<resolved>`, `entity_id=None`, structured `detail` dict.
- **Run both test suites** before declaring any change green: `cd ~/Projects/sable-roles && .venv/bin/pytest tests/` AND `cd ~/Projects/SablePlatform && .venv/bin/pytest tests/db/test_discord_streaks.py tests/db/test_schema.py`. Schema parity tests will catch any `discord_streak_events` `Table()` drift vs migration 043.
- **No new repo dependencies without justification.** Current deps: `discord.py>=2.7`, `python-dotenv`, SablePlatform (editable), `pytest`, `pytest-asyncio`. Bot-feature work should be doable with just these.

---

## What's built and working

- `SableRolesClient(discord.Client)` with `setup_hook` (per-guild instant `/streak` sync), `on_ready` (24h-empty warning), `close()` (debounce drain)
- `on_message`: image-only enforcement in configured `#fitcheck` channels — upsert streak event → 🔥 reaction → auto-thread `<display_name> · <YYYY-MM-DD>` (UTC date, 100-char truncation)
- Text-only / GIF-picker / emoji-only deletion + rotating DM (4-line bank) + 5-min per-user cooldown + audit row
- `on_raw_reaction_add` / `on_raw_reaction_remove` → 2s debounced per-post recompute → optimistic-locked write, stale-write logged + dropped
- `/streak` ephemeral slash command: current/longest/total + today's reactions + jump-link to most-reacted-ever fit (angle-bracketed to suppress embed)
- 76 tests passing (`tests/test_image_detection.py`, `tests/test_dm_bank.py`, `tests/test_dm_cooldown.py`, `tests/test_reaction_recompute.py`, `tests/test_debounce_race.py`, `tests/test_handler_resilience.py`, `tests/test_unconfigured_guild.py`, `tests/test_format_streak.py`). Plus 19 SablePlatform tests at `~/Projects/SablePlatform/tests/db/test_discord_streaks.py`.
- Live in SolStitch (guild `1501026101730869290`, `#fitcheck` channel `1501073373252292709`) since 2026-05-13.

---

## What's not built yet

1. **VPS deployment** — runs on Sieggy's local machine via `python -m sable_roles.main`. Target: Hetzner VPS within 24-48h of go-live (per build plan §6). See `OPERATIONS_RUNBOOK.md` §6.
2. **`tree.sync` try/except in `setup_hook`** (`main.py:47`) — hardening pass before any second-guild onboarding. Currently a Forbidden on one guild crashes the whole bot.
3. **Operator allowlist for `#fitcheck` enforcement** — Brian (admin) gets deleted same as anyone. Config-driven user_id list to bypass delete+DM.
4. **`@influenza` rotation feature** — same bot host. Monthly top-N yappers via SableTracking listener data → role grant/revoke. Memory: `project_solstitch_influenza`.
5. **Backfill admin CLI** — V1 starts streaks at gateway-connect; no history import. Defer until asked.
6. **Health/status surfacing** — V1 logs to stdout only. `#sable-ops` health-ping deliberately removed (plan round-3 audit — bot has no channel overwrite). When deployed: pull stdout from journalctl/compose logs; consider a `/sable-roles-status` slash command or a SablePlatform alert on `discord_streak_events.created_at` staleness.
7. **Tier-weighted reactions, public leaderboard, freeze policy, thread-reply scoring, squads, streak-tier roles, AI-gen detection** — all deferred to V2 per plan §8.

See `~/Projects/SolStitch/internal/fitcheck_build_TODO.md` for chunk-level minor follow-ups (cosmetic + non-blocking).

---

## Secrets & credentials

**GitHub status:** not pushed to a remote yet — repo is local-only at time of writing. `.env` is gitignored. No secrets in source.

**What's in `.env` (live credentials on local disk):**
- `SABLE_ROLES_DISCORD_TOKEN` — Discord bot token for the `Sable Roles` app (application_id `1504314425581244548`). Resets via developer portal Bot → Reset Token if leaked.
- `SABLE_ROLES_FITCHECK_CHANNELS_JSON` — JSON: `{"<guild_id>": {"org_id": "<sable_org>", "channel_id": "<fitcheck_channel>"}}`. Live SolStitch entry: `{"1501026101730869290":{"org_id":"solstitch","channel_id":"1501073373252292709"}}`.
- `SABLE_ROLES_GUILD_TO_ORG_JSON` — JSON: `{"<guild_id>": "<org_id>"}`. Live SolStitch: `{"1501026101730869290":"solstitch"}`.
- `SABLE_ROLES_HEALTH_CHANNELS_JSON` — Reserved, currently `{}`. V1 health is stdout-only.

**Hardcoded (not sensitive):** `DM_BANK`, `DM_COOLDOWN_SECONDS=300`, `CONFIRMATION_EMOJI="🔥"`, `DEBOUNCE_SECONDS=2.0`, `IMAGE_EXT_ALLOWLIST` — all in `sable_roles/config.py`. Change those by editing config and restarting.

---

## Key symbols

- `SableRolesClient` (`main.py:32`) — `discord.Client` subclass with `setup_hook` / `on_ready` / `close()` overrides
- `fitcheck_streak.register(client)` — wires `on_message` + reaction handlers to the client instance
- `fitcheck_streak.register_commands(tree)` — registers `/streak` against the command tree
- `fitcheck_streak.close()` — debounce drain hook (cancels + awaits all `_pending_recomputes`)
- `is_image(att)` — content-type-first + extension-fallback image detection
- `_recompute_after_delay(channel_id, post_id)` — the 2-second debounce body with self-identity-guarded pop
- `_format_streak(state, guild_id)` — `/streak` output renderer; both posted-today / no-fit-today + best-fit / none-yet branches
- `FITCHECK_CHANNELS`, `GUILD_TO_ORG` — env-loaded routing dicts in `config.py`
- `_FITCHECK_CHANNEL_IDS`, `_CHANNEL_TO_GUILD` — module-level reverse-lookup tables built once at import

---

## Active plans / decisions in progress

*(Add entries here when a plan is agreed but not yet implemented)*

- **Item 1 — VPS deploy.** Hetzner VPS already hosts SP's Docker stack. Add `sable-roles` to compose; mount `~/.sable/sable.db` (or `SABLE_DATABASE_URL` for Postgres) so the bot writes to the same `discord_streak_events` table prod queries from. No design decisions outstanding — execution only.
- **Item 2 — `setup_hook` try/except hardening.** Wrap `tree.sync(guild=...)` in `try/except discord.HTTPException` per SableTracking `bot.py:31-34` precedent. One bad guild_id should log + skip, not crash the whole process. Trivially a one-block edit.
- **Item 3 — Operator allowlist.** Add `SABLE_ROLES_FITCHECK_ALLOWLIST_JSON` env var (shape: `{"<guild_id>": ["<user_id>", ...]}`). On image-less message in fit-check channel, check allowlist first — if member, skip delete+DM but still audit-log `allowlist_skipped` for traceability. ~10 LOC.

---

## File map

```
sable_roles/
  __init__.py
  main.py                    — SableRolesClient + entrypoint (registers fitcheck → roast → vibe_observer in order)
  cli.py                     — operator CLI (backfill_blocklist, grandfather_restoration_tokens)
  config.py                  — env-driven config: token, FITCHECK_CHANNELS, GUILD_TO_ORG, MOD_ROLES,
                               INNER_CIRCLE_*, BURN_*, PEER_ROAST_ROLES (R2), PERSONALIZE_ADMINS (R2),
                               OBSERVATION_CHANNELS (R2), VIBE_* (R2), DM_BANK + tunables
  features/
    __init__.py
    fitcheck_streak.py       — on_message, on_raw_reaction_add/remove, /streak, /relax-mode,
                               _format_streak, _schedule_recompute, _recompute_after_delay,
                               close (debounce drain). Image-branch tail dispatches:
                               burn_me.maybe_roast + roast.maybe_grant_restoration_token (R8)
    burn_me.py               — /set-burn-mode, /burn-me, /stop-pls (sticky blocklist + vibe-purge R4);
                               generate_roast → (text, audit_id) tuple (R7) w/ optional actor_user_id +
                               vibe_block kwargs (R11); record_roast_reply helper (R7); maybe_roast
    roast.py                 — /set-personalize-mode (R3); context-menu "Roast this fit" router
                               (R5 mod + R7 peer dispatch); _handle_peer_roast w/ token economy + caps +
                               refunds + DM + flag (R7); _maybe_grant_monthly_token seam (R6);
                               /my-roasts (R6); /peer-roast-report (R9); _maybe_fetch_vibe_block (R11);
                               maybe_grant_restoration_token (R8); _handle_flag_reaction (R7);
                               _send_peer_roast_dm; register(client) composes with existing handlers
    vibe_observer.py         — R10/R11: on_message + on_raw_reaction_add raw capture (composes with
                               existing handlers); daily rollup cron; nightly GC; weekly inference cron
                               (gated on personalize_mode_on + check_budget); _maybe_grant_*
                               token + _send_peer_roast_dm. VIBE_OBSERVATION_ENABLED kill switch.
                               start_tasks / stop_tasks for background loops
    airlock.py               — A3-A6: invite-source-aware new-member verification.
                               _fetch_live_invites + _persist_invite_snapshot (split-fetch pattern so
                               diff baseline survives until after attribute_join), _on_invite_create,
                               _on_invite_delete, _handle_member_join (team auto-admit OR
                               non-team hold w/ DM + #triage ping), _handle_member_remove
                               (left_during_airlock transition), /admit + /ban + /kick +
                               /airlock-status (AIRLOCK_TRIAGE_ROLES tier), /add-team-inviter +
                               /list-team-inviters (MOD_ROLES team-only tier), _can_triage_airlock
                               + _format_mod_ping pure helpers. AIRLOCK_ENABLED kill switch.
                               bootstrap(client) wires env-seed team-inviters + invite-snapshot
                               first-fetch on on_ready (reconnect-safe).
  prompts/
    burn_me_system.py        — locked roast voice + safety rails (B5)
    vibe_infer_system.py     — R11: strict-JSON vibe inference prompt (5 fields, imperative denylist)
tests/
  conftest.py                — fitcheck_module fixture, fetch_audit_rows, fetch_streak_rows
  test_image_detection.py / test_dm_bank.py / test_dm_cooldown.py / test_unconfigured_guild.py
  test_handler_resilience.py / test_reaction_recompute.py / test_debounce_race.py
  test_format_streak.py / test_is_mod.py
  test_relax_mode_behavior.py / test_relax_mode_command.py
  test_burn_me_commands.py / test_burn_me_integration.py / test_burn_me_pipeline.py / test_burn_me_state.py
  test_stop_pls_blocklist.py / test_maybe_roast_blocklist.py / test_cli_backfill_blocklist.py
  test_personalize_toggle.py             — R3 /set-personalize-mode
  test_roast_mod_path.py                 — R5 mod context-menu
  test_my_roasts.py                      — R6 /my-roasts + lazy-grant seam
  test_roast_peer_path.py                — R7 peer path + DM + 🚩 flag + router dispatch
  test_streak_restoration.py             — R8 maybe_grant_restoration_token + CLI grandfather
  test_peer_roast_report.py              — R9 /peer-roast-report
  test_vibe_observer.py                  — R10 listener + rollup + GC + kill switch
  test_vibe_inference.py                 — R11 inference + vibe_block injection
  test_airlock.py                        — A3-A6 invite snapshot + member join + mod commands
INVITE_SETUP.md              — Discord developer portal walkthrough + invite URL
SMOKE_TEST.md                — fitcheck V1 smoke (10 scenarios)
SMOKE_TEST_ROAST.md          — R12: /roast V1+V2 + personalization smoke (28 scenarios)
PINNED_FITCHECK_MESSAGE.md   — R12: canonical mechanic reference text for ops to pin in #fitcheck
SMOKE_TEST_AIRLOCK.md        — A7: airlock smoke (15+ scenarios)
PINNED_WAITING_ROOM_MESSAGE.md — A7: proof-of-aura text for ops to pin in #outside
OPERATIONS_RUNBOOK.md        — Live-ops runbook: boot, monitor, vibe cron, pin sequence, rollback
AGENTS.md / CLAUDE.md        — Mirror context files for AI assistants
README.md                    — Setup + run + test
pyproject.toml               — discord.py>=2.7, anthropic, python-dotenv, pytest, pytest-asyncio
.env / .env.example          — live env / template (gitignored)
.gitignore                   — excludes .env + .venv + caches
```

**External dependencies (other Sable repos):**
- `~/Projects/SablePlatform/sable_platform/db/discord_streaks.py` — streak helpers + list_active_streak_users (R8)
- `~/Projects/SablePlatform/sable_platform/db/discord_burn.py` — opt-in + daily-cap helpers (B5)
- `~/Projects/SablePlatform/sable_platform/db/discord_guild_config.py` — relax/burn/personalize mode (R3)
- `~/Projects/SablePlatform/sable_platform/db/discord_roast.py` — blocklist + token economy + flags +
  aggregate_peer_roast_report + last_consumed_token (R6) + find_peer_roast_for_bot_reply (R7) (R1+R6+R7)
- `~/Projects/SablePlatform/sable_platform/db/discord_user_vibes.py` — message-observations,
  rollups, vibe upsert + validation, purge (R1) + list_recent_observation_users (R10)
- `~/Projects/SablePlatform/sable_platform/db/discord_airlock.py` — A1: invite snapshot diff,
  team-inviter allowlist, member admit ledger with airlock state machine
- `~/Projects/SablePlatform/sable_platform/db/migrations/043_discord_streak_events.sql`
- `~/Projects/SablePlatform/sable_platform/db/migrations/045_relax_mode_persist.sql` (B3)
- `~/Projects/SablePlatform/sable_platform/db/migrations/046_burn_optins_random_log.sql` (B5/R0)
- `~/Projects/SablePlatform/sable_platform/db/migrations/047_roast_personalization.sql` (R1) — 6 new tables + alter
- `~/Projects/SablePlatform/sable_platform/db/migrations/048_airlock.sql` (A1) — 3 tables for airlock
- `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md` — fitcheck V1 plan
- `~/Projects/SolStitch/internal/fitcheck_build_TODO.md` — fitcheck V1 audit history
- `~/Projects/SolStitch/internal/burn_me_v1_build_plan.md` — burn-me V1 plan
- `~/Projects/SolStitch/internal/burn_me_build_TODO.md` — burn-me V1 audit history
- `~/Projects/SolStitch/internal/roast_v1_v2_personalization_plan.md` — /roast plan (R0-R13)
- `~/Projects/SolStitch/internal/roast_build_TODO.md` — /roast audit history
- `~/Projects/SolStitch/internal/ship_dms.md` — Live-ship runbook (Brian + Cahit DMs)
