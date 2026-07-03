# sable-roles — Claude Context

This file captures project context, architectural decisions, and active plans so they survive conversation restarts. Mirrors `AGENTS.md` — keep them in sync.

---

## Repository layout note (read first if you're external)

`sable-roles` is one of several repos in the Sable tool stack. This file was written for the maintainer's local environment and references absolute paths like `~/Projects/SablePlatform/...` and `~/Projects/SolStitch/internal/...`. **Those are Sable-internal repos and documents — they are not part of this GitHub repository.**

The one dependency that genuinely matters for understanding the code is **SablePlatform** (the bot imports `sable_platform.db.*`). Its full surface — the six symbols and one table this bot uses — is specified self-contained in [`docs/SABLEPLATFORM_CONTRACT.md`](docs/SABLEPLATFORM_CONTRACT.md). When this file points at a `~/Projects/SablePlatform/...` file, that contract doc is the in-repo substitute. The other `~/Projects/...` references (build plan, ship runbook) are design-history context that lives outside this repo by design; the code plus this file plus the contract doc is the complete picture for review.

---

## What this is

A dedicated Discord bot for **Sable's community-role automation across client servers**. V1 ships fit-check streak tracking + image-only enforcement for SolStitch's `#fitcheck`. V2 adds the burn-me + roast + vibe + airlock + scored-mode stack. Future features (e.g. `@influenza` monthly rotation, role-tier grants tied to points) plug into the same bot process.

**Repo root:** `~/Projects/sable-roles/`
**Status:** V1 live in SolStitch since 2026-05-13; V2 burn-me + roast + vibe + airlock shipped on Hetzner VPS 2026-05-16 (per `project_stitzy_vps_deployed`). Scored Mode V2 (Pass A+B+C) lives on branch `scored-mode-pass-ab`, default `state='off'` so the deploy ships invisible until a mod flips per-guild.

**Build plan (source of truth):** `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md`
**Chunked build TODO + audit history:** `~/Projects/SolStitch/internal/fitcheck_build_TODO.md` (C1-C9 all `[x]`)
**Ship runbook (live-ops):** `~/Projects/SolStitch/internal/ship_dms.md`
**Scored Mode V2 plan:** `~/Projects/SolStitch/internal/fitcheck_scored_mode_plan.md` (design locked 2026-05-16; canonical for Pass A+B+C+D)
**Scored Mode QA log:** `~/Projects/SolStitch/internal/scored_mode_pass_ab_qa_log.md` (2 rounds approved per pass)

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
        set_phash_on_streak_event   once-set immutable image_phash (Scored Mode Pass A)
        list_recent_phashes_for_collision  90d window for collision detection
    sable_platform.db.discord_fitcheck_scores   (Scored Mode Pass B+C)
        upsert_score_success / record_score_failure / get_score
        count_pool_size / fetch_curve_pool_raw_totals
        mark_reveal_fired           one-and-done CAS, 'pending' placeholder for reveal_post_id
        update_reveal_post_id       guarded swap 'pending' → real reply message id
        mark_reveal_publish_failed  guarded 'pending' → 'publish_failed' (HIGH audit)
        convert_pending_to_cancelled_deleted  guarded 'pending' → 'cancelled_deleted' (HIGH audit)
        mark_reveal_cancelled_deleted        delete-handler CAS path
        record_emoji_milestone_crossing      INSERT ... ON CONFLICT DO NOTHING per (post, emoji, milestone)
        invalidate_score
    sable_platform.db.discord_scoring_config   (Scored Mode Pass B)
        get_config (defaults state='off')
        set_state  validates off|silent|revealed; audit inside
        count_status_breakdown
    sable_platform.db.audit.log_audit  (source="sable-roles", actor="discord:bot:<bot_user_id>")
    Migrations 043 (streaks) + 045–048 (V2 stack) + 049–052 (Scored Mode V2 Pass A+B+C)
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

### Scored Mode V2 (Pass A+B+C; branch `scored-mode-pass-ab`, default `state='off'`)

**Default state `'off'` is triple-guarded.** SQL `DEFAULT 'off'` on `discord_scoring_config.state` (migration 051), `get_config` returns `state='off'` when no row exists, and `scoring_pipeline.maybe_score_fit` short-circuits BEFORE any Anthropic call when state is `'off'`. Tests assert each layer (`tests/db/test_discord_scoring_config.py` + `test_scoring_pipeline.py::test_state_off_blocks_all_scoring_no_api_no_db_write`). NO file in either repo flips state to `silent` or `revealed` at init time — operator must `/scoring set` explicitly.

**3-state machine: off / silent / revealed (per-guild).** State lives in `discord_scoring_config`. Pass A (image phash + delete monitor) runs regardless of state; Pass B (vision pipeline + scoring) only fires when state ≠ `off`; Pass C reveal-fire only fires when state == `revealed` AND `posted_at >= state_changed_at` (silent-period posts never reveal even after a Silent → Revealed flip — design §8.3 strict reading; implemented via the post-time gate in `reveal_pipeline._recompute_after_delay` step 9).

**Pass A is always-on defensive infra.** `image_hashing.compute_phash_and_check_collisions` runs on every counted fit regardless of scoring state. pHash stored on `discord_streak_events.image_phash` (Pass A migration 049). Collision detection emits `fitcheck_repost_detected` (LOW, same user) or `fitcheck_image_theft_detected` (HIGH, different user). 90-day window, Hamming distance ≤ 8. `delete_monitor` REPLACE-binds `on_raw_message_delete` + `on_raw_message_edit` — the docstring is honest about REPLACE semantics, future delete/edit binders MUST compose via the `roast.py:register` pattern.

**Prompt-injection defense in scoring pipeline.** Round-1 QA caught user-controlled `display_name` flowing into the Sonnet payload (`scoring_pipeline.py` pre-fix lines 397-401). Fix dropped the `poster:` line entirely; `context_text` now only carries operator-controlled `posted_at` + the JSON schema reminder. Re-introducing display_name (or any user-controlled field) into the API payload is a regression — the inline threat-model comment block in `scoring_pipeline.py` is the durable warning.

