"""Tests for /my-roasts + the lazy-grant seam _maybe_grant_monthly_token (R6).

R6 ships:
  * `_maybe_grant_monthly_token(conn, guild_id, actor_user_id) -> bool` — pure
    seam R7's peer-/roast handler will reuse verbatim.
  * `/my-roasts` ephemeral slash command + its underlying handler
    `_handle_my_roasts(interaction)`.
  * `_format_my_roasts(...)` — pure renderer split out for direct testing.

NOT in scope: consume/refund, peer routing, 🚩 detection, streak restoration
grant (R7/R8). Lazy-grant in R6 ONLY ever calls grant_monthly_token (never
grant_restoration_token).
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_platform.db import discord_roast
from sable_roles.features import roast

from tests.conftest import fetch_audit_rows


# ---------------------------------------------------------------------------
# Fixture helpers (mirroring R5's test_roast_mod_path.py for consistency)
# ---------------------------------------------------------------------------


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _make_member(*, user_id: int, role_ids: list[int] | None = None) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    roles = []
    for rid in role_ids or []:
        role = MagicMock(spec=discord.Role)
        role.id = rid
        roles.append(role)
    member.roles = roles
    return member


def _make_interaction(
    *,
    guild_id: int | None = 100,
    user_id: int = 999,
    in_dm: bool = False,
    user_is_member: bool = True,
    role_ids: list[int] | None = None,
) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    if in_dm:
        interaction.guild_id = None
        interaction.guild = None
    else:
        interaction.guild_id = guild_id
        interaction.guild = MagicMock()
    if user_is_member:
        user = _make_member(user_id=user_id, role_ids=role_ids)
    else:
        user = MagicMock(spec=discord.User)
        user.id = user_id
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _last_followup(interaction: MagicMock) -> str:
    """Return lowercased body of last followup.send + pin ephemeral=True on
    EVERY call (mirrors R5's contract — gate state must never leak public)."""
    for call in interaction.followup.send.call_args_list:
        assert call.kwargs.get("ephemeral") is True, (
            f"non-ephemeral followup.send call: {call}"
        )
    args, _ = interaction.followup.send.call_args
    return args[0].lower()


def _last_followup_raw(interaction: MagicMock) -> str:
    """Same as _last_followup but case-preserving (for rules-footer assertions
    that include uppercase/lowercase variants like @Stitch)."""
    for call in interaction.followup.send.call_args_list:
        assert call.kwargs.get("ephemeral") is True
    args, _ = interaction.followup.send.call_args
    return args[0]


@pytest.fixture
def patched_roast(monkeypatch, db_conn):
    monkeypatch.setattr(roast, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(roast, "PEER_ROAST_ROLES", {"100": [777]})
    monkeypatch.setattr(roast, "PERSONALIZE_ADMINS", {})
    return roast


def _register_and_get_my_roasts(patched_roast):
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    patched_roast.register_commands(tree, client=client)
    cmd = tree.get_command("my-roasts")
    return cmd, tree


# ---------------------------------------------------------------------------
# Tree registration
# ---------------------------------------------------------------------------


def test_register_commands_installs_my_roasts_slash(patched_roast):
    cmd, tree = _register_and_get_my_roasts(patched_roast)
    assert cmd is not None
    # /my-roasts is a slash command, NOT a context menu.
    assert not isinstance(cmd, app_commands.ContextMenu)
    assert cmd.name == "my-roasts"
    # User-facing surface; must not carry an "(admins)" label like
    # /set-personalize-mode does. Defends against accidental copy-paste of
    # the admin command's description string.
    assert "admins" not in (cmd.description or "").lower()
    slash_names = {c.name for c in tree.get_commands()}
    assert "my-roasts" in slash_names


# ---------------------------------------------------------------------------
# Bounce gates — no DB writes, no audit rows, no token grants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_context_bounces_no_db_writes(patched_roast, db_conn):
    interaction = _make_interaction(in_dm=True)
    await roast._handle_my_roasts(interaction)
    assert "inside a server" in _last_followup(interaction)
    # No token grant landed.
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_unconfigured_guild_bounces_no_db_writes(patched_roast, db_conn):
    """Guild present but not in GUILD_TO_ORG → bounce. Critically, no token
    grant row appears (lazy grant must fire ONLY for configured guilds)."""
    interaction = _make_interaction(guild_id=999)
    await roast._handle_my_roasts(interaction)
    assert "not configured" in _last_followup(interaction)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0
    assert fetch_audit_rows(db_conn) == []


@pytest.mark.asyncio
async def test_non_member_user_bounces(patched_roast, db_conn):
    """interaction.user is a discord.User (cross-guild shape) → bounce."""
    interaction = _make_interaction(user_is_member=False)
    await roast._handle_my_roasts(interaction)
    assert "inside the server" in _last_followup(interaction)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


# ---------------------------------------------------------------------------
# Lazy-grant behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_grant_fires_on_first_call(patched_roast, db_conn):
    """First /my-roasts of the month grants a token + writes a single audit
    row. Body advertises the fresh-token line."""
    # Precondition: zero tokens.
    n_before = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
        " WHERE guild_id='100' AND actor_user_id='999'"
    ).fetchone()
    assert dict(n_before._mapping if hasattr(n_before, "_mapping") else n_before)["n"] == 0

    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)

    # One token row landed.
    n_after = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
        " WHERE guild_id='100' AND actor_user_id='999'"
    ).fetchone()
    assert dict(n_after._mapping if hasattr(n_after, "_mapping") else n_after)["n"] == 1

    # One audit row landed: fitcheck_peer_roast_token_granted with monthly source.
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["actor_user_id"] == "999"
    assert detail["guild_id"] == "100"
    assert detail["source"] == "monthly"
    assert detail["year_month"] == discord_roast._current_year_month()
    assert audits[0]["actor"] == "discord:user:999"
    assert audits[0]["org_id"] == "solstitch"
    assert audits[0]["source"] == "sable-roles"

    # Body advertises the fresh token.
    assert "fresh token granted" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_lazy_grant_is_idempotent_same_month(patched_roast, db_conn):
    """Second invocation in the same calendar month grants nothing new — no
    second token row, no second audit row, and the body OMITS the fresh-grant
    line so the user doesn't see "fresh token granted" on every call."""
    interaction1 = _make_interaction()
    await roast._handle_my_roasts(interaction1)
    # Sanity: first call did grant.
    assert "fresh token granted" in _last_followup(interaction1)

    interaction2 = _make_interaction()
    await roast._handle_my_roasts(interaction2)

    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_peer_roast_tokens"
        " WHERE guild_id='100' AND actor_user_id='999'"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 1

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 1  # still ONE total — second call no-op'd

    assert "fresh token granted" not in _last_followup(interaction2)


