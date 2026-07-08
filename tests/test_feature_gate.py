"""ENABLED_FEATURES gate — multi-tenant instances register only their groups.

The default ("all", var unset) must be byte-identical to the historical single-bot
deployment: every feature registers, Members intent requested. A duel-only client
instance (SABLE_ROLES_ENABLED_FEATURES=duel) must register ONLY the duel commands —
no event observers (airlock joins, vibe watchers), no other client's commands in the
picker, and NO Members privileged intent (requesting one the portal hasn't enabled
fails the whole gateway connection).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import sable_roles.config as cfg
import sable_roles.main as main_mod
from sable_roles.config import feature_enabled

_ALL_GROUPS = (
    "fitcheck", "burn_me", "roast", "vibe_observer", "airlock",
    "state_pin", "content_deck", "duel",
)


def _set_features(monkeypatch, value: frozenset):
    monkeypatch.setattr(cfg, "ENABLED_FEATURES", value)


# --- feature_enabled unit semantics -----------------------------------------

def test_default_all_enables_everything(monkeypatch):
    _set_features(monkeypatch, frozenset({"all"}))
    assert all(feature_enabled(g) for g in _ALL_GROUPS)


def test_named_list_enables_only_named(monkeypatch):
    _set_features(monkeypatch, frozenset({"duel"}))
    assert feature_enabled("duel")
    assert not any(feature_enabled(g) for g in _ALL_GROUPS if g != "duel")


def test_unknown_name_disables_never_widens(monkeypatch):
    _set_features(monkeypatch, frozenset({"duell"}))  # typo'd
    assert not any(feature_enabled(g) for g in _ALL_GROUPS)


def test_env_parse_shape():
    import os
    import importlib
    old = os.environ.get("SABLE_ROLES_ENABLED_FEATURES")
    try:
        os.environ["SABLE_ROLES_ENABLED_FEATURES"] = " Duel , state_pin ,"
        importlib.reload(cfg)
        assert cfg.ENABLED_FEATURES == frozenset({"duel", "state_pin"})
    finally:
        if old is None:
            os.environ.pop("SABLE_ROLES_ENABLED_FEATURES", None)
        else:
            os.environ["SABLE_ROLES_ENABLED_FEATURES"] = old
        importlib.reload(cfg)


# --- setup_hook wiring -------------------------------------------------------

@pytest.fixture()
def mocked_features(monkeypatch):
    """MagicMock every feature module main.py registers, so setup_hook wiring can be
    asserted without touching discord or the DB. GUILD_TO_ORG emptied so the sync
    loop is a no-op."""
    mocks = {}
    for name in ("fitcheck_streak", "burn_me", "roast", "vibe_observer", "airlock",
                 "delete_monitor", "scoring_pipeline", "reveal_pipeline",
                 "leaderboard", "state_pin", "content_deck", "content_duel"):
        m = MagicMock()
        m.register_commands = MagicMock(return_value=[])
        monkeypatch.setattr(main_mod, name, m)
        mocks[name] = m
    monkeypatch.setattr(main_mod, "GUILD_TO_ORG", {})
    return mocks


async def test_setup_hook_all_registers_everything(monkeypatch, mocked_features):
    _set_features(monkeypatch, frozenset({"all"}))
    client = main_mod.SableRolesClient()
    await client.setup_hook()
    for name, m in mocked_features.items():
        called = m.register.called or m.register_commands.called or m.start_tasks.called
        assert called, f"{name} did not register under 'all'"
    assert client.intents.members is True


async def test_setup_hook_duel_only_registers_only_duel(monkeypatch, mocked_features):
    _set_features(monkeypatch, frozenset({"duel"}))
    client = main_mod.SableRolesClient()
    await client.setup_hook()
    assert mocked_features["content_duel"].register_commands.called
    for name, m in mocked_features.items():
        if name == "content_duel":
            continue
        assert not m.register.called, f"{name}.register leaked into duel-only instance"
        assert not m.register_commands.called, f"{name} commands leaked into duel-only picker"
    assert not mocked_features["vibe_observer"].start_tasks.called
    assert client.intents.members is False  # portal has Members OFF — must not request


async def test_on_ready_skips_airlock_bootstrap_when_gated(monkeypatch, mocked_features):
    _set_features(monkeypatch, frozenset({"duel"}))
    client = main_mod.SableRolesClient()
    mocked_features["airlock"].bootstrap = AsyncMock()

    class _Row:
        def fetchone(self):
            return [1]

    conn = MagicMock()
    conn.execute.return_value = _Row()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(main_mod, "get_db", MagicMock(return_value=conn))
    monkeypatch.setattr(
        type(client), "user", property(lambda self: "test-bot"), raising=False
    )
    await client.on_ready()
    assert not mocked_features["airlock"].bootstrap.called