**`/scoring set` requires a confirmation view.** `_ScoringSetConfirmView` (`scoring_pipeline.py`) — Discord `ui.View` with danger-style Confirm + neutral Cancel, author-locked via `interaction_check`, buttons disable + `self.stop()` on click, 60s timeout (`on_timeout` disables buttons defensively), same-state no-op short-circuits BEFORE view construction. The Confirm callback wraps `set_state` in try/except for graceful "DB error — try again" on failure. Single typo cost was deemed too high for any one-shot path.

**Reveal pipeline composes; it does not REPLACE.** `reveal_pipeline.register(client)` wraps existing `on_raw_reaction_add` / `on_raw_reaction_remove` / `on_message` / `on_raw_message_delete` handlers via the `roast.py:register` compose-existing-handler pattern. Registered LAST in `main.py.setup_hook` (after `fitcheck_streak`, `burn_me`, `roast`, `vibe_observer`, `airlock`, `delete_monitor`, `scoring_pipeline`). Holds the reverse-lookup tables BY REFERENCE (`fs._FITCHECK_CHANNEL_IDS`), not by snapshot — runtime channel-config changes propagate without re-register.

**5-second debounce + CAS-locked reveal-fire.** `reveal_pipeline._recompute_after_delay` mirrors V1 fitcheck_streak's debounce pattern (self-identity-guarded pop, `CancelledError` re-raise, `close()` drain). Per-post task dict capped at 1024 entries with oldest-insertion eviction. On trigger + state == `'revealed'` + post-time gate: re-read live `discord_scoring_config` (defends against mid-recompute mod flip), CAS-lock via `mark_reveal_fired` with placeholder `reveal_post_id='pending'`, publish reply via `message.reply(..., allowed_mentions=AllowedMentions.none())` (defends against display_name mention-injection), then EITHER `update_reveal_post_id` (success) OR `convert_pending_to_cancelled_deleted` (404 mid-publish → HIGH `fitcheck_reveal_cancelled_deleted` audit with `via:publish_404`) OR `mark_reveal_publish_failed` (5xx → HIGH `fitcheck_reveal_publish_failed` audit). The 404-during-publish branch preserves a gaming-vector signal that a CAS-first design would have silently dropped.

**Per-emoji unique-reactor counting.** Reveal trigger is ≥10 unique reactors on a SINGLE emoji (not aggregated across emojis), per design §3. Bot reactions + OP self-reactions excluded. Per-(post_id, emoji, milestone) crossing state is durable via `discord_fitcheck_emoji_milestones` (Pass C migration 052) — restarts don't re-fire milestone audits. Low-age reactor (<30d Discord account) dedup is in-memory only (M1 punt — accepted at V1 scale, documented).

**Tone band is audit-only.** Reveal reply text uses display_name with NO @-ping. Tone band (`high` ≥80 / `mid` 40-79 / `low` <40) shifts warmth, not voice. The `caught:` line is included ONLY when `catch_detected` is non-null. The other 3 axis rationales live in `axis_rationales_json` on the score row but are NOT surfaced in the reveal — mods pull them from audit if needed.

**Leaderboard query contract (Pass D — future).** The `reveal_fired_at` column is the one-and-done lock for FOUR distinct trigger states: `reactions` and `thread_messages` (real reveals) plus `cancelled_deleted` and `publish_failed` (terminal failure locks). Pass D leaderboard queries MUST filter `reveal_trigger IN ('reactions','thread_messages')` to exclude failure-lock rows. Contract spelled out in `discord_fitcheck_scores.py` module docstring + plan §6.4 + §9.2.

**Prompt caching is mandatory.** `scoring_pipeline` uses Anthropic's `cache_control: ephemeral` on the rubric system block. Per the `claude-api` memory: any Claude SDK call must use prompt caching. Tests assert `cache_control` is set on the system payload (`test_scoring_pipeline.py::test_anthropic_call_uses_cache_control_on_system_block`).

**Audit log retention = forever.** Design §7.5. All `fitcheck_*` actions land in `audit_log` with structured `detail` dict + `actor="discord:bot:<bot_user_id>"`. Reaction logging is NOT per-event — only on milestones (5/8/10) and on suspicious additions (`fitcheck_low_age_reactor`). Keeps table size bounded.

---

### State Pin (branch `state-pin`, default-invisible)

**One-line:** when a mod changes any Stitzy-managed state dimension (scoring / burn_mode / relax_mode / personalize_mode) AND the new state differs from the old, the bot posts a system-voice state-summary message in the per-guild ops channel and pins it, unpinning any prior pin for the same dimension. #sable-ops's pinned-message list becomes a live dashboard of current bot state.

**Default-invisible deploy.** Until `SABLE_ROLES_OPS_CHANNELS_JSON` has an entry for a guild, `state_pin.announce_state_change` audits `fitcheck_state_pin_no_ops_channel` LOW and returns without sending or pinning. No new prod behavior on deploy; operator opts in per-guild by setting the env var + granting the bot Manage Messages on #sable-ops.

**Four characteristics tracked.** `scoring` (set via `/scoring action:set state:<off|silent|revealed>`), `burn_mode` (`/set-burn-mode`), `relax_mode` (`/relax-mode`), `personalize_mode` (`/set-personalize-mode`). Each gets a dedicated pinned message in the ops channel via `discord_state_pins` (mig 054) one-row-per-(guild_id, characteristic) with an optimistic-lock UPDATE for replace.

**Same-state no-op gate.** Each of the four call sites reads prior config BEFORE the SP-side helper write and only fires `asyncio.create_task(state_pin.announce_state_change(...))` when the new state differs. Operator pressing `/relax-mode off` when already off does NOT rotate the pin (operator confusion: "did I actually change anything?").

