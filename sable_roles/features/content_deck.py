"""Content Deck (Phase 0 spike) — a guild-scoped /content-deck swipe game.

The "Tinder of marketing" mechanic in Discord: the bot posts a content-candidate card
with Keep / Reject / Skip buttons; the invoker swipes through a seed deck in one
message; each decision is audit-logged. This is the Discord half of the Content Deck
initiative (see ~/sable-workspace/CONTENT_DECK_MASTERPLAN.md, Phase 0).

ISOLATION (masterplan round-1 SEC-1/F1 + round-2 SEC-1-GUILD-ALLOWLIST) — this is the
load-bearing safety property:
  * The command is registered **GUILD-SCOPED**, never on the global tree, so it is NEVER
    `copy_global_to`'d onto a live client guild.
  * It registers ONLY to guilds in SABLE_ROLES_CONTENT_DECK_GUILDS_JSON, and at
    registration it asserts that set is DISJOINT from the live GUILD_TO_ORG map — any
    overlap is refused (logged + skipped). So isolation is code-enforced, not discipline.
  * HARD PREREQUISITE for a tonight run: use a SEPARATE test bot token whose bot is a
    member of NO live client guild (the live token is already in SolStitch; a 2nd process
    on it would double-fire — single-process constraint). Point the test `.env` at a test
    guild that is NOT in GUILD_TO_ORG.

If SABLE_ROLES_CONTENT_DECK_GUILDS_JSON is empty (the default), nothing registers — the
feature ships invisible.
"""
from __future__ import annotations

import json
import logging

import discord
from discord import app_commands

from sable_platform.db import content_deck as cd_db
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db

from sable_roles.config import CONTENT_DECK_GUILDS, GUILD_TO_ORG

logger = logging.getLogger("sable_roles")

_VIEW_TIMEOUT_SECONDS = 180.0

# Defense-in-depth: every send/edit suppresses mentions, matching the bot's convention
# (reveal_pipeline / state_pin set AllowedMentions.none() on their sends). Phase-0 delivery
# is embed-only with a static operator seed, so there is no ping vector today — but the guard
# means a future edit that adds user-influenced `content=` can never @-ping by accident.
_NO_MENTIONS = discord.AllowedMentions.none()

# Static per-org seed (mirrors the web deck). Sample drafts that exercise the swipe
# mechanic — NOT real posted content. Real candidates arrive with the Phase-3 producers.
_SEED: dict[str, list[dict]] = {
    "tig": [
        {"ref": "tig-seed-1", "kind": "tweet", "text": "the innovation game isn't \"AI does science.\" it's a market where proof-of-work submissions compete, the best get voted into IP, and the patents get licensed. the flywheel is the point."},
        {"ref": "tig-seed-2", "kind": "tweet", "text": "every benchmark you've seen is someone's cherry-pick. TIG makes the optimization itself the contest — open, adversarial, on-chain. you don't trust the number, you watch it get beaten."},
        {"ref": "tig-seed-3", "kind": "meme", "text": "[two-panel] top: \"trust me bro, our model is SOTA\" · bottom: \"watch someone beat it live on-chain in round 122\""},
    ],
    "solstitch": [
        {"ref": "ss-seed-1", "kind": "tweet", "text": "fashion has always been a launchpad — you just couldn't own a piece of the drop. now you can. tokenized fits, RWA settlement, the fit-check IS the market."},
        {"ref": "ss-seed-2", "kind": "tweet", "text": "post your fit. the room reacts. the best fits earn. that's the whole loop and it's already live in the discord. no roadmap slide needed."},
    ],
    "robotmoney": [
        {"ref": "rm-seed-1", "kind": "tweet", "text": "the machine economy doesn't need your permission to settle. it needs rails. that's the whole thesis — boring infrastructure, enormous surface area."},
    ],
}
_FALLBACK: list[dict] = [
    {"ref": "generic-1", "kind": "tweet", "text": "this is a Content Deck seed card. Keep to bank it, Reject to drop, Skip to pass. real candidates arrive when the Phase-3 producers are wired in."},
    {"ref": "generic-2", "kind": "tweet", "text": "the mechanic: content is generated for you, you triage it fast, you keep the good ones. the Fastlane loop, ported to Sable."},
]


