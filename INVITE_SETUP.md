# Sable Roles — Discord App Setup

Step-by-step instructions to create the `Sable Roles` Discord application, generate a bot token, enable the right intents, and build the invite URL. Source of truth: `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md` §1.

**Time:** ~5 minutes. **Prereq:** logged into a Discord account that will own the app.

---

## 1. Create the application

1. Open https://discord.com/developers/applications
2. Click **New Application** (top right).
3. Name: **`Sable Roles`** (exact, including space + capitalization).
4. Tick the developer ToS box. Click **Create**.

You're now on the app's **General Information** page. Leave the default icon for now (can swap later).

---

## 2. Create the bot user + grab the token

1. Left sidebar → **Bot**.
2. The page shows a Bot user that was auto-created with the app (current Discord behavior — no separate "Add Bot" click needed).
3. Under **Username**, set: **`sable-roles`** (lowercase, hyphenated). Click **Save Changes**.
4. Under **Token**, click **Reset Token** → confirm. Discord shows the new token **once**. Copy it immediately.
5. **Paste it into `~/Projects/sable-roles/.env`** as the `SABLE_ROLES_DISCORD_TOKEN` value:

   ```bash
   # ~/Projects/sable-roles/.env
   SABLE_ROLES_DISCORD_TOKEN=<paste-token-here>
   ```

   If `.env` doesn't exist yet:

   ```bash
   cp ~/Projects/sable-roles/.env.example ~/Projects/sable-roles/.env
   # then edit and paste the token
   ```

6. **Do NOT commit the token.** `.gitignore` already excludes `.env`, but double-check before any `git add`.

If you ever leak the token: come back to this page and **Reset Token** again — invalidates the old one.

---

## 3. Enable the right intents

Still on the **Bot** page, scroll down to **Privileged Gateway Intents**.

| Intent | Setting | Why |
|---|---|---|
| **Presence Intent** | OFF | Not needed. |
| **Server Members Intent** | OFF | Plan §1 round-2 audit removed `members` intent — V1 doesn't use member cache. |
| **Message Content Intent** | **ON** | **Required** — bot reads attachments + text in `#fitcheck` to enforce image-only rule. Without this, gateway connection fails with close code `4014`. |

Click **Save Changes** at the bottom.

**Sanity check:** the green toggle should be ON only for **Message Content Intent**. Members + Presence stay off.

---

## 4. Build the invite URL

You can either use the OAuth2 URL Generator in the portal, or paste the URL below directly (faster). Both produce the same result.

### Option A — manual URL (recommended)

1. Left sidebar → **General Information**. Copy the **Application ID** (under the app name, an 18–19 digit number).
2. Substitute the application ID into this URL:

   ```
   https://discord.com/api/oauth2/authorize?client_id=<APPLICATION_ID>&permissions=311385205824&scope=bot+applications.commands
   ```

   That's the **complete invite URL.** Save it at the bottom of this file under the "Invite URL" header (see template below).

### Option B — OAuth2 URL Generator (portal-driven)

1. Left sidebar → **OAuth2** → **URL Generator**.
2. **Scopes:** tick **`bot`** AND **`applications.commands`**. Both are required — `bot` alone won't let `/streak` register.
3. **Bot Permissions** (tick exactly these 8):
   - **View Channel**
   - **Send Messages**
   - **Send Messages in Threads**
   - **Create Public Threads**
   - **Manage Messages**
   - **Read Message History**
   - **Add Reactions**
   - **Use Application Commands**
4. The generated URL appears at the bottom — should end with `permissions=311385205824&scope=bot+applications.commands`. If the permission integer differs, you ticked the wrong boxes; recheck against the table above.
5. Copy the URL, save it under "Invite URL" below.

### Why these permissions

| Permission | Used for |
|---|---|
| View Channel | See messages in `#fitcheck` |
| Send Messages | DM rotating-bank line on text-only deletes (DMs need this scope at the bot level) |
| Send Messages in Threads | Future thread-reply features (V2); harmless to grant now |
| Create Public Threads | Auto-create thread under every counted fit-post |
| Manage Messages | **Delete** text-only / GIF-picker / emoji-only messages in `#fitcheck` |
| Read Message History | Reaction recompute fetches messages by ID via `fetch_message` |
| Add Reactions | Bot's 🔥 confirmation reaction on counted fits |
| Use Application Commands | `/streak` slash command |

`MANAGE_GUILD`, `MENTION_EVERYONE`, `MANAGE_ROLES`, `KICK_MEMBERS` etc. are **NOT** granted — V1 enforces only at the message level.

---

## 5. Test guild install (before SolStitch)

Per build plan §5: smoke-test in your own test guild **before** inviting to live SolStitch.

1. Open the invite URL in a browser logged into your Discord account.
2. Pick a test guild (or create one — `+ → Create My Own → For me and my friends`).
3. Confirm the permissions list matches the 8 above. Click **Authorize**.
4. The bot now appears in the guild's member list, offline (it'll come online when you run `python -m sable_roles.main` in chunk C8).

You can re-use the same invite URL for SolStitch later — same app ID, same permissions.

---

## 6. Pre-flight checklist

Before declaring C7 done, confirm all six:

- [ ] Application named `Sable Roles` exists at https://discord.com/developers/applications
- [ ] Bot username is `sable-roles`
- [ ] Token copied into `~/Projects/sable-roles/.env` as `SABLE_ROLES_DISCORD_TOKEN=...`
- [ ] **Message Content Intent** is ON in the developer portal
- [ ] Server Members Intent + Presence Intent are OFF
- [ ] Invite URL (with `permissions=311385205824&scope=bot+applications.commands`) is saved below

---

## Invite URL

<!-- Sieggy: paste the final invite URL on the line below. Keep the surrounding angle-brackets to suppress preview. -->

<https://discord.com/oauth2/authorize?client_id=1504314425581244548&permissions=311385205824&integration_type=0&scope=bot+applications.commands>

---

## Troubleshooting

- **`4014` close code on gateway connect** → Message Content intent not enabled, or token is for a different app. Re-check §3.
- **`/streak` doesn't appear in the slash-command picker** → `applications.commands` scope was missing from the invite. Kick the bot, re-invite with the correct URL.
- **`Forbidden` (`50013`) when bot tries to delete a text-only post** → bot's role doesn't have **Manage Messages** in `#fitcheck`. Check guild settings → Roles → `sable-roles` → Permissions → Channel Overrides for `#fitcheck`.
- **Bot doesn't react with 🔥** → Add Reactions permission missing or rate-limited. Check audit log in `sable.db` for `add_reaction failed` warnings.
- **Token leaked accidentally** → Reset Token in the portal (invalidates the old one), update `.env`, restart bot.
