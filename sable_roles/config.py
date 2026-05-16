"""Env-driven config for sable-roles. Reads from .env via python-dotenv.

Per plan §4 Config: required vars are documented as `os.environ[...]` reads.
Skeleton uses `.get()` with safe defaults so module-import never crashes when
.env is missing or incomplete (chunks C2-C6 build the bot before C7 sets the
token). Runtime validation lives in `main.py` — `client.run()` errors on empty
token.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

SABLE_ROLES_DISCORD_TOKEN: str = os.environ.get("SABLE_ROLES_DISCORD_TOKEN", "")
FITCHECK_CHANNELS: dict = json.loads(
    os.environ.get("SABLE_ROLES_FITCHECK_CHANNELS_JSON", "{}")
)
GUILD_TO_ORG: dict = json.loads(
    os.environ.get("SABLE_ROLES_GUILD_TO_ORG_JSON", "{}")
)
HEALTH_CHANNELS: dict = json.loads(
    os.environ.get("SABLE_ROLES_HEALTH_CHANNELS_JSON", "{}")
)
# Per-guild list of role IDs whose holders count as "mods" for mod-only slash
# commands like /relax-mode and (V2) /set-burn-mode + /burn-me @user. Shape:
#   {"<guild_id>": ["<role_id>", ...]}
# Roles are role IDs (strings). Discord's built-in Administrator permission
# does NOT auto-grant mod status here — Brian (@Atelier admin) is only a mod
# if his role is explicitly in this list. Decoupled by design.
MOD_ROLES: dict = json.loads(
    os.environ.get("SABLE_ROLES_MOD_ROLES_JSON", "{}")
)

# --- Burn-me feature config (V2) ---

# Anthropic API key for /burn-me LLM calls. The anthropic SDK auto-reads
# ANTHROPIC_API_KEY from the environment; we re-expose it here so a missing
# key is visible at config-import time rather than only on first roast.
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# Model id. Sonnet 4.6 default per locked design. Override for cost experimentation.
BURN_MODEL: str = os.environ.get("SABLE_ROLES_BURN_MODEL", "claude-sonnet-4-6")

# Random-bypass probability per inner-circle image post (0.025 = 2.5%).
BURN_RANDOM_PROB: float = float(
    os.environ.get("SABLE_ROLES_BURN_RANDOM_PROB", "0.025")
)

# Per-target cooldown for random roasts (no double-random within N days).
BURN_RANDOM_DEDUP_DAYS: int = int(
    os.environ.get("SABLE_ROLES_BURN_RANDOM_DEDUP_DAYS", "7")
)

# Per-user cooldown on /burn-me invocations (prevent spam).
BURN_INVOKE_COOLDOWN_SECONDS: int = int(
    os.environ.get("SABLE_ROLES_BURN_INVOKE_COOLDOWN_SECONDS", "30")
)

# Per-user daily cap on total roasts (opt-in + random both count).
BURN_DAILY_CAP_PER_USER: int = int(
    os.environ.get("SABLE_ROLES_BURN_DAILY_CAP_PER_USER", "20")
)

# Inner-circle: roles per guild. Members holding any of these roles are
# eligible for random-bypass roasts. Shape: {"<guild_id>": ["<role_id>", ...]}
INNER_CIRCLE_ROLES: dict = json.loads(
    os.environ.get("SABLE_ROLES_INNER_CIRCLE_ROLES_JSON", "{}")
)

# Inner-circle: explicit user IDs per guild (union with role-based).
# Shape: {"<guild_id>": ["<user_id>", ...]}
INNER_CIRCLE_USERS: dict = json.loads(
    os.environ.get("SABLE_ROLES_INNER_CIRCLE_USERS_JSON", "{}")
)

# --- /roast V2 personalization (mig 047) ---

# Peer-roast eligibility: per-guild role-ID allowlist whose holders can
# invoke peer /roast. Shape: {"<guild_id>": ["<role_id>", ...]}
PEER_ROAST_ROLES: dict = json.loads(
    os.environ.get("SABLE_ROLES_PEER_ROAST_ROLES_JSON", "{}")
)

# /set-personalize-mode admin allowlist (user IDs, per guild). Not
# role-based — lets a single operator (e.g. Arf) flip personalization
# without granting every mod the keys.
# Shape: {"<guild_id>": ["<user_id>", ...]}
PERSONALIZE_ADMINS: dict = json.loads(
    os.environ.get("SABLE_ROLES_PERSONALIZE_ADMINS_JSON", "{}")
)

# Vibe observation: per-guild channel-ID allowlist the observer pulls
# messages from. Empty/missing list for a guild = all text channels the
# bot has read access to. Shape: {"<guild_id>": ["<channel_id>", ...]}
OBSERVATION_CHANNELS: dict = json.loads(
    os.environ.get("SABLE_ROLES_OBSERVATION_CHANNELS_JSON", "{}")
)

# Weekly cadence for the vibe inference cron.
VIBE_INFERENCE_INTERVAL_DAYS: int = int(
    os.environ.get("SABLE_ROLES_VIBE_INFERENCE_INTERVAL_DAYS", "7")
)

# Model id for vibe-summarization calls. Sonnet 4.6 default.
VIBE_INFERENCE_MODEL: str = os.environ.get(
    "SABLE_ROLES_VIBE_INFERENCE_MODEL", "claude-sonnet-4-6"
)

# Rolling window the vibe inference looks back over per refresh.
VIBE_OBSERVATION_WINDOW_DAYS: int = int(
    os.environ.get("SABLE_ROLES_VIBE_OBSERVATION_WINDOW_DAYS", "30")
)

# Hard env kill switch for the entire vibe pipeline (raw observation +
# rollup + inference). Distinct from the per-guild /set-personalize-mode
# toggle — this short-circuits BEFORE any DB read so a single deploy can
# stop all data collection across guilds.
VIBE_OBSERVATION_ENABLED: bool = (
    os.environ.get("SABLE_ROLES_VIBE_OBSERVATION_ENABLED", "true").lower() == "true"
)

# --- airlock (mig 048) ---

# Role assigned to new joiners whose invite isn't team-attributed.
# Shape: {"<guild_id>": "<airlock_role_id>"}
AIRLOCK_ROLES: dict = json.loads(
    os.environ.get("SABLE_ROLES_AIRLOCK_ROLES_JSON", "{}")
)

# Role granted on /admit + auto-admit (team-invite path). Empty for a
# guild → /admit just removes the airlock role; user falls through to
# @everyone-level access.
# Shape: {"<guild_id>": "<member_role_id>"}
AIRLOCK_DEFAULT_MEMBER_ROLES: dict = json.loads(
    os.environ.get("SABLE_ROLES_AIRLOCK_DEFAULT_MEMBER_ROLES_JSON", "{}")
)

# Channel where the bot posts airlock-hold pings for mods to triage.
# Distinct from the user-facing waiting room (#outside in SolStitch);
# this one is mod-only.
# Shape: {"<guild_id>": "<channel_id>"}
AIRLOCK_MOD_CHANNELS: dict = json.loads(
    os.environ.get("SABLE_ROLES_AIRLOCK_MOD_CHANNELS_JSON", "{}")
)

# Roles whose holders can run airlock triage slash commands
# (/admit, /ban, /kick, /airlock-status). Two-tier perm model:
# this list = team + community-mod; /add-team-inviter + /list-team-inviters
# stay gated on the existing MOD_ROLES (team only).
# Shape: {"<guild_id>": ["<role_id>", ...]}
AIRLOCK_TRIAGE_ROLES: dict = json.loads(
    os.environ.get("SABLE_ROLES_AIRLOCK_TRIAGE_ROLES_JSON", "{}")
)

# Bootstrap allowlist of Sable-team Discord user-IDs whose invites
# auto-admit. Persisted to discord_team_inviters on bot boot (UPSERT,
# idempotent). Runtime mgmt via /add-team-inviter + /list-team-inviters.
# Shape: {"<guild_id>": ["<user_id>", ...]}
TEAM_INVITERS_BOOTSTRAP: dict = json.loads(
    os.environ.get("SABLE_ROLES_TEAM_INVITERS_JSON", "{}")
)

# Hard kill switch for the entire airlock pipeline. When False,
# on_member_join is a no-op (no DM, no role assignment, no mod ping).
# Use for emergency offload when airlock is misbehaving.
AIRLOCK_ENABLED: bool = (
    os.environ.get("SABLE_ROLES_AIRLOCK_ENABLED", "true").lower() == "true"
)

DM_BANK: list[str] = [
    "images do the talking in here — yours got returned to sender. drop a fit or hop into a thread.",
    "woah sailor, that doesn't go there. pop off a fit in that thread or can it, but no text in the main feed.",
    "caught you posting words in the picture room. fits in front, words in the thread. cheers.",
    "text in `#fitcheck` is contraband. fits up top, talk in the thread.",
]
DM_COOLDOWN_SECONDS: int = 300
CONFIRMATION_EMOJI: str = "🔥"
DEBOUNCE_SECONDS: float = 2.0
IMAGE_EXT_ALLOWLIST: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".heic", ".heif", ".avif", ".bmp",
}
