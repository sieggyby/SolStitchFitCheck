# sable-roles — Operations Runbook

Live-ops doc for the SolStitch fit-check bot (and future tenants). Source of truth for boot, monitoring, restart, deployment, and rollback procedures.

**Status:** V1 running locally on Sieggy's machine since 2026-05-13. VPS deploy targeted within 24-48h.

For project context: `CLAUDE.md` / `AGENTS.md`. For the build plan: `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md`. For the live-ship checklist: `~/Projects/SolStitch/internal/ship_dms.md`.

---

## 1. Daily-ops cheat sheet

| Task | Command |
|------|---------|
| Check bot is running (local) | `pgrep -fa sable_roles.main` or `tmux ls \| grep sable-roles` |
| Tail the live log | `tail -f ~/Projects/sable-roles/.fitcheck.live.log` |
| Verify `/streak` works | Run `/streak` in any SolStitch channel — bot replies ephemerally |
| Pause enforcement (mod-only) | Run `/relax-mode mode:on` — text-only allowed in `#fitcheck`, no auto-threading. `/relax-mode mode:off` resumes. Mod role from `SABLE_ROLES_MOD_ROLES_JSON` required. |
| Recent fits across all orgs | `sqlite3 ~/.sable/sable.db "SELECT counted_for_day, org_id, user_id FROM discord_streak_events ORDER BY id DESC LIMIT 20;"` |
| Recent enforcement actions | `sqlite3 ~/.sable/sable.db "SELECT created_at, action, json_extract(detail,'$.dm_success') FROM audit_log WHERE source='sable-roles' ORDER BY id DESC LIMIT 20;"` |
| Restart bot (local tmux) | `tmux kill-session -t sable-roles && tmux new -d -s sable-roles 'cd ~/Projects/sable-roles && .venv/bin/python -m sable_roles.main 2>&1 \| tee ~/Projects/sable-roles/.fitcheck.live.log'` |
| Run test suite | `cd ~/Projects/sable-roles && .venv/bin/pytest tests/` |

---

## 2. Boot procedure (local)

### Pre-boot checks (every time)

```bash
# 1. .env parses + maps the right guild → org
cd ~/Projects/sable-roles
.venv/bin/python -c 'from dotenv import dotenv_values; import json; v=dotenv_values(".env"); j1=json.loads(v["SABLE_ROLES_FITCHECK_CHANNELS_JSON"]); j2=json.loads(v["SABLE_ROLES_GUILD_TO_ORG_JSON"]); print("env ok:", list(j1.keys()), list(j2.values()))'
# Must print env ok with the expected guild + org values.

# 2. Migration 047 applied (R0-R11 ship: burn-me + /roast peer economy + vibe layer)
sqlite3 ~/.sable/sable.db "SELECT MAX(version) FROM schema_version;"
# Must return >= 47.

# 3. No competing process
pgrep -fa sable_roles.main
# Must return empty (no stale process). If a stale process exists, see §5.
```

### Boot (foreground)

```bash
cd ~/Projects/sable-roles
.venv/bin/python -m sable_roles.main 2>&1 | tee ~/Projects/sable-roles/.fitcheck.live.log
```

### Boot (detached tmux — preferred for hands-off runs)

```bash
tmux new -d -s sable-roles 'cd ~/Projects/sable-roles && .venv/bin/python -m sable_roles.main 2>&1 | tee ~/Projects/sable-roles/.fitcheck.live.log'
tmux ls   # must show: sable-roles: 1 windows (...)
```

### What you should see in the log within ~5 seconds

```
INFO:sable_roles:sable-roles connected as sable-roles · fitcheck streak active
```

(Discriminator may be present on legacy accounts: `sable-roles#XXXX`. The `· fitcheck streak active` suffix is from `main.py:on_ready` — the literal grep target.)

### Boot-order trap (read before booting BEFORE bot is in any guild)

