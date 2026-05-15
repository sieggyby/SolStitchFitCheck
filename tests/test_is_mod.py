"""Tests for the _is_mod helper that gates V2 mod-only slash commands."""
from __future__ import annotations

from types import SimpleNamespace

from sable_roles.features import fitcheck_streak as mod


def _member(role_ids: list[int | str]) -> SimpleNamespace:
    return SimpleNamespace(roles=[SimpleNamespace(id=rid) for rid in role_ids])


def test_member_with_mod_role_passes(monkeypatch):
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": ["999"]})
    assert mod._is_mod(_member([999]), "100") is True


def test_member_with_matching_role_id_as_string_passes(monkeypatch):
    # Discord member.roles[*].id is an int; MOD_ROLES env entries are strings.
    # The helper normalizes both sides to str — covers both shapes.
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": ["999"]})
    assert mod._is_mod(_member(["999"]), "100") is True


def test_member_without_mod_role_fails(monkeypatch):
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": ["999"]})
    assert mod._is_mod(_member([1234]), "100") is False


def test_member_with_multiple_roles_one_matches_passes(monkeypatch):
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": ["999"]})
    assert mod._is_mod(_member([111, 999, 222]), "100") is True


def test_guild_not_in_mod_roles_fails(monkeypatch):
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": ["999"]})
    # Even if the member's role id (999) is a configured mod role for guild 100,
    # querying for a different guild (200) must return False.
    assert mod._is_mod(_member([999]), "200") is False


def test_empty_mod_role_list_fails(monkeypatch):
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": []})
    assert mod._is_mod(_member([999]), "100") is False


def test_member_with_no_roles_fails(monkeypatch):
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": ["999"]})
    assert mod._is_mod(_member([]), "100") is False


def test_administrator_role_not_auto_promoted(monkeypatch):
    # Discord Administrator permission bypasses role/channel perms but the
    # _is_mod check is explicitly decoupled from that. A member with admin
    # permission whose admin role is NOT in MOD_ROLES must NOT be a mod.
    monkeypatch.setattr(mod, "MOD_ROLES", {"100": ["999"]})
    # Simulate Brian-as-@Atelier-admin: role id 8888 holds Administrator in Discord
    # but is not in MOD_ROLES. _is_mod doesn't even inspect role permissions.
    member = SimpleNamespace(
        roles=[SimpleNamespace(id=8888, permissions=SimpleNamespace(administrator=True))]
    )
    assert mod._is_mod(member, "100") is False