**Coalescing dict + per-channel async lock.** `_pending_announcements: dict[(guild_id, characteristic), Task]` cancels in-flight prior on a rapid toggle (last-write-wins). `_channel_locks: dict[channel_id, asyncio.Lock]` serializes cross-characteristic pin ops on the same channel — Discord rate-limits pin/unpin at ~5/4s per channel. Both reset via `.clear()` in the autouse `state_pin_module` test fixture.

**CancelledError discipline preserved.** Outer try in `announce_state_change` RE-RAISES `asyncio.CancelledError` so the `close()` drain's `asyncio.gather` sees clean unwind. Inner excepts catch `SQLAlchemyError` + `discord.HTTPException` + a final `Exception` sink for defense-in-depth.

**Optimistic-lock millisecond resolution.** `discord_state_pins.upsert_state_pin` uses `_now_iso_ms` (mirror of `discord_streaks._now_iso_ms`) so two writers in the same wall-clock second can't both succeed against the same expected token. Caller passes `expected_updated_at=prior["updated_at"]` from the immediately-prior `get_state_pin` call; lost race returns `False` and the caller self-deletes the just-posted pin + audits `fitcheck_state_pin_lost_race`.

**Boot-time orphan sweep + opportunistic dup-pin check.** `sweep_orphan_pins` (one-shot per process via internal `_sweep_done` guard, composed onto on_ready by `register()`) lists each ops channel's pinned messages, parses the `**stitzy state · <char>**` headline, and unpins any pin whose DB pointer doesn't match. Step d.5 inside `_do_announce` runs the same parser-driven check inside the per-channel lock so a cancelled-prior orphan doesn't sit visible until next boot. Both audit `fitcheck_state_pin_orphan_swept` with `via: restart_sweep | opportunistic_pre_post`.

**OPS_CHANNELS misconfig is non-fatal but loud.** `_filter_unique_ops_channels` rejects duplicate channel_id values across guilds (would cause cross-guild pin-stomp) and blank/whitespace/0 values, logging an error per case with the surviving guild named so the operator sees the misconfig in journalctl.

**Message body voice = system, not Stitzy.** Headline `**stitzy state · <characteristic>**` (lowercase, matches Stitzy house style without sliding into roast voice) + `state: <value>` first line + characteristic-specific config lines + `last changed: <iso-minute> by @<plain-text-name> (id: <user_id>)`. NO clickable mention (P9), NO `prompt_version` or `model_id` leak (P10), `allowed_mentions=AllowedMentions.none()` (defense against display-name mention-injection).

**Six-place migration contract for 054.** Per AGENTS.md: SQL + Alembic + connection.py + migrate_pg.py (`TABLE_LOAD_ORDER` + `SEQUENCE_TABLES`) + schema.py + version-literal bumps in `tests/db/test_migrations.py`, `tests/db/test_connection.py`, `tests/cli/test_init.py`, `docs/CLI_REFERENCE.md`. Mig 054 follows the existing Alembic-`BigInteger` / schema.py-`Integer` precedent (mirroring migs 050+052 line-for-line — pre-existing drift accepted scope-out per plan PR4-H2).

**Plan + QA refs.** Design plan: `~/Projects/SolStitch/internal/state_pin_plan.md` (rev 7, APPROVED after 6 adversarial rounds). Implementation QA log: `~/Projects/SolStitch/internal/state_pin_qa_log.md` (3 adversarial rounds, APPROVED).

---

---

### Community Content Duel (Phase 5, `features/content_duel.py` — LIVE on SolStitch 2026-07-02)

**One-line:** operator-triggered `/duel` (NAMED starters via `SABLE_ROLES_DUEL_STARTERS_JSON` — Arf/P0ison/Monasex by user id, key-presence semantics: explicit `[]` = locked, absent key = MOD_ROLES fallback) posts two pending Content-Deck candidates into the client channel; members vote 🅰/🅱 (one vote each); votes are preference data for Sable's content engine, QUARANTINED from the operator Elo; `/tasteboard` shows the community leaderboard.

**THE DISCLOSURE GATE (fail-closed — the consent boundary):** every command re-checks `orgs.config_json.pairwise_disclosure_signed` AT INVOCATION via `get_org_config_value` (defensive import — an old SablePlatform fails the gate closed, never boot-crashes the bot). Non-empty NON-SENTINEL string required (`"false"`/`"no"`/`"revoked"`/`"off"`/`"0"`… refuse — `_REVOKED_SENTINELS`); any error refuses. Member-facing disclosure rides every duel embed footer. Registration is NOT authorization: `/duel`+`/tasteboard` are GLOBAL-tree commands fanned onto GUILD_TO_ORG guilds (the OPPOSITE posture from the content_deck Phase-0 spike, which stays test-only + untouched) — an unsigned org gets a polite refusal.

**Vote integrity:** the repo's FIRST non-author-locked View. One vote per member enforced twice — a SYNCHRONOUS pre-mark in the View dict BEFORE any await (discord.py dispatches each click as its own task; the pre-mark closes the double-click race the durable guard can't see mid-transaction; rolled back on write failure) + the durable `has_recent_duel_vote` DB guard (survives restarts). Blind count-only tally while open (no herding); A/B split reveals at close. HARD 10-min wall-clock deadline (`_DUEL_OPEN_SECONDS` + a monotonic `_deadline`; `View.timeout` alone is a refreshable INACTIVITY timer — after each vote it is shrunk to the remaining wall-clock). Vote DB work runs via `asyncio.to_thread` (the gateway event loop never stalls on Postgres). One open duel per org (`_OPEN_DUELS`, in-process — single-process constraint; cleared `.clear()`-style in tests). The duel posts as a REGULAR bot channel message (interaction webhook tokens expire at 15 min — an interaction-owned message could not be edited at close). Strict public-render whitelist: ONLY `payload.text` / `[format] captions` ever reach the channel — an unrecognized payload renders "" and the candidate is dropped (guardrail_hits / internal fields never post). `AllowedMentions.none()` on every send/edit.