def _safe_test_guilds() -> dict[str, str]:
    """The test guilds this feature may register to: CONTENT_DECK_GUILDS minus any guild
    that is also in the live GUILD_TO_ORG map (refused, logged). Returns {guild_id: org_id}.
    """
    safe: dict[str, str] = {}
    for gid, org in CONTENT_DECK_GUILDS.items():
        if gid in GUILD_TO_ORG:
            logger.error(
                "content_deck: REFUSING to register on guild %s — it is a LIVE GUILD_TO_ORG "
                "guild. /content-deck is test-only; remove it from "
                "SABLE_ROLES_CONTENT_DECK_GUILDS_JSON.",
                gid,
            )
            continue
        safe[str(gid)] = str(org)
    return safe


def _static_cards(org: str) -> list[dict]:
    """The static fallback deck (id=None -> swipes go to the audit_log sink)."""
    return [{**c, "id": None} for c in _SEED.get(org, _FALLBACK)]


def _payload_text(payload_json: str) -> str:
    try:
        p = json.loads(payload_json)
        if not isinstance(p, dict):
            return payload_json
        if isinstance(p.get("text"), str):
            return p["text"]
        # meme producer payload: {template_id, format, captions:{zone:text}, remix_of?} — show the
        # FORMAT (human name) + caption text, matching SableWeb's deck card (no surface drift).
        caps_obj = p.get("captions")
        if isinstance(caps_obj, dict):
            caps = " / ".join(str(v) for v in caps_obj.values() if isinstance(v, str) and v)
            fmt = p.get("format") if isinstance(p.get("format"), str) else None
            fmt = fmt or (p.get("template_id") if isinstance(p.get("template_id"), str) else None)
            remix = f" · remix of {p['remix_of']}" if isinstance(p.get("remix_of"), str) and p["remix_of"] else ""
            return f"[{fmt}{remix}] {caps}" if fmt else (caps or payload_json)
        return payload_json
    except (ValueError, TypeError):
        return payload_json


def _load_cards(org: str, operator_handle: str) -> list[dict]:
    """The deck for `org`, WIRED to mig 076: durable PENDING content_candidates if any, else
    the static seed. A durable card carries its int `id` (swipes -> content_deck_decisions);
    a static card has id=None (swipes -> audit_log). Degrades to static if the table is absent.
    """
    try:
        with get_db() as conn:
            rows = cd_db.list_deck_candidates(conn, org, operator_handle)
        # community_tweet is duel-only ingest (the no-repost wall, W6) — a real member
        # tweet must never be swipeable, even on a test guild mapped to a live org.
        rows = [r for r in rows if r["kind"] != "community_tweet"]
        if rows:
            return [
                {
                    "id": int(r["id"]),
                    "ref": str(r["id"]),
                    "kind": r["kind"],
                    "text": _payload_text(r["payload_json"]),
                }
                for r in rows
            ]
    except Exception as exc:  # noqa: BLE001  -- table absent / read error -> static fallback
        logger.warning("content_deck durable read failed (%s) -- using static seed", exc)
    return _static_cards(org)


def _card_embed(card: dict, *, index: int, total: int, org: str, kept: int) -> discord.Embed:
    embed = discord.Embed(
        description=card["text"],
        color=discord.Color.from_str("#C8A86E"),
    )
    embed.set_author(name=f"content deck · {org}")
    embed.add_field(name="kind", value=f"`{card['kind']}`", inline=True)
    embed.add_field(name="card", value=f"{index + 1} / {total}", inline=True)
    embed.add_field(name="kept", value=str(kept), inline=True)
    embed.set_footer(text="seed sample · ✓ keep · ✕ reject · ⏭ skip")
    return embed


def _summary_embed(org: str, *, triaged: int, kept: int) -> discord.Embed:
    embed = discord.Embed(
        title="deck cleared",
        description=f"{triaged} cards triaged, **{kept}** kept for **{org}**.",
        color=discord.Color.from_str("#4ADE80"),
    )
    embed.set_footer(text="seed spike — durable storage + ambient producers land in later phases")
    return embed


