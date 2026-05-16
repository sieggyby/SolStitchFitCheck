# SMOKE_TEST_AIRLOCK.md

Live smoke matrix for the airlock feature. Walk this before announcing
or relying on the feature in any client server. Each scenario has a step
+ an expected DB state. SQL via `sqlite3 ~/.sable/sable.db` (local) or
`psql` (VPS).

**Prerequisites (per `airlock_plan.md` A0):**
- schema_version=48
- Server Members Intent ON in Discord developer portal
- Stitzy has Manage Roles + Ban Members + Kick Members in SolStitch + role positioned ABOVE @Outsider
- `@Outsider`, `@Insider`, `#outside`, `#triage` created manually with correct overrides
- `.env` has 6 airlock vars populated (AIRLOCK_ROLES, AIRLOCK_DEFAULT_MEMBER_ROLES, AIRLOCK_MOD_CHANNELS, AIRLOCK_TRIAGE_ROLES, TEAM_INVITERS_BOOTSTRAP, AIRLOCK_ENABLED)

## Pre-flight (operator)

```bash
sqlite3 ~/.sable/sable.db "SELECT MAX(version) FROM schema_version"
# → 48

sqlite3 ~/.sable/sable.db ".tables" \
  | grep -oE "discord_(invite_snapshot|team_inviters|member_admit)"
# all 3 present

cd ~/Projects/sable-roles && .venv/bin/python -c "
from sable_roles.config import (
    AIRLOCK_ROLES, AIRLOCK_DEFAULT_MEMBER_ROLES, AIRLOCK_MOD_CHANNELS,
    AIRLOCK_TRIAGE_ROLES, TEAM_INVITERS_BOOTSTRAP, AIRLOCK_ENABLED,
)
print('AIRLOCK_ROLES:', AIRLOCK_ROLES)
print('AIRLOCK_DEFAULT_MEMBER_ROLES:', AIRLOCK_DEFAULT_MEMBER_ROLES)
print('AIRLOCK_MOD_CHANNELS:', AIRLOCK_MOD_CHANNELS)
print('AIRLOCK_TRIAGE_ROLES:', AIRLOCK_TRIAGE_ROLES)
print('TEAM_INVITERS_BOOTSTRAP:', TEAM_INVITERS_BOOTSTRAP)
print('AIRLOCK_ENABLED:', AIRLOCK_ENABLED)
"

# Confirm team-inviter env seed landed in SP after a boot
sqlite3 ~/.sable/sable.db "SELECT user_id FROM discord_team_inviters WHERE guild_id='1501026101730869290';"
# → should match TEAM_INVITERS_BOOTSTRAP
```

---

## Team-invite path (3 scenarios)

### 1. Sieggy invites a friend, friend joins → auto-admit

**Step:** Sieggy generates an invite link in any SolStitch channel + sends to a test alt account. Alt joins.

**Expect:** alt receives @Insider role automatically. No DM from Stitzy. No mod ping in #triage.

**SQL:**
```sql
SELECT airlock_status, is_team_invite, attributed_inviter_user_id
FROM discord_member_admit
WHERE user_id='<alt_id>';
-- airlock_status='auto_admitted', is_team_invite=1, attributed_inviter_user_id='402620324744790017'
```

### 2. Sparta invites someone, same flow

Same as #1 but Sparta's user-id (`209577624618401802`) as inviter. Confirms multi-team-inviter allowlist works.

### 3. Re-running grandfather audit after a restart shouldn't duplicate

**Step:** Restart Stitzy (`pkill -f sable_roles.main`, restart).
**Expect:** `bootstrap()` re-runs but `discord_team_inviters` row count for SolStitch stays at 4 (idempotent UPSERT).

```sql
SELECT COUNT(*) FROM discord_team_inviters WHERE guild_id='1501026101730869290';
-- → 4
```

---

## Non-team path (5 scenarios)

### 4. Public invite → airlock

**Step:** Create a public invite as a non-team mod (anyone not on the allowlist). Alt account joins via that link.

**Expect:**
- Alt receives @Outsider role (NOT @Insider)
- Alt receives DM from Stitzy with the "proof of aura" text
- `#triage` channel gets a ping: `🔔 airlock: <@alt> joined via invite <code> (from <@inviter>). use /admit ...`

