"""State-pin call-site integration tests.

Verifies that the four slash-command handlers fire announce_state_change
exactly once on a real state change, and ZERO times on a same-state
no-op flip. Each test uses the real handler module (with state_pin's
announce_state_change monkeypatched to a counting stub) so the no-op
gate + summary formatter wiring is end-to-end-correct.

Per state-pin plan §9: four call sites
  1. scoring_pipeline._ScoringSetConfirmView.confirm (after set_state)
  2. fitcheck_streak relax-mode handler (after set_relax_mode)
  3. burn_me set-burn-mode handler (after set_burn_mode)
  4. roast _handle_set_personalize_mode (after set_personalize_mode)
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import text


# ---------------------------------------------------------------------------
# Shared test client / interaction
# ---------------------------------------------------------------------------


def _make_interaction(
    *,
    guild_id: int = 100,
    user_id: int = 555,
    is_admin_for_personalize: bool = True,
    user_role_ids: tuple = (),
):
    """Build a discord.Interaction stub usable by all four call sites."""
    bot_user = SimpleNamespace(id=99999)
    client = SimpleNamespace(user=bot_user)

    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.display_name = "mod_tester"
    user.guild_permissions = SimpleNamespace(manage_guild=True)
    user.roles = [SimpleNamespace(id=rid) for rid in user_role_ids]

    interaction = MagicMock()
    interaction.user = user
    interaction.guild = SimpleNamespace(id=guild_id)
    interaction.guild_id = guild_id
    interaction.client = client
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.fixture
def announce_calls(monkeypatch):
    """Replace state_pin.announce_state_change with a counting stub so
    we can assert call counts + the keyword args each handler passes."""
    calls = []

    async def _record(*args, **kwargs):
        calls.append(kwargs)

    from sable_roles.features import state_pin as sp
    monkeypatch.setattr(sp, "announce_state_change", _record)
    return calls


@pytest.fixture
def call_site_db(monkeypatch, db_conn):
    """Patch get_db in each handler's importing module to return our
    shared in-memory connection. Patches GUILD_TO_ORG + MOD_ROLES /
    PERSONALIZE_ADMINS where the handler reads them."""
    class _DBContext:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    from sable_roles.features import (
        burn_me as bm,
        fitcheck_streak as fs,
        roast as rt,
        scoring_pipeline as sp_p,
    )

    for mod in (bm, fs, rt, sp_p):
        monkeypatch.setattr(mod, "get_db", lambda: _DBContext())

    # Guild config: every test guild is configured for solstitch.
    monkeypatch.setattr(fs, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(bm, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(rt, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(sp_p, "GUILD_TO_ORG", {"100": "solstitch"})

    # Mod / admin allowlists per handler.
    monkeypatch.setattr(fs, "MOD_ROLES", {"100": [42]})
    monkeypatch.setattr(bm, "_is_mod", lambda member, gid: True)
    monkeypatch.setattr(rt, "PERSONALIZE_ADMINS", {"100": [555]})
    return db_conn


# ---------------------------------------------------------------------------
# Helper to wait for any background tasks the handler may schedule
# ---------------------------------------------------------------------------


async def _drain():
    """Yield long enough for any asyncio.create_task(...) the handler
    spawned to register itself; the stubbed announce_state_change
    completes immediately so a single yield is enough."""
    for _ in range(3):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# scoring_pipeline confirm view
# ---------------------------------------------------------------------------


async def test_scoring_confirm_view_fires_announce_on_real_change(
    announce_calls, call_site_db,
):
    from sable_roles.features.scoring_pipeline import _ScoringSetConfirmView

    interaction = _make_interaction()
    interaction.response.edit_message = AsyncMock()

    view = _ScoringSetConfirmView(
        invoker_user_id=555,
        org_id="solstitch",
        guild_id="100",
        target_state="silent",
        current_state="off",
    )
    # @discord.ui.button decorates the method into a Button whose
    # `.callback` is an _ItemCallback that binds (view, button) itself
    # so the test only needs to pass the interaction.
    await view.confirm.callback(interaction)
    await _drain()

    assert len(announce_calls) == 1
    call = announce_calls[0]
    assert call["characteristic"] == "scoring"
    assert call["guild_id"] == "100"
    assert call["org_id"] == "solstitch"
    assert call["changed_by_user_id"] == 555
    assert call["new_state_summary"].startswith("state: silent\n")


# ---------------------------------------------------------------------------
# fitcheck_streak relax-mode
# ---------------------------------------------------------------------------


async def test_relax_mode_fires_announce_on_change(
    announce_calls, call_site_db,
):
    """First-time toggle: prior config has relax_mode_on=0, new=1 →
    announce_state_change fires once."""
    from sable_roles.features.fitcheck_streak import register_commands

    interaction = _make_interaction(user_role_ids=(42,))
    interaction.followup.send = AsyncMock()
    tree = MagicMock()
    captured = {}

    def _cmd(*args, **kwargs):
        def deco(fn):
            captured[kwargs.get("name", fn.__name__)] = fn
            return fn
        return deco

    tree.command = _cmd
    register_commands(tree)
    relax = captured["relax-mode"]
    # The decorator stack from app_commands wraps the callable; the
    # wrapped function lives in the closure. tree.command captured the
    # outermost wrapper which is the actual handler since we replaced
    # the decorators with a passthrough.
    on_choice = MagicMock()
    on_choice.value = "on"
    await relax(interaction, on_choice)
    await _drain()

    assert len(announce_calls) == 1
    assert announce_calls[0]["characteristic"] == "relax_mode"
    assert announce_calls[0]["new_state_summary"].startswith("state: on\n")


async def test_relax_mode_no_op_skips_announce(
    announce_calls, call_site_db, db_conn,
):
    """Setting relax-mode to off when prior is off (default) →
    no announce. Same-state gate is the load-bearing check here."""
    from sable_roles.features.fitcheck_streak import register_commands

    interaction = _make_interaction(user_role_ids=(42,))
    interaction.followup.send = AsyncMock()
    tree = MagicMock()
    captured = {}

    def _cmd(*args, **kwargs):
        def deco(fn):
            captured[kwargs.get("name", fn.__name__)] = fn
            return fn
        return deco

    tree.command = _cmd
    register_commands(tree)
    relax = captured["relax-mode"]
    off_choice = MagicMock()
    off_choice.value = "off"  # current is off (default)
    await relax(interaction, off_choice)
    await _drain()

    assert len(announce_calls) == 0


# ---------------------------------------------------------------------------
# burn_me set-burn-mode
# ---------------------------------------------------------------------------


async def test_set_burn_mode_fires_announce_on_change(
    announce_calls, call_site_db,
):
    """First-time toggle: prior current_burn_mode='once' (default), new='persist'."""
    from sable_roles.features.burn_me import register_commands

    interaction = _make_interaction()
    interaction.followup.send = AsyncMock()
    tree = MagicMock()
    captured = {}

    def _cmd(*args, **kwargs):
        def deco(fn):
            captured[kwargs.get("name", fn.__name__)] = fn
            return fn
        return deco

    tree.command = _cmd
    register_commands(tree)
    set_burn = captured["set-burn-mode"]
    mode_choice = MagicMock()
    mode_choice.value = "persist"
    await set_burn(interaction, mode_choice)
    await _drain()

    assert len(announce_calls) == 1
    assert announce_calls[0]["characteristic"] == "burn_mode"
    assert announce_calls[0]["new_state_summary"].startswith("state: persist\n")


async def test_set_burn_mode_no_op_skips_announce(
    announce_calls, call_site_db,
):
    """Default current_burn_mode is 'once'. Setting to 'once' → no announce."""
    from sable_roles.features.burn_me import register_commands

    interaction = _make_interaction()
    interaction.followup.send = AsyncMock()
    tree = MagicMock()
    captured = {}

    def _cmd(*args, **kwargs):
        def deco(fn):
            captured[kwargs.get("name", fn.__name__)] = fn
            return fn
        return deco

    tree.command = _cmd
    register_commands(tree)
    set_burn = captured["set-burn-mode"]
    mode_choice = MagicMock()
    mode_choice.value = "once"
    await set_burn(interaction, mode_choice)
    await _drain()

    assert len(announce_calls) == 0


# ---------------------------------------------------------------------------
# roast set-personalize-mode
# ---------------------------------------------------------------------------


async def test_set_personalize_mode_fires_announce_on_change(
    announce_calls, call_site_db,
):
    from sable_roles.features.roast import _handle_set_personalize_mode

    interaction = _make_interaction()
    interaction.followup.send = AsyncMock()
    await _handle_set_personalize_mode(interaction, "on")
    await _drain()

    assert len(announce_calls) == 1
    assert announce_calls[0]["characteristic"] == "personalize_mode"
    assert announce_calls[0]["new_state_summary"].startswith("state: on\n")


async def test_set_personalize_mode_no_op_skips_announce(
    announce_calls, call_site_db,
):
    """Default personalize_mode_on=0 → setting to off is a no-op."""
    from sable_roles.features.roast import _handle_set_personalize_mode

    interaction = _make_interaction()
    interaction.followup.send = AsyncMock()
    await _handle_set_personalize_mode(interaction, "off")
    await _drain()

    assert len(announce_calls) == 0