**The quarantine (SP side):** votes land as `content_deck_decisions` rows (`actor_kind='community'`, `surface='discord'`, `decision='keep'` + `pair_loser_id`) and fold into `community:`-prefixed `content_quality` rows at BOTH grains — no operator-Elo consumer (deck ranking, meme template loop, text format tilt, keep-rate, hard-negatives) reads prefixed keys. Promotion past the quarantine is gated on the masterplan §11 K-tests, a deliberate future change.

**Known limits (documented, accepted for v1):** views are non-persistent — a bot restart orphans an open duel's buttons ("interaction failed"; the vote ledger survives, the tally never reveals); the org lock is in-process; candidate images aren't embedded (pending candidates have no R2 ref — needs a render endpoint). Activation for a NEW org = bot in guild + GUILD_TO_ORG entry + `sable-platform org config set <org> pairwise_disclosure_signed "<date + who + authority>"`. TIG is prepped but NOT active (needs the guild invite + client confirmation). Record: `~/sable-workspace/CONTENT_DECK_PHASE5_SHIPPED.md`. Tests: `tests/test_content_duel.py` (20).

---

## Working conventions

- **Small patches over rewrites.** Don't refactor `fitcheck_streak.py` cosmetically — it was audited byte-for-byte against the build plan across 5 chunks.
- **Tests use `pytest-asyncio` in `asyncio_mode=auto`.** Don't add explicit `@pytest.mark.asyncio` decorators — `pyproject.toml` sets the mode globally.
- **`conftest.py` fixture `fitcheck_module` patches the three module-level dicts** (`FITCHECK_CHANNELS`, `_FITCHECK_CHANNEL_IDS`, `_CHANNEL_TO_GUILD`, `_pending_recomputes`) per test. Any new module-level state needs to be added there or tests will leak state across runs.
- **Module-level dicts shared cross-feature MUST be reset via `.clear()`, NOT rebound.** `burn_me._burn_invoke_cooldown` is imported by reference into `roast.py`; rebinding it (`monkeypatch.setattr(bm, "_burn_invoke_cooldown", {})`) silently severs the identity that `roast.py` sees, so a cross-feature test would stop sharing the cooldown. Use the autouse `.clear()` pattern in `tests/test_roast_peer_path.py` as the template.
- **Don't change `_format_streak` output without updating the SableWeb / future-API consumer expectations** — the angle-bracket embed suppression on the best-fit URL is load-bearing.
- **DB writes go through SablePlatform helpers, not raw SQL.** `discord_streaks.upsert_streak_event` / `update_reaction_score` / `get_event` / `compute_streak_state` are the only surface. Match the SableTracking pattern of strict layering.
- **Audit-log every enforcement action.** `actor="discord:bot:<bot_user_id>"`, `source="sable-roles"`, `org_id=<resolved>`, `entity_id=None`, structured `detail` dict.
- **Run both test suites** before declaring any change green: `cd ~/Projects/sable-roles && .venv/bin/pytest tests/` AND `cd ~/Projects/SablePlatform && .venv/bin/pytest tests/db/test_discord_streaks.py tests/db/test_schema.py`. Schema parity tests will catch any `discord_streak_events` `Table()` drift vs migration 043. For Scored Mode V2 changes also include `tests/db/test_discord_fitcheck_scores.py tests/db/test_discord_scoring_config.py tests/db/test_discord_fitcheck_reveal.py tests/db/test_migrations.py` on the SP side and `tests/test_image_hashing.py tests/test_delete_monitor.py tests/test_scoring_pipeline.py tests/test_scoring_state_machine.py tests/test_reveal_pipeline.py` on this side.
- **Migrations touch six places.** Any new SP migration needs: SQL file, Alembic revision (chained), `connection.py._MIGRATIONS` tuple append, `migrate_pg.py.TABLE_LOAD_ORDER` + `SEQUENCE_TABLES`, `schema.py` `Table(...)` block (bare-imports style — `Table, Column, Integer, Text, func, text` NOT `sa.X`), plus version-literal bumps in `tests/db/test_migrations.py`, `tests/db/test_connection.py`, `tests/cli/test_init.py`, `docs/CLI_REFERENCE.md`. Schema parity tests will fail loudly if any of the six are missed. SQL files: NO `;` inside `--` comments (the runner splits literally — see `feedback_sableplatform_migration_sql` memory).
- **No new repo dependencies without justification.** Current deps: `discord.py>=2.7`, `python-dotenv`, `anthropic>=0.40`, `imagehash>=4.3`, `Pillow>=10.0`, SablePlatform (editable), `pytest`, `pytest-asyncio`. Bot-feature work should be doable with just these.

---

## What's built and working

- `SableRolesClient(discord.Client)` with `setup_hook` (per-guild instant `/streak` sync), `on_ready` (24h-empty warning), `close()` (debounce drain)
- `on_message`: image-only enforcement in configured `#fitcheck` channels — upsert streak event → 🔥 reaction → auto-thread `<display_name> · <YYYY-MM-DD>` (UTC date, 100-char truncation)
- Text-only / GIF-picker / emoji-only deletion + rotating DM (4-line bank) + 5-min per-user cooldown + audit row
- `on_raw_reaction_add` / `on_raw_reaction_remove` → 2s debounced per-post recompute → optimistic-locked write, stale-write logged + dropped
- `/streak` ephemeral slash command: current/longest/total + today's reactions + jump-link to most-reacted-ever fit (angle-bracketed to suppress embed)
- 76 tests passing (`tests/test_image_detection.py`, `tests/test_dm_bank.py`, `tests/test_dm_cooldown.py`, `tests/test_reaction_recompute.py`, `tests/test_debounce_race.py`, `tests/test_handler_resilience.py`, `tests/test_unconfigured_guild.py`, `tests/test_format_streak.py`). Plus 19 SablePlatform tests at `~/Projects/SablePlatform/tests/db/test_discord_streaks.py`.
- Live in SolStitch (guild `1501026101730869290`, `#fitcheck` channel `1501073373252292709`) since 2026-05-13.

**Scored Mode V2 (Pass A+B+C — on branch `scored-mode-pass-ab`, default `state='off'`, ships invisible):**

