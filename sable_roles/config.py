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