@pytest.mark.asyncio
async def test_lazy_grant_fires_again_in_next_month(monkeypatch, patched_roast, db_conn):
    """Patch _current_year_month to simulate a new calendar month — second
    grant lands with the new year_month."""
    # First call in May.
    monkeypatch.setattr(
        discord_roast, "_current_year_month", lambda as_of_utc=None: "2026-05"
    )
    monkeypatch.setattr(roast, "_current_year_month", lambda as_of_utc=None: "2026-05")
    interaction1 = _make_interaction()
    await roast._handle_my_roasts(interaction1)

    # Bump to June.
    monkeypatch.setattr(
        discord_roast, "_current_year_month", lambda as_of_utc=None: "2026-06"
    )
    monkeypatch.setattr(roast, "_current_year_month", lambda as_of_utc=None: "2026-06")
    interaction2 = _make_interaction()
    await roast._handle_my_roasts(interaction2)

    rows = db_conn.execute(
        "SELECT year_month FROM discord_peer_roast_tokens"
        " WHERE guild_id='100' AND actor_user_id='999'"
        " ORDER BY year_month ASC"
    ).fetchall()
    months = [dict(r._mapping if hasattr(r, "_mapping") else r)["year_month"] for r in rows]
    assert months == ["2026-05", "2026-06"]

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 2
    ym_set = {json.loads(a["detail_json"])["year_month"] for a in audits}
    assert ym_set == {"2026-05", "2026-06"}


# ---------------------------------------------------------------------------
# Body rendering — tokens_left, streak, last roast, role gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tokens_left_rendering_counts_monthly_plus_restoration(
    patched_roast, db_conn
):
    """Seed BOTH a monthly + restoration token unspent → body shows 2."""
    ym = discord_roast._current_year_month()
    discord_roast.grant_monthly_token(db_conn, "100", "999", year_month=ym)
    discord_roast.grant_restoration_token(db_conn, "100", "999", year_month=ym)

    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)

    body = _last_followup(interaction)
    assert "tokens left this month: 2" in body
    # No "fresh token granted" line — pre-seeded grant landed before handler.
    assert "fresh token granted" not in body