- `features/image_hashing.py` — pHash compute on every counted fit + 90d collision detection. Emits `fitcheck_image_phash_recorded` / `fitcheck_image_phash_failed` (INFO) / `fitcheck_repost_detected` (LOW) / `fitcheck_image_theft_detected` (HIGH). Runs regardless of scoring state.
- `features/delete_monitor.py` — `on_raw_message_delete` severity classifier (LOW / MEDIUM / CRITICAL per design §7.2) + `on_raw_message_edit` text-edit audit (lengths only, never content). REPLACE binder. Runs regardless of scoring state.
- `features/scoring_pipeline.py` — Sonnet 4.6 vision call, temp=0, mandatory prompt caching on the rubric system block, structured-JSON output validation, retry-once-then-fail (streak credit preserved). State-gated on `silent`/`revealed`. `/scoring status | set <off|silent|revealed>` slash command with danger-style Confirm/Cancel view, Manage-Guild gated.
- `features/reveal_pipeline.py` — debounced per-post recompute, per-emoji unique-reactor counts, milestone audits (5/8/10 reactions), low-age reactor audit (<30d account), CAS-locked reveal-fire with tone-banded plain-text reply (no @-ping), 404-during-publish → cancelled_deleted HIGH audit, 5xx → publish_failed HIGH audit. Gated on `state='revealed'` AND `posted_at >= state_changed_at` (silent-period posts never reveal).
- `prompts/scoring_system.py` — rubric_v1 system prompt (Cohesion · Execution · Concept · Catch axes, Bollin-calibrated).
- Default state `'off'` triple-guarded (DEFAULT + helper + pipeline gate). Pass A audit/detection rows still land on `off` state — only Pass B (scoring) + Pass C (reveal) bail.
- Branch state: 4 commits on sable-roles (`b57463b` scaffold → `010c002` tests+QA → `d5295c7` Pass C → `221f8eb` §8.3 strict), 2 commits on SablePlatform (`fb8dc8f` migs 049-051 → `eecbb90` mig 052). PRs sieggyby/SolStitchFitCheck#1 + sieggyby/SablePlatform#1 OPEN.
- Test suites: sable-roles 512 passed (`+88` scored-mode); SablePlatform 1529 passed / 3 skipped (`+115` scored-mode).
- NOT yet flipped on any live guild — `/scoring set silent` must be run manually after deploy per design §10 phasing.

---

## What's not built yet

1. **VPS deployment** — runs on Sieggy's local machine via `python -m sable_roles.main`. Target: Hetzner VPS within 24-48h of go-live (per build plan §6). See `OPERATIONS_RUNBOOK.md` §6.
2. **`tree.sync` try/except in `setup_hook`** (`main.py:47`) — hardening pass before any second-guild onboarding. Currently a Forbidden on one guild crashes the whole bot.
3. **Operator allowlist for `#fitcheck` enforcement** — Brian (admin) gets deleted same as anyone. Config-driven user_id list to bypass delete+DM.
4. **`@influenza` rotation feature** — same bot host. Monthly top-N yappers via SableTracking listener data → role grant/revoke. Memory: `project_solstitch_influenza`.
5. **Backfill admin CLI** — V1 starts streaks at gateway-connect; no history import. Defer until asked.
6. **Health/status surfacing** — V1 logs to stdout only. `#sable-ops` health-ping deliberately removed (plan round-3 audit — bot has no channel overwrite). When deployed: pull stdout from journalctl/compose logs; consider a `/sable-roles-status` slash command or a SablePlatform alert on `discord_streak_events.created_at` staleness.
7. **Tier-weighted reactions, public leaderboard, freeze policy, thread-reply scoring, squads, streak-tier roles, AI-gen detection** — all deferred to V2 per plan §8.

**Scored Mode V2 deferred (post-Pass-C):**

8. **Pass D — `/leaderboard`** — two boards (Top Revealed Fits + Best Per User), revealed-only, ephemeral default with `public:true` opt-in, per-guild, rate-limited 1/user/min. Gated on ≥10 revealed fits + ≥2 weeks in Revealed mode + Brian sign-off (design §10.2 / §10.6). The query contract `reveal_trigger IN ('reactions','thread_messages')` is already documented (see `discord_fitcheck_scores.py` module docstring) so Pass D can lift it directly.
9. **`/scoring config` mod command** — edit thresholds + model in DB without redeploy. V1 hardcodes plan §6.3 defaults.
10. **`/scoring suspicious` mod-review surface** — show HIGH/CRITICAL audit rows from last 30d with jump-links. Premature UX until real suspect rows exist.
11. **Cross-guild image hash collision** — current pHash collision query is scoped to `org_id`. Multi-guild SolStitch needs cross-guild query.
12. **Web reverse-image-search per fit** — augment pHash with public web search at score time.
13. **Configurable axis weights via config table** — V1 hardcodes equal weights. Lever is currently re-prompting only.
14. **Reference-corpus RAG augmentation for Catch axis** — curated Raf/Helmut/Margiela/Issey/anime visual library. Months of work.
15. **Alt-cluster reaction analysis query** — uses V1-captured reactor account-age data to surface "all 10 reactors joined Discord within the same 2-week window" patterns.
16. **Auto-invalidate on Nth repost.**
17. **Cross-guild SolStitch leaderboard** — when SolStitch goes multi-server.
18. **Durable low-age-reactor dedup** — currently in-memory bounded dict (M1 punt from Pass C QA). Restart re-audits.
19. **Last-touch LRU on `_pending_reveals`** — currently insertion-order eviction at cap 1024 (L-NEW-1 punt from Pass C QA). Matters only under sustained burst near cap.

See `~/Projects/SolStitch/internal/fitcheck_build_TODO.md` for chunk-level minor follow-ups (cosmetic + non-blocking). See `~/Projects/SolStitch/internal/scored_mode_pass_ab_qa_log.md` for Pass A+B+C QA history + ready-for-deploy checklist.

---

