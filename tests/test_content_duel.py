"""Phase-5 community duel (`features/content_duel.py`).

Load-bearing claims: the disclosure gate FAILS CLOSED (missing key/empty/error →
refusal, never a duel); the trigger is MOD_ROLES-gated; voting is OPEN (any member)
but one-vote-per-member (in-View dict + the durable DB guard); each vote writes a
correct community decision row; the open tally shows only a COUNT (blind — no A/B
split until close); timeout reveals the tally + disables buttons; the disclosure
line rides every embed; every send passes AllowedMentions.none().
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import text

from sable_platform.db import content_deck as cd_db
from sable_roles.features import content_duel as mod


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def duel_env(monkeypatch, db_conn):
    """content_duel wired to the in-memory SP db + a solstitch test guild."""
    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})

    @contextlib.contextmanager
    def _fake_get_db():
        yield db_conn

    monkeypatch.setattr(mod, "get_db", _fake_get_db)
    # mod gate: role 555 is the solstitch mod role
    monkeypatch.setattr(
        "sable_roles.features.fitcheck_streak.MOD_ROLES", {"100": ["555"]}
    )
    # the in-memory SQLite conn is thread-bound — run the module's to_thread work
    # INLINE in tests (prod uses real Postgres across a worker thread).
    async def _inline_to_thread(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(to_thread=_inline_to_thread))
    # module-level state: cleared, never rebound (repo convention).
    mod._OPEN_DUELS.clear()
    return db_conn


def _sign_disclosure(conn, org="solstitch"):
    conn.execute(
        "UPDATE orgs SET config_json = ? WHERE org_id = ?",
        ('{"pairwise_disclosure_signed": "2026-07-02 sieggy — full-reign"}', org),
    )
    conn.commit()


def _seed_pending(conn, cid, *, kind="tweet", payload='{"text": "candidate text"}'):
    conn.execute(
        "INSERT INTO content_candidates (id, org_id, kind, status, payload_json, source, created_at) "
        "VALUES (?, 'solstitch', ?, 'pending', ?, 'seed', '2026-07-02T00:00:00Z')",
        (cid, kind, payload),
    )
    conn.commit()


def _member(user_id: int, *, role_ids=("555",), is_member=True):
    member = MagicMock(spec=discord.Member if is_member else discord.User)
    member.id = user_id
    member.bot = False
    if is_member:
        member.roles = [SimpleNamespace(id=r) for r in role_ids]
    return member


def _interaction(user, *, guild_id=100):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return interaction


def _sent_kwargs(interaction):
    return interaction.response.send_message.call_args.kwargs


def _sent_text(interaction):
    args = interaction.response.send_message.call_args.args
    return args[0] if args else ""


# --- the disclosure gate (fail-closed) ---------------------------------------

async def test_duel_refused_without_disclosure(duel_env):
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "disclosure" in _sent_text(i)
    assert _sent_kwargs(i)["ephemeral"] is True


async def test_duel_refused_on_empty_disclosure_value(duel_env):
    duel_env.execute(
        "UPDATE orgs SET config_json = '{\"pairwise_disclosure_signed\": \"  \"}' "
        "WHERE org_id = 'solstitch'"
    )
    duel_env.commit()
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "disclosure" in _sent_text(i)


async def test_gate_fails_closed_on_db_error(monkeypatch, duel_env):
    @contextlib.contextmanager
    def _broken():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(mod, "get_db", _broken)
    assert mod._disclosure_signed("solstitch") is False


async def test_unmapped_guild_refused(duel_env):
    i = _interaction(_member(1), guild_id=999)
    await mod._handle_duel(i)
    assert "isn't configured" in _sent_text(i)


# --- the mod trigger gate -----------------------------------------------------

async def test_non_mod_cannot_start_a_duel(duel_env):
    _sign_disclosure(duel_env)
    i = _interaction(_member(1, role_ids=("777",)))  # not the mod role
    await mod._handle_duel(i)
    assert "mod" in _sent_text(i)
    assert _sent_kwargs(i)["ephemeral"] is True


async def test_thin_deck_refused(duel_env):
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)  # only ONE pending candidate
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "not enough" in _sent_text(i)


# --- the happy path + voting ---------------------------------------------------

async def test_duel_posts_public_embed_with_disclosure(duel_env):
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2, kind="meme",
                  payload='{"template_id":"drake","format":"Drake","captions":{"a":"x","b":"y"}}')
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    # the duel itself is a REGULAR bot channel message (webhook tokens die at 15 min);
    # the interaction response is just an ephemeral ack to the triggering mod.
    kwargs = i.channel.send.call_args.kwargs
    embed = kwargs["embed"]
    assert "train Sable's content engine" in embed.footer.text  # member-facing disclosure
    assert isinstance(kwargs["view"], mod._DuelView)
    assert kwargs["allowed_mentions"] is not None
    assert _sent_kwargs(i)["ephemeral"] is True  # the mod ack


async def _open_duel(duel_env):
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    return i.channel.send.call_args.kwargs["view"]


async def test_two_members_vote_two_rows(duel_env):
    view = await _open_duel(duel_env)
    v1 = _interaction(_member(10, role_ids=()))  # NOT mods — voting is open
    v2 = _interaction(_member(11, role_ids=()))
    await view._vote(v1, "a")
    await view._vote(v2, "b")
    rows = duel_env.execute(
        "SELECT actor, actor_kind, decision, surface, candidate_id, pair_loser_id "
        "FROM content_deck_decisions ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "discord:user:10" and rows[0][1] == "community"
    assert rows[0][2] == "keep" and rows[0][3] == "discord"
    winner_a, loser_a = rows[0][4], rows[0][5]
    winner_b, loser_b = rows[1][4], rows[1][5]
    assert {winner_a, loser_a} == {1, 2} and winner_a != winner_b  # opposite picks


async def test_same_member_votes_once(duel_env):
    view = await _open_duel(duel_env)
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    v2 = _interaction(_member(10, role_ids=()))
    await view._vote(v2, "b")
    n = duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0]
    assert n == 1
    assert "already voted" in _sent_text(v2)


async def test_durable_dedup_survives_restart(duel_env):
    """The in-View dict dies on restart — the DB guard must still refuse a re-vote."""
    view = await _open_duel(duel_env)
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    view._votes.clear()  # simulate a bot restart mid-duel
    v2 = _interaction(_member(10, role_ids=()))
    await view._vote(v2, "b")
    n = duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0]
    assert n == 1
    assert "already voted" in _sent_text(v2)


async def test_failed_write_never_counts_the_vote(monkeypatch, duel_env):
    view = await _open_duel(duel_env)
    monkeypatch.setattr(mod.cd_db, "record_deck_decision",
                        MagicMock(side_effect=RuntimeError("boom")))
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    assert "couldn't record" in _sent_text(v)
    assert 10 not in view._votes  # the member can retry


async def test_open_tally_is_blind_and_close_reveals(duel_env):
    view = await _open_duel(duel_env)
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    open_embed = v.response.edit_message.call_args.kwargs["embed"]
    open_text = " ".join(f"{f.name} {f.value}" for f in open_embed.fields)
    assert "votes 1" in open_text and "🅰 1" not in open_text  # count only, no split

    msg = MagicMock(spec=discord.Message)
    msg.edit = AsyncMock()
    view.bind_message(msg)
    await view.on_timeout()
    closed_embed = msg.edit.call_args.kwargs["embed"]
    closed_text = " ".join(f"{f.name} {f.value}" for f in closed_embed.fields)
    assert "🅰 1 — 0 🅱" in closed_text and "🅰 wins" in closed_text
    assert all(child.disabled for child in view.children)


async def test_second_duel_refused_while_one_is_open(duel_env):
    """Codex F2: a mod double-post (before any vote lands — invisible to the SP 12h
    exclusion) is refused by the per-org open-duel lock."""
    await _open_duel(duel_env)
    _seed_pending(duel_env, 3)
    _seed_pending(duel_env, 4)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "already open" in _sent_text(i)


async def test_vote_after_deadline_refused(duel_env):
    """Codex F4: the HARD wall-clock deadline — a vote past it is refused even if the
    inactivity timer hasn't fired yet, and writes nothing."""
    view = await _open_duel(duel_env)
    view._deadline = 0.0  # force past-deadline
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    assert "closed" in _sent_text(v)
    assert duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0] == 0


