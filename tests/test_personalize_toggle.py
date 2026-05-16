"""Tests for the /set-personalize-mode admin-gated slash command (R3).

R3 ships ONE command. Gate is PERSONALIZE_ADMINS user-ID allowlist (not
MOD_ROLES) per plan §0.3 — the toggle is intentionally narrower than mod.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands

from sable_roles.features import roast


def _make_interaction(
    *,
    guild_id: int | None,
    user_id: int,
    in_dm: bool = False,
):
    """Build a discord.Interaction double for the personalize-mode handler.

    `in_dm=True` zeroes both `guild` and `guild_id` (DM context — Discord
    sends both None together). Otherwise a Guild stub is attached so the
    handler's `interaction.guild is None` branch falls through.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = None if in_dm else guild_id
    interaction.guild = None if in_dm else MagicMock()
    user = MagicMock(spec=discord.Member)
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


def _register_and_get(monkeypatch, db_conn):
    """Wire roast.register_commands against a real CommandTree + return the cmd."""
    monkeypatch.setattr(roast, "PERSONALIZE_ADMINS", {"100": ["555"]})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    roast.register_commands(tree, client=client)
    cmd = tree.get_command("set-personalize-mode")
    assert cmd is not None
    return cmd, tree


# ---------------------------------------------------------------------------
# Tree registration
# ---------------------------------------------------------------------------


def test_register_commands_registers_set_personalize_mode(monkeypatch, db_conn):
    cmd, tree = _register_and_get(monkeypatch, db_conn)
    assert cmd.name == "set-personalize-mode"
    assert "(admins)" in (cmd.description or "")
    names = {c.name for c in tree.get_commands()}
    assert "set-personalize-mode" in names


def test_register_commands_accepts_client_kwarg(monkeypatch, db_conn):
    """register_commands(tree, *, client) — `client` is kwargs-only.

    Locks the signature so R5+ (when it actually starts using `client` for
    context-menu commands) doesn't quietly drift to positional or rename.
    """
    monkeypatch.setattr(roast, "PERSONALIZE_ADMINS", {})
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    with pytest.raises(TypeError):
        roast.register_commands(tree, client)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Admin happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_flips_personalize_mode_on(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555)

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    row = db_conn.execute(
        "SELECT personalize_mode_on, updated_by"
        " FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    assert row is not None
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["personalize_mode_on"] == 1
    assert rd["updated_by"] == "555"

    interaction.followup.send.assert_awaited()
    # Two sends are allowed (defer + final) but the final send confirms ON.
    last_call = interaction.followup.send.call_args_list[-1]
    args, kwargs = last_call
    assert "on" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_can_toggle_on_off_on(monkeypatch, db_conn):
    """on → off → on: each call invokes the SP helper once with the right
    kwargs and the DB row reflects the latest value at every step."""
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555)

    with patch(
        "sable_roles.features.roast.discord_guild_config.set_personalize_mode"
    ) as spm:
        await cmd.callback(
            interaction, app_commands.Choice(name="on", value="on")
        )
        await cmd.callback(
            interaction, app_commands.Choice(name="off", value="off")
        )
        await cmd.callback(
            interaction, app_commands.Choice(name="on", value="on")
        )

    assert spm.call_count == 3
    call_kwargs = [c.kwargs for c in spm.call_args_list]
    assert [ck["on"] for ck in call_kwargs] == [True, False, True]
    assert {ck["guild_id"] for ck in call_kwargs} == {"100"}
    assert {ck["updated_by"] for ck in call_kwargs} == {"555"}


