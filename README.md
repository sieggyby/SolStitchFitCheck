# sable-roles

Sable's dedicated Discord bot for community-role automation. V1 ships fit-check streak
tracking + image-only enforcement for SolStitch's `#fitcheck`.

**Status:** V1 live in SolStitch since 2026-05-13.

## Documentation

| File | When to read |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md) | Project context + architecture + design decisions. Read first if you're picking up the repo cold. |
| [`OPERATIONS_RUNBOOK.md`](OPERATIONS_RUNBOOK.md) | Live-ops: boot, monitor, restart, deploy, rollback, troubleshooting. |
| [`INVITE_SETUP.md`](INVITE_SETUP.md) | One-time Discord developer-portal walkthrough + invite URL. |
| [`SMOKE_TEST.md`](SMOKE_TEST.md) | 10-scenario manual smoke against a test guild before any new-tenant install. |
| `~/Projects/SolStitch/internal/fitcheck_v1_build_plan.md` | Source-of-truth plan — read before any architectural change. |
| `~/Projects/SolStitch/internal/fitcheck_build_TODO.md` | Chunked build TODO + audit history per chunk. |
| `~/Projects/SolStitch/internal/ship_dms.md` | Live-ship runbook (one-time): Brian + Cahit DMs, pre-flight, magic moment, rollback. |

## Setup

```bash
cd ~/Projects/sable-roles
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install -e ../SablePlatform
cp .env.example .env
# fill in SABLE_ROLES_DISCORD_TOKEN + JSON config blobs
```

## Run

```bash
.venv/bin/python -m sable_roles
```

## Test

```bash
.venv/bin/pytest tests/
```

## Discord intents

`Intents.default()` + `message_content` (privileged — must be enabled in the developer
portal under Bot → Privileged Gateway Intents). `members` intent is NOT required.

## Invite scopes / permissions

OAuth2 scopes: `bot` + `applications.commands`. Required permissions:
View Channel · Send Messages · Send Messages in Threads · Create Public Threads ·
Manage Messages · Read Message History · Add Reactions · Use Application Commands.