@pytest.mark.asyncio
async def test_streak_progress_rendering_3_of_7(monkeypatch, patched_roast, db_conn):
    """compute_streak_state → current_streak=3 must render "3/7 days"."""
    monkeypatch.setattr(
        roast.discord_streaks,
        "compute_streak_state",
        lambda conn, org_id, user_id: {"current_streak": 3},
    )
    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    assert "3/7 days" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_streak_progress_caps_at_7(monkeypatch, patched_roast, db_conn):
    """current_streak=12 must render capped at 7/7 (no inflated brag)."""
    monkeypatch.setattr(
        roast.discord_streaks,
        "compute_streak_state",
        lambda conn, org_id, user_id: {"current_streak": 12},
    )
    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    assert "7/7 days" in _last_followup(interaction)
    assert "12/7 days" not in _last_followup(interaction)


@pytest.mark.asyncio
async def test_monthly_reset_date_is_first_of_next_month(
    monkeypatch, patched_roast, db_conn
):
    """Freeze 'now' via the seam helper and assert the reset line shows the
    first day of the NEXT calendar month UTC."""
    fixed_today = date(2026, 5, 16)

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            class _N:
                def date(self_inner):
                    return fixed_today
            return _N()

    monkeypatch.setattr(roast, "datetime", _FrozenDateTime)

    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    body = _last_followup(interaction)
    assert "monthly reset: 2026-06-01" in body


@pytest.mark.asyncio
async def test_monthly_reset_date_rolls_over_year(
    monkeypatch, patched_roast, db_conn
):
    """December → next year January 1st."""
    fixed_today = date(2026, 12, 5)

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            class _N:
                def date(self_inner):
                    return fixed_today
            return _N()

    monkeypatch.setattr(roast, "datetime", _FrozenDateTime)

    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    assert "monthly reset: 2027-01-01" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_last_roast_none_yet(patched_roast, db_conn):
    """No consumed token → "none yet"."""
    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    assert "last roast cast: none yet" in _last_followup(interaction)


@pytest.mark.asyncio
async def test_last_roast_renders_date_and_target(patched_roast, db_conn):
    """Seed a granted+consumed token → body shows the consumed_at date and
    target_user_id on the last-roast line."""
    ym = discord_roast._current_year_month()
    discord_roast.grant_monthly_token(db_conn, "100", "999", year_month=ym)
    tok = discord_roast.available_token(
        db_conn, "100", "999", year_month=ym
    )
    discord_roast.consume_token(
        db_conn, tok["id"], target_user_id="555", post_id="post_xyz"
    )

    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    body = _last_followup(interaction)
    # consumed_at is iso-seconds; helper renders first 10 chars (YYYY-MM-DD).
    consumed_at_row = db_conn.execute(
        "SELECT consumed_at FROM discord_peer_roast_tokens WHERE id = ?",
        (tok["id"],),
    ).fetchone()
    expected_date = dict(
        consumed_at_row._mapping if hasattr(consumed_at_row, "_mapping") else consumed_at_row
    )["consumed_at"][:10]
    assert f"last roast cast: {expected_date} on user 555" in body


@pytest.mark.asyncio
async def test_role_gate_hint_appears_when_tokens_and_no_role(
    patched_roast, db_conn
):
    """Seed 1 unspent token, user lacks PEER_ROAST_ROLES role → hint fires."""
    ym = discord_roast._current_year_month()
    discord_roast.grant_monthly_token(db_conn, "100", "999", year_month=ym)

    # User has role 222, NOT 777 (the peer-roast role for guild 100).
    interaction = _make_interaction(role_ids=[222])
    await roast._handle_my_roasts(interaction)
    body = _last_followup_raw(interaction)
    assert "need the @Stitch role" in body


@pytest.mark.asyncio
async def test_role_gate_hint_suppressed_when_user_has_role(
    patched_roast, db_conn
):
    """User HOLDS PEER_ROAST_ROLES role → no role-gate hint, even with tokens."""
    ym = discord_roast._current_year_month()
    discord_roast.grant_monthly_token(db_conn, "100", "999", year_month=ym)

    interaction = _make_interaction(role_ids=[777])  # 777 is in PEER_ROAST_ROLES
    await roast._handle_my_roasts(interaction)
    body = _last_followup_raw(interaction)
    assert "need the @Stitch role" not in body


