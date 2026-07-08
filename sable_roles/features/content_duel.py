"""Phase 5 — the COMMUNITY content duel (the disclosed preference-data flywheel).

`/duel` posts two PENDING Content-Deck candidates into a live client channel; community
members pick the better one. Each vote is one `content_deck_decisions` row
(`actor_kind='community'`, `surface='discord'`, winner=`candidate_id` beats
`pair_loser_id`) — the same substrate the operator web duel writes — and folds into
'community:'-prefixed Elo rows (the SablePlatform quarantine), so community taste NEVER
steers the operator Elo, ranking, or production until the masterplan §11 K-tests pass.

THE DISCLOSURE GATE (fail-closed, per masterplan round-1 SEC-4): every command here
re-checks the org's `pairwise_disclosure_signed` config value AT INVOCATION — a
non-empty string (the signed-consent provenance: date + who + authority) enables the
game; missing org, missing key, empty value, unparseable blob, or ANY read error
refuses. Registration is NOT authorization — the commands exist on every GUILD_TO_ORG
guild but no-op politely until the org's disclosure is recorded via
`sable-platform org config set <org> pairwise_disclosure_signed "<provenance>"`.
Member-facing disclosure rides every duel embed's footer.

Vote integrity: OPEN voting (deliberately NOT the author-locked View pattern — this is
the bot's first multi-voter surface), one vote per member per duel enforced twice: an
in-View dict (fast path) backed by a DURABLE `has_recent_duel_vote` check (survives a
bot restart mid-duel). The running tally shows only the vote COUNT while open — the
A/B split is revealed only at close, so early votes can't herd later ones. Sable never
auto-posts: votes are preference data; publishing stays operator-gated end-to-end.

Unlike `features/content_deck.py` (the Phase-0 spike, whose invariant is test-guilds-
only isolation), this feature TARGETS live GUILD_TO_ORG guilds — the disclosure gate
is what makes that safe. The spike stays untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

import discord
from discord import app_commands

from sable_platform.db import content_deck as cd_db
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db
try:
    from sable_platform.db.orgs import get_org_config_value
except ImportError:  # older SablePlatform without the accessor — the gate FAILS CLOSED
    get_org_config_value = None  # type: ignore[assignment]

from sable_roles.config import DUEL_STARTERS, GUILD_TO_ORG
from sable_roles.features.fitcheck_streak import _is_mod

logger = logging.getLogger("sable_roles.content_duel")

_NO_MENTIONS = discord.AllowedMentions.none()
# HARD wall-clock duel length. discord.py View.timeout is an INACTIVITY timeout that
# refreshes on every button press (Codex F4), so _DuelView also tracks an absolute
# deadline and shrinks self.timeout to the REMAINING wall-clock after each vote — a
# steady vote stream can never hold a duel open past the deadline. Kept modest so a
# bot restart mid-duel (which orphans the message buttons — views are not persistent;
# clicks after restart show "interaction failed" and the tally never reveals) has a
# small blast window; the VOTE LEDGER always survives restart (rows are written per
# click + the durable dedup guard), only the message surface is lost (Codex F3).
_DUEL_OPEN_SECONDS = 10 * 60
_MAX_CARD_CHARS = 900  # embed-safe candidate text clip
_DISCLOSURE_FOOTER = (
    "community duel · your picks help train Sable's content engine for this community"
)
# The disclosure variant for an all-community pair — the "which popped?" prediction
# game over REAL member tweets (Phase A of the community-duel plan).
_COMMUNITY_FOOTER = (
    "community duel · real tweets from this community · guess which popped — "
    "picks help calibrate Sable's taste engine"
)
_CONFIG_KEY = "pairwise_disclosure_signed"
_DUEL_KINDS_KEY = "duel_kinds"
# The mig-083 CHECK set — a configured duel_kinds may only name these; anything else
# is operator error and REFUSES the duel (fail-closed, same posture as the disclosure
# gate: a typo'd config must never silently widen the pool).
_VALID_DUEL_KINDS = frozenset(
    {"clip", "tweet", "thread", "quote_card", "meme", "copypasta", "community_tweet"}
)
# None is a VALID _org_duel_kinds result ("no filter"), so refusal needs a sentinel.
_KINDS_REFUSED = object()
# X handle shape — a community card whose author_handle doesn't match is DROPPED
# (a malformed handle would render as a fake attribution in a recognition game).
_AUTHOR_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# The reveal's ground-truth counters. Views deliberately excluded (retroactively
# unreliable — same call as the ingest-side ranking).
_ENGAGEMENT_KEYS = ("likes", "retweets", "replies", "quotes")

# F2: one OPEN duel per org at a time (an in-process registry: org -> monotonic
# deadline). Stops a mod double-posting the same pair before any vote lands (the SP
# 12h exclusion only sees recorded VOTES). In-process only — a restart forgets an
# open duel; residual: a mod could then re-open the same pair early, whose votes
# still dedup durably. Single-process constraint applies (like every module dict).
_OPEN_DUELS: dict[str, float] = {}


def _org_for(guild_id: int | str | None) -> str | None:
    return GUILD_TO_ORG.get(str(guild_id)) if guild_id is not None else None


def _can_start_duel(member: discord.Member, guild_id: str) -> bool:
    """The /duel trigger gate. When the guild has a NON-EMPTY ``DUEL_STARTERS`` entry,
    that NAMED user-id allowlist is the ONLY trigger (roles deliberately ignored — the
    operator's "by username not by role for now"); an unconfigured guild falls back to
    the MOD_ROLES role gate. Both paths fail closed when unconfigured."""
    if guild_id in DUEL_STARTERS:
        # key PRESENCE selects the allowlist path — an explicit empty list means
        # "locked: nobody starts duels", never a silent fall-through to roles (Codex).
        return str(member.id) in {str(s) for s in DUEL_STARTERS[guild_id] or []}
    return _is_mod(member, guild_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# An operator "turning it off" with any of these must actually turn it off — a bare
# non-empty-string gate would treat pairwise_disclosure_signed="false" as SIGNED (T2-4).
_REVOKED_SENTINELS = frozenset({"false", "no", "off", "revoked", "0", "none", "null", "disabled"})


def _disclosure_signed(org: str) -> bool:
    """FAIL-CLOSED: True only when the org's ``pairwise_disclosure_signed`` config value
    is a non-empty, non-sentinel string (the signed-consent provenance). Missing
    org/key/value, a non-string, a revocation sentinel ("false"/"no"/"revoked"/…), an
    OLD SablePlatform without the accessor, or ANY error → False."""
    if get_org_config_value is None:
        return False
    try:
        with get_db() as conn:
            val = get_org_config_value(conn, org, _CONFIG_KEY)
        if not isinstance(val, str):
            return False
        cleaned = val.strip()
        return bool(cleaned) and cleaned.lower() not in _REVOKED_SENTINELS
    except Exception:  # noqa: BLE001 — a broken read must refuse, never enable
        return False


def _org_duel_kinds(org: str):
    """The org's ``duel_kinds`` pool filter, FAIL-CLOSED like the disclosure gate.
    Key absent/None → None (no filter — today's pool, byte-identical behavior).
    A list of known kinds — stored as a real JSON list OR as a JSON-string-encoded
    list, org config carries either — → that tuple. An EXPLICIT empty list → refused
    (explicit-empty-means-locked, the DUEL_STARTERS convention). Malformed JSON, a
    non-list, non-string entries, unknown kinds, a missing accessor, or ANY read
    error → refused (``_KINDS_REFUSED``)."""
    if get_org_config_value is None:
        return _KINDS_REFUSED
    try:
        with get_db() as conn:
            val = get_org_config_value(conn, org, _DUEL_KINDS_KEY)
    except Exception:  # noqa: BLE001 — a broken read must refuse, never widen the pool
        return _KINDS_REFUSED
    if val is None:
        return None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (ValueError, TypeError):
            return _KINDS_REFUSED
    if not isinstance(val, list) or not val:
        return _KINDS_REFUSED
    if not all(isinstance(k, str) and k in _VALID_DUEL_KINDS for k in val):
        return _KINDS_REFUSED
    return tuple(val)


def _community_fields(payload_json: str) -> dict | None:
    """The EXTRA whitelisted render fields for a ``community_tweet`` card: ``author``
    (``payload.author_handle``, handle-shaped or the card is dropped), ``engagement``
    (ints for likes/retweets/replies/quotes — the reveal's ground truth; a card whose
    numbers are missing/malformed is dropped rather than reveal a wrong score), and
    optional ``engagement_as_of``. Internal payload fields (x_id, url, ingest_batch)
    stay unrendered — the strict-whitelist posture is unchanged. None = drop card."""
    try:
        p = json.loads(payload_json or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(p, dict):
        return None
    handle = p.get("author_handle")
    # fullmatch, not match — "$" alone would accept a trailing newline into an
    # embed field name.
    if not isinstance(handle, str) or not _AUTHOR_HANDLE_RE.fullmatch(handle):
        return None
    raw = p.get("engagement")
    if not isinstance(raw, dict):
        return None
    engagement: dict[str, int] = {}
    for key in _ENGAGEMENT_KEYS:
        v = raw.get(key)
        if isinstance(v, bool):  # bool is an int subclass — never a real counter
            return None
        try:
            engagement[key] = int(v)
        except (TypeError, ValueError):
            return None
    fields = {"author": handle, "engagement": engagement}
    as_of = p.get("engagement_as_of")
    if isinstance(as_of, str) and as_of.strip():
        fields["engagement_as_of"] = as_of
    return fields


def _popped_score(engagement: dict) -> int:
    """The ingest-side popped-score formula (retweets count DOUBLE; views excluded)
    — the displayed reveal score IS the ranking formula, never a plain sum."""
    return (engagement["likes"] + 2 * engagement["retweets"]
            + engagement["replies"] + engagement["quotes"])


def _payload_text(kind: str, payload_json: str) -> str:
    """Candidate → PUBLIC display text under a STRICT whitelist: ONLY ``payload.text``
    (text kinds) or ``[format] captions`` (memes) ever renders. There is deliberately NO
    raw-JSON fallback (Codex F1) — a payload we don't positively recognize returns ""
    and the caller SKIPS the candidate, because payload_json carries internal fields
    (guardrail_hits / do_not_mention / scoring notes) that must never reach a client
    channel. Pending candidates have no R2 media, so v1 duels are text-rendered."""
    try:
        p = json.loads(payload_json or "{}")
        if not isinstance(p, dict):
            return ""
        if isinstance(p.get("text"), str) and p["text"].strip():
            return p["text"]
        caps_obj = p.get("captions")
        if isinstance(caps_obj, dict):
            caps = " / ".join(str(v) for v in caps_obj.values() if isinstance(v, str) and v)
            if not caps:
                return ""
            fmt = p.get("format") if isinstance(p.get("format"), str) else None
            fmt = fmt or (p.get("template_id") if isinstance(p.get("template_id"), str) else None)
            return f"[{fmt}] {caps}" if fmt else caps
        return ""
    except (ValueError, TypeError):
        return ""


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text[: _MAX_CARD_CHARS - 1] + "…" if len(text) > _MAX_CARD_CHARS else text


def _duel_embed(org: str, card_a: dict, card_b: dict, *, votes: int, closed: bool = False,
                tally: tuple[int, int] | None = None) -> discord.Embed:
    embed = discord.Embed(color=discord.Color.from_str("#C8A86E"))
    embed.set_author(name=f"content duel · {org}")
    # a community card names its REAL author (recognition is the hook); other kinds
    # keep the kind label. Handles are pre-sanitized in _load_pair (≤15 word chars),
    # far under Discord's 256-char field-name cap.
    name_a = f"🅰 · @{card_a['author']}" if card_a.get("author") else f"🅰 · {card_a['kind']}"
    name_b = f"🅱 · @{card_b['author']}" if card_b.get("author") else f"🅱 · {card_b['kind']}"
    embed.add_field(name=name_a, value=_clip(card_a["text"]) or "—", inline=False)
    embed.add_field(name=name_b, value=_clip(card_b["text"]) or "—", inline=False)
    footer = (
        _COMMUNITY_FOOTER
        if card_a.get("author") and card_b.get("author")
        else _DISCLOSURE_FOOTER
    )
    if closed and tally is not None:
        a, b = tally
        verdict = "🅰 wins" if a > b else ("🅱 wins" if b > a else "a tie")
        embed.add_field(name="final", value=f"🅰 {a} — {b} 🅱 · {verdict}", inline=False)
        eng_a, eng_b = card_a.get("engagement"), card_b.get("engagement")
        if eng_a and eng_b:
            # the "which popped?" answer — real engagement, weighted score
            s_a, s_b = _popped_score(eng_a), _popped_score(eng_b)
            popped = "🅰 popped" if s_a > s_b else ("🅱 popped" if s_b > s_a else "dead heat")
            reality = (
                f"🅰 score {s_a} ({eng_a['likes']}❤ {eng_a['retweets']}🔁 "
                f"{eng_a['replies']}💬 {eng_a['quotes']}❞) · 🅱 score {s_b} — {popped}"
            )
            if a != b and s_a != s_b:
                # a tie on either axis leaves nothing to compare — the line is omitted
                reality += (
                    "\nthe room called it" if (a > b) == (s_a > s_b)
                    else "\nupset — the room picked the other one"
                )
            embed.add_field(name="reality", value=reality, inline=False)
            as_of = card_a.get("engagement_as_of") or card_b.get("engagement_as_of")
            if as_of:
                # date only — numbers are as-of-ingest, not a live ticker
                footer += f" · numbers as of {as_of.split('T')[0]}"
    else:
        # count only while open — the A/B split stays hidden so votes can't herd
        embed.add_field(name="votes", value=str(votes), inline=False)
    embed.set_footer(text=footer)
    return embed


class _DuelView(discord.ui.View):
    """OPEN-voting view (the bot's first non-author-locked View): any guild member may
    press 🅰/🅱 once. Vote dedup = in-View dict backed by the durable DB check; each
    vote writes its decision row IMMEDIATELY (a restart never loses recorded votes).
    Buttons disable + the blind tally reveals on timeout."""

    def __init__(self, *, org: str, guild_id: str, card_a: dict, card_b: dict) -> None:
        super().__init__(timeout=float(_DUEL_OPEN_SECONDS))
        self._org = org
        self._guild_id = guild_id
        self._card_a = card_a
        self._card_b = card_b
        self._opened_at = _now_iso()
        # HARD deadline (monotonic): View.timeout alone is an inactivity timer that
        # refreshes on every press (Codex F4) — after each vote we shrink it to the
        # REMAINING wall-clock so the duel always closes by the deadline.
        self._deadline = time.monotonic() + _DUEL_OPEN_SECONDS
        self._votes: dict[int, str] = {}
        self._message: discord.Message | None = None

    def bind_message(self, message: discord.Message) -> None:
        self._message = message

    def _remaining(self) -> float:
        return self._deadline - time.monotonic()

    def _vote_count(self) -> int:
        """Real recorded choices only — dedup markers like '(pre-restart)' don't count,
        so the displayed total always equals the closing 🅰+🅱 tally."""
        return sum(1 for c in self._votes.values() if c in ("a", "b"))

    async def _vote(self, interaction: discord.Interaction, choice: str) -> None:
        user = interaction.user
        if user.bot:
            return
        if user.id in self._votes:
            await interaction.response.send_message(
                "you already voted on this duel.", ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        if self._remaining() <= 0:
            # past the hard deadline but on_timeout hasn't fired yet (inactivity-timer
            # race at the boundary) — refuse the vote and let the close land.
            await interaction.response.send_message(
                "this duel just closed.", ephemeral=True, allowed_mentions=_NO_MENTIONS,
            )
            return
        winner = self._card_a if choice == "a" else self._card_b
        loser = self._card_b if choice == "a" else self._card_a
        actor = f"discord:user:{user.id}"
        # PRE-MARK before any await (T2-1): discord.py dispatches each click as its own
        # task, so two rapid clicks would BOTH pass the dict check above and reach the
        # worker thread — the durable guard can't see an uncommitted sibling insert.
        # Marking synchronously here makes the second task hit the already-voted branch;
        # rolled back below if the write fails (the member may retry).
        self._votes[user.id] = choice

        def _record_sync() -> bool:
            """True = recorded; False = the durable guard says this member already
            voted. Runs in a worker thread (Codex F5) — the public-voting path must
            never stall the gateway event loop on a slow Postgres round-trip."""
            with get_db() as conn:
                if cd_db.has_recent_duel_vote(
                    conn, self._org, actor=actor,
                    candidate_ids=(int(winner["id"]), int(loser["id"])),
                    since=self._opened_at,
                ):
                    return False
                cd_db.record_deck_decision(
                    conn,
                    candidate_id=int(winner["id"]),
                    org_id=self._org,
                    actor=actor,
                    actor_kind="community",
                    decision="keep",
                    surface="discord",
                    pair_loser_id=int(loser["id"]),
                )
                conn.commit()
                log_audit(
                    conn, actor, "content_duel_vote", org_id=self._org,
                    detail={
                        "guild_id": self._guild_id, "winner_id": int(winner["id"]),
                        "loser_id": int(loser["id"]), "choice": choice,
                    },
                    source="sable-roles",
                )
            return True

        try:
            recorded = await asyncio.to_thread(_record_sync)
        except Exception as exc:  # noqa: BLE001 — a failed write must not eat the interaction
            self._votes.pop(user.id, None)  # roll back the pre-mark — the member may retry
            logger.warning("duel vote write failed for %s/%s: %s", self._org, actor, exc)
            await interaction.response.send_message(
                "couldn't record that vote — try again in a moment.", ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        if not recorded:
            # they DID vote (pre-restart) — keep them marked, but as a non-counting entry
            self._votes[user.id] = "(pre-restart)"
            await interaction.response.send_message(
                "you already voted on this duel.", ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        # shrink the inactivity timer to the REMAINING wall-clock (Codex F4) so steady
        # voting can never extend the duel past its deadline.
        self.timeout = max(1.0, self._remaining())
        await interaction.response.edit_message(
            embed=_duel_embed(self._org, self._card_a, self._card_b, votes=self._vote_count()),
            view=self, allowed_mentions=_NO_MENTIONS,
        )

    @discord.ui.button(label="🅰 this one", style=discord.ButtonStyle.primary)
    async def vote_a(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._vote(interaction, "a")

    @discord.ui.button(label="🅱 this one", style=discord.ButtonStyle.primary)
    async def vote_b(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._vote(interaction, "b")

    async def on_timeout(self) -> None:
        _OPEN_DUELS.pop(self._org, None)
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        tally = (
            sum(1 for c in self._votes.values() if c == "a"),
            sum(1 for c in self._votes.values() if c == "b"),
        )
        if self._message is not None:
            try:
                await self._message.edit(
                    embed=_duel_embed(
                        self._org, self._card_a, self._card_b,
                        votes=self._vote_count(), closed=True, tally=tally,
                    ),
                    view=self, allowed_mentions=_NO_MENTIONS,
                )
            except discord.HTTPException as exc:
                logger.warning("duel close edit failed: %s", exc)


def _load_pair(org: str, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Two fresh pending candidates for a duel (empty/short list when the deck is thin).
    A candidate whose payload doesn't pass the strict public-render whitelist (F1)
    renders "" and is DROPPED — internal payload fields never reach the channel. A
    ``community_tweet`` card additionally carries author + engagement (dropped when
    either is missing/invalid — see ``_community_fields``); other kinds unchanged."""
    with get_db() as conn:
        if kinds is None:
            # MIXED-VERSION SAFETY (audit F1): the unconfigured path stays
            # byte-identical to the pre-duel_kinds call — a stale baked SablePlatform
            # in the Docker image (old signature) can never TypeError this path into
            # "not enough candidates" (the caller's try/except would swallow it).
            rows = cd_db.get_deck_duel_pair(conn, org)
        else:
            rows = cd_db.get_deck_duel_pair(conn, org, kinds=tuple(kinds))
    cards: list[dict] = []
    for r in rows:
        kind = str(r["kind"])
        card = {"id": int(r["id"]), "kind": kind,
                "text": _payload_text(kind, r["payload_json"])}
        if kind == "community_tweet":
            extra = _community_fields(r["payload_json"])
            if extra is None:
                continue  # bad author/engagement — never render, never reveal wrong
            card.update(extra)
        if card["text"].strip():
            cards.append(card)
    return cards


def register_commands(tree: app_commands.CommandTree) -> None:
    """Register /duel + /tasteboard on the GLOBAL tree — main.py's copy_global_to loop
    fans them onto every GUILD_TO_ORG guild. Runtime gates (org mapping + mod trigger +
    the fail-closed disclosure check) are the authorization; registration is not."""

    @tree.command(name="duel", description="Post a community content duel (mods only)")
    async def duel(interaction: discord.Interaction) -> None:  # pragma: no cover — thin shell
        await _handle_duel(interaction)

    @tree.command(name="tasteboard", description="The community taste leaderboard")
    async def tasteboard(interaction: discord.Interaction) -> None:  # pragma: no cover
        await _handle_tasteboard(interaction)


async def _handle_duel(interaction: discord.Interaction) -> None:
    org = _org_for(interaction.guild_id)
    if org is None or interaction.guild is None:
        await interaction.response.send_message(
            "this server isn't configured for content duels.", ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )
        return
    member = interaction.user
    if not isinstance(member, discord.Member) or not _can_start_duel(member, str(interaction.guild_id)):
        await interaction.response.send_message(
            "duels are started by the Sable team — ask one of them to run one.",
            ephemeral=True, allowed_mentions=_NO_MENTIONS,
        )
        return
    if not await asyncio.to_thread(_disclosure_signed, org):
        await interaction.response.send_message(
            "community duels aren't enabled for this server (the client's data-use "
            "disclosure isn't on file).", ephemeral=True, allowed_mentions=_NO_MENTIONS,
        )
        return
    # F2: one open duel per org — a second /duel while one is live (or a double-click
    # before any vote lands, which the SP 12h exclusion can't see) is refused.
    if _OPEN_DUELS.get(org, 0.0) > time.monotonic():
        await interaction.response.send_message(
            "a duel is already open for this server — let it finish first.",
            ephemeral=True, allowed_mentions=_NO_MENTIONS,
        )
        return
    kinds = await asyncio.to_thread(_org_duel_kinds, org)
    if kinds is _KINDS_REFUSED:
        await interaction.response.send_message(
            "this server's duel pool config (`duel_kinds`) is empty or malformed — "
            "duels stay off until an operator fixes the org config.",
            ephemeral=True, allowed_mentions=_NO_MENTIONS,
        )
        return
    try:
        pair = await asyncio.to_thread(_load_pair, org, kinds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("duel pair load failed for %s: %s", org, exc)
        pair = []
    if len(pair) < 2:
        await interaction.response.send_message(
            "not enough fresh content to duel right now — try again later.",
            ephemeral=True, allowed_mentions=_NO_MENTIONS,
        )
        return
    card_a, card_b = pair[0], pair[1]
    view = _DuelView(org=org, guild_id=str(interaction.guild_id), card_a=card_a, card_b=card_b)
    # Post the duel as a REGULAR bot channel message, not the interaction response: an
    # interaction's webhook token expires after 15 minutes — exactly this View's
    # lifetime — so the closing-tally edit on an interaction-owned message would 401.
    # A bot-authored message stays editable forever.
    channel = interaction.channel
    if channel is None or not hasattr(channel, "send"):
        await interaction.response.send_message(
            "can't post a duel in this channel.", ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )
        return
    await interaction.response.send_message(
        "duel posted ⚔", ephemeral=True, allowed_mentions=_NO_MENTIONS,
    )
    # claim the org lock only once the ack landed — an ack failure must never leave the
    # org duel-locked with no duel (adversarial T3); the send-failure path below releases.
    _OPEN_DUELS[org] = time.monotonic() + _DUEL_OPEN_SECONDS
    try:
        message = await channel.send(
            embed=_duel_embed(org, card_a, card_b, votes=0), view=view,
            allowed_mentions=_NO_MENTIONS,
        )
    except discord.HTTPException:
        _OPEN_DUELS.pop(org, None)  # never hold the lock for a duel that never posted
        # The ack above already said "posted" — correct the record so the starter isn't
        # left believing a duel is live somewhere (the mod-chat lesson: a private channel
        # the bot can't access 403s HERE, after a green ack). Best-effort: the correction
        # must never mask the original failure.
        try:
            await interaction.followup.send(
                "…actually, I couldn't post the duel in this channel (no access). "
                "run /duel in a channel where I can send messages.",
                ephemeral=True, allowed_mentions=_NO_MENTIONS,
            )
        except discord.HTTPException:
            pass
        raise
    view.bind_message(message)
    try:
        with get_db() as conn:
            log_audit(
                conn, f"discord:user:{member.id}", "content_duel_opened", org_id=org,
                detail={"guild_id": str(interaction.guild_id),
                        "candidate_a": card_a["id"], "candidate_b": card_b["id"]},
                source="sable-roles",
            )
    except Exception as exc:  # noqa: BLE001 — audit is best-effort here
        logger.warning("duel open audit failed for %s: %s", org, exc)


async def _handle_tasteboard(interaction: discord.Interaction) -> None:
    org = _org_for(interaction.guild_id)
    if org is None or interaction.guild is None:
        await interaction.response.send_message(
            "this server isn't configured for content duels.", ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )
        return
    if not await asyncio.to_thread(_disclosure_signed, org):
        await interaction.response.send_message(
            "community duels aren't enabled for this server.", ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )
        return
    def _board_sync() -> list[dict]:
        with get_db() as conn:
            return cd_db.get_community_duel_leaderboard(conn, org)

    try:
        rows = await asyncio.to_thread(_board_sync)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tasteboard read failed for %s: %s", org, exc)
        rows = []
    if not rows:
        await interaction.response.send_message(
            "no duel votes yet — ask a mod to `/duel`.", ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )
        return
    lines = []
    for i, r in enumerate(rows, start=1):
        uid = r["actor"].removeprefix("discord:user:")
        member = interaction.guild.get_member(int(uid)) if uid.isdigit() else None
        name = member.display_name if member else f"member {uid[-4:]}"
        agree = f" · agrees with ops {round(100 * r['agreed'] / r['decided'])}%" if r["decided"] else ""
        lines.append(f"`{i:>2}` **{name}** · {r['votes']} votes{agree}")
    embed = discord.Embed(
        description="\n".join(lines), color=discord.Color.from_str("#C8A86E"),
    )
    embed.set_author(name=f"community taste board · {org}")
    embed.set_footer(text=_DISCLOSURE_FOOTER + " · agreement = picks matching the ops verdict")
    await interaction.response.send_message(
        embed=embed, ephemeral=True, allowed_mentions=_NO_MENTIONS,
    )