## Secrets & credentials

**GitHub status:** not pushed to a remote yet — repo is local-only at time of writing. `.env` is gitignored. No secrets in source.

**What's in `.env` (live credentials on local disk):**
- `SABLE_ROLES_DISCORD_TOKEN` — Discord bot token for the `Sable Roles` app (application_id `1504314425581244548`). Resets via developer portal Bot → Reset Token if leaked.
- `SABLE_ROLES_FITCHECK_CHANNELS_JSON` — JSON: `{"<guild_id>": {"org_id": "<sable_org>", "channel_id": "<fitcheck_channel>"}}`. Live SolStitch entry: `{"1501026101730869290":{"org_id":"solstitch","channel_id":"1501073373252292709"}}`.
- `SABLE_ROLES_GUILD_TO_ORG_JSON` — JSON: `{"<guild_id>": "<org_id>"}`. Live SolStitch: `{"1501026101730869290":"solstitch"}`.
- `SABLE_ROLES_HEALTH_CHANNELS_JSON` — Reserved, currently `{}`. V1 health is stdout-only.
- `SABLE_ROLES_OPS_CHANNELS_JSON` — JSON: `{"<guild_id>": "<ops_channel_id>"}` for the State Pin surface. Empty `{}` (the deploy default) makes `state_pin.announce_state_change` a no-op + LOW audit per guild. Operator sets per-guild entry + grants the bot **Manage Messages** on the named channel to enable the pinned-state-dashboard. Distinct from `HEALTH_CHANNELS_JSON` by design (state_pin plan P14 — semantic drift cheaper than reusing a "health"-named var for the operational-state-dashboard purpose).

**Scored Mode V2 env vars (Pass A+B+C — all optional with sensible defaults):**

- `ANTHROPIC_API_KEY` — **required** for scoring + burn_me + roast. Same key shared across all Anthropic-calling features. SolStitch-dedicated key planned per design §11.1 but not yet split.
- `SABLE_ROLES_SCORED_MODE_ENABLED` — hard kill switch (default `true`). Set `false` to disable Pass A pHash, Pass B scoring, AND Pass C reveal-fire entirely without flipping per-guild state.
- `SABLE_ROLES_SCORING_MODEL` — default `claude-sonnet-4-6`. Future Haiku/Opus branch lives in `_compute_cost_per_million` (currently single Sonnet rate).
- `SABLE_ROLES_SCORING_PROMPT_VERSION` — default `rubric_v1`. Stored on every score row for partitioning. Bump on rubric revisions.
- `SABLE_ROLES_PHASH_COLLISION_DISTANCE` — default `8`. Hamming-distance threshold for pHash collision.
- `SABLE_ROLES_PHASH_COLLISION_WINDOW_DAYS` — default `90`. Lookback window for collision check.
- `SABLE_ROLES_SCORING_RETRY_DELAY_SECONDS` — default `5.0`. Delay before retry-once on transient Anthropic errors.

**Hardcoded (not sensitive):** `DM_BANK`, `DM_COOLDOWN_SECONDS=300`, `CONFIRMATION_EMOJI="🔥"`, `DEBOUNCE_SECONDS=2.0`, `REVEAL_DEBOUNCE_SECONDS=5.0`, `IMAGE_EXT_ALLOWLIST`, `_PENDING_REVEALS_CAP=1024` — all in `sable_roles/config.py` or feature modules. Change those by editing config and restarting.

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

**Scored Mode V2:**
- `image_hashing.compute_phash_and_check_collisions(image_bytes, ctx)` — Pass A entry point; runs regardless of scoring state
- `delete_monitor.register(client)` — REPLACE binder for `on_raw_message_delete` + `on_raw_message_edit`
- `scoring_pipeline.maybe_score_fit(message, ctx)` — Pass B entry point; state-gated, no-op when `state='off'`
- `scoring_pipeline._ScoringSetConfirmView` — danger-style Confirm/Cancel `ui.View` for `/scoring set`
- `scoring_pipeline.register_commands(tree)` — registers `/scoring` (mod-only, Manage Guild + in-handler defense-in-depth)
- `reveal_pipeline.register(client)` — composes `on_raw_reaction_add/remove` + `on_message` + `on_raw_message_delete`; registered LAST in `setup_hook`
- `reveal_pipeline._recompute_after_delay(post_id, channel_id, guild_id, org_id)` — 5s debounce body with self-identity-guarded pop
- `reveal_pipeline.close()` — drains `_pending_reveals` tasks; called from `SableRolesClient.close()` before `super().close()`
- `reveal_pipeline.TRIGGER_REACTIONS` / `TRIGGER_THREAD_MESSAGES` / `TRIGGER_PENDING` / `SUCCESS_TRIGGERS` — reveal-trigger string constants (NIT-N3 fix)
- `reveal_pipeline._build_reveal_text(score_row, display_name)` — pure formatter; tone band by percentile
- `reveal_pipeline._PENDING_REVEALS_CAP` — module constant (1024) gating eviction in `_pending_reveals` dict

**State Pin:**
- `state_pin.announce_state_change(client, *, guild_id, org_id, characteristic, new_state_summary, changed_by_user_id)` — fire-and-forget entry called from the four slash-command handlers via `asyncio.create_task`. Coalesces in-flight prior, holds per-channel lock, posts + pins + upserts under optimistic lock + cleans up on lost race
- `state_pin.sweep_orphan_pins(client)` — idempotent one-shot boot-time orphan-pin cleanup; internal `_sweep_done` guard makes any re-entrant call a no-op
- `state_pin.register(client)` — wires the sweep onto on_ready; safe-binds `_client`; the only module-level discord.py event hook (sweep is slash-command-triggered, not gateway-event-triggered)
- `state_pin.close()` — drains `_pending_announcements` tasks; called from `SableRolesClient.close()` BEFORE `super().close()` (mirrors `fitcheck_streak.close` + `reveal_pipeline.close` precedents)
- `state_pin._format_body(client, characteristic, summary, user_id)` — async body formatter; awaits `leaderboard._resolve_display_name`. Single source of truth for the headline shape consumed by `_extract_characteristic_from_headline`
- `state_pin._STATE_HEADLINE_PREFIX` / `_STATE_HEADLINE_SUFFIX` — module constants paired across formatter + sweep parser via `removesuffix(_STATE_HEADLINE_SUFFIX)`
- `state_pin._KNOWN_CHARACTERISTICS` — frozenset whitelist; entry-side defense in `announce_state_change` raises `ValueError` synchronously on call-site typo (PR6-L2)
- `state_pin._filter_unique_ops_channels()` — boot-time OPS_CHANNELS_JSON validator; rejects duplicate channel_ids across guilds + blank/whitespace/0 values with operator-readable error logs
- `state_pin._PENDING_ANNOUNCEMENTS_CAP` — module constant (256) gating eviction in `_pending_announcements` dict (post-insert + `>` predicate, mirrors `leaderboard._evict_cooldown_if_full`)