@pytest.mark.asyncio
async def test_admin_flips_personalize_mode_off(monkeypatch, db_conn):
    """Pre-seed personalize_mode_on=1, then verify OFF flips it back to 0
    and the audit trail (R3 audit-inside-helper contract) shows both events."""
    from sable_platform.db.discord_guild_config import set_personalize_mode

    set_personalize_mode(
        db_conn, guild_id="100", on=True, updated_by="seed_admin"
    )

    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555)
    await cmd.callback(interaction, app_commands.Choice(name="off", value="off"))

    row = db_conn.execute(
        "SELECT personalize_mode_on, updated_by"
        " FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["personalize_mode_on"] == 0
    assert rd["updated_by"] == "555"

    # Audit trail proves SP helper actually ran both times (R3 contract:
    # audit row is written inside set_personalize_mode, not by this handler).
    rows = db_conn.execute(
        "SELECT actor, detail_json FROM audit_log"
        " WHERE action='fitcheck_personalize_mode_set' ORDER BY id ASC"
    ).fetchall()
    audits = [dict(r._mapping if hasattr(r, "_mapping") else r) for r in rows]
    assert len(audits) == 2
    assert audits[0]["actor"] == "discord:user:seed_admin"
    assert audits[1]["actor"] == "discord:user:555"


# ---------------------------------------------------------------------------
# Gate behavior — non-admin / unconfigured / DM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_denied_no_db_write(monkeypatch, db_conn):
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=999)  # not in allowlist

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0

    # Strict-zero on the WHOLE audit_log table — guards against a future buggy
    # implementation writing a different audit action on bounce (e.g.
    # `fitcheck_personalize_bounce`). Plan §0.3 says bounces stay silent.
    audits_total = db_conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log"
    ).fetchone()
    assert dict(
        audits_total._mapping if hasattr(audits_total, "_mapping") else audits_total
    )["n"] == 0

    args, kwargs = interaction.followup.send.call_args
    assert "not authorized" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_scoped_per_guild(monkeypatch, db_conn):
    """Admin of guild_A is not an admin of guild_B. PERSONALIZE_ADMINS is
    keyed by guild_id — cross-guild leakage would let one operator flip
    every server's toggle."""
    monkeypatch.setattr(
        roast,
        "PERSONALIZE_ADMINS",
        {"100": ["555"], "200": ["888"]},
    )
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    roast.register_commands(tree, client=client)
    cmd = tree.get_command("set-personalize-mode")

    # user 555 is admin of guild 100, not 200 — request against 200 must bounce.
    interaction = _make_interaction(guild_id=200, user_id=555)
    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config WHERE guild_id='200'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0

    args, _ = interaction.followup.send.call_args
    assert "not authorized" in args[0].lower()


@pytest.mark.asyncio
async def test_dm_context_bounces_without_reading_allowlist(monkeypatch, db_conn):
    """interaction.guild is None when invoked in DMs. Handler must bounce
    BEFORE reading PERSONALIZE_ADMINS so an unset PERSONALIZE_ADMINS dict
    can't accidentally allow a DM-invoked toggle."""
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=None, user_id=555, in_dm=True)

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0

    args, kwargs = interaction.followup.send.call_args
    assert "inside a server" in args[0].lower() or "server" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_dm_bounce_when_only_guild_id_is_none(monkeypatch, db_conn):
    """Split-branch coverage: guild attribute set but guild_id None still
    bounces. Defends the OR-gate (`guild is None or guild_id is None`)
    against a future refactor silently collapsing to AND."""
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = None
    interaction.guild = MagicMock()  # not None
    user = MagicMock(spec=discord.Member)
    user.id = 555
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0
    args, _ = interaction.followup.send.call_args
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_dm_bounce_when_only_guild_is_none(monkeypatch, db_conn):
    """Split-branch coverage: guild_id set but guild attribute None still
    bounces. Mirrors the inverse of the previous test."""
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 100  # set
    interaction.guild = None
    user = MagicMock(spec=discord.Member)
    user.id = 555
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0
    args, _ = interaction.followup.send.call_args
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_guild_with_empty_allowlist_allows_nobody(monkeypatch, db_conn):
    """Guild present in PERSONALIZE_ADMINS but with [] list → no one is admin.
    Guards against `.get(guild_id, []) → [] → 'not in {}' → deny` regressing
    to a truthy default."""
    monkeypatch.setattr(roast, "PERSONALIZE_ADMINS", {"100": []})
    monkeypatch.setattr(roast, "get_db", lambda: _make_db_context(db_conn)())
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    roast.register_commands(tree, client=client)
    cmd = tree.get_command("set-personalize-mode")

    interaction = _make_interaction(guild_id=100, user_id=555)
    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_guild_config WHERE guild_id='100'"
    ).fetchone()
    assert dict(row._mapping if hasattr(row, "_mapping") else row)["n"] == 0

    args, _ = interaction.followup.send.call_args
    assert "not authorized" in args[0].lower()


@pytest.mark.asyncio
async def test_response_deferred_ephemeral_on_success(monkeypatch, db_conn):
    """defer(ephemeral=True) must fire before any followup — otherwise the
    user sees a 'thinking...' message that goes public, then a private reply.
    """
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555)

    await cmd.callback(interaction, app_commands.Choice(name="on", value="on"))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_sp_helper_called_with_kwargs_only(monkeypatch, db_conn):
    """Locks the call shape: set_personalize_mode is invoked with keyword
    args (guild_id, on, updated_by) — never positional. Drift here would
    fail at call time given the helper's kwargs-only signature, but the
    assertion is cheap insurance against a future refactor."""
    cmd, _ = _register_and_get(monkeypatch, db_conn)
    interaction = _make_interaction(guild_id=100, user_id=555)

    with patch(
        "sable_roles.features.roast.discord_guild_config.set_personalize_mode"
    ) as spm:
        await cmd.callback(
            interaction, app_commands.Choice(name="on", value="on")
        )

    assert spm.call_count == 1
    call = spm.call_args
    # The conn is the only positional after self; all others must be kwargs.
    assert len(call.args) == 1  # just the conn
    assert call.kwargs == {
        "guild_id": "100",
        "on": True,
        "updated_by": "555",
    }