class _DeckView(discord.ui.View):
    """One-message swipe deck. Author-locked; advances the embed on each decision and
    audit-logs it. Buttons disable on timeout."""

    def __init__(self, *, invoker_id: int, org: str, guild_id: str, cards: list[dict]) -> None:
        super().__init__(timeout=_VIEW_TIMEOUT_SECONDS)
        self._invoker_id = invoker_id
        self._org = org
        self._guild_id = guild_id
        self._cards = cards
        self._index = 0
        self._kept = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message(
                "this deck isn't yours — run `/content-deck` to start your own.",
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

    def _current(self) -> dict | None:
        if self._index < len(self._cards):
            return self._cards[self._index]
        return None

    async def _decide(self, interaction: discord.Interaction, decision: str) -> None:
        card = self._current()
        if card is None:
            await interaction.response.defer()
            return
        if decision == "keep":
            self._kept += 1
        # Record the swipe (best-effort -- a write failure must not break the deck). DURABLE
        # path (a real candidate id) -> content_deck_decisions via the fail-closed accessor
        # (org-checked); STATIC fallback (id is None) -> the append-only audit_log sink.
        try:
            with get_db() as conn:
                if card.get("id") is not None:
                    cd_db.record_deck_decision(
                        conn,
                        candidate_id=int(card["id"]),
                        org_id=self._org,
                        actor=f"discord:user:{interaction.user.id}",
                        actor_kind="community",
                        decision=decision,
                        surface="discord",
                    )
                    conn.commit()
                else:
                    log_audit(
                        conn,
                        actor=f"discord:user:{interaction.user.id}",
                        action=f"content_deck_{decision}",
                        org_id=self._org,
                        entity_id=None,
                        detail={
                            "guild_id": self._guild_id,
                            "candidate_ref": card["ref"],
                            "kind": card["kind"],
                            "decision": decision,
                            "surface": "discord",
                        },
                        source="sable-roles",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("content_deck swipe write failed: %s", exc)

        self._index += 1
        nxt = self._current()
        if nxt is None:
            for child in self.children:
                if hasattr(child, "disabled"):
                    child.disabled = True
            await interaction.response.edit_message(
                embed=_summary_embed(self._org, triaged=self._index, kept=self._kept),
                view=self,
                allowed_mentions=_NO_MENTIONS,
            )
            self.stop()
            return
        await interaction.response.edit_message(
            embed=_card_embed(nxt, index=self._index, total=len(self._cards), org=self._org, kept=self._kept),
            view=self,
            allowed_mentions=_NO_MENTIONS,
        )

    @discord.ui.button(label="Reject", emoji="✕", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._decide(interaction, "reject")

    @discord.ui.button(label="Skip", emoji="⏭", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._decide(interaction, "skip")

    @discord.ui.button(label="Keep", emoji="✓", style=discord.ButtonStyle.success)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._decide(interaction, "keep")


def register_commands(
    tree: app_commands.CommandTree,
    *,
    client: discord.Client | None = None,
) -> list[str]:
    """Register /content-deck GUILD-SCOPED to the safe test guilds only.

    Returns the list of guild ids it registered to, so the caller (main.py setup_hook) can
    `tree.sync(guild=...)` each one. Returns [] when there are no safe test guilds (the
    feature then ships invisible). NEVER adds the command to the global tree.
    """
    safe = _safe_test_guilds()
    if not safe:
        logger.info("content_deck: no safe test guilds configured — /content-deck not registered.")
        return []

    @app_commands.command(name="content-deck", description="Swipe through content candidates (keep / reject / skip).")
    async def content_deck_cmd(interaction: discord.Interaction) -> None:
        guild_id = str(interaction.guild_id)
        org = safe.get(guild_id)
        if org is None:
            await interaction.response.send_message(
                "content deck isn't enabled here.", ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        cards = _load_cards(org, f"discord:user:{interaction.user.id}")
        if not cards:
            await interaction.response.send_message(
                "no candidates seeded for this org yet.", ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        view = _DeckView(invoker_id=interaction.user.id, org=org, guild_id=guild_id, cards=cards)
        await interaction.response.send_message(
            embed=_card_embed(cards[0], index=0, total=len(cards), org=org, kept=0),
            view=view,
            allowed_mentions=_NO_MENTIONS,
        )

    for gid in safe:
        tree.add_command(content_deck_cmd, guild=discord.Object(id=int(gid)))
    logger.info("content_deck: registered /content-deck guild-scoped to %s", list(safe))
    return list(safe.keys())