---

## Active plans / decisions in progress

*(Add entries here when a plan is agreed but not yet implemented)*

- ~~**Item 1 — VPS deploy.**~~ DONE 2026-05-16 (per `project_stitzy_vps_deployed`). Hetzner host runs the V2 stack via docker compose; SablePlatform Postgres lives on the same host.
- **Item 2 — `setup_hook` try/except hardening.** Wrap `tree.sync(guild=...)` in `try/except discord.HTTPException` per SableTracking `bot.py:31-34` precedent. One bad guild_id should log + skip, not crash the whole process. Trivially a one-block edit.
- **Item 3 — Operator allowlist.** Add `SABLE_ROLES_FITCHECK_ALLOWLIST_JSON` env var (shape: `{"<guild_id>": ["<user_id>", ...]}`). On image-less message in fit-check channel, check allowlist first — if member, skip delete+DM but still audit-log `allowlist_skipped` for traceability. ~10 LOC.
- **Item 4 — Scored Mode V2 Phase 0 → Phase 1 (Off → Silent).** Pass A+B+C shipped on branch `scored-mode-pass-ab`. Gate to flip: merge both PRs, deploy to VPS, smoke test per `scored_mode_pass_ab_qa_log.md` runbook on Sieggy's test guild, then `/scoring set silent` on a live guild. Default deploy = `off`, no behavior change.
- **Item 5 — Scored Mode V2 Phase 1 → Phase 2 (Silent → Revealed).** Gated on ≥20 scored fits + ≥7d silent data + ≥5 active posters in #fitcheck during silent + Sieggy spot-check ≥10 + vision API failure rate <5% + Brian sign-off on sample reveals. Pure ops decision, no code change required.
- **Item 6 — Scored Mode V2 Phase 2 → Phase 3 (Pass D leaderboard).** Build + ship Pass D once ≥10 revealed fits exist + ≥2 weeks Revealed mode + Brian sign-off on opening competitive surface. Build plan exists at design §9 + §10.2.

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
    image_hashing.py         — Scored Mode Pass A: pHash compute + 90d collision detection.
                               compute_phash_and_check_collisions(image_bytes, ctx) runs from
                               fitcheck_streak's image branch regardless of scoring state. Emits
                               fitcheck_image_phash_recorded / _failed (INFO),
                               fitcheck_repost_detected (LOW, same user),
                               fitcheck_image_theft_detected (HIGH, different user).
    delete_monitor.py        — Scored Mode Pass A: on_raw_message_delete severity classifier
                               (LOW / MEDIUM / CRITICAL per design §7.2) + on_raw_message_edit
                               text-edit audit (lengths only). REPLACE binder — future binders MUST
                               compose via roast.py:register pattern (docstring is honest).
    scoring_pipeline.py      — Scored Mode Pass B: Sonnet 4.6 vision call, temp=0, mandatory
                               prompt caching on rubric system block, structured-JSON validation,
                               retry-once-then-fail. State-gated on silent|revealed (no-op on off).
                               maybe_score_fit(message, ctx) is the entry point.
                               /scoring status | set <off|silent|revealed> slash command +
                               _ScoringSetConfirmView (danger Confirm, author-lock, on_timeout +
                               try/except around set_state per Pass C deferred polish).
    state_pin.py             — State-pin surface: per-guild ops-channel pinned-state dashboard.
                               announce_state_change (slash-command tail; fire-and-forget),
                               sweep_orphan_pins (one-shot boot cleanup via _sweep_done guard,
                               composed onto on_ready), per-channel lock + coalescing dict +
                               optimistic-lock upsert + opportunistic dup-pin sweep at step d.5.
                               Default-invisible: SABLE_ROLES_OPS_CHANNELS_JSON empty → no-op +
                               LOW audit. Four characteristics (scoring / burn_mode / relax_mode
                               / personalize_mode) each get one pin via discord_state_pins (mig
                               054). close() drain wired into SableRolesClient.close().
    reveal_pipeline.py       — Scored Mode Pass C: debounced per-post recompute (5s, mirrors V1
                               fitcheck_streak debounce). on_raw_reaction_add/remove + on_message
                               (thread filter) + on_raw_message_delete COMPOSE wrappers. Per-emoji
                               unique-reactor counts (bot + OP filtered). Milestone audits 5/8/10
                               (durable via discord_fitcheck_emoji_milestones). Low-age reactor
                               audit (<30d account, in-memory dedup). CAS-locked reveal-fire with
                               'pending' placeholder, AllowedMentions.none(), 404→cancelled_deleted
                               HIGH, 5xx→publish_failed HIGH. State='revealed' AND posted_at >=
                               state_changed_at gate. _PENDING_REVEALS_CAP=1024. close() drain
                               wired into SableRolesClient.close().
  prompts/
    burn_me_system.py        — locked roast voice + safety rails (B5)
    vibe_infer_system.py     — R11: strict-JSON vibe inference prompt (5 fields, imperative denylist)
    scoring_system.py        — Scored Mode rubric_v1 system prompt (Cohesion · Execution · Concept
                               · Catch axes, Bollin-calibrated). Cached as the system block on
                               every Sonnet call.
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
  test_image_hashing.py                  — Scored Mode Pass A: pHash compute, Hamming, collision (16)
  test_delete_monitor.py                 — Scored Mode Pass A: severity matrix + edit audit (13)
  test_scoring_pipeline.py               — Scored Mode Pass B: state gate, retry, ON CONFLICT,
                                           cache_control, _ScoringSetConfirmView, on_timeout,
                                           Confirm-callback DB-error graceful path (17 = 14 + 3)
  test_scoring_state_machine.py          — Scored Mode Pass B: off↔silent↔revealed transitions (5)
  test_reveal_pipeline.py                — Scored Mode Pass C: build text, tone band, schedule
                                           replace, handler gates, recompute paths, one-and-done
                                           lock, invalidated bail, 404→cancelled_deleted, 5xx→
                                           publish_failed, AllowedMentions.none, mid-recompute
                                           state-flip race, milestone dedup, pending-reveals cap,
                                           register-binds-by-reference, §8.3 strict gate (38 = 35 + 3)
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
- `~/Projects/SablePlatform/sable_platform/db/discord_streaks.py` — streak helpers + list_active_streak_users (R8) + set_phash_on_streak_event + list_recent_phashes_for_collision (Scored Mode Pass A)
- `~/Projects/SablePlatform/sable_platform/db/discord_burn.py` — opt-in + daily-cap helpers (B5)
- `~/Projects/SablePlatform/sable_platform/db/discord_guild_config.py` — relax/burn/personalize mode (R3)
- `~/Projects/SablePlatform/sable_platform/db/discord_roast.py` — blocklist + token economy + flags +
  aggregate_peer_roast_report + last_consumed_token (R6) + find_peer_roast_for_bot_reply (R7) (R1+R6+R7)
