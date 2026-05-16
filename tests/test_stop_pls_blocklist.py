"""R4 tests: /stop-pls now writes to discord_burn_blocklist + purges retained
personalization data (discord_user_vibes, discord_user_observations,
discord_message_observations) on top of the existing opt-out delete.

Covers plan §0.3 (privacy: opt-out actively purges) and §13 R4 (sticky
blocklist + idempotent re-stop-pls). Existing B4-era /stop-pls tests live
in test_burn_me_state.py; this file scopes strictly to the R4 additions.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from sable_platform.db import discord_burn
from sable_roles.features import burn_me as bm
from sable_roles.features import fitcheck_streak as fcs

from tests.conftest import fetch_audit_rows


# Reset the SHARED cross-module cooldown via .clear() (NOT rebind). Rebinding
# severs the identity that roast.py imports by reference. See R7's
# test_roast_peer_path.py for the cross-feature contract this preserves.
@pytest.fixture(autouse=True)
def _reset_burn_cooldown():
    bm._burn_invoke_cooldown.clear()
    yield
    bm._burn_invoke_cooldown.clear()


def _make_interaction(
    *,
    guild_id: int | None,
    user_id: int,
    user_role_ids: list[int] | None = None,
):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = [SimpleNamespace(id=rid) for rid in (user_role_ids or [])]
    interaction.user = member
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_dm_interaction(*, user_id: int):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = None
    user = MagicMock(spec=discord.User)
    user.id = user_id
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _register_and_get_stop_pls(monkeypatch, db_conn):
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())
    monkeypatch.setattr(fcs, "MOD_ROLES", {"100": ["999"]})

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    bm.register_commands(tree)
    stop_cmd = tree.get_command("stop-pls")
    assert stop_cmd is not None
    return stop_cmd


def _count_blocklist(db_conn, guild_id: str, user_id: str) -> int:
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_burn_blocklist"
        f" WHERE guild_id='{guild_id}' AND user_id='{user_id}'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    return rd["n"]


_seed_message_counter = 0


def _seed_message_observation(db_conn, guild_id: str, user_id: str) -> None:
    """Insert a discord_message_observations row. UNIQUE(guild_id, message_id)
    requires a unique message_id per call within the same guild."""
    global _seed_message_counter
    _seed_message_counter += 1
    db_conn.execute(
        "INSERT INTO discord_message_observations"
        " (guild_id, channel_id, message_id, user_id, content_truncated,"
        " posted_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            guild_id, "c1", f"m-{user_id}-{_seed_message_counter}",
            user_id, "hi", "2026-05-01T00:00:00Z",
        ),
    )
    db_conn.commit()


def _seed_user_observation(db_conn, guild_id: str, user_id: str) -> int:
    """Insert a discord_user_observations row and return its id (FK target for vibes)."""
    db_conn.execute(
        "INSERT INTO discord_user_observations"
        " (guild_id, user_id, window_start, window_end, message_count)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            guild_id, user_id,
            "2026-04-01T00:00:00Z", "2026-05-01T00:00:00Z", 5,
        ),
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT id FROM discord_user_observations"
        f" WHERE guild_id='{guild_id}' AND user_id='{user_id}'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    return rd["id"]


def _seed_user_vibe(db_conn, guild_id: str, user_id: str, observation_id: int) -> None:
    db_conn.execute(
        "INSERT INTO discord_user_vibes"
        " (guild_id, user_id, vibe_block_text, identity, tone,"
        " inferred_at, source_observation_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id, user_id, "<user_vibe>\nidentity: y2k\n</user_vibe>",
            "y2k", "ironic", "2026-05-01T00:00:00Z", observation_id,
        ),
    )
    db_conn.commit()


# --- Happy path: opt-in → /stop-pls writes blocklist + audit ---


@pytest.mark.asyncio
async def test_stop_pls_writes_blocklist_row_and_audit(monkeypatch, db_conn):
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)
    # Seed an opt-in to exercise the full chain.
    discord_burn.opt_in(db_conn, "100", "555", "persist", opted_in_by="555")

    interaction = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(interaction)

    assert _count_blocklist(db_conn, "100", "555") == 1

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_burn_blocklist_added"
    ]
    assert len(audits) == 1
    assert audits[0]["actor"] == "discord:user:555"
    assert audits[0]["org_id"] == "solstitch"
    assert audits[0]["source"] == "sable-roles"
    detail = json.loads(audits[0]["detail_json"])
    assert detail["guild_id"] == "100"
    assert detail["user_id"] == "555"
    assert detail["blocklist_was_new"] is True
    # purge_counts must be a dict with the three personalization tables.
    assert set(detail["purge_counts"].keys()) == {
        "discord_user_vibes",
        "discord_user_observations",
        "discord_message_observations",
    }


@pytest.mark.asyncio
async def test_stop_pls_second_call_is_idempotent_no_new_audit(monkeypatch, db_conn):
    """User runs /stop-pls twice. Second call: opt_out finds nothing,
    insert_blocklist returns False, NO new fitcheck_burn_blocklist_added row."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)

    first = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(first)
    second = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(second)

    # Still exactly one blocklist row (UNIQUE constraint + ON CONFLICT DO NOTHING).
    assert _count_blocklist(db_conn, "100", "555") == 1

    blocklist_audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_burn_blocklist_added"
    ]
    assert len(blocklist_audits) == 1  # only the first call audited

    args, _ = second.followup.send.call_args
    assert "already" in args[0].lower()


