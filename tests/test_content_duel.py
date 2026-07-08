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
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import text

from sable_platform.db import content_deck as cd_db
from sable_roles.features import content_deck as deck_mod
from sable_roles.features import content_duel as mod


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def duel_env(monkeypatch, db_conn):
    """content_duel wired to the in-memory SP db + a solstitch test guild."""
    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(mod, "DUEL_STARTERS", {})  # tests opt in per-case

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


def _set_duel_kinds(conn, kinds_value, org="solstitch"):
    """Write orgs.config_json carrying BOTH the signed disclosure and a duel_kinds
    value (a real list, a JSON-string-encoded list, or garbage — whatever the test
    needs the org config to hold)."""
    cfg = {
        "pairwise_disclosure_signed": "2026-07-07 sieggy — full-reign",
        "duel_kinds": kinds_value,
    }
    conn.execute(
        "UPDATE orgs SET config_json = ? WHERE org_id = ?", (json.dumps(cfg), org)
    )
    conn.commit()


def _ct_payload(*, author="gabbyvorbeck", engagement=None, as_of="2026-07-07T18:00:00Z",
                text_="real tweet from the community", drop=()):
    """A §1.1-shaped community_tweet payload (internal fields included — they must
    never render). `drop` removes keys to build the invalid variants."""
    p = {
        "text": text_,
        "author_handle": author,
        "author_name": "Gabby",
        "x_id": "1938291000000000000",
        "url": f"https://x.com/{author}/status/1938291000000000000",
        "posted_at": "2026-06-28T14:03:00Z",
        "engagement": engagement if engagement is not None else
            {"likes": 120, "retweets": 18, "replies": 22, "quotes": 3, "views": 15400},
        "engagement_as_of": as_of,
        "ingest_batch": "2026-07-07",
    }
    for key in drop:
        p.pop(key, None)
    return json.dumps(p)


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


# --- the trigger gate (named starters first, mod roles as fallback) -------------

async def test_non_mod_cannot_start_a_duel(duel_env):
    _sign_disclosure(duel_env)
    i = _interaction(_member(1, role_ids=("777",)))  # not the mod role, no starters set
    await mod._handle_duel(i)
    assert "Sable team" in _sent_text(i)
    assert _sent_kwargs(i)["ephemeral"] is True


async def test_named_starter_can_duel_without_any_role(monkeypatch, duel_env):
    """The operator ask: Arf/P0ison/Monasex start duels BY USERNAME — no role needed."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["402620324744790017"]})
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(402620324744790017, role_ids=()))  # zero roles
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # the duel posted


async def test_starters_configured_means_roles_are_ignored(monkeypatch, duel_env):
    """'By username NOT by role': with a starters list set, even a full MOD-role holder
    who isn't on the list is refused."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["402620324744790017"]})
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(999, role_ids=("555",)))  # the mod role — still refused
    await mod._handle_duel(i)
    assert "Sable team" in _sent_text(i)
    i.channel.send.assert_not_called()


async def test_unconfigured_guild_falls_back_to_mod_roles(monkeypatch, duel_env):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {})
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(1, role_ids=("555",)))  # the mod role works as before
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()


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