@pytest.mark.asyncio
async def test_role_gate_hint_suppressed_when_zero_tokens(
    patched_roast, db_conn
):
    """Zero tokens AND no role → hint must STILL be suppressed (telling
    someone with nothing to cast that they "need a role to cast" is noise)."""
    # No tokens seeded — but lazy grant will fire. To get a true zero-tokens
    # state, seed an already-spent token so count_available_tokens returns 0.
    # The lazy grant ON CONFLICT will no-op since a row already exists.
    ym = discord_roast._current_year_month()
    discord_roast.grant_monthly_token(db_conn, "100", "999", year_month=ym)
    tok = discord_roast.available_token(db_conn, "100", "999", year_month=ym)
    discord_roast.consume_token(
        db_conn, tok["id"], target_user_id="555", post_id="p"
    )

    # User lacks the role.
    interaction = _make_interaction(role_ids=[222])
    await roast._handle_my_roasts(interaction)
    body = _last_followup_raw(interaction)
    assert "need the @Stitch role" not in body
    assert "tokens left this month: 0" in body.lower()


# ---------------------------------------------------------------------------
# Rules footer + seam load-bearing test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rules_footer_lines_present(patched_roast, db_conn):
    """All 5 footer lines must appear regardless of branch."""
    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    body = _last_followup(interaction)
    assert "peer /roast lets you cast 1 burn per calendar month" in body
    assert "hit a 7-day streak to earn a bonus restoration token" in body
    assert "🚩 react to flag mods" in body
    assert "sticky stop-pls protects" in body
    assert "/my-roasts grants your monthly token" in body


@pytest.mark.asyncio
async def test_handle_my_roasts_seam_invokable_directly(patched_roast, db_conn):
    """The private `_handle_my_roasts` seam is invoked directly here (not via
    the slash callback) so a future refactor that dead-code-removes the seam
    will fail this test. Closes the R3 follow-up #2 gap — mirrors R5's
    test_handle_mod_roast_seam_invokable_directly pattern."""
    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    # Body landed (any non-empty ephemeral response counts).
    body = _last_followup(interaction)
    assert "your peer-roast status" in body


@pytest.mark.asyncio
async def test_response_deferred_ephemeral(patched_roast, db_conn):
    """defer(ephemeral=True) must fire before any followup."""
    interaction = _make_interaction()
    await roast._handle_my_roasts(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


# ---------------------------------------------------------------------------
# _format_my_roasts pure-function tests (bypass interaction entirely)
# ---------------------------------------------------------------------------


def test_format_my_roasts_zero_tokens_no_role():
    body = roast._format_my_roasts(
        tokens_left=0,
        peer_eligible=False,
        current_streak=0,
        reset_date="2026-06-01",
        last_consumed=None,
        just_granted=False,
    )
    assert "tokens left this month: 0" in body
    assert "0/7 days" in body
    assert "monthly reset: 2026-06-01" in body
    assert "none yet" in body
    assert "fresh token granted" not in body
    assert "need the @Stitch role" not in body


def test_format_my_roasts_role_gate_line_when_unprivileged_with_tokens():
    body = roast._format_my_roasts(
        tokens_left=1,
        peer_eligible=False,
        current_streak=2,
        reset_date="2026-06-01",
        last_consumed=None,
        just_granted=False,
    )
    assert "need the @Stitch role" in body


def test_format_my_roasts_no_role_gate_when_peer_eligible():
    body = roast._format_my_roasts(
        tokens_left=1,
        peer_eligible=True,
        current_streak=2,
        reset_date="2026-06-01",
        last_consumed=None,
        just_granted=False,
    )
    assert "need the @Stitch role" not in body


def test_format_my_roasts_fresh_token_line():
    body = roast._format_my_roasts(
        tokens_left=1,
        peer_eligible=True,
        current_streak=0,
        reset_date="2026-06-01",
        last_consumed=None,
        just_granted=True,
    )
    assert "fresh token granted" in body


# ---------------------------------------------------------------------------
# _maybe_grant_monthly_token seam — direct unit tests
# ---------------------------------------------------------------------------


def test_maybe_grant_monthly_token_returns_true_on_fresh_grant(patched_roast, db_conn):
    granted = roast._maybe_grant_monthly_token(db_conn, "100", "999")
    assert granted is True
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 1
    assert audits[0]["actor"] == "discord:user:999"
    detail = json.loads(audits[0]["detail_json"])
    assert detail["source"] == "monthly"
    assert detail["guild_id"] == "100"
    assert detail["actor_user_id"] == "999"


def test_maybe_grant_monthly_token_returns_false_when_already_granted(
    patched_roast, db_conn
):
    assert roast._maybe_grant_monthly_token(db_conn, "100", "999") is True
    assert roast._maybe_grant_monthly_token(db_conn, "100", "999") is False
    # Audit row count stayed at 1 — no double-audit on the no-op grant.
    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_peer_roast_token_granted"
    ]
    assert len(audits) == 1
