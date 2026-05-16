"""Burn-me feature: vision-based roast generation for #fitcheck opt-ins.

B3 ships the mod-only /set-burn-mode slash command. B4 adds /burn-me + /stop-pls
plus the in-memory `_burn_invoke_cooldown` dict that gates rapid re-invocation.
B5 adds the roast pipeline (Anthropic vision call + cost/audit log), B6 wires
into on_message. See ~/Projects/SolStitch/internal/burn_me_v1_build_plan.md.

Post-V1 additions (see ~/Projects/SolStitch/internal/roast_build_TODO.md):

* R4 — /stop-pls extended to write `discord_burn_blocklist` + purge retained
  personalization data (discord_user_vibes / observations) in the same txn;
  `maybe_roast` gets a position-0 blocklist gate.
* R7 — `generate_roast` returns ``(roast_text, audit_id)`` so peer-roast can
  link bot replies back via the new `record_roast_reply` helper; adds
  `actor_user_id` kwarg stamped in audit detail for /peer-roast-report.
* R11 — `generate_roast` accepts `vibe_block` kwarg, injects it as a
  USER-role text block between the image and the context block (system
  prompt stays static + cached per plan §5.3 POST-AUDIT); audit detail
  stamps `vibe_present` for /peer-roast-report telemetry.
"""
from __future__ import annotations

import base64
import logging
import random
import re
from datetime import datetime, timedelta, timezone

import discord
from anthropic import AsyncAnthropic, BadRequestError
from discord import app_commands

from sable_platform.db import (
    discord_burn,
    discord_guild_config,
    discord_roast,
    discord_user_vibes,
)
from sable_platform.db.audit import log_audit
from sable_platform.db.connection import get_db
from sable_platform.db.cost import log_cost

from sable_roles.config import (
    BURN_DAILY_CAP_PER_USER,
    BURN_INVOKE_COOLDOWN_SECONDS,
    BURN_MODEL,
    BURN_RANDOM_DEDUP_DAYS,
    BURN_RANDOM_PROB,
    GUILD_TO_ORG,
)
from sable_roles.features.fitcheck_streak import _is_mod
from sable_roles.prompts.burn_me_system import SYSTEM_PROMPT

logger = logging.getLogger("sable_roles.burn_me")

# Lazy-init Anthropic async client. Reused across calls so the SDK's underlying
# httpx pool stays warm. Tests monkeypatch `_anthropic_client` directly to bypass.
_anthropic_client: AsyncAnthropic | None = None


def _client() -> AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic()
    return _anthropic_client


# Locked text used by every redacted-bounce surface when burn_mode == 'never'.
# Single string so the lockdown reads consistently across /burn-me, /my-roasts,
# peer /roast right-click, etc.
REDACTED_MESSAGE = "--REDACTED--"


def is_burn_mode_never(conn, guild_id: str) -> bool:
    """True iff the guild's current_burn_mode is 'never' — burn lockdown.

    In 'never' mode, the auto-fire pipeline (`maybe_roast`), the /burn-me
    opt-in surface, peer /roast right-click, and /my-roasts ALL bounce
    with REDACTED_MESSAGE. Only the team-mod path (right-click → "Roast
    this fit" by a holder of MOD_ROLES) and the /set-burn-mode itself
    still function — team retains the ability to manually roast + to
    flip the mode back.

    Streak-restoration grants still fire silently (token + audit row land,
    DM is suppressed per the operator's choice — see roast.py
    maybe_grant_restoration_token).
    """
    cfg = discord_guild_config.get_config(conn, guild_id)
    return cfg.get("current_burn_mode") == "never"

# Module-level state: per-user last-invocation timestamp for the /burn-me
# cooldown gate. Mirrors the `_dm_cooldown` pattern in fitcheck_streak — no
# LRU cap, no cross-process sync (single-bot constraint per CLAUDE.md).
_burn_invoke_cooldown: dict[int, datetime] = {}


