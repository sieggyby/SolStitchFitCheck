# Sable Roles — Test-Guild Smoke Test

Manual smoke for the fit-check bot **before** inviting it to live SolStitch. Source of truth: `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md` §5 (10 scenarios). Bot identity / invite URL: `INVITE_SETUP.md` (chunk C7).

**Time:** ~30 minutes. **Prereq:** C7 complete (token in `.env`, invite URL ready, Message Content intent ON).

After every scenario, the **agent** runs the DB-verification query and confirms the row count / column values. Sieggy can paste the bot's stdout into the chat so the agent can also confirm "zero unhandled exceptions" (one of the hard pass criteria).

---

## 0. Pre-flight (one-time, ~10 min)

### 0.1 Pick a test guild

Use Sieggy's existing testing Discord server **or** spin up a fresh one:
1. Discord client → `+ → Create My Own → For me and my friends`. Name: `sable-roles smoke`.
2. Create a text channel called `#fitcheck` (lowercase). **Right-click → Copy Channel ID.** (If "Copy ID" isn't visible: User Settings → Advanced → **Developer Mode** ON, then retry.)
3. **Right-click the server icon → Copy Server ID** for the guild ID.

Record both — they go into `.env` below.

### 0.2 Install bot via invite URL

Open the invite URL from `INVITE_SETUP.md` (the angle-bracketed one under "Invite URL"). Pick the test guild, click **Authorize**. Confirm the bot now appears in the guild's member list, status **offline** (it comes online when you run the process in §0.5).

### 0.3 Configure `.env`

Edit `~/Projects/sable-roles/.env` and fill in **all three** JSON env vars. Replace `<GUILD_ID>` and `<CHANNEL_ID>` with the IDs from §0.1. Keep the test `org_id` as `smoke` so it's unambiguous in DB queries vs. real `solstitch` data.

```bash
# ~/Projects/sable-roles/.env

SABLE_ROLES_DISCORD_TOKEN=<token-from-INVITE_SETUP-§2>

# Map test-guild id → org_id. Use a distinct org_id ("smoke") so verification
# queries can filter out any prior SolStitch rows.
SABLE_ROLES_GUILD_TO_ORG_JSON={"<GUILD_ID>":"smoke"}

# Map test-guild id → fitcheck channel config. Same guild_id, same org_id.
SABLE_ROLES_FITCHECK_CHANNELS_JSON={"<GUILD_ID>":{"org_id":"smoke","channel_id":"<CHANNEL_ID>"}}
```

JSON must be single-line + valid. Sanity-check via `python-dotenv` (the same loader `config.py:18` uses — keeps the quotes intact, unlike `xargs`):

```bash
cd ~/Projects/sable-roles
.venv/bin/python -c 'from dotenv import dotenv_values; import json; v=dotenv_values(".env"); json.loads(v["SABLE_ROLES_FITCHECK_CHANNELS_JSON"]); json.loads(v["SABLE_ROLES_GUILD_TO_ORG_JSON"]); print("ok")'
```

Prints `ok` → both JSON blobs parse. Any traceback → fix the offending line in `.env` before continuing.

### 0.4 Ensure migration 043 is applied

The bot reads/writes `discord_streak_events` in `~/.sable/sable.db`. If you haven't run `sable-platform init` since C1, do it now:

```bash
cd ~/Projects/SablePlatform
.venv/bin/sable-platform init
```

Verify migration 043 ran (expect `MAX(version) >= 43` — `schema_version` accumulates rows across migrations, so take the max):

```bash
sqlite3 ~/.sable/sable.db "SELECT MAX(version) FROM schema_version;"
sqlite3 ~/.sable/sable.db ".schema discord_streak_events" | head -5
```

If `MAX(version) < 43` or the table is missing → re-run `init` and re-check before proceeding. Don't smoke-test against an unmigrated DB.

### 0.5 Boot the bot

Open a new terminal so stdout is visible the whole run:

```bash
cd ~/Projects/sable-roles
.venv/bin/python -m sable_roles.main 2>&1 | tee -a /tmp/sable-roles-smoke.log
```

Expected: within a few seconds, a log line `sable-roles connected as sable-roles#<discriminator> · fitcheck streak active`. The bot's status flips to online in the test guild.

`tee` to `/tmp/sable-roles-smoke.log` lets the agent grep stdout for unhandled exceptions at the end.

If you see `4014` close code → Message Content intent isn't enabled in the dev portal (`INVITE_SETUP.md` §3). Stop, fix, restart.

### 0.6 Establish a clean baseline

Wipe any prior smoke runs so verification counts start at zero:

```bash
sqlite3 ~/.sable/sable.db "DELETE FROM discord_streak_events WHERE org_id = 'smoke';"
sqlite3 ~/.sable/sable.db "DELETE FROM audit_log WHERE org_id = 'smoke' AND source = 'sable-roles';"
```

(Safe: scoped to `org_id='smoke'`, won't touch live SolStitch data.)

Confirm both return 0:

```bash
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM discord_streak_events WHERE org_id='smoke';"
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM audit_log WHERE org_id='smoke' AND source='sable-roles';"
```

---

## Conventions for the 10 scenarios

- **`<POST_ID>`** = right-click the message in Discord → **Copy Message ID**. Sieggy paste-supplies this to the agent after each scenario.
- **DB queries** run against `~/.sable/sable.db` (override only via `SABLE_DB_PATH`). The agent runs them; Sieggy doesn't need to.
- **`/streak`** is **ephemeral**: only the runner sees it. Sieggy reports the rendered text back to the agent.
- **Stdout**: any `ERROR` or uncaught traceback in `/tmp/sable-roles-smoke.log` fails the run; transient `add_reaction failed` / `create_thread failed` `WARNING`s are not blockers on their own but the agent investigates them.
- **Bot reactions / self-reactions**: scenarios 6–8 share a single image post. Don't post a new fit between them — the test depends on the same `post_id` accumulating events.

---

## Scenario 1 — Post a fit image

**Sieggy action:** in `#fitcheck`, attach any image (PNG/JPG/etc.) + send. Wait ~2 seconds. Then run `/streak`.

**Expected Discord:**
- Bot reacts with 🔥 within ~1s.
- A thread spawns under the post titled `<your display name> · <YYYY-MM-DD UTC>` (truncated to 100 chars).
- `/streak` returns the ephemeral embed with `current: 1 day(s)`, `longest: 1 day(s)`, `total fits: 1`, `today: posted · 0 reaction(s)`, `best fit ever: <jump-link> · 0 reaction(s)`.

**Sieggy reports:** the `<POST_ID>` of the fit + the rendered `/streak` text.

**Agent DB check:**

```bash
# Exactly one row, image_attachment_count >= 1, reaction_score still 0 (no human reactions yet)
sqlite3 ~/.sable/sable.db "SELECT post_id, user_id, counted_for_day, attachment_count, image_attachment_count, reaction_score, counts_for_streak, invalidated_at FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID>';"
# No audit row for this post (only text-deletes get audited)
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM audit_log WHERE org_id='smoke' AND source='sable-roles' AND detail_json LIKE '%<POST_ID>%';"
```

**Pass:** row exists, `image_attachment_count >= 1`, `counts_for_streak = 1`, `invalidated_at` NULL, audit count = 0.

---

## Scenario 2 — Post text-only in `#fitcheck`

**Sieggy action:** type `hello` (or any plain text, no attachments) into `#fitcheck` and send. Watch.

**Expected Discord:**
- Message disappears within ~1s.
- A DM arrives from `sable-roles` with one of the four lines from plan §0 DM bank (rotating; today's may not match tomorrow's).

**Sieggy reports:** the `<POST_ID>` (copy before it's deleted — easiest: right-click → Copy ID immediately after send) and which DM line landed.

**Agent DB check:**

```bash
# No streak event for a text post
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID>';"
# One audit row, action=fitcheck_text_message_deleted, dm_success=true, dm_suppressed_for_cooldown=false
sqlite3 ~/.sable/sable.db "SELECT action, actor, json_extract(detail_json,'$.dm_success'), json_extract(detail_json,'$.dm_suppressed_for_cooldown'), json_extract(detail_json,'$.post_id') FROM audit_log WHERE org_id='smoke' AND source='sable-roles' AND json_extract(detail_json,'$.post_id')='<POST_ID>';"
```

**Pass:** streak count = 0, audit row has `action='fitcheck_text_message_deleted'`, `actor` starts with `discord:bot:`, `dm_success=1`, `dm_suppressed_for_cooldown=0`.

> **Prereq for §3:** the DM must actually arrive in §2. The 5-min cooldown gate at `fitcheck_streak.py:179` only sets `_dm_cooldown[user_id] = now` inside the **success** branch. If your Discord privacy settings block server-DMs (`Forbidden` error), §3's "still inside cooldown" prediction will be wrong (you'll get a second DM instead of suppression). If `dm_success` is `0` here: User Settings → Privacy & Safety → enable "Allow direct messages from server members" **for the test guild**, then retry §2 from a fresh post before continuing to §3.

---

## Scenario 3 — Post a GIF-picker GIF (embed, no attachment)

**Sieggy action:** in `#fitcheck`, click the GIF picker (the `GIF` button on the message box), pick any GIF, send.

**Expected Discord:** identical to scenario 2 — message deleted, DM received. (GIF picker produces an embed, not an attachment; plan §0 treats this as text-only.)

**DM cooldown caveat:** scenario 2 just consumed Sieggy's 5-minute DM window. Plan §4 says we still **delete + audit** even when DM is suppressed — `dm_suppressed_for_cooldown=true` lands in the audit detail. So the expected DM here is **0 messages**, and the audit row reflects suppression.

**Sieggy reports:** `<POST_ID>` and whether a DM arrived (expected: no).

**Agent DB check:**

```bash
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID>';"
sqlite3 ~/.sable/sable.db "SELECT action, json_extract(detail_json,'$.dm_success'), json_extract(detail_json,'$.dm_suppressed_for_cooldown') FROM audit_log WHERE org_id='smoke' AND source='sable-roles' AND json_extract(detail_json,'$.post_id')='<POST_ID>';"
```

**Pass:** streak count = 0, audit row exists with `dm_success=0`, `dm_suppressed_for_cooldown=1`.

(If `dm_success=1` here, the cooldown gate is broken — BLOCKER, investigate before continuing.)

---

## Scenario 4 — Post an emoji-only message

**Sieggy action:** in `#fitcheck`, send a message containing only an emoji (e.g. `🔥` or `:fire:`). No attachment, no GIF.

**Expected Discord:** message deleted; no DM (still inside the 5-min cooldown from scenarios 2–3).

**Sieggy reports:** `<POST_ID>`.

**Agent DB check:**

```bash
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID>';"
sqlite3 ~/.sable/sable.db "SELECT action, json_extract(detail_json,'$.dm_suppressed_for_cooldown') FROM audit_log WHERE org_id='smoke' AND source='sable-roles' AND json_extract(detail_json,'$.post_id')='<POST_ID>';"
```

**Pass:** streak count = 0, audit row `action='fitcheck_text_message_deleted'`, `dm_suppressed_for_cooldown=1` (still inside cooldown).

---

## Scenario 5 — Post inside the auto-thread under scenario 1's fit

**Sieggy action:** open the thread the bot created under scenario 1's image post. Send a text message inside the thread (e.g. `nice fit`). Optionally also send a GIF-picker GIF — both should be allowed.

**Expected Discord:**
- Both messages remain. No deletion.
- No DM.
- No 🔥 reaction (thread messages aren't fits).

**Sieggy reports:** `<POST_ID>` of the thread message and confirmation it's still present.

**Agent DB check:**

```bash
# Thread message should not create a streak event
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID>';"
# Thread message should not create an audit row
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM audit_log WHERE org_id='smoke' AND source='sable-roles' AND json_extract(detail_json,'$.post_id')='<POST_ID>';"
```

**Pass:** both counts = 0.

---

## Scenario 6 — React to scenario 1's image post (human, non-self)

**Setup:** scenarios 6–8 share **scenario 1's image post**. Don't post a new fit. Keep `<POST_ID_1>` (the post_id from §1) handy.

**Sieggy action:** ask another human in the test guild (or a second Discord account Sieggy controls) to react with any emoji to scenario 1's image post. Wait **>2 seconds** (debounce window) before the DB check.

**Expected Discord:** the reaction appears; no bot response beyond the existing 🔥.

**Sieggy reports:** confirmation the reaction landed + how many seconds elapsed before the DB check (must be > 2s).

**Agent DB check:**

```bash
# reaction_score should be 1 (one non-bot, non-self reaction)
sqlite3 ~/.sable/sable.db "SELECT reaction_score, updated_at FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID_1>';"
# Re-run /streak (Sieggy): today line should show "posted · 1 reaction(s)"
```

**Pass:** `reaction_score = 1`. If still 0 after >5s, check `/tmp/sable-roles-smoke.log` for `reaction recompute failed` or `lost race` — agent must inspect before declaring pass.

---

## Scenario 7 — Self-react on own fit (filter check)

**Sieggy action:** as the **author** of scenario 1's post, react to your own post with any emoji. Wait >2 seconds.

**Expected Discord:** reaction appears.

**Agent DB check:**

```bash
sqlite3 ~/.sable/sable.db "SELECT reaction_score FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID_1>';"
```

**Pass:** `reaction_score` **unchanged from scenario 6** (still 1). Self-reactions are filtered in `_recompute_after_delay` (plan §4) — Sieggy's react must not increment.

(If it incremented to 2 → BLOCKER, the `user.id == author_id` filter is broken.)

---

## Scenario 8 — Bot reacts (filter check)

**Sieggy action:** there's nothing to do — the bot already 🔥-reacted in scenario 1. Confirm the existing 🔥 from sable-roles is still on the post.

**Agent DB check (no new action, just verify steady-state):**

```bash
sqlite3 ~/.sable/sable.db "SELECT reaction_score FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID_1>';"
```

**Pass:** `reaction_score = 1` (only the human reactor from §6 counted; the bot's own 🔥 was always filtered via `user.id in bot_ids`).

**Stronger check (optional but recommended):** Sieggy removes-then-re-adds the bot's 🔥 (right-click → reactions → remove bot, then re-react). Wait >2s. `reaction_score` must still equal 1.

---

## Scenario 9 — Restart the bot mid-run (no double-counts)

**Sieggy action:**
1. In the terminal running the bot: `Ctrl+C`. Wait for `super().close()` drain + clean exit.
2. Snapshot the streak row immediately:
   ```bash
   sqlite3 ~/.sable/sable.db "SELECT post_id, reaction_score, attachment_count, image_attachment_count, updated_at FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID_1>';"
   ```
   Keep this output.
3. Re-launch the bot:
   ```bash
   cd ~/Projects/sable-roles && .venv/bin/python -m sable_roles.main 2>&1 | tee -a /tmp/sable-roles-smoke.log
   ```
   Wait for the `sable-roles connected as ...` log line.
4. Post a **second** image fit in `#fitcheck`. Capture `<POST_ID_2>`.

**Expected Discord:**
- Clean exit, no `Task was destroyed but it is pending!` warnings (plan §5 reaction debounce close() drain).
- On restart: bot online, reacts 🔥 + creates thread for the new fit.

**Agent DB check:**

```bash
# Scenario 1's row is unchanged by the restart (no ON CONFLICT clobber).
sqlite3 ~/.sable/sable.db "SELECT post_id, reaction_score, attachment_count, image_attachment_count FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID_1>';"

# Scenario 9's new fit creates a fresh row.
sqlite3 ~/.sable/sable.db "SELECT post_id, user_id, counted_for_day, attachment_count, image_attachment_count FROM discord_streak_events WHERE org_id='smoke' AND post_id='<POST_ID_2>';"

# Total fit rows for the smoke org: exactly 2 (scenario 1 + scenario 9).
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM discord_streak_events WHERE org_id='smoke';"
```

**Pass:** row from §1 untouched (`reaction_score`, `attachment_count`, `image_attachment_count` match the pre-restart snapshot byte-for-byte). New row for `<POST_ID_2>` present. Total row count = 2.

(Same-day double-post → `/streak` still shows `current: 1 day(s)`, `total fits: 2` — the streak ticks once per day even though the fit count is 2. Sieggy reports the rendered `/streak` text.)

---

## Scenario 10 — Two messages arrive ~simultaneously

**Sieggy action:** in two browser tabs / clients / accounts, prep two image posts in `#fitcheck`. Send them as close to simultaneously as possible (sub-second). Capture both `<POST_ID_A>` and `<POST_ID_B>`.

(If only one Discord account is available: open the same channel in two Discord clients — e.g. desktop + browser — and send back-to-back as fast as possible. Acceptable approximation.)

**Expected Discord:** both messages get 🔥 + thread. No crash, no missed reaction, no missed thread.

**Agent DB check:**

```bash
sqlite3 ~/.sable/sable.db "SELECT post_id, user_id, counted_for_day, image_attachment_count, reaction_score FROM discord_streak_events WHERE org_id='smoke' AND post_id IN ('<POST_ID_A>', '<POST_ID_B>') ORDER BY post_id;"
```

**Pass:** two rows, both with `image_attachment_count >= 1`, `counted_for_day` populated. No SQL UNIQUE collisions in `/tmp/sable-roles-smoke.log`.

---

## Final gate

Before marking C8 done, the agent runs **all three** end-of-run checks:

```bash
# 1. No unhandled exceptions or stale-write log lines in bot stdout.
grep -nE "Traceback|ERROR|Task was destroyed but it is pending|lost race" /tmp/sable-roles-smoke.log || echo "CLEAN"

# 2. Total row tally matches the scenarios (fits = §1 + §9 + §10 = 4; thread post in §5 = 0 rows).
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM discord_streak_events WHERE org_id='smoke';"

# 3a. Text-deletion audit rows: expect exactly 3 (§2 + §3 + §4).
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM audit_log WHERE org_id='smoke' AND source='sable-roles' AND action='fitcheck_text_message_deleted';"

# 3b. Thread-create-failure audit rows: expect exactly 0 (thread creation worked across the run).
sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM audit_log WHERE org_id='smoke' AND source='sable-roles' AND action='fitcheck_thread_create_failed';"
```

**Pass criteria for C8:**
- Grep returns `CLEAN` (no Tracebacks, no `Task was destroyed but it is pending!`, no `ERROR` lines, no `lost race` info lines).
- Streak event count = 4 (scenarios 1, 9, plus 2 from §10).
- Text-deletion audit count = 3 (one each for §2, §3, §4).
- Thread-create-failure audit count = 0.
- Sieggy reported every scenario's Discord-side outcome matching "Expected Discord:" above.

If any of the three end-of-run checks fail: **don't mark C8 done.** Capture the failing query output and the relevant log slice, and decide whether it's a bot bug (re-open the relevant earlier chunk) or a smoke-rig misconfig (rerun the affected scenarios).

---

## Cleanup (after pass)

Keep the test guild around — useful for regression smokes before each VPS deploy. But wipe the smoke rows out of `sable.db` so they don't pollute future `/streak` outputs in case you re-use the same org_id:

```bash
sqlite3 ~/.sable/sable.db "DELETE FROM discord_streak_events WHERE org_id='smoke';"
sqlite3 ~/.sable/sable.db "DELETE FROM audit_log WHERE org_id='smoke' AND source='sable-roles';"
```

Stop the bot (`Ctrl+C`). Don't kick it from the test guild — you'll want it there for the C9 dress rehearsal.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot status stays offline + `4014` close code in stdout | Message Content intent off in dev portal | `INVITE_SETUP.md` §3, then restart. |
| Image post → no row in `discord_streak_events` | Guild/channel IDs in `.env` don't match where you posted | Re-verify §0.3 IDs; restart bot after edit. |
| `/streak` returns "not configured for this server." | `SABLE_ROLES_GUILD_TO_ORG_JSON` missing this guild | Add mapping in `.env`, restart bot. |
| Text post → not deleted | Bot lacks **Manage Messages** in `#fitcheck` | Guild → Roles → `sable-roles` → ensure Manage Messages granted (or via channel override). |
| Reaction → `reaction_score` stays 0 after >5s | Reaction debounce / recompute broken; check `/tmp/sable-roles-smoke.log` for `reaction recompute failed` | C5 regression — re-open chunk. |
| `sqlite3 ~/.sable/sable.db ".schema discord_streak_events"` empty | Migration 043 didn't run | §0.4 — re-run `sable-platform init`. |
| Stdout shows `Task was destroyed but it is pending!` after `Ctrl+C` | `close()` drain regression | C5 regression — re-open chunk. |