async def test_explicit_empty_starters_locks_duels(monkeypatch, duel_env):
    """Codex: an EXPLICIT empty starters entry means locked — never a silent
    fall-through to the role gate."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": []})
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(1, role_ids=("555",)))  # even the mod role is refused
    await mod._handle_duel(i)
    assert "Sable team" in _sent_text(i)
    i.channel.send.assert_not_called()


# --- duel_kinds pool selection (community-tweet duels, mig 083) ------------------

async def test_duel_kinds_community_only_never_pairs_ai(duel_env):
    """duel_kinds=["community_tweet"] (a REAL list — org config may store the list
    itself) restricts the pool: an AI card never pairs even with tweets/memes pending."""
    _set_duel_kinds(duel_env, ["community_tweet"])
    _seed_pending(duel_env, 1, kind="community_tweet", payload=_ct_payload(author="gabbyvorbeck"))
    _seed_pending(duel_env, 2, kind="community_tweet", payload=_ct_payload(author="syebastian"))
    _seed_pending(duel_env, 3, kind="tweet")
    _seed_pending(duel_env, 4, kind="meme",
                  payload='{"template_id":"drake","format":"Drake","captions":{"a":"x","b":"y"}}')
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    view = i.channel.send.call_args.kwargs["view"]
    assert {view._card_a["id"], view._card_b["id"]} == {1, 2}
    assert view._card_a["kind"] == view._card_b["kind"] == "community_tweet"


async def test_json_string_encoded_duel_kinds_accepted(duel_env):
    """org config may also store duel_kinds as a JSON-STRING-encoded list — parsed,
    not refused."""
    _set_duel_kinds(duel_env, '["community_tweet"]')
    _seed_pending(duel_env, 1, kind="community_tweet", payload=_ct_payload(author="gabbyvorbeck"))
    _seed_pending(duel_env, 2, kind="community_tweet", payload=_ct_payload(author="syebastian"))
    _seed_pending(duel_env, 3, kind="tweet")
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    view = i.channel.send.call_args.kwargs["view"]
    assert {view._card_a["id"], view._card_b["id"]} == {1, 2}


async def test_explicit_empty_duel_kinds_refuses(duel_env):
    """Explicit-empty-means-locked (the DUEL_STARTERS convention, audit S4): a
    present-but-EMPTY duel_kinds refuses the duel — never a silent full-pool
    fall-through."""
    _set_duel_kinds(duel_env, [])
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "duel_kinds" in _sent_text(i)
    assert _sent_kwargs(i)["ephemeral"] is True
    i.channel.send.assert_not_called()


@pytest.mark.parametrize("kinds_value", [
    "not-json[",              # malformed JSON string
    '"tweet"',                # valid JSON, not a list
    42,                       # non-list, non-string
    {"kinds": ["tweet"]},     # non-list container
    ["hologram"],             # unknown kind (outside the mig-083 CHECK set)
    ["tweet", 42],            # non-string entry
])
async def test_bad_duel_kinds_config_refuses(duel_env, kinds_value):
    """FAIL-CLOSED: a duel_kinds value we don't positively recognize refuses the duel
    — a typo'd config must never widen the pool."""
    _set_duel_kinds(duel_env, kinds_value)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "duel_kinds" in _sent_text(i)
    i.channel.send.assert_not_called()


async def test_no_config_org_calls_pair_accessor_without_kinds_kwarg(monkeypatch, duel_env):
    """SolStitch regression pin (audit F1, load-bearing): with NO duel_kinds config the
    SP accessor is called WITHOUT the kinds kwarg — byte-identical to the pre-083 call
    shape, so a stale baked SablePlatform (old signature) can never TypeError the
    unconfigured path into "not enough candidates"."""
    _sign_disclosure(duel_env)  # config carries ONLY the disclosure — no duel_kinds key
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    real = cd_db.get_deck_duel_pair
    calls = []

    def _capture(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod.cd_db, "get_deck_duel_pair", _capture)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # behavior unchanged: the duel posted
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 2  # (conn, org) positionally
    assert kwargs == {}  # and NOTHING else — no kinds kwarg


# --- community card render + reveal ----------------------------------------------

async def _open_community_duel(duel_env, *, hi_engagement=None, lo_engagement=None):
    """Two community cards (id 1 = high engagement, id 2 = low), duel opened.
    Returns (view, posted_kwargs)."""
    _set_duel_kinds(duel_env, ["community_tweet"])
    hi = hi_engagement or {"likes": 120, "retweets": 18, "replies": 22, "quotes": 3, "views": 15400}
    lo = lo_engagement or {"likes": 10, "retweets": 2, "replies": 5, "quotes": 0, "views": 900}
    _seed_pending(duel_env, 1, kind="community_tweet",
                  payload=_ct_payload(author="gabbyvorbeck", engagement=hi))
    _seed_pending(duel_env, 2, kind="community_tweet",
                  payload=_ct_payload(author="syebastian", engagement=lo))
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    kwargs = i.channel.send.call_args.kwargs
    return kwargs["view"], kwargs