def register_commands(tree: app_commands.CommandTree) -> None:
    """Register burn-me slash commands against the command tree.

    Called from `SableRolesClient.setup_hook` AFTER
    `fitcheck_streak.register_commands(tree)` so all commands sync together in
    the same `copy_global_to` + `tree.sync` pass per guild.
    """

    @tree.command(
        name="set-burn-mode",
        description="(mods) Set the global burn-me default mode for this server",
    )
    @app_commands.describe(
        mode=(
            "once (consume on first roast), persist (until /stop-pls),"
            " or never (team-only manual burns, lock down community surface)"
        ),
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="once", value="once"),
            app_commands.Choice(name="persist", value="persist"),
            app_commands.Choice(name="never", value="never"),
        ]
    )
    async def set_burn_mode_cmd(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        org_id = GUILD_TO_ORG.get(guild_id) if guild_id else None
        if guild_id is None or org_id is None:
            await interaction.followup.send(
                "not configured for this server.", ephemeral=True
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(
                "this command must be run inside the server.", ephemeral=True
            )
            return
        if not _is_mod(interaction.user, guild_id):
            await interaction.followup.send(
                "you're not a mod.", ephemeral=True
            )
            return
        with get_db() as conn:
            discord_guild_config.set_burn_mode(
                conn,
                guild_id,
                mode.value,
                updated_by=str(interaction.user.id),
            )
            log_audit(
                conn,
                actor=f"discord:user:{interaction.user.id}",
                action="fitcheck_burn_mode_set",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "mode": mode.value,
                    "by_user_id": str(interaction.user.id),
                },
                source="sable-roles",
            )
        await interaction.followup.send(
            f"burn-me default mode set to **{mode.value}**.",
            ephemeral=True,
        )

    @tree.command(
        name="burn-me",
        description="Opt in to be roasted on your next fit (or persist mode)",
    )
    @app_commands.describe(target="(mods only) opt someone else in")
    async def burn_me_cmd(
        interaction: discord.Interaction,
        target: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        org_id = GUILD_TO_ORG.get(guild_id) if guild_id else None
        if guild_id is None or org_id is None:
            await interaction.followup.send(
                "not configured for this server.", ephemeral=True
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(
                "this command must be run inside the server.", ephemeral=True
            )
            return
        if target is not None and not _is_mod(interaction.user, guild_id):
            await interaction.followup.send(
                "only mods can opt someone else in.", ephemeral=True
            )
            return
        # never-mode lockdown — community surface is REDACTED. Team can
        # still opt others in (`target is not None` path) only via mod
        # /roast right-click which goes through a different handler.
        with get_db() as conn:
            if is_burn_mode_never(conn, guild_id):
                await interaction.followup.send(
                    REDACTED_MESSAGE, ephemeral=True
                )
                return
        now = datetime.now(timezone.utc)
        last = _burn_invoke_cooldown.get(interaction.user.id)
        if last is not None and now - last < timedelta(
            seconds=BURN_INVOKE_COOLDOWN_SECONDS
        ):
            remaining = BURN_INVOKE_COOLDOWN_SECONDS - int((now - last).total_seconds())
            await interaction.followup.send(
                f"slow down — try again in {remaining}s.", ephemeral=True
            )
            return
        _burn_invoke_cooldown[interaction.user.id] = now
        invoker_id = str(interaction.user.id)
        if target is not None:
            user_id = str(target.id)
            opted_in_by = invoker_id
        else:
            user_id = invoker_id
            opted_in_by = invoker_id
        with get_db() as conn:
            cfg = discord_guild_config.get_config(conn, guild_id)
            mode = cfg["current_burn_mode"]
            discord_burn.opt_in(
                conn,
                guild_id,
                user_id,
                mode,
                opted_in_by,
            )
            log_audit(
                conn,
                actor=f"discord:user:{invoker_id}",
                action="fitcheck_burn_optin",
                org_id=org_id,
                entity_id=None,
                detail={
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "mode": mode,
                    "opted_in_by": opted_in_by,
                    "self_optin": target is None,
                },
                source="sable-roles",
            )
        if target is not None:
            body = (
                f"opted **{target.display_name}** in — mode **{mode}**."
            )
        else:
            body = f"you're opted in — mode **{mode}**. post a fit."
        await interaction.followup.send(body, ephemeral=True)

    @tree.command(
        name="stop-pls",
        description="Stop being roasted (clears your burn-me opt-in)",
    )
    async def stop_pls_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        org_id = GUILD_TO_ORG.get(guild_id) if guild_id else None
        if guild_id is None or org_id is None:
            await interaction.followup.send(
                "not configured for this server.", ephemeral=True
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(
                "this command must be run inside the server.", ephemeral=True
            )
            return
        user_id = str(interaction.user.id)
        with get_db() as conn:
            removed = discord_burn.opt_out(conn, guild_id, user_id)
            if removed:
                log_audit(
                    conn,
                    actor=f"discord:user:{user_id}",
                    action="fitcheck_burn_optout",
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "user_id": user_id,
                    },
                    source="sable-roles",
                )
            # R4: sticky stop-pls — blocklist + purge retained personalization
            # data so opt-out actually means "no data retained," not just
            # "no future inference." Audit gated on insert_blocklist returning
            # True so re-stop-pls invocations don't double-audit.
            newly_blocked = discord_roast.insert_blocklist(
                conn, guild_id, user_id
            )
            purge_counts = discord_user_vibes.purge_user_personalization_data(
                conn, guild_id, user_id
            )
            if newly_blocked:
                log_audit(
                    conn,
                    actor=f"discord:user:{user_id}",
                    action="fitcheck_burn_blocklist_added",
                    org_id=org_id,
                    entity_id=None,
                    detail={
                        "guild_id": guild_id,
                        "user_id": user_id,
                        "purge_counts": purge_counts,
                        "blocklist_was_new": True,
                    },
                    source="sable-roles",
                )
        body = (
            "no more burns coming your way."
            if newly_blocked
            else "you're already on the list — no roasts incoming."
        )
        await interaction.followup.send(body, ephemeral=True)


# --- B5: roast pipeline ---


def _sniff_image_type(data: bytes) -> str | None:
    """Magic-byte detection for the 4 Anthropic-accepted vision formats.

    Discord's attachment.content_type is uploader-supplied and frequently lies
    (e.g. declares image/webp on bytes that are actually PNG). Anthropic
    strict-validates media_type vs bytes and 400s on mismatch. Trust the bytes.
    """
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _fetch_image_bytes(
    attachment: discord.Attachment,
) -> tuple[bytes, str] | None:
    """Fetch attachment bytes for the Anthropic vision call.

    Returns (bytes, media_type) or None when the image is too large, unfetchable,
    or in a format the vision endpoint won't accept. Anthropic vision caps at
    5 MB; we enforce that upfront so we never round-trip oversized payloads.
    SVG is rejected because the vision endpoint refuses it (and the bot's
    image-detection layer already filters SVGs out of #fitcheck, see
    fitcheck_streak.is_image).
    """
    if attachment.size > 5 * 1024 * 1024:
        return None
    try:
        data = await attachment.read()
    except (discord.HTTPException, discord.NotFound):
        return None
    declared = (attachment.content_type or "image/jpeg").split(";")[0].strip()
    if declared == "image/svg+xml":
        return None
    sniffed = _sniff_image_type(data)
    return data, sniffed or declared


def _compute_cost(
    model: str,
    plain_in: int,
    out: int,
    cache_read: int,
    cache_write: int,
) -> float:
    """USD per call. Pricing as of plan §5: Sonnet 4.6 in $3 / out $15 /
    cache_write $3.75 / cache_read $0.30 per MTok; Haiku 4.5 in $0.80 /
    out $4 / cache_write $1 / cache_read $0.08. Unknown model → 0 (so the
    cost row stays present but obviously needs operator attention).
    """
    if model.startswith("claude-sonnet"):
        return (
            plain_in * 3.0
            + out * 15.0
            + cache_write * 3.75
            + cache_read * 0.30
        ) / 1_000_000
    if model.startswith("claude-haiku"):
        return (
            plain_in * 0.80
            + out * 4.0
            + cache_write * 1.0
            + cache_read * 0.08
        ) / 1_000_000
    return 0.0


def _audit_skipped(
    org_id: str,
    guild_id: str,
    user_id: str,
    post_id: str,
    invocation_path: str,
    reason: str,
) -> None:
    """Write a single fitcheck_roast_skipped audit row for an error or refusal.

    Used by error paths in generate_roast that never reach the cost-logging
    branch. Refusals (the model returning "pass") get their cost + audit in the
    same connection inside generate_roast itself.
    """
    with get_db() as conn:
        log_audit(
            conn,
            actor="discord:bot:auto",
            action="fitcheck_roast_skipped",
            org_id=org_id,
            entity_id=None,
            detail={
                "guild_id": guild_id,
                "user_id": user_id,
                "post_id": post_id,
                "invocation_path": invocation_path,
                "reason": reason,
            },
            source="sable-roles",
        )


async def generate_roast(
    *,
    org_id: str,
    guild_id: str,
    user_id: str,
    post_id: str,
    image_bytes: bytes,
    media_type: str,
    author_display_name: str,
    invocation_path: str,
    actor_user_id: str | None = None,
    vibe_block: str | None = None,
) -> tuple[str, int] | None:
    """Call Anthropic vision, log cost + audit, return (roast_text, audit_id) or None.

    None covers three cases:
      - Model returned "pass" (refusal per the system prompt).
      - Anthropic BadRequestError (e.g. unsupported media type, content policy).
      - Any other exception during the API call.

    invocation_path widened in R7: {"optin_once", "optin_persist",
    "random_bypass", "mod_roast", "peer_roast", "peer_roast_restored"}.

    actor_user_id (R7) is the invoker — None for opt-in/random/burn-me-style
    paths where there's no distinct invoker, set to a stringified Discord
    user id for mod_roast / peer_roast paths. Stamped in audit detail when
    present so /peer-roast-report can JOIN by both target and actor.

    Returns (roast_text, audit_log_id) on success so R7's peer path can
    JOIN reply audit rows back to the originating roast event. Opt-in /
    random / mod-roast callers that don't need the id discard it with a
    `_` unpack.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    context = f"poster: {author_display_name}\npath: {invocation_path}"

    # Per plan §5.3 POST-AUDIT: vibe rides in the USER-role content (NOT
    # the cached SYSTEM_PROMPT). System block stays static so prompt
    # caching hits across per-user calls; vibe is data, not instruction,
    # so injection here is defused against any imperative content that
    # leaked past the imperative-guard regex.
    user_content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        },
    ]
    if vibe_block:
        user_content.append({"type": "text", "text": vibe_block})
    user_content.append({"type": "text", "text": context})

    try:
        response = await _client().messages.create(
            model=BURN_MODEL,
            max_tokens=120,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": user_content,
                }
            ],
        )
    except BadRequestError as e:
        logger.warning("anthropic BadRequestError for post %s: %s", post_id, e)
        _audit_skipped(
            org_id, guild_id, user_id, post_id, invocation_path,
            reason=f"bad_request:{e}",
        )
        return None
    except Exception as e:  # noqa: BLE001 — any SDK / network exception is a skip.
        logger.warning("anthropic call failed for post %s", post_id, exc_info=e)
        _audit_skipped(
            org_id, guild_id, user_id, post_id, invocation_path,
            reason=f"exception:{type(e).__name__}",
        )
        return None

    in_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    cost = _compute_cost(BURN_MODEL, in_tok, out_tok, cache_read, cache_write)

    roast_text = "".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()
    refused = roast_text.lower() in {"pass", "pass."}

    detail = {
        "guild_id": guild_id,
        "user_id": user_id,
        "post_id": post_id,
        "invocation_path": invocation_path,
        "model": BURN_MODEL,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost_usd": cost,
        "roast_len": len(roast_text),
        "refused": refused,
    }
    if actor_user_id is not None:
        detail["actor_user_id"] = actor_user_id
    detail["vibe_present"] = bool(vibe_block)

    with get_db() as conn:
        log_cost(
            conn,
            org_id=org_id,
            call_type="sable_roles_burn",
            cost_usd=cost,
            model=BURN_MODEL,
            input_tokens=in_tok,
            output_tokens=out_tok,
            call_status="refused" if refused else "success",
        )
        audit_id = log_audit(
            conn,
            actor="discord:bot:auto",
            action="fitcheck_roast_skipped" if refused else "fitcheck_roast_generated",
            org_id=org_id,
            entity_id=None,
            detail=detail,
            source="sable-roles",
        )

    if refused:
        return None
    # Strip a single pair of wrapping quotes if the model added them despite
    # the system prompt saying not to. Leave inner quotes alone.
    cleaned = re.sub(r'^["\']|["\']$', "", roast_text).strip()
    return cleaned, audit_id


def record_roast_reply(
    *,
    audit_log_id: int,
    bot_reply_id: str,
    guild_id: str,
    org_id: str,
    actor_user_id: str | None,
    target_user_id: str,
    post_id: str,
) -> None:
    """Write a `fitcheck_roast_replied` audit row linking the bot's reply
    message id back to the originating `fitcheck_roast_generated` row.

    This is the join key the 🚩 flag handler uses: when a user reacts 🚩
    on a bot-roast reply, the handler looks up THIS audit row by
    `bot_reply_id` to find the target/actor metadata. Without this row
    the flag is silently ignored (matches plan §8.2 — only peer/restored
    roasts produce flag-eligible audit trails).

    Opt-in / random / mod paths do NOT call this — only peer-roast +
    streak-restored peer-roast paths produce flag-trackable replies per
    plan §8.2 ("opt-in/random replies will not appear there → flag
    silently ignored").
    """
    with get_db() as conn:
        log_audit(
            conn,
            actor="discord:bot:auto",
            action="fitcheck_roast_replied",
            org_id=org_id,
            entity_id=None,
            detail={
                "audit_log_id": audit_log_id,
                "bot_reply_id": bot_reply_id,
                "guild_id": guild_id,
                "actor_user_id": actor_user_id,
                "target_user_id": target_user_id,
                "post_id": post_id,
            },
            source="sable-roles",
        )


# --- B6: on_message integration + random inner-circle bypass ---


def _is_inner_circle(member: discord.Member, guild_id: str) -> bool:
    """True if member is in the role-allowlist OR user-id-allowlist for the guild.

    Inline import of the config dicts so tests that monkeypatch them on the
    config module pick up the change without needing to also patch this module.
    """
    from sable_roles.config import INNER_CIRCLE_ROLES, INNER_CIRCLE_USERS

    inner_role_ids = {str(rid) for rid in INNER_CIRCLE_ROLES.get(guild_id, [])}
    inner_user_ids = {str(uid) for uid in INNER_CIRCLE_USERS.get(guild_id, [])}
    if str(member.id) in inner_user_ids:
        return True
    if not inner_role_ids:
        return False
    member_role_ids = {str(role.id) for role in member.roles}
    return bool(member_role_ids & inner_role_ids)


def _is_image_for_roast(att: discord.Attachment) -> bool:
    """Mirror fitcheck_streak.is_image, then double-exclude SVG.

    The base `is_image` already excludes `image/svg+xml` via content_type, but
    its extension fallback could let a `.svg` slip through when content_type is
    missing or generic. Anthropic's vision endpoint rejects SVG outright, so we
    guard belt-and-suspenders here.
    """
    from sable_roles.features.fitcheck_streak import is_image

    if not is_image(att):
        return False
    return not (att.content_type or "").startswith("image/svg")


async def maybe_roast(
    message: discord.Message,
    org_id: str,
    guild_id: str,
) -> None:
    """Decide whether to roast this image post; if so, generate + reply.

    Order of gates:
      0) Sticky stop-pls blocklist (R4) — silent skip BEFORE any other DB
         read. User has already consented to be ignored; no audit row.
      1) Daily cap (covers opt-in + random equally).
      2) Opt-in path — consumes 'once' rows, leaves 'persist' in place.
      3) Random inner-circle bypass — only when no opt-in.
      4) Image fetch (oversize/SVG/unfetchable → skip).
      5) generate_roast (refusal/error → skip; audit written by callee).
      6) message.reply with mention_author=False so the ping is suppressed.

    Fire-and-forget from on_message via asyncio.create_task. The race where
    two near-simultaneous posts both see the same 'once' opt-in is documented
    in plan §6 and accepted for V1.
    """
    user_id = str(message.author.id)

    with get_db() as conn:
        # never-mode short-circuit. Auto-fire path (opt-in consume +
        # random inner-circle bypass) ALL halt. Mod /roast (right-click)
        # remains the only roast path while locked down.
        if is_burn_mode_never(conn, guild_id):
            return
        if discord_roast.is_blocklisted(conn, guild_id, user_id):
            return
        if discord_burn.count_roasts_today(conn, guild_id, user_id) >= BURN_DAILY_CAP_PER_USER:
            logger.info(
                "burn-me daily cap hit for user %s in guild %s", user_id, guild_id
            )
            return

    invocation_path: str | None = None
    with get_db() as conn:
        mode = discord_burn.consume_optin_if_present(conn, guild_id, user_id)
    if mode is not None:
        invocation_path = f"optin_{mode}"
    elif _is_inner_circle(message.author, guild_id):
        with get_db() as conn:
            if not discord_burn.was_recently_random_roasted(
                conn, guild_id, user_id, within_days=BURN_RANDOM_DEDUP_DAYS
            ):
                if random.random() < BURN_RANDOM_PROB:
                    discord_burn.log_random_roast(conn, guild_id, user_id)
                    invocation_path = "random_bypass"

    if invocation_path is None:
        return

    attachment = next(
        (a for a in message.attachments if _is_image_for_roast(a)), None
    )
    if attachment is None:
        return
    fetched = await _fetch_image_bytes(attachment)
    if fetched is None:
        return
    image_bytes, media_type = fetched

    result = await generate_roast(
        org_id=org_id,
        guild_id=guild_id,
        user_id=user_id,
        post_id=str(message.id),
        image_bytes=image_bytes,
        media_type=media_type,
        author_display_name=message.author.display_name,
        invocation_path=invocation_path,
    )
    if result is None:
        return  # refusal or error — generate_roast already audited it
    roast, _audit_id = result  # opt-in/random paths don't need the audit id

    try:
        await message.reply(roast, mention_author=False)
    except discord.HTTPException as exc:
        logger.warning(
            "burn reply failed for post %s", message.id, exc_info=exc
        )