@pytest.mark.asyncio
async def test_stop_pls_purges_vibes_and_observations(monkeypatch, db_conn):
    """Seed observation + vibe rows then run /stop-pls; both tables empty after."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)

    observation_id = _seed_user_observation(db_conn, "100", "555")
    _seed_user_vibe(db_conn, "100", "555", observation_id)
    _seed_message_observation(db_conn, "100", "555")

    # Pre-condition sanity.
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_user_vibes WHERE user_id='555'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 1
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_user_observations WHERE user_id='555'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 1
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations WHERE user_id='555'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 1

    interaction = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(interaction)

    # All three personalization tables wiped for this (guild, user).
    for table in (
        "discord_user_vibes",
        "discord_user_observations",
        "discord_message_observations",
    ):
        row = db_conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE user_id='555'"
        ).fetchone()
        assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0, (
            f"expected zero rows in {table} after /stop-pls"
        )


@pytest.mark.asyncio
async def test_stop_pls_purge_counts_surfaced_in_audit_detail(monkeypatch, db_conn):
    """purge_user_personalization_data returns per-table counts; audit detail
    must carry them so /peer-roast-report and future privacy queries can
    confirm what was wiped."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)

    observation_id = _seed_user_observation(db_conn, "100", "555")
    _seed_user_vibe(db_conn, "100", "555", observation_id)
    _seed_message_observation(db_conn, "100", "555")
    _seed_message_observation(db_conn, "100", "555")

    interaction = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(interaction)

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_burn_blocklist_added"
    ]
    detail = json.loads(audits[0]["detail_json"])
    counts = detail["purge_counts"]
    assert counts["discord_user_vibes"] == 1
    assert counts["discord_user_observations"] == 1
    assert counts["discord_message_observations"] == 2


@pytest.mark.asyncio
async def test_stop_pls_purge_only_target_user_isolated(monkeypatch, db_conn):
    """/stop-pls for user A does not delete user B's personalization rows."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)

    obs_a = _seed_user_observation(db_conn, "100", "555")
    _seed_user_vibe(db_conn, "100", "555", obs_a)
    obs_b = _seed_user_observation(db_conn, "100", "777")
    _seed_user_vibe(db_conn, "100", "777", obs_b)

    interaction = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(interaction)

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_user_vibes WHERE user_id='555'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_user_vibes WHERE user_id='777'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 1


@pytest.mark.asyncio
async def test_stop_pls_blocklist_isolated_per_guild(monkeypatch, db_conn):
    """Blocklist row for guild A doesn't appear in guild B."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch", "200": "solstitch"})

    interaction = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(interaction)

    assert _count_blocklist(db_conn, "100", "555") == 1
    assert _count_blocklist(db_conn, "200", "555") == 0


@pytest.mark.asyncio
async def test_stop_pls_unconfigured_guild_bounces_no_db_writes(monkeypatch, db_conn):
    """Unconfigured guild bounce fires BEFORE blocklist+purge — protects against
    rogue cross-org calls. Verifies the early-return defense unchanged from B4."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)

    interaction = _make_interaction(guild_id=999, user_id=555)
    await stop_cmd.callback(interaction)

    assert _count_blocklist(db_conn, "999", "555") == 0
    blocklist_audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_burn_blocklist_added"
    ]
    assert blocklist_audits == []

    args, _ = interaction.followup.send.call_args
    assert "not configured" in args[0].lower()


@pytest.mark.asyncio
async def test_stop_pls_dm_context_bounces_no_db_writes(monkeypatch, db_conn):
    """DM invocation (no guild_id) bounces BEFORE blocklist+purge."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)

    interaction = _make_dm_interaction(user_id=555)
    await stop_cmd.callback(interaction)

    # No blocklist row anywhere.
    row = db_conn.execute("SELECT COUNT(*) AS n FROM discord_burn_blocklist").fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0

    blocklist_audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_burn_blocklist_added"
    ]
    assert blocklist_audits == []

    args, _ = interaction.followup.send.call_args
    assert "not configured" in args[0].lower()


@pytest.mark.asyncio
async def test_stop_pls_opt_out_and_blocklist_audit_co_exist(monkeypatch, db_conn):
    """When user had an opt-in row, both audit actions fire:
    fitcheck_burn_optout (opt-in removed) AND fitcheck_burn_blocklist_added (new block).
    Verifies the two audit paths don't shadow each other."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)
    discord_burn.opt_in(db_conn, "100", "555", "once", opted_in_by="555")

    interaction = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(interaction)

    actions = sorted(
        a["action"] for a in fetch_audit_rows(db_conn)
        if a["action"] in {"fitcheck_burn_optout", "fitcheck_burn_blocklist_added"}
    )
    assert actions == ["fitcheck_burn_blocklist_added", "fitcheck_burn_optout"]


@pytest.mark.asyncio
async def test_stop_pls_purge_counts_zero_when_no_personalization_data(
    monkeypatch, db_conn
):
    """User with no prior observations/vibes: purge_counts all zero, blocklist
    still landed + audit still written."""
    stop_cmd = _register_and_get_stop_pls(monkeypatch, db_conn)

    interaction = _make_interaction(guild_id=100, user_id=555)
    await stop_cmd.callback(interaction)

    audits = [
        a for a in fetch_audit_rows(db_conn)
        if a["action"] == "fitcheck_burn_blocklist_added"
    ]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["purge_counts"] == {
        "discord_user_vibes": 0,
        "discord_user_observations": 0,
        "discord_message_observations": 0,
    }
    assert _count_blocklist(db_conn, "100", "555") == 1