- `~/Projects/SablePlatform/sable_platform/db/discord_user_vibes.py` — message-observations,
  rollups, vibe upsert + validation, purge (R1) + list_recent_observation_users (R10)
- `~/Projects/SablePlatform/sable_platform/db/discord_airlock.py` — A1: invite snapshot diff,
  team-inviter allowlist, member admit ledger with airlock state machine
- `~/Projects/SablePlatform/sable_platform/db/discord_fitcheck_scores.py` — Scored Mode Pass B+C:
  upsert_score_success / record_score_failure / get_score / count_pool_size /
  fetch_curve_pool_raw_totals / mark_reveal_fired (CAS) / update_reveal_post_id (guarded swap) /
  mark_reveal_publish_failed / convert_pending_to_cancelled_deleted / mark_reveal_cancelled_deleted /
  record_emoji_milestone_crossing / list_emoji_milestone_crossings_for_post / invalidate_score.
  Module docstring contains the leaderboard query contract (trigger-IN filter).
- `~/Projects/SablePlatform/sable_platform/db/discord_scoring_config.py` — Scored Mode Pass B:
  get_config (defaults state='off'), set_state (validates off|silent|revealed; audit inside),
  count_status_breakdown.
- `~/Projects/SablePlatform/sable_platform/db/migrations/043_discord_streak_events.sql`
- `~/Projects/SablePlatform/sable_platform/db/migrations/045_relax_mode_persist.sql` (B3)
- `~/Projects/SablePlatform/sable_platform/db/migrations/046_burn_optins_random_log.sql` (B5/R0)
- `~/Projects/SablePlatform/sable_platform/db/migrations/047_roast_personalization.sql` (R1) — 6 new tables + alter
- `~/Projects/SablePlatform/sable_platform/db/migrations/048_airlock.sql` (A1) — 3 tables for airlock
- `~/Projects/SablePlatform/sable_platform/db/migrations/049_discord_streak_events_phash.sql` — Scored Mode Pass A: ALTER discord_streak_events ADD COLUMN image_phash + idx_org_phash
- `~/Projects/SablePlatform/sable_platform/db/migrations/050_discord_fitcheck_scores.sql` — Scored Mode Pass B: per-fit scoring row (success/failed)
- `~/Projects/SablePlatform/sable_platform/db/migrations/051_discord_scoring_config.sql` — Scored Mode Pass B: per-guild state machine; default state='off'
- `~/Projects/SablePlatform/sable_platform/db/migrations/052_discord_fitcheck_emoji_milestones.sql` — Scored Mode Pass C: per-(post, emoji, milestone) crossing state for durable dedup
- `~/Projects/SablePlatform/sable_platform/db/migrations/054_discord_state_pins.sql` — State Pin: one row per (guild_id, characteristic) tracking the currently-pinned "stitzy state" message id in #sable-ops. Optimistic-lock UPDATE via discord_state_pins.upsert_state_pin
- `~/Projects/SablePlatform/sable_platform/db/discord_state_pins.py` — State Pin helper: get_state_pin + upsert_state_pin (millisecond-resolution optimistic-lock token mirroring discord_streaks._now_iso_ms)
- `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md` — fitcheck V1 plan
- `~/Projects/SolStitch/internal/fitcheck_build_TODO.md` — fitcheck V1 audit history
- `~/Projects/SolStitch/internal/burn_me_v1_build_plan.md` — burn-me V1 plan
- `~/Projects/SolStitch/internal/burn_me_build_TODO.md` — burn-me V1 audit history
- `~/Projects/SolStitch/internal/roast_v1_v2_personalization_plan.md` — /roast plan (R0-R13)
- `~/Projects/SolStitch/internal/roast_build_TODO.md` — /roast audit history
- `~/Projects/SolStitch/internal/ship_dms.md` — Live-ship runbook (Brian + Cahit DMs)
- `~/Projects/SolStitch/internal/fitcheck_scored_mode_plan.md` — Scored Mode V2 canonical design (Pass A+B+C+D)
- `~/Projects/SolStitch/internal/scored_mode_pass_ab_qa_log.md` — Pass A+B+C adversarial QA history + deploy runbook
