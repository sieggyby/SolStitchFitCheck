# sable-roles

Sable's dedicated Discord bot for community-role automation. V1 ships fit-check streak
tracking + image-only enforcement for a client server's `#fitcheck` channel.

**Status:** V1 live in the SolStitch Discord since 2026-05-13.

---

## Important: this repo has an external dependency

`sable-roles` does **not** own its database layer. It depends on **SablePlatform** — the
shared backbone for the Sable tool stack, which owns `sable.db`, all schema migrations,
and the DB helper modules this bot imports:

```python
from sable_platform.db import discord_streaks
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db
```

**SablePlatform is a separate, currently-private Sable repository — it is not on GitHub.**
Without it installed, this repo is **review-only**: the code reads and reasons fine, but
`import sable_roles.main` raises `ModuleNotFoundError: sable_platform` and `pytest` fails
at collection.

The complete `sable_platform` surface this bot touches — six symbols and one table — is
fully specified in **[`docs/SABLEPLATFORM_CONTRACT.md`](docs/SABLEPLATFORM_CONTRACT.md)**.
If you're reviewing the code or pointing an AI assistant at it, read that first; it's the
substitute for shipping SablePlatform.

---

## Reviewing this repo (no SablePlatform needed)

Everything you need to understand the project is in the repo:

1. **[`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md)** — project context, architecture
   diagram, and every audited design decision. Start here.
2. **[`docs/SABLEPLATFORM_CONTRACT.md`](docs/SABLEPLATFORM_CONTRACT.md)** — the external DB
   dependency, fully specified.
3. **`sable_roles/`** — the bot source (3 files, ~500 LOC).
4. **`tests/`** — 76 tests covering image detection, DM rotation/cooldown, reaction
   debounce, handler resilience, and `/streak` formatting.
5. **[`OPERATIONS_RUNBOOK.md`](OPERATIONS_RUNBOOK.md)** — live-ops: boot, monitor, restart,
   deploy, rollback, troubleshooting.

---

## Running this repo (requires SablePlatform)

```bash
cd sable-roles
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install -e <path-to-SablePlatform>   # the private dependency
cp .env.example .env
# fill in SABLE_ROLES_DISCORD_TOKEN + the JSON config blobs (see .env.example)
```

Run the bot:

```bash
.venv/bin/python -m sable_roles.main
```

Run the tests:

```bash
.venv/bin/pytest tests/
```

Without SablePlatform on the path, both commands fail at import — see the dependency
note above. `docs/SABLEPLATFORM_CONTRACT.md` §6 describes the stub option if you want
to run it standalone.

---

## Configuration

All config is environment-driven via `.env` (gitignored). See `.env.example` for the
template. Four variables:

| Variable | Purpose |
|---|---|
| `SABLE_ROLES_DISCORD_TOKEN` | Discord bot token. Privileged Message Content intent must be enabled in the developer portal. |
| `SABLE_ROLES_FITCHECK_CHANNELS_JSON` | `{"<guild_id>": {"org_id": "<org>", "channel_id": "<fitcheck_channel>"}}` — which channel to enforce, per guild. |
| `SABLE_ROLES_GUILD_TO_ORG_JSON` | `{"<guild_id>": "<org_id>"}` — guild → SablePlatform org mapping for `/streak` resolution. |
| `SABLE_ROLES_HEALTH_CHANNELS_JSON` | Reserved. Currently unused — V1 health goes to stdout. |

---

## Discord intents

`Intents.default()` + `message_content` (privileged — must be enabled in the developer
portal under Bot → Privileged Gateway Intents). The `members` intent is **not** required.
See [`INVITE_SETUP.md`](INVITE_SETUP.md) for the full developer-portal walkthrough.

## Invite scopes / permissions

OAuth2 scopes: `bot` + `applications.commands`. Required permissions:
View Channel · Send Messages · Send Messages in Threads · Create Public Threads ·
Manage Messages · Read Message History · Add Reactions · Use Application Commands.

---

## Repository map

```
sable_roles/
  main.py                    SableRolesClient (discord.Client subclass) + entrypoint
  config.py                  env-driven config + DM bank + tunables
  features/fitcheck_streak.py  on_message enforcement, reaction debounce, /streak
tests/                       76 tests (pytest + pytest-asyncio)
docs/
  SABLEPLATFORM_CONTRACT.md  the external DB dependency, fully specified
CLAUDE.md / AGENTS.md        project context for AI assistants (mirror files)
OPERATIONS_RUNBOOK.md        live-ops runbook
INVITE_SETUP.md              one-time Discord developer-portal walkthrough
SMOKE_TEST.md                10-scenario manual smoke against a test guild
.env.example                 config template (no real values)
```

---

## A note on Sable-internal references

`CLAUDE.md`, `AGENTS.md`, and `OPERATIONS_RUNBOOK.md` were written for the maintainer's
local environment and reference paths like `~/Projects/SablePlatform/...` and
`~/Projects/SolStitch/internal/...`. Those are **Sable-internal repos and documents, not
part of this GitHub repo.** The build plan and ship runbook they point to live outside
this repo by design. For the one dependency that genuinely matters to understanding the
code — SablePlatform — see `docs/SABLEPLATFORM_CONTRACT.md`, which is self-contained.