`setup_hook` iterates every guild_id in `SABLE_ROLES_GUILD_TO_ORG_JSON` and calls `await tree.sync(guild=discord.Object(id=...))`. **If the bot is not yet a member of that guild**, Discord returns `Forbidden 50001 Missing Access` and the bot crashes at startup (no try/except in `main.py:39-47` — tracked as SP-TODO follow-up #2).

**Resolution paths:**
- **Easiest:** install the bot in the guild *first* (via the invite URL in `INVITE_SETUP.md`), then boot. The bot is already in SolStitch as of 2026-05-13, so this only matters for a fresh tenant.
- **Alternative (if you need the bot running before install):** temporarily blank `SABLE_ROLES_GUILD_TO_ORG_JSON` to `{}` in `.env`, boot the bot, restore the JSON after install, then restart. Two restarts; only useful when you need the live process visible for diagnostics.

---

## 3. What to monitor

### Healthy steady state

| Signal | Where | What's normal |
|---|---|---|
| Process exists | `pgrep -fa sable_roles.main` | One line. Restart if zero. |
| Gateway connected | tail of `.fitcheck.live.log` | `sable-roles connected as ...` near boot, then silence until activity |
| Streak rows accumulating | `sqlite3 ~/.sable/sable.db "SELECT COUNT(*) FROM discord_streak_events WHERE created_at > datetime('now','-24 hours');"` | ≥ 1 per active tenant per day (seed cohort target: ~1 fit/day) |
| No "lost race" spam | `grep "lost race" ~/Projects/sable-roles/.fitcheck.live.log \| wc -l` | Single digits / hour — expected on burst reactions. Hundreds/hour means recompute is racing itself. |
| No `Task was destroyed` | `grep -c "Task was destroyed" ~/Projects/sable-roles/.fitcheck.live.log` | 0. If non-zero, `close()` drain isn't firing — investigate before re-deploy. |

### Red flags

| Symptom | Likely cause | First check |
|---|---|---|
| Bot offline in Discord member list | Process died | `pgrep` for the process; `tail -50` the log for traceback. |
| `4014` close code on boot | Message Content intent disabled in dev portal | `INVITE_SETUP.md` §3. |
| `50001 Missing Access` on boot | `setup_hook` syncing to a guild the bot isn't in | §2 boot-order trap above. |
| `50013 Forbidden` mid-run on delete | Bot's role lost Manage Messages on `#fitcheck` | Server settings → Roles → `sable-roles` → channel overrides. |
| Streak rows stop appearing | Image detection broken / bot disconnected | Check for new images posted; run `/streak` to confirm bot online. |
| DM cooldown dict grows unbounded | Many users, no LRU cap (known limitation) | Restart bot to clear `_dm_cooldown`. C3 follow-up (c). |
| Multiple "lost race" per second | Two bot processes running | `pgrep`; kill the older PID. |

### Periodic spot-checks (weekly)

```bash
# Enforcement actions in the past week
sqlite3 ~/.sable/sable.db <<SQL
SELECT date(created_at) AS day, action, COUNT(*) AS n
FROM audit_log
WHERE source='sable-roles' AND created_at > datetime('now','-7 days')
GROUP BY day, action ORDER BY day DESC, n DESC;
SQL

# Top streak holders (per org)
sqlite3 ~/.sable/sable.db <<SQL
SELECT org_id, user_id, COUNT(DISTINCT counted_for_day) AS days, MAX(counted_for_day) AS last_fit
FROM discord_streak_events
WHERE counts_for_streak = 1 AND invalidated_at IS NULL
GROUP BY org_id, user_id
ORDER BY days DESC LIMIT 10;
SQL

# Reaction-score distribution
sqlite3 ~/.sable/sable.db "SELECT org_id, MAX(reaction_score), AVG(reaction_score) FROM discord_streak_events GROUP BY org_id;"
```

---

## 4. Common operations

### Add a new tenant guild

1. Verify migration 043 applied on target DB (SQLite locally, Postgres on VPS): `SELECT MAX(version) FROM schema_version;` ≥ 43.
2. Get the new guild's `guild_id` and target `#fitcheck` channel ID.
3. Confirm a SablePlatform `org_id` exists for the tenant: `sable-platform org list`.
4. Edit `.env`:
   ```bash
   SABLE_ROLES_FITCHECK_CHANNELS_JSON={"<existing_guild>":{...},"<new_guild>":{"org_id":"<new_org>","channel_id":"<new_channel>"}}
   SABLE_ROLES_GUILD_TO_ORG_JSON={"<existing_guild>":"<existing_org>","<new_guild>":"<new_org>"}
   ```
5. Invite the bot to the new guild using the URL in `INVITE_SETUP.md` (same Discord app, same scopes).
6. Restart the bot per §2. `setup_hook` will sync `/streak` to the new guild on boot.
7. Smoke-test: post an image in the new `#fitcheck`, verify 🔥 + thread + `/streak` returns `1 day`.

**Note:** until SP-TODO follow-up #2 (try/except on `tree.sync`) lands, if EITHER mapped guild is bad on boot, the bot crashes. Verify both guild IDs are valid (i.e. bot is a member of both) before restart.

### Reset a specific user's streak (moderation / accidental fit)

```bash
sqlite3 ~/.sable/sable.db <<SQL
UPDATE discord_streak_events
   SET counts_for_streak = 0,
       invalidated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       invalidated_reason = 'manual: <reason>'
 WHERE org_id = '<org>' AND user_id = '<user_id>' AND counted_for_day = '<YYYY-MM-DD UTC>';
SQL
```

Row is preserved (additive design) — `counts_for_streak=0` excludes it from `compute_streak_state`. `/streak` will reflect the change on next invocation.

### Pause enforcement temporarily (e.g. event-day relaxation)

V1 has no in-bot toggle. Options:
1. Stop the bot (`tmux kill-session -t sable-roles`). DMs + deletes stop. Streak DB unchanged. Restart when ready.
2. Edit `.env` and remove the target guild from `FITCHECK_CHANNELS` (but keep in `GUILD_TO_ORG` so `/streak` still works). Restart bot. The on_message guard `_is_fitcheck_channel(channel_id)` will return False for that guild's channel, so no enforcement fires — but `/streak` will return `org_id=None` and respond "not configured for this server" because `_is_fitcheck_channel` is the gate. Net: this option breaks `/streak` too. Option 1 is cleaner unless you only want to silence ONE guild while another stays live.

Future option (when allowlist follow-up #3 lands): set the allowlist to a wildcard for that guild.

### Rotate the bot token

1. Discord developer portal → app `Sable Roles` → Bot → Reset Token.
2. Update `.env` `SABLE_ROLES_DISCORD_TOKEN=<new>`.
3. Restart bot per §2.

Old token is invalidated immediately on reset. If a token leaks (e.g. accidental commit, screenshot in DM), reset first, ask questions later.

---

## 5. Restart procedures

### Clean restart (local)

```bash
tmux kill-session -t sable-roles 2>/dev/null || pkill -f sable_roles.main
sleep 1
pgrep -fa sable_roles.main   # should return empty
tmux new -d -s sable-roles 'cd ~/Projects/sable-roles && .venv/bin/python -m sable_roles.main 2>&1 | tee ~/Projects/sable-roles/.fitcheck.live.log'
sleep 3
tail -20 ~/Projects/sable-roles/.fitcheck.live.log
# look for "sable-roles connected as ..."
```

### Force-kill (when clean stop hangs)

```bash
pkill -9 -f sable_roles.main
```

discord.py's `close()` should run on SIGTERM (sent by `tmux kill-session`), but if the debounce drain hangs (shouldn't — `asyncio.gather(..., return_exceptions=True)` won't block), SIGKILL is safe. The DB layer is crash-safe: every state-changing write commits in-flight, no in-memory queue waiting to flush.

After a force-kill, on next boot you may see `no events in last 24h` warning from `on_ready` if downtime spanned a quiet period. That's informational, not an error.

---

## 5.5 Roast feature env validation (R12)

Run before announcing /roast in any client server. Augments §2.1.

```bash
cd ~/Projects/sable-roles
.venv/bin/python -c "
from sable_roles.config import (
    PEER_ROAST_ROLES, PERSONALIZE_ADMINS, OBSERVATION_CHANNELS,
    VIBE_INFERENCE_INTERVAL_DAYS, VIBE_INFERENCE_MODEL,
    VIBE_OBSERVATION_WINDOW_DAYS, VIBE_OBSERVATION_ENABLED,
    GUILD_TO_ORG,
)
gid = next(iter(GUILD_TO_ORG))
assert PEER_ROAST_ROLES.get(gid), f'PEER_ROAST_ROLES missing for guild {gid}'
assert PERSONALIZE_ADMINS.get(gid), f'PERSONALIZE_ADMINS missing for guild {gid}'
print('roast env ok:', {
    'peer_roles': len(PEER_ROAST_ROLES[gid]),
    'admins': len(PERSONALIZE_ADMINS[gid]),
    'obs_channels': OBSERVATION_CHANNELS.get(gid, '(all readable)'),
    'inference_days': VIBE_INFERENCE_INTERVAL_DAYS,
    'window_days': VIBE_OBSERVATION_WINDOW_DAYS,
    'obs_enabled': VIBE_OBSERVATION_ENABLED,
})
"

# Schema at 47
sqlite3 ~/.sable/sable.db "SELECT MAX(version) FROM schema_version;"
# → 47

# All 6 mig 047 tables present
sqlite3 ~/.sable/sable.db ".tables" \
  | grep -oE "discord_(burn_blocklist|peer_roast_tokens|peer_roast_flags|message_observations|user_observations|user_vibes)"
# → all 6 must appear
```

If any line fails: STOP. Don't announce until env is validated; an empty
`PEER_ROAST_ROLES` mapping silently makes peer /roast inaccessible to
every member.

---

## 6. Vibe-inference cron + observation pipeline (R10/R11)

The observation pipeline runs silently as soon as the bot boots; it
captures messages + reactions across `OBSERVATION_CHANNELS` (empty
list = all readable text channels). The inference cron fires weekly
(or whatever `VIBE_INFERENCE_INTERVAL_DAYS` is set to). Both halves
respect the `VIBE_OBSERVATION_ENABLED` env kill switch and the
per-guild `personalize_mode_on` toggle.

### Healthy cron telemetry

```bash
# Observation pipeline activity in last 24h (raw message rows)
sqlite3 ~/.sable/sable.db "
SELECT COUNT(*) AS rows, COUNT(DISTINCT user_id) AS distinct_users
FROM discord_message_observations
WHERE captured_at > datetime('now', '-1 day');
"

# Inference cost over last 7 days (per org)
sqlite3 ~/.sable/sable.db "
SELECT org_id, ROUND(SUM(cost_usd), 4) AS spend, COUNT(*) AS calls
FROM cost_events
WHERE call_type = 'sable_roles_vibe_infer'
  AND created_at > datetime('now', '-7 days')
GROUP BY org_id;
"
# Plan §0.3 budget cap: ~$0.15-0.25/wk per guild. Above that means
# either inference is firing too often or a guild has 500+ active users.

# Inference rejections (validation failures, refused payloads)
sqlite3 ~/.sable/sable.db "
SELECT COUNT(*) AS refused
FROM cost_events
WHERE call_type = 'sable_roles_vibe_infer' AND call_status = 'refused'
  AND created_at > datetime('now', '-7 days');
"
# Spike in `refused` → adversarial input or model drift; investigate.
```

### Forcing a manual inference pass (debug)

```bash
cd ~/Projects/sable-roles
.venv/bin/python -c "
import asyncio
from sable_roles.features import vibe_observer
asyncio.run(vibe_observer._inference_pass())
"
```

This honors the kill switch + per-guild toggle just like the cron tick.
Useful for debugging or after a personalize-mode flip when you don't
want to wait for the next weekly cycle.

### Disabling the entire observation pipeline (emergency)

```bash
# Add to .env, then restart
SABLE_ROLES_VIBE_OBSERVATION_ENABLED=false
```

This short-circuits the listener writes, the daily rollup, the GC,
AND the inference cron. Data already in the tables is preserved.

---

## 7. Pin/announce sequence for new feature launch

Per build plan §15 migration sequence. Sequence to follow when bringing
/roast live in a new server (or re-launching after a major change):

1. Verify env (§5.5 above).
2. Pin canonical mechanic message in #fitcheck (paste contents of
   `PINNED_FITCHECK_MESSAGE.md` verbatim, then Pin Message via the
   message context menu — needs Manage Messages).
3. Walk the 28-scenario smoke matrix in `SMOKE_TEST_ROAST.md`. Anything
   red → DO NOT proceed.
4. Let observation pipeline run silently 2-3 weeks. No user-visible
   changes; vibes accumulate quietly.
5. Post #announcements: "we added /roast — see the pin in #fitcheck for
   how it works. peer-roast tokens are live; personalization comes
   online next week."
6. Admin runs `/set-personalize-mode mode:on` for the guild. Feature is
   fully live.
7. Monitor `cost_events` for the first 48h post-toggle (§6 telemetry).
8. Iterate prompt + caps in `roast_v1_v2_personalization_plan.md` based
   on observed roasts; revisit after the first month of data.

---

## 8. VPS deployment plan (PENDING — target 24-48h post-go-live)

V1 currently runs on Sieggy's local machine. The build plan §6 calls for VPS deployment within 24-48h. The Hetzner VPS already hosts SablePlatform's Docker stack — `sable-roles` should live there.

### Proposed compose addition

```yaml
# sableplatform/compose.yaml — add this service
services:
  sable-roles:
    build: ../sable-roles    # or pull from a future image registry
    restart: unless-stopped
    environment:
      SABLE_ROLES_DISCORD_TOKEN: ${SABLE_ROLES_DISCORD_TOKEN}
      SABLE_ROLES_FITCHECK_CHANNELS_JSON: ${SABLE_ROLES_FITCHECK_CHANNELS_JSON}
      SABLE_ROLES_GUILD_TO_ORG_JSON: ${SABLE_ROLES_GUILD_TO_ORG_JSON}
      SABLE_ROLES_HEALTH_CHANNELS_JSON: ${SABLE_ROLES_HEALTH_CHANNELS_JSON:-{}}
      SABLE_DATABASE_URL: ${SABLE_DATABASE_URL}    # share SP's Postgres
    depends_on:
      - postgres
```

### Pre-deploy checklist

1. Add a `Dockerfile` to `~/Projects/sable-roles/` (~5 lines: python:3.11-slim + pip install -e . + pip install -e /SablePlatform + CMD `python -m sable_roles.main`).
2. Mount SablePlatform's volume so `pip install -e ../SablePlatform` resolves (or use a multi-stage build that pip-installs from the host path).
3. Set `SABLE_ROLES_*` env vars in the VPS's compose `.env`. The Discord token is one-per-deployment — sharing the prod token across local + VPS means whichever boots last wins gateway (one Discord session per token). **Run only on VPS** after deploy; stop the local tmux process.
4. Verify migration 043 has applied to the Postgres instance (`SELECT version_num FROM alembic_version;` should show `b2da0d6b1be1`).
5. Smoke: run `/streak` in SolStitch after VPS boot. Should respond from the VPS process. Verify by tailing VPS logs (`docker compose logs -f sable-roles`).

### Post-deploy cleanup

- Stop the local tmux process: `tmux kill-session -t sable-roles`.
- Update memory `project_solstitch_fitcheck` with the VPS deploy date.
- Add `sable-roles` to whatever Hetzner monitoring SablePlatform already has (uptime check on the bot process).

---

## 9. Rollback (per plan §12)

If the bot misbehaves in production (deletes wrong messages, DMs wrong people, infinite-loops):

### Step 1: Stop the bot

```bash
# Local
tmux kill-session -t sable-roles
# VPS
docker compose stop sable-roles
```

### Step 2: (optional) Kick from the affected guild

Discord client → server settings → Members → `Sable Roles` → Kick (or Ban). Reversible — re-invite via the URL in `INVITE_SETUP.md` once fixed.

### Step 3: No DB rollback needed

`discord_streak_events` is **additive**. Bad rows are flagged in-place via `counts_for_streak=0` + `invalidated_at` + `invalidated_reason`. Use the moderation SQL in §4 to neutralize a specific row without deleting it.

### Step 4: Audit trail

Every delete + DM has a row in `audit_log` with `source='sable-roles'`. Recovery query for a wrongful delete:

```bash
sqlite3 ~/.sable/sable.db <<SQL
SELECT created_at, action, detail
FROM audit_log
WHERE source='sable-roles' AND action='fitcheck_text_message_deleted'
  AND created_at > '<YYYY-MM-DDTHH:MM:SSZ>'
ORDER BY id DESC;
SQL
```

`detail` is JSON with `post_id`, `user_id`, `dm_success`, etc. — enough to identify and DM-apologize to any wrongfully-deleted user.

---

## 10. Troubleshooting quick-ref

| Issue | Diagnosis | Fix |
|---|---|---|
| Bot won't start, `4014` in log | Message Content intent off | Dev portal → Bot → Privileged Gateway Intents → Message Content Intent ON. Restart. |
| Bot won't start, `50001` from `tree.sync` | A mapped guild isn't installed yet | Install in all mapped guilds, OR remove the bad guild from `GUILD_TO_ORG`. Restart. |
| Bot online but `/streak` missing | Slash command sync failed silently | Restart — `setup_hook` retries. If still missing, check `applications.commands` scope was in invite URL. |
| Bot deletes a legit image post | Image detection failed | Check `att.content_type` and filename ext on the deleted message (via `audit_log.detail`). If both look legit, file a bug — `is_image()` likely needs an allowlist extension. |
| `/streak` returns `current: 0` after just posting | UTC date edge case OR upsert failed | Verify row in `discord_streak_events` with today's UTC `counted_for_day`. If row exists but streak says 0, check `_compute_streak_state` (likely process clock skew). If row missing, check log for exceptions during upsert. |
| Reactions don't update score | Debounce stuck OR recompute crashed | `grep "reaction recompute" ~/Projects/sable-roles/.fitcheck.live.log` for traces. Restart bot (clears `_pending_recomputes`). |
| DM not sent on text-only delete | User blocked the bot OR `Forbidden` OR cooldown | `audit_log.detail.dm_success=false` + `dm_suppressed_for_cooldown` flag tell the story. Forbidden = user has DMs disabled or blocked bot — by design, not an error. |
| Two bots reacting | Two processes running | `pgrep -fa sable_roles.main` to find both PIDs; kill the older one. Update §2 boot procedure if this keeps happening. |

---

## 11. Airlock ops (A0-A8)

Invite-source-aware member verification. Plan source:
`~/Projects/SolStitch/internal/airlock_plan.md`. Smoke matrix:
`SMOKE_TEST_AIRLOCK.md` (28+ scenarios).

### Pre-flight (do once per server)

1. Discord developer portal → Stitzy app → Bot → enable **Server Members Intent**
2. Server settings → Roles → Stitzy → enable **Manage Roles + Ban Members + Kick Members**; drag Stitzy's role ABOVE `@Outsider` in the hierarchy
3. Create roles `@Outsider`, `@Insider` (or use existing default-member role)
4. Create channels `#outside` (visible to @Outsider + mods only, posting by @Outsider allowed), `#triage` (mod-only)
5. Populate `.env`:
   ```
   SABLE_ROLES_AIRLOCK_ROLES_JSON={"<guild_id>":"<outsider_role_id>"}
   SABLE_ROLES_AIRLOCK_DEFAULT_MEMBER_ROLES_JSON={"<guild_id>":"<insider_role_id>"}
   SABLE_ROLES_AIRLOCK_MOD_CHANNELS_JSON={"<guild_id>":"<triage_channel_id>"}
   SABLE_ROLES_AIRLOCK_TRIAGE_ROLES_JSON={"<guild_id>":["<team_role_id>","<mod_role_id>"]}
   SABLE_ROLES_TEAM_INVITERS_JSON={"<guild_id>":["<user_id>","..."]}
   SABLE_ROLES_AIRLOCK_ENABLED=true
   ```
6. Pin `PINNED_WAITING_ROOM_MESSAGE.md` content in `#outside`
7. Walk `SMOKE_TEST_AIRLOCK.md` end-to-end with an alt account

### Daily monitoring

```bash
# Pending holds RIGHT NOW
sqlite3 ~/.sable/sable.db "
SELECT user_id, joined_at, attributed_invite_code
FROM discord_member_admit
WHERE airlock_status='held'
ORDER BY joined_at ASC;
"

# Last 24h activity histogram
sqlite3 ~/.sable/sable.db "
SELECT airlock_status, COUNT(*)
FROM discord_member_admit
WHERE joined_at > datetime('now','-1 day')
GROUP BY airlock_status;
"

# Recent mod decisions
sqlite3 ~/.sable/sable.db "
SELECT timestamp, action, json_extract(detail_json,'\$.user_id'), json_extract(detail_json,'\$.reason')
FROM audit_log
WHERE source='sable-roles' AND action LIKE 'fitcheck_airlock_%'
AND timestamp > datetime('now','-1 day')
ORDER BY id DESC LIMIT 20;
"
```

### Emergency kill switch

```bash
# Edit .env
SABLE_ROLES_AIRLOCK_ENABLED=false
# Then restart
pkill -f sable_roles.main && cd ~/Projects/sable-roles && nohup .venv/bin/python -m sable_roles.main >> /tmp/sable_roles.log 2>&1 & disown
```

New joiners after the restart bypass airlock entirely. Existing
airlocked members stay airlocked — mod must `/admit` or strip
@Outsider manually via Discord UI. Re-flip `AIRLOCK_ENABLED=true` to
resume normal operation.

### Common ops

- **Force re-snapshot of invites**: `pkill -f sable_roles.main` + restart. `on_ready → airlock.bootstrap()` re-fetches `guild.invites()`.
- **Add a team-inviter at runtime**: mod (team-tier) runs `/add-team-inviter @user` in SolStitch. Persists to `discord_team_inviters`, no restart.
- **List team-inviters**: mod runs `/list-team-inviters` — ephemeral.
- **Inspect a user's airlock state**: mod runs `/airlock-status @user` — ephemeral with attribution + decision history.
- **Triage queue**: mod runs `/airlock-status` (no arg) — ephemeral list of all pending holds.

### Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| New joiners get default access (no airlock role) | Members intent not enabled in dev portal | Toggle ON, restart bot |
| `discord.Forbidden` on role assignment | Stitzy role below @Outsider in hierarchy | Drag Stitzy above @Outsider |
| All joiners airlocked (even team invites) | `TEAM_INVITERS_BOOTSTRAP` empty / wrong guild_id | Repopulate `.env`, restart |
| `/admit` not visible to mods | `AIRLOCK_TRIAGE_ROLES` empty for guild | Populate `.env`, restart |
| Mod ping never lands in #triage | `AIRLOCK_MOD_CHANNELS` wrong channel id, OR Stitzy can't view #triage | Fix env + grant View Channel + Send Messages to Stitzy on #triage |

---

## 12. References

- **Build plan:** `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md` — read before any architectural change
- **Build TODO + audit history:** `~/Projects/SolStitch/internal/fitcheck_build_TODO.md`
- **Ship runbook (one-time):** `~/Projects/SolStitch/internal/ship_dms.md`
- **Discord developer portal walkthrough:** `INVITE_SETUP.md`
- **Test-guild smoke procedure:** `SMOKE_TEST.md`
- **Project context:** `CLAUDE.md` / `AGENTS.md`
- **SablePlatform open follow-ups:** `~/Projects/SablePlatform/TODO.md` §SolStitch fit-check bot
- **Discord-streaks SP helpers:** `~/Projects/SablePlatform/sable_platform/db/discord_streaks.py`
- **Migration 043:** `~/Projects/SablePlatform/sable_platform/db/migrations/043_discord_streak_events.sql`
