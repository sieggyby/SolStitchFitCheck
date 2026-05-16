## Stitzy (sable-roles) — SolStitch Discord bot.
##
## Mirrors the SableKOL preflight sidecar build pattern: image bakes both
## SablePlatform and sable-roles at build time. Rebuild required for source
## changes (`docker compose build sable-roles`).
##
## Build context: the *parent* of sable-roles (so SablePlatform is available
## at ./SablePlatform). Run from /opt/sable on the VPS:
##
##     docker build \
##         -f sable-roles/Dockerfile \
##         -t stitzy:latest \
##         .
##
## Runtime env (set via /opt/sable-web/.env + docker-compose.override.yml):
##   SABLE_ROLES_DISCORD_TOKEN          (required) Discord bot token
##   SABLE_ROLES_FITCHECK_CHANNELS_JSON (required) per-guild fitcheck routing
##   SABLE_ROLES_GUILD_TO_ORG_JSON      (required) guild -> SP org mapping
##   SABLE_ROLES_MOD_ROLES_JSON         (required) per-guild mod role IDs
##   SABLE_ROLES_AIRLOCK_*              (required for A0-A8 airlock features)
##   SABLE_ROLES_TEAM_INVITERS_JSON     (required) airlock team-inviter seed
##   SABLE_ROLES_INNER_CIRCLE_*         (optional) burn-me inner-circle allowlists
##   SABLE_ROLES_BURN_*                 (optional) burn-me tunables (config.py defaults)
##   ANTHROPIC_API_KEY                  (required) for /burn-me + vibe inference
##   SABLE_DATABASE_URL                 (required) Postgres URL (container-reachable
##                                                 host, e.g. host.docker.internal)

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

## SablePlatform first — hard dep. The [postgres] extra brings psycopg2-binary
## which sable-roles needs because we run against the production Postgres
## (not the SQLite default). SP also brings anthropic + sqlalchemy + pydantic.
COPY SablePlatform /opt/sable-platform
RUN pip install --no-cache-dir -e "/opt/sable-platform[postgres]"

## sable-roles. Brings discord.py>=2.7 + python-dotenv. anthropic was already
## installed by SP, but sable-roles imports it directly (burn_me + vibe_observer).
COPY sable-roles /opt/sable-roles
RUN pip install --no-cache-dir -e /opt/sable-roles

## Gateway connection only — no port exposed, no HTTP healthcheck endpoint.
## discord.py rebinds gateway internally on transient disconnects; container
## liveness (compose `restart: unless-stopped`) handles hard failures.

CMD ["python", "-m", "sable_roles.main"]