async def test_community_cards_render_author_and_variant_footer(duel_env):
    view, kwargs = await _open_community_duel(duel_env)
    embed = kwargs["embed"]
    f_a, f_b = embed.fields[0], embed.fields[1]
    assert f_a.name.startswith("🅰 · @") and f_b.name.startswith("🅱 · @")
    assert {f_a.name.split("@")[1], f_b.name.split("@")[1]} == {"gabbyvorbeck", "syebastian"}
    assert "real tweets from this community" in embed.footer.text
    assert "guess which popped" in embed.footer.text
    # the open tally stays BLIND: count only — no split, no reality, no numbers
    assert embed.fields[2].name == "votes" and embed.fields[2].value == "0"
    assert all(f.name != "reality" for f in embed.fields)
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    open_embed = v.response.edit_message.call_args.kwargs["embed"]
    open_text = " ".join(f"{f.name} {f.value}" for f in open_embed.fields)
    assert "votes 1" in open_text
    assert "reality" not in open_text and "popped" not in open_text


async def test_close_reveals_weighted_reality_and_room_verdict(duel_env):
    """The reveal: WEIGHTED score (likes + 2·RT + replies + quotes), the popped
    verdict, the room-vs-reality line, and the as-of date in the closed footer."""
    view, _ = await _open_community_duel(duel_env)
    hi_is_a = view._card_a["id"] == 1  # RANDOM() pair order — resolve which side is which
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a" if hi_is_a else "b")  # the room picks the popped card
    msg = MagicMock(spec=discord.Message)
    msg.edit = AsyncMock()
    view.bind_message(msg)
    await view.on_timeout()
    closed_embed = msg.edit.call_args.kwargs["embed"]
    reality = next(f for f in closed_embed.fields if f.name == "reality").value
    if hi_is_a:  # hi: 120 + 2*18 + 22 + 3 = 181 · lo: 10 + 2*2 + 5 + 0 = 19
        assert reality.startswith("🅰 score 181 (120❤ 18🔁 22💬 3❞) · 🅱 score 19 — 🅰 popped")
    else:
        assert reality.startswith("🅰 score 19 (10❤ 2🔁 5💬 0❞) · 🅱 score 181 — 🅱 popped")
    assert "the room called it" in reality
    assert "numbers as of 2026-07-07" in closed_embed.footer.text
    assert "real tweets from this community" in closed_embed.footer.text


async def test_close_reveal_upset_when_room_picked_the_flop(duel_env):
    view, _ = await _open_community_duel(duel_env)
    hi_is_a = view._card_a["id"] == 1
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "b" if hi_is_a else "a")  # the room picks the LOW card
    msg = MagicMock(spec=discord.Message)
    msg.edit = AsyncMock()
    view.bind_message(msg)
    await view.on_timeout()
    reality = next(
        f for f in msg.edit.call_args.kwargs["embed"].fields if f.name == "reality"
    ).value
    assert "upset — the room picked the other one" in reality
    assert "the room called it" not in reality


async def test_close_reveal_omits_room_line_on_vote_tie(duel_env):
    """No votes (0–0) → nothing to compare — the room-vs-reality line is omitted,
    the reality numbers still show."""
    view, _ = await _open_community_duel(duel_env)
    msg = MagicMock(spec=discord.Message)
    msg.edit = AsyncMock()
    view.bind_message(msg)
    await view.on_timeout()
    reality = next(
        f for f in msg.edit.call_args.kwargs["embed"].fields if f.name == "reality"
    ).value
    assert "popped" in reality
    assert "room" not in reality and "upset" not in reality


@pytest.mark.parametrize("bad_payload", [
    _ct_payload(author="not a handle!"),           # invalid handle characters
    _ct_payload(author="a" * 16),                  # too long for an X handle
    _ct_payload(author="gabbyvorbeck\n"),          # trailing newline ("$" quirk)
    _ct_payload(drop=("author_handle",)),          # author missing
    _ct_payload(drop=("engagement",)),             # engagement missing
    _ct_payload(engagement={"likes": "many", "retweets": 1, "replies": 1, "quotes": 1}),
    _ct_payload(engagement={"likes": 1, "retweets": 2}),  # counter keys missing
])
async def test_invalid_community_card_is_dropped(duel_env, bad_payload):
    """A community card with a bad author or bad numbers is DROPPED — the duel refuses
    on <2 valid cards rather than render a fake attribution or reveal a wrong score."""
    _set_duel_kinds(duel_env, ["community_tweet"])
    _seed_pending(duel_env, 1, kind="community_tweet", payload=bad_payload)
    _seed_pending(duel_env, 2, kind="community_tweet", payload=_ct_payload())
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "not enough" in _sent_text(i)
    i.channel.send.assert_not_called()