async def test_unrenderable_payload_never_reaches_the_channel(duel_env):
    """Codex F1: a payload outside the strict whitelist (no text/captions) renders ''
    and the candidate is DROPPED — raw JSON with internal fields never posts."""
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1, payload='{"guardrail_hits":[{"term":"secret"}],"internal":"x"}')
    _seed_pending(duel_env, 2)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "not enough" in _sent_text(i)  # the unrenderable card was dropped
    i.channel.send.assert_not_called()
    assert mod._payload_text("quote_card", '{"guardrail_hits":[{"term":"secret"}]}') == ""


async def test_revocation_sentinel_disables_the_gate(duel_env):
    """Adversarial T2-4: an operator 'turning it off' with pairwise_disclosure_signed=
    'false'/'revoked'/'no' must actually turn it off — never read as signed."""
    for sentinel in ("false", "No", " REVOKED ", "off", "0"):
        duel_env.execute(
            "UPDATE orgs SET config_json = ? WHERE org_id = 'solstitch'",
            ('{"pairwise_disclosure_signed": "%s"}' % sentinel,),
        )
        duel_env.commit()
        assert mod._disclosure_signed("solstitch") is False, sentinel


async def test_missing_accessor_fails_gate_closed_not_boot(monkeypatch, duel_env):
    """Adversarial T2-3: against an old SablePlatform (no get_org_config_value) the
    module imports fine and the GATE fails closed — the bot must never boot-crash."""
    monkeypatch.setattr(mod, "get_org_config_value", None)
    _sign_disclosure(duel_env)
    assert mod._disclosure_signed("solstitch") is False