**SQL:**
```sql
SELECT airlock_status, is_team_invite, attributed_invite_code
FROM discord_member_admit
WHERE user_id='<alt_id>';
-- airlock_status='held', is_team_invite=0, attributed_invite_code=<code>
```

### 5. Vanity URL join → airlock (if SolStitch has a vanity URL)

**Step:** Alt joins via the server's vanity URL (if configured).

**Expect:** Airlock with `attributed_invite_code=NULL` (vanity doesn't appear in `guild.invites()`).

```sql
SELECT attributed_invite_code FROM discord_member_admit WHERE user_id='<alt_id>';
-- → NULL
```

### 6. Alt DMs disabled → still airlocked, audit captures DM failure

**Step:** Alt has DMs from non-friends disabled. Joins via public invite.

**Expect:** Role granted + mod ping landed; DM failed silently.

**SQL:**
```sql
SELECT json_extract(detail_json, '$.dm_status'), json_extract(detail_json, '$.role_grant_status')
FROM audit_log
WHERE action='fitcheck_airlock_held' ORDER BY id DESC LIMIT 1;
-- → 'failed:Forbidden', 'granted'
```

### 7. Two simultaneous joins → both airlocked

**Step:** Two test accounts join within ~1s via different invites.

**Expect:** BOTH get airlocked with `attributed_invite_code=NULL` (ambiguous diff fails closed).

### 8. Restart-blackout — first joiner after restart

**Step:** Restart Stitzy. Within 5 seconds of `connected as` log line, have an alt join via a team invite.

**Expect:** Airlocked (fail-closed), because the on_ready bootstrap hasn't completed yet OR completed but the diff still can't attribute. Mod manually admits.

---

## Mod commands (5 scenarios)

### 9. Mod (community-tier) `/admit @user` works

**Step:** As a holder of the new @Mod role (NOT @Team), run `/admit @<airlocked_user>`.

**Expect:** ephemeral "admitted <@user>"; user loses @Outsider + gains @Insider; admit row transitions to `admitted`; `fitcheck_airlock_admitted` audit row.

### 10. Mod `/ban @user [reason]` works

**Step:** `/ban @<airlocked_user> reason: scam profile`

**Expect:** Discord bans the user; `fitcheck_airlock_banned` audit row with reason in detail; admit row transitions to `banned`.

```sql
SELECT decision_reason FROM discord_member_admit WHERE user_id='<banned_id>';
-- → "scam profile"
```

### 11. Mod `/kick @user [reason]` — rejoin re-airlocks

**Step:** `/kick @<airlocked_user> reason: low effort`. Then have them rejoin via a public invite.

**Expect:** First kick: admit row 'kicked'. After rejoin: SAME row overwrites to 'held' (UNIQUE constraint + ON CONFLICT DO UPDATE), fresh attribution attempted.

### 12. Non-mod `/admit` bounces silently

**Step:** Regular @Insider runs `/admit @anyone`.

**Expect:** ephemeral "you're not authorized to triage airlock." No audit row.

### 13. `/airlock-status` with no arg lists pending holds

**Step:** `/airlock-status` (no @user arg) as a mod with at least one pending hold.

**Expect:** ephemeral list of users in 'held' state with their attribution + join time.

---

## Team-only commands (2 scenarios)

### 14. Community @Mod can't `/add-team-inviter`

**Step:** As @Mod (not @Team), `/add-team-inviter @someone`.

**Expect:** ephemeral "team-only command." No `discord_team_inviters` row.

### 15. Team mod adds a new team inviter, future invites bypass

**Step:** As @Team, `/add-team-inviter @newteammate`. Then have @newteammate create an invite + a friend joins via it.

**Expect:** Friend auto-admits as team-invite.

---

## Kill switch + rollback

### Kill switch

Set `SABLE_ROLES_AIRLOCK_ENABLED=false` in `.env`, restart Stitzy. Next joiner: bot handler is a no-op (no role, no DM, no audit). User lands with default `@everyone` perms. Existing airlocked users stay airlocked — `/admit` still works.

### Migration revert (rare)

`alembic downgrade -1` on SP drops the 3 mig-048 tables. Data is LOST — pre-emptively `pg_dump --table=discord_invite_snapshot --table=discord_team_inviters --table=discord_member_admit sable` before downgrade.