async def test_community_votes_still_write_actor_kind_community(duel_env):
    """The vote substrate is UNCHANGED on the community path: keep + pair_loser_id,
    actor_kind='community', surface='discord'."""
    view, _ = await _open_community_duel(duel_env)
    v = _interaction(_member(10, role_ids=()))
    await view._vote(v, "a")
    row = duel_env.execute(
        "SELECT actor, actor_kind, decision, surface, candidate_id, pair_loser_id "
        "FROM content_deck_decisions"
    ).fetchone()
    assert row[0] == "discord:user:10" and row[1] == "community"
    assert row[2] == "keep" and row[3] == "discord"
    assert {row[4], row[5]} == {1, 2}


# --- W6: the Phase-0 swipe spike never serves a community tweet -------------------

async def test_spike_feed_excludes_community_tweet(monkeypatch, duel_env):
    """W6 (audit F12a): a test guild mapped to a live org must never swipe an ingested
    member tweet — the no-repost wall covers the spike's durable feed too."""
    _seed_pending(duel_env, 1, kind="community_tweet", payload=_ct_payload())
    _seed_pending(duel_env, 2, kind="tweet")

    @contextlib.contextmanager
    def _fake_get_db():
        yield duel_env

    monkeypatch.setattr(deck_mod, "get_db", _fake_get_db)
    cards = deck_mod._load_cards("solstitch", "discord:user:1")
    assert [c["id"] for c in cards] == [2]  # the community row is gone, the tweet stays
    # an all-community durable feed degrades to the static seed, as if it were empty
    duel_env.execute("DELETE FROM content_candidates WHERE id = 2")
    duel_env.commit()
    cards = deck_mod._load_cards("solstitch", "discord:user:1")
    assert cards and all(c["id"] is None for c in cards)


async def test_channel_send_forbidden_releases_lock_and_corrects_the_ack(monkeypatch, duel_env):
    """The mod-chat lesson (TIG first-run, 2026-07-08): /duel in a private channel the
    bot can't access acks "duel posted ⚔" and THEN 403s on the channel send. The org
    lock must release (a retry elsewhere works immediately), the starter must get an
    ephemeral correction via followup, and the Forbidden still propagates to the tree
    log."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["402620324744790017"]})
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(402620324744790017, role_ids=()))
    i.followup = MagicMock()
    i.followup.send = AsyncMock()
    resp = MagicMock()
    resp.status = 403
    i.channel.send = AsyncMock(side_effect=discord.Forbidden(resp, "Missing Access"))

    with pytest.raises(discord.Forbidden):
        await mod._handle_duel(i)

    assert mod._OPEN_DUELS == {}  # lock released — a retry in a visible channel works now
    i.followup.send.assert_called_once()
    kwargs = i.followup.send.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    text = i.followup.send.call_args.args[0]
    assert "couldn't post" in text  # the green ack is corrected, not left standing


async def test_ack_correction_failure_never_masks_the_original_error(monkeypatch, duel_env):
    """If the followup correction ITSELF fails (expired token, perms), the original
    Forbidden must still propagate and the lock must still release."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["402620324744790017"]})
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(402620324744790017, role_ids=()))
    resp = MagicMock()
    resp.status = 403
    i.channel.send = AsyncMock(side_effect=discord.Forbidden(resp, "Missing Access"))
    i.followup = MagicMock()
    i.followup.send = AsyncMock(side_effect=discord.HTTPException(resp, "also broken"))

    with pytest.raises(discord.Forbidden):
        await mod._handle_duel(i)
    assert mod._OPEN_DUELS == {}