async def test_rapid_double_click_yields_one_row(monkeypatch, duel_env):
    """Adversarial T2-1: discord.py dispatches each click as its OWN task — two rapid
    clicks must still land exactly one row (the synchronous pre-mark closes the race
    the durable guard can't see mid-transaction)."""
    view = await _open_duel(duel_env)

    real_sleep = asyncio.sleep

    async def _yielding_to_thread(fn, *a, **k):
        await real_sleep(0)  # force a task switch INSIDE the vote critical section
        return fn(*a, **k)

    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(to_thread=_yielding_to_thread))
    v1 = _interaction(_member(10, role_ids=()))
    v2 = _interaction(_member(10, role_ids=()))  # same member, opposite button
    await asyncio.gather(view._vote(v1, "a"), view._vote(v2, "b"))
    n = duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0]
    assert n == 1
    assert view._vote_count() == 1


async def test_failed_write_rolls_back_the_premark(monkeypatch, duel_env):
    """The T2-1 pre-mark must not lock a member out after a transient write failure."""
    view = await _open_duel(duel_env)
    monkeypatch.setattr(mod.cd_db, "record_deck_decision",
                        MagicMock(side_effect=RuntimeError("boom")))
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    assert 10 not in view._votes  # rolled back — retry possible
    monkeypatch.undo()


# --- the tasteboard -------------------------------------------------------------

async def test_tasteboard_gated_and_renders(duel_env):
    i = _interaction(_member(10, role_ids=()))
    await mod._handle_tasteboard(i)
    assert "aren't enabled" in _sent_text(i)  # fail-closed before the flag

    _sign_disclosure(duel_env)
    i2 = _interaction(_member(10, role_ids=()))
    i2.guild.get_member = MagicMock(return_value=None)
    await mod._handle_tasteboard(i2)
    assert "no duel votes yet" in _sent_text(i2)

    view = await _open_duel(duel_env)
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    i3 = _interaction(_member(11, role_ids=()))
    i3.guild.get_member = MagicMock(return_value=None)
    await mod._handle_tasteboard(i3)
    kwargs = _sent_kwargs(i3)
    assert kwargs["ephemeral"] is True
    assert "1 votes" in kwargs["embed"].description
