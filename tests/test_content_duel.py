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
    mod._VOTING.clear()
    return db_conn


def _served_cards(db_conn, message_id):
    """The two rendered-card snapshots the duel was posted with, from the mig-084 row."""
    duel = mod.cduels.get_duel(db_conn, str(message_id))
    return json.loads(duel["card_a_json"]), json.loads(duel["card_b_json"])


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
                text_="real tweet from the community", lang="en", drop=()):
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
        "lang": lang,
    }
    for key in drop:
        p.pop(key, None)
    return json.dumps(p)


def _member(user_id: int, *, role_ids=("555",), is_member=True, manage_guild=False):
    member = MagicMock(spec=discord.Member if is_member else discord.User)
    member.id = user_id
    member.bot = False
    if is_member:
        member.roles = [SimpleNamespace(id=r) for r in role_ids]
        # deterministic — a bare MagicMock's .manage_guild would read truthy
        member.guild_permissions = SimpleNamespace(manage_guild=manage_guild)
    return member


_NEXT_MSG_ID = [9000]


def _interaction(user, *, guild_id=100, channel_id=500):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.channel_id = channel_id
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.channel = MagicMock(spec=discord.TextChannel)
    # channel.send returns a message with a REAL id so _handle_duel's durable content_duels
    # insert (keyed by str(message.id)) is deterministic and votes can look it up.
    sent = MagicMock(spec=discord.Message)
    _NEXT_MSG_ID[0] += 1
    sent.id = _NEXT_MSG_ID[0]
    interaction.channel.send = AsyncMock(return_value=sent)
    return interaction


def _vote_interaction(user, message_id):
    """A button-click interaction on a specific duel message (the persistent view keys on
    interaction.message.id)."""
    vi = _interaction(user)
    vi.message = MagicMock(spec=discord.Message)
    vi.message.id = int(message_id)
    return vi


async def _cast(db_conn, user, choice, message_id):
    """Simulate a button click via the stateless persistent view."""
    vi = _vote_interaction(user, message_id)
    await mod._DuelView()._vote(vi, choice)
    return vi


async def _close(db_conn, client, message_id):
    """Run the durable close on one duel (as the sweep would)."""
    duel = mod.cduels.get_duel(db_conn, str(message_id))
    await mod._close_one(client, duel)


def _fake_client(edited_msg):
    """A client whose channel.fetch_message returns edited_msg (for the reveal edit)."""
    client = MagicMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=edited_msg)
    client.get_channel = MagicMock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    return client


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
    assert "ask one of them" in _sent_text(i)
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
    assert "ask one of them" in _sent_text(i)
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
    assert "your vote is recorded" in embed.footer.text  # member-facing disclosure
    assert isinstance(kwargs["view"], mod._DuelView)
    assert kwargs["allowed_mentions"] is not None
    assert _sent_kwargs(i)["ephemeral"] is True  # the mod ack


async def _open_duel(duel_env):
    """Post a duel and return its durable message_id (the content_duels row now exists)."""
    _sign_disclosure(duel_env)
    _seed_pending(duel_env, 1)
    _seed_pending(duel_env, 2)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    return str(i.channel.send.return_value.id)


async def test_two_members_vote_two_rows(duel_env):
    mid = await _open_duel(duel_env)
    await _cast(duel_env, _member(10, role_ids=()), "a", mid)  # NOT mods — voting is open
    await _cast(duel_env, _member(11, role_ids=()), "b", mid)
    rows = duel_env.execute(
        "SELECT actor, actor_kind, decision, surface, candidate_id, pair_loser_id "
        "FROM content_deck_decisions ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "discord:user:10" and rows[0][1] == "community"
    assert rows[0][2] == "keep" and rows[0][3] == "discord"
    winner_a, loser_a = rows[0][4], rows[0][5]
    winner_b = rows[1][4]
    assert {winner_a, loser_a} == {1, 2} and winner_a != winner_b  # opposite picks


async def test_same_member_votes_once(duel_env):
    mid = await _open_duel(duel_env)
    await _cast(duel_env, _member(10, role_ids=()), "a", mid)
    v2 = await _cast(duel_env, _member(10, role_ids=()), "b", mid)
    n = duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0]
    assert n == 1
    assert "already voted" in _sent_text(v2)


async def test_durable_dedup_survives_restart(duel_env):
    """The stateless persistent view has NO in-memory vote state — the durable DB guard is
    the only dedup, so a re-vote after a 'restart' (fresh view instance) still refuses."""
    mid = await _open_duel(duel_env)
    await _cast(duel_env, _member(10, role_ids=()), "a", mid)
    # a fresh _DuelView() (what a restart yields) has no memory of the prior vote
    v2 = await _cast(duel_env, _member(10, role_ids=()), "b", mid)
    n = duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0]
    assert n == 1
    assert "already voted" in _sent_text(v2)


async def test_failed_write_never_counts_the_vote(monkeypatch, duel_env):
    mid = await _open_duel(duel_env)
    monkeypatch.setattr(mod.cd_db, "record_deck_decision",
                        MagicMock(side_effect=RuntimeError("boom")))
    v = await _cast(duel_env, _member(10, role_ids=()), "a", mid)
    assert "couldn't record" in _sent_text(v)
    assert duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0] == 0


async def test_open_tally_is_blind_and_close_reveals(duel_env):
    mid = await _open_duel(duel_env)
    v = await _cast(duel_env, _member(10, role_ids=()), "a", mid)
    open_embed = v.response.edit_message.call_args.kwargs["embed"]
    open_text = " ".join(f"{f.name} {f.value}" for f in open_embed.fields)
    assert "votes 1" in open_text and "🅰 1" not in open_text  # count only, no split

    edited = MagicMock(spec=discord.Message)
    edited.edit = AsyncMock()
    await _close(duel_env, _fake_client(edited), mid)
    closed_embed = edited.edit.call_args.kwargs["embed"]
    closed_text = " ".join(f"{f.name} {f.value}" for f in closed_embed.fields)
    assert "🅰 1 — 0 🅱" in closed_text and "🅰 wins" in closed_text
    view = edited.edit.call_args.kwargs["view"]
    assert all(child.disabled for child in view.children)
    # the duel row is now closed
    assert mod.cduels.get_duel(duel_env, mid)["status"] == "closed"


async def test_second_duel_refused_while_one_is_open(duel_env):
    """A mod double-post (before any vote lands) is refused by the DURABLE per-channel lock
    (the content_duels open row)."""
    await _open_duel(duel_env)
    _seed_pending(duel_env, 3)
    _seed_pending(duel_env, 4)
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    assert "already open" in _sent_text(i)


async def test_close_is_single_flight(duel_env):
    """Two sweeps hitting the same duel reveal it ONCE (the DB close claim is single-flight)."""
    mid = await _open_duel(duel_env)
    e1 = MagicMock(spec=discord.Message); e1.edit = AsyncMock()
    e2 = MagicMock(spec=discord.Message); e2.edit = AsyncMock()
    duel = mod.cduels.get_duel(duel_env, mid)
    await mod._close_one(_fake_client(e1), duel)
    await mod._close_one(_fake_client(e2), duel)  # second claim loses
    assert e1.edit.called and not e2.edit.called


async def test_vote_after_deadline_refused(duel_env):
    """A vote past the hard deadline (row still 'open', sweep not yet run) is refused and
    writes nothing."""
    mid = await _open_duel(duel_env)
    duel_env.execute("UPDATE content_duels SET deadline='2000-01-01T00:00:00Z' WHERE message_id=?",
                     (mid,))
    duel_env.commit()
    v = await _cast(duel_env, _member(10, role_ids=()), "a", mid)
    assert "ended" in _sent_text(v)
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
    clicks by one member must still land exactly one row. The stateless view's
    (message,user) _VOTING guard closes the race the durable guard can't see mid-write."""
    mid = await _open_duel(duel_env)
    real_sleep = asyncio.sleep

    async def _yielding_to_thread(fn, *a, **k):
        await real_sleep(0)  # force a task switch INSIDE the vote critical section
        return fn(*a, **k)

    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(to_thread=_yielding_to_thread))
    v1 = _vote_interaction(_member(10, role_ids=()), mid)
    v2 = _vote_interaction(_member(10, role_ids=()), mid)  # same member+message, opposite button
    view = mod._DuelView()
    await asyncio.gather(view._vote(v1, "a"), view._vote(v2, "b"))
    n = duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0]
    assert n == 1


async def test_failed_write_rolls_back_the_premark(monkeypatch, duel_env):
    """A transient write failure must not lock a member out — the _VOTING guard is released
    in finally, so a retry can proceed."""
    mid = await _open_duel(duel_env)
    monkeypatch.setattr(mod.cd_db, "record_deck_decision",
                        MagicMock(side_effect=RuntimeError("boom")))
    await _cast(duel_env, _member(10, role_ids=()), "a", mid)
    assert (mid, 10) not in mod._VOTING  # released — retry possible
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

    mid = await _open_duel(duel_env)
    await _cast(duel_env, _member(10, role_ids=()), "a", mid)
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
    assert "ask one of them" in _sent_text(i)
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
    ca, cb = _served_cards(duel_env, i.channel.send.return_value.id)
    assert {ca["id"], cb["id"]} == {1, 2}
    assert ca["kind"] == cb["kind"] == "community_tweet"


async def test_json_string_encoded_duel_kinds_accepted(duel_env):
    """org config may also store duel_kinds as a JSON-STRING-encoded list — parsed,
    not refused."""
    _set_duel_kinds(duel_env, '["community_tweet"]')
    _seed_pending(duel_env, 1, kind="community_tweet", payload=_ct_payload(author="gabbyvorbeck"))
    _seed_pending(duel_env, 2, kind="community_tweet", payload=_ct_payload(author="syebastian"))
    _seed_pending(duel_env, 3, kind="tweet")
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    ca, cb = _served_cards(duel_env, i.channel.send.return_value.id)
    assert {ca["id"], cb["id"]} == {1, 2}


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
    """Two community cards (id 1 = high engagement, id 2 = low), duel opened. Returns
    (message_id, posted_kwargs)."""
    _set_duel_kinds(duel_env, ["community_tweet"])
    hi = hi_engagement or {"likes": 120, "retweets": 18, "replies": 22, "quotes": 3, "views": 15400}
    lo = lo_engagement or {"likes": 10, "retweets": 2, "replies": 5, "quotes": 0, "views": 900}
    _seed_pending(duel_env, 1, kind="community_tweet",
                  payload=_ct_payload(author="gabbyvorbeck", engagement=hi))
    _seed_pending(duel_env, 2, kind="community_tweet",
                  payload=_ct_payload(author="syebastian", engagement=lo))
    i = _interaction(_member(1))
    await mod._handle_duel(i)
    return str(i.channel.send.return_value.id), i.channel.send.call_args.kwargs


async def _closed_embed(duel_env, mid):
    """Run the durable close and return the revealed embed."""
    edited = MagicMock(spec=discord.Message)
    edited.edit = AsyncMock()
    await _close(duel_env, _fake_client(edited), mid)
    return edited.edit.call_args.kwargs["embed"]


async def test_community_cards_render_author_and_variant_footer(duel_env):
    mid, kwargs = await _open_community_duel(duel_env)
    embed = kwargs["embed"]
    f_a, f_b = embed.fields[0], embed.fields[1]
    assert f_a.name.startswith("🅰 · @") and f_b.name.startswith("🅱 · @")
    assert {f_a.name.split("@")[1], f_b.name.split("@")[1]} == {"gabbyvorbeck", "syebastian"}
    assert "real tweets from this community" in embed.footer.text
    assert "which popped?" in embed.footer.text
    # the open tally stays BLIND: count only — no split, no reality, no numbers
    assert embed.fields[2].name == "votes" and embed.fields[2].value == "0"
    assert all(f.name != "reality" for f in embed.fields)
    v = await _cast(duel_env, _member(10, role_ids=()), "a", mid)
    open_embed = v.response.edit_message.call_args.kwargs["embed"]
    open_text = " ".join(f"{f.name} {f.value}" for f in open_embed.fields)
    assert "votes 1" in open_text
    assert "reality" not in open_text and "popped" not in open_text


async def test_close_reveals_weighted_reality_and_room_verdict(duel_env):
    """The reveal: WEIGHTED score (likes + 2·RT + replies + quotes), the popped
    verdict, the room-vs-reality line, and the as-of date in the closed footer."""
    mid, _ = await _open_community_duel(duel_env)
    ca, _cb = _served_cards(duel_env, mid)
    hi_is_a = ca["id"] == 1  # RANDOM() pair order — resolve which side is which
    await _cast(duel_env, _member(10, role_ids=()), "a" if hi_is_a else "b", mid)
    closed_embed = await _closed_embed(duel_env, mid)
    reality = next(f for f in closed_embed.fields if f.name == "reality").value
    if hi_is_a:  # hi: 120 + 2*18 + 22 + 3 = 181 · lo: 10 + 2*2 + 5 + 0 = 19
        assert reality.startswith("🅰 score 181 (120❤ 18🔁 22💬 3❞) · 🅱 score 19 — 🅰 popped")
    else:
        assert reality.startswith("🅰 score 19 (10❤ 2🔁 5💬 0❞) · 🅱 score 181 — 🅱 popped")
    assert "the room called it" in reality
    assert "numbers as of 2026-07-07" in closed_embed.footer.text
    assert "real tweets from this community" in closed_embed.footer.text


async def test_close_reveal_upset_when_room_picked_the_flop(duel_env):
    mid, _ = await _open_community_duel(duel_env)
    ca, _cb = _served_cards(duel_env, mid)
    hi_is_a = ca["id"] == 1
    await _cast(duel_env, _member(10, role_ids=()), "b" if hi_is_a else "a", mid)  # picks the LOW card
    reality = next(
        f for f in (await _closed_embed(duel_env, mid)).fields if f.name == "reality"
    ).value
    assert "upset — the room picked the other one" in reality
    assert "the room called it" not in reality


async def test_close_reveal_omits_room_line_on_vote_tie(duel_env):
    """No votes (0–0) → nothing to compare — the room-vs-reality line is omitted,
    the reality numbers still show."""
    mid, _ = await _open_community_duel(duel_env)
    reality = next(
        f for f in (await _closed_embed(duel_env, mid)).fields if f.name == "reality"
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
    mid, _ = await _open_community_duel(duel_env)
    await _cast(duel_env, _member(10, role_ids=()), "a", mid)
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


# --- render polish: side-by-side, tweet-preserving text, reveal-only links ----

def _cards_from_embed(embed):
    return {f.name: f for f in embed.fields}


async def test_cards_render_side_by_side_and_links_only_at_close(monkeypatch, duel_env):
    """Cards are inline (desktop columns). The x.com permalink appears ONLY in the
    closed embed — a link during the open vote leaks the real counts (the answer)."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["402620324744790017"]})
    _sign_disclosure(duel_env)
    _set_duel_kinds(duel_env, '["community_tweet"]')
    _seed_pending(duel_env, 1, kind="community_tweet", payload=_ct_payload(author="gabbyvorbeck"))
    _seed_pending(duel_env, 2, kind="community_tweet", payload=_ct_payload(author="syebastian"))
    i = _interaction(_member(402620324744790017, role_ids=()))
    await mod._handle_duel(i)
    open_embed = i.channel.send.call_args.kwargs["embed"]

    card_fields = [f for f in open_embed.fields if f.name.startswith(("🅰", "🅱"))]
    assert len(card_fields) == 2 and all(f.inline for f in card_fields)
    # answer-leak guard: NO tweet link anywhere while the vote is open
    assert "x.com" not in str(open_embed.to_dict())

    ca, cb = _served_cards(duel_env, i.channel.send.return_value.id)
    closed = mod._duel_embed("solstitch", ca, cb, votes=1, closed=True, tally=(1, 0))
    links = next(f for f in closed.fields if f.name == "the tweets")
    assert "https://x.com/gabbyvorbeck/status/1938291000000000000" in links.value
    assert "https://x.com/syebastian/status/1938291000000000000" in links.value


def test_tweet_render_unescapes_entities_and_keeps_line_breaks():
    """Real-tweet cosmetics: SocialData full_text carries HTML entities (&amp;) and
    meaningful line breaks — both must survive to the card. AI cards keep the original
    single-line collapse."""
    community = {"author": "a", "text": "gm &amp; gn\n\nline two   spaced"}
    ai = {"kind": "tweet", "text": "gm &amp; gn\n\nline two   spaced"}
    assert mod._clip_tweet(community["text"]) == "gm & gn\n\nline two spaced"
    assert mod._clip(ai["text"]) == "gm &amp; gn line two spaced"


def test_tweet_url_is_derived_and_validated():
    assert mod._tweet_url({"author": "gabby", "x_id": "123456"}) == \
        "https://x.com/gabby/status/123456"
    assert mod._tweet_url({"author": "gabby", "x_id": "12x456"}) is None  # non-digits
    assert mod._tweet_url({"author": "gabby"}) is None                    # no id
    assert mod._tweet_url({"x_id": "123456"}) is None                     # no author
    assert mod._tweet_url({"author": "gabby", "x_id": "1" * 26}) is None  # absurd length


# --- mod-managed duel-starter whitelist (/duel-allow) + mention leaderboard ---

def _config(conn, org="solstitch"):
    return mod.get_org_config_value(conn, org, "duel_starters_extra")


async def test_extra_starter_is_additive_and_can_start(monkeypatch, duel_env, db_conn):
    """A member who is NOT an env starter and NOT a mod, but IS on the config
    duel_starters_extra list, can start — checked first, purely additive."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["111"]})  # env baseline = user 111
    _sign_disclosure(db_conn)
    _set_duel_kinds(db_conn, '["community_tweet"]')
    mod.set_org_config(db_conn, "solstitch", "duel_starters_extra", json.dumps(["999"]))
    db_conn.commit()
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="a"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="b"))
    # user 999: not env-seeded (111 is), zero mod roles — allowed only via config extras
    i = _interaction(_member(999, role_ids=()))
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()


async def test_extra_starters_do_not_lock_out_mod_fallback(monkeypatch, duel_env, db_conn):
    """A guild with NO env starters uses the MOD_ROLES fallback. Adding a config extra
    must stay ADDITIVE — a mod-role holder can STILL start (the base mode is unchanged,
    unlike the env allowlist which is exclusive)."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {})  # no env allowlist → MOD_ROLES fallback
    _sign_disclosure(db_conn)
    _set_duel_kinds(db_conn, '["community_tweet"]')
    mod.set_org_config(db_conn, "solstitch", "duel_starters_extra", json.dumps(["999"]))
    db_conn.commit()
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="a"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="b"))
    i = _interaction(_member(42, role_ids=("555",)))  # a mod-role holder, NOT in config
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # mods still start — extras never locked them out


async def test_duel_allow_grants_persists_and_enables(duel_env, db_conn):
    """A Manage-Server actor grants a member → config list updated → that member can
    now start a duel."""
    _sign_disclosure(db_conn)
    admin = _member(1, role_ids=(), manage_guild=True)
    target = _member(777, role_ids=())
    i = _interaction(admin)
    await mod._handle_starter_change(i, target, grant=True)
    assert "777" in (json.loads(_config(db_conn)) or [])
    assert mod._can_start_duel(target, "100", "solstitch") is True
    assert "can now start duels" in _sent_text(i)


async def test_duel_allow_denied_for_plain_member(duel_env, db_conn):
    """No Manage-Server, no mod role → refused, config untouched."""
    _sign_disclosure(db_conn)
    plain = _member(2, role_ids=())  # no mod role, manage_guild False
    target = _member(777, role_ids=())
    i = _interaction(plain)
    await mod._handle_starter_change(i, target, grant=True)
    assert "Manage Server" in _sent_text(i)
    assert _config(db_conn) is None  # nothing written


async def test_duel_allow_mod_role_holder_may_manage(duel_env, db_conn):
    """A configured MOD_ROLES holder can manage the list even without Manage-Server."""
    _sign_disclosure(db_conn)
    modder = _member(3, role_ids=("555",), manage_guild=False)
    target = _member(777, role_ids=())
    i = _interaction(modder)
    await mod._handle_starter_change(i, target, grant=True)
    assert "777" in (json.loads(_config(db_conn)) or [])


async def test_duel_revoke_removes_config_but_not_env_seeded(monkeypatch, duel_env, db_conn):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["111"]})  # 111 is env-seeded
    _sign_disclosure(db_conn)
    mod.set_org_config(db_conn, "solstitch", "duel_starters_extra", json.dumps(["777"]))
    db_conn.commit()
    admin = _member(1, role_ids=(), manage_guild=True)

    i = _interaction(admin)
    await mod._handle_starter_change(i, _member(777, role_ids=()), grant=False)
    assert "777" not in (json.loads(_config(db_conn)) or [])
    assert "no longer" in _sent_text(i)

    # env-seeded 111 can't be revoked here — reported, not removed
    i2 = _interaction(admin)
    await mod._handle_starter_change(i2, _member(111, role_ids=()), grant=False)
    assert "team-seeded starter" in _sent_text(i2)


async def test_duel_allow_rejects_bots(duel_env, db_conn):
    _sign_disclosure(db_conn)
    admin = _member(1, role_ids=(), manage_guild=True)
    bot_target = _member(9, role_ids=())
    bot_target.bot = True
    i = _interaction(admin)
    await mod._handle_starter_change(i, bot_target, grant=True)
    assert "bots can't" in _sent_text(i)
    assert _config(db_conn) is None


async def test_leaderboard_renders_mentions_not_raw_ids(monkeypatch, duel_env, db_conn):
    """The old code showed 'member 1234' (raw-id tail) whenever the member wasn't in the
    gateway cache — which is ALWAYS for a bot running without the Members intent. The
    leaderboard now renders <@id> mentions (client-resolved, no cache, no ping)."""
    _sign_disclosure(db_conn)
    monkeypatch.setattr(
        mod.cd_db, "get_community_duel_leaderboard",
        lambda conn, org: [
            {"actor": "discord:user:402620324744790017", "votes": 7, "agreed": 0, "decided": 0},
            {"actor": "discord:user:683759563316789266", "votes": 4, "agreed": 0, "decided": 0},
        ],
    )
    i = _interaction(_member(1, role_ids=()))
    await mod._handle_tasteboard(i)
    desc = i.response.send_message.call_args.kwargs["embed"].description
    assert "<@402620324744790017>" in desc and "<@683759563316789266>" in desc
    assert "member " not in desc  # no raw-id fallback
    # community_tweet duels never reach an ops verdict → no "called it" note, no jargon
    footer = i.response.send_message.call_args.kwargs["embed"].footer.text
    assert "top guessers" in footer and "ops" not in footer


# --- per-channel language routing (zh channel serves zh cards) ----------------

def _set_lang_cfg(conn, *, channel_map=None, default=None, kinds='["community_tweet"]',
                  org="solstitch"):
    """Write the disclosure + duel_kinds + duel_channel_lang/duel_default_lang config."""
    cfg = {"pairwise_disclosure_signed": "2026-07-08 sieggy — full-reign",
           "duel_kinds": kinds}
    if channel_map is not None:
        cfg["duel_channel_lang"] = json.dumps(channel_map)
    if default is not None:
        cfg["duel_default_lang"] = default
    conn.execute("UPDATE orgs SET config_json = ? WHERE org_id = ?", (json.dumps(cfg), org))
    conn.commit()


def test_channel_lang_reader(duel_env, db_conn):
    """_channel_lang: no config → None; mapped channel → its lang; unmapped w/ default →
    default; unmapped w/ a map but no default → 'en'; malformed map → None (fail-safe)."""
    assert mod._channel_lang("solstitch", 500) is None  # nothing configured
    _set_lang_cfg(db_conn, channel_map={"777": "zh"})
    assert mod._channel_lang("solstitch", 777) == "zh"          # mapped
    assert mod._channel_lang("solstitch", 999) == "en"          # unmapped, map exists → default bucket
    _set_lang_cfg(db_conn, channel_map={"777": "zh"}, default="es")
    assert mod._channel_lang("solstitch", 999) == "es"          # unmapped → configured default
    # malformed map → None (serve whole pool, never mis-route)
    cfg = {"pairwise_disclosure_signed": "x", "duel_channel_lang": "{not json"}
    db_conn.execute("UPDATE orgs SET config_json = ? WHERE org_id = 'solstitch'",
                    (json.dumps(cfg),))
    db_conn.commit()
    assert mod._channel_lang("solstitch", 777) is None


async def test_duel_in_zh_channel_serves_zh_cards(monkeypatch, duel_env, db_conn):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["1"]})
    _set_lang_cfg(db_conn, channel_map={"777": "zh"})
    # 2 zh + 2 en community_tweet cards, distinct authors
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="zhone", lang="zh"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="zhtwo", lang="zh"))
    _seed_pending(db_conn, 3, kind="community_tweet", payload=_ct_payload(author="enone", lang="en"))
    _seed_pending(db_conn, 4, kind="community_tweet", payload=_ct_payload(author="entwo", lang="en"))
    i = _interaction(_member(1, role_ids=()), channel_id=777)
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()
    embed = i.channel.send.call_args.kwargs["embed"]
    authors = {f.name.split("@")[1] for f in embed.fields if "@" in f.name}
    assert authors <= {"zhone", "zhtwo"}  # ONLY zh authors, never en


async def test_zh_channel_refuses_when_pool_too_thin(monkeypatch, duel_env, db_conn):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["1"]})
    _set_lang_cfg(db_conn, channel_map={"777": "zh"})
    # only ONE zh card + plenty of en — the zh channel must REFUSE, never serve en
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="zhone", lang="zh"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="enone", lang="en"))
    _seed_pending(db_conn, 3, kind="community_tweet", payload=_ct_payload(author="entwo", lang="en"))
    i = _interaction(_member(1, role_ids=()), channel_id=777)
    await mod._handle_duel(i)
    i.channel.send.assert_not_called()  # no duel posted
    assert "not enough fresh zh content" in _sent_text(i)


async def test_unmapped_channel_serves_default_not_zh(monkeypatch, duel_env, db_conn):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["1"]})
    _set_lang_cfg(db_conn, channel_map={"777": "zh"})  # 777 is zh; we run in 999
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="zhone", lang="zh"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="zhtwo", lang="zh"))
    _seed_pending(db_conn, 3, kind="community_tweet", payload=_ct_payload(author="enone", lang="en"))
    _seed_pending(db_conn, 4, kind="community_tweet", payload=_ct_payload(author="entwo", lang="en"))
    i = _interaction(_member(1, role_ids=()), channel_id=999)  # unmapped → default 'en'
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()
    embed = i.channel.send.call_args.kwargs["embed"]
    authors = {f.name.split("@")[1] for f in embed.fields if "@" in f.name}
    assert authors <= {"enone", "entwo"}  # zh cards stay in their channel


async def test_no_lang_config_serves_whole_pool(monkeypatch, duel_env, db_conn):
    """With no duel_channel_lang/default set, routing is off — the whole pool (any lang)."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["1"]})
    _set_duel_kinds(db_conn, '["community_tweet"]')  # disclosure + kinds, NO lang keys
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="zhone", lang="zh"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="entwo", lang="en"))
    i = _interaction(_member(1, role_ids=()), channel_id=500)
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # serves the pair from the whole pool


# --- duel rate limits (Arf unlimited; others 1/day/channel) -------------------

def _set_unlimited(conn, user_ids, org="solstitch"):
    """Set duel_unlimited_starters alongside the disclosure gate."""
    cfg = {"pairwise_disclosure_signed": "2026-07-09 sieggy — full-reign",
           "duel_kinds": '["community_tweet"]',
           "duel_unlimited_starters": json.dumps([str(u) for u in user_ids])}
    conn.execute("UPDATE orgs SET config_json = ? WHERE org_id = ?", (json.dumps(cfg), org))
    conn.commit()


def _log_duel_open(conn, user_id, channel_id, *, org="solstitch", ts=None):
    """Simulate a durable content_duel_opened audit row (what the rate limit counts)."""
    import datetime as _dt
    ts = ts or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO audit_log (timestamp, actor, action, org_id, detail_json, source) "
        "VALUES (?, ?, 'content_duel_opened', ?, ?, 'sable-roles')",
        (ts, f"discord:user:{user_id}", org,
         json.dumps({"channel_id": str(channel_id), "candidate_a": 1, "candidate_b": 2})),
    )
    conn.commit()


def _zh_ready(conn):
    """Disclosure + community_tweet kind + 2 pending cards so a duel can actually post."""
    _set_duel_kinds(conn, '["community_tweet"]')
    _seed_pending(conn, 1, kind="community_tweet", payload=_ct_payload(author="a"))
    _seed_pending(conn, 2, kind="community_tweet", payload=_ct_payload(author="b"))


async def test_non_unlimited_starter_capped_one_per_channel_per_day(monkeypatch, duel_env, db_conn):
    """P0ison-style: one duel per channel per day. A prior open TODAY in this channel
    (from the durable audit trail) blocks a second."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["50"]})
    _zh_ready(db_conn)  # writes disclosure+kinds, no unlimited list
    _log_duel_open(db_conn, 50, 500)  # already opened one in channel 500 today
    i = _interaction(_member(50, role_ids=()), channel_id=500)
    await mod._handle_duel(i)
    i.channel.send.assert_not_called()
    assert "already started a duel in this channel today" in _sent_text(i)


async def test_cap_is_per_channel_other_channel_ok(monkeypatch, duel_env, db_conn):
    """The cap is PER channel — a duel used in channel 500 doesn't block channel 600."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["50"]})
    _zh_ready(db_conn)
    _log_duel_open(db_conn, 50, 500)  # used channel 500 today
    i = _interaction(_member(50, role_ids=()), channel_id=600)  # different channel
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # channel 600 is free


async def test_yesterdays_open_does_not_count(monkeypatch, duel_env, db_conn):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["50"]})
    _zh_ready(db_conn)
    _log_duel_open(db_conn, 50, 500, ts="2020-01-01 00:00:00")  # ancient
    i = _interaction(_member(50, role_ids=()), channel_id=500)
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # only TODAY's opens count


async def test_arf_unlimited_bypasses_the_cap(monkeypatch, duel_env, db_conn):
    """Arf (on duel_unlimited_starters) can open even after a same-channel open today."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["402620324744790017"]})
    _set_unlimited(db_conn, ["402620324744790017"])
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="a"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="b"))
    _log_duel_open(db_conn, 402620324744790017, 500)  # already one today
    i = _interaction(_member(402620324744790017, role_ids=()), channel_id=500)
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # no cap for the unlimited allowlist


async def test_open_lock_is_per_channel(monkeypatch, duel_env, db_conn):
    """A live duel in channel 500 doesn't block a duel in channel 600 (per-channel lock)."""
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["402620324744790017"]})
    _set_unlimited(db_conn, ["402620324744790017"])  # unlimited so the daily cap is irrelevant
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="a"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="b"))
    import time as _t
    mod._OPEN_DUELS["500"] = _t.monotonic() + 600  # channel 500 has a live duel
    i = _interaction(_member(402620324744790017, role_ids=()), channel_id=600)
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # 600 is free
    # but the SAME channel with a live duel is refused
    j = _interaction(_member(402620324744790017, role_ids=()), channel_id=500)
    await mod._handle_duel(j)
    assert "already open in this channel" in _sent_text(j)


# --- generalized per-channel content profiles (duel_channels) -----------------

def _set_channels(conn, channels, *, default=None, kinds='["community_tweet"]', org="solstitch"):
    cfg = {"pairwise_disclosure_signed": "2026-07-11 sieggy — full-reign",
           "duel_kinds": kinds,
           "duel_channels": json.dumps(channels)}
    if default is not None:
        cfg["duel_default_channel"] = json.dumps(default)
    conn.execute("UPDATE orgs SET config_json = ? WHERE org_id = ?", (json.dumps(cfg), org))
    conn.commit()


def test_channel_profile_resolution(duel_env, db_conn):
    """duel_channels resolves per-channel profiles; unmapped → default; malformed dropped;
    no duel_channels → legacy duel_channel_lang fallback."""
    _set_channels(db_conn, {
        "700": {"require_terms": ["prometheus"], "label": "Prometheus"},
        "701": {"kinds": ["meme"], "label": "memes"},
        "702": {},                       # general — anything
        "703": {"kinds": ["nonsense"], "require_terms": ["x"]},  # bad kind dropped, term kept
    }, default={"lang": "en"})
    assert mod._channel_profile("solstitch", 700) == {"require_terms": ("prometheus",), "label": "Prometheus"}
    assert mod._channel_profile("solstitch", 701) == {"kinds": ("meme",), "label": "memes"}
    assert mod._channel_profile("solstitch", 702) == {}                 # general
    assert mod._channel_profile("solstitch", 703) == {"require_terms": ("x",)}  # bad kind pruned
    assert mod._channel_profile("solstitch", 999) == {"lang": "en"}     # unmapped → default

    # no duel_channels at all → legacy lang path
    _set_lang_cfg(db_conn, channel_map={"800": "zh"})
    assert mod._channel_profile("solstitch", 800) == {"lang": "zh"}


async def test_prometheus_channel_serves_only_prometheus(monkeypatch, duel_env, db_conn):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["1"]})
    _set_unlimited(db_conn, [1])  # skip the daily cap for the test
    # re-apply channels config (the _set_unlimited call above overwrote config_json)
    cfg = json.loads(db_conn.execute("SELECT config_json FROM orgs WHERE org_id='solstitch'").fetchone()[0])
    cfg["duel_channels"] = json.dumps({"700": {"require_terms": ["prometheus"], "label": "Prometheus"}})
    db_conn.execute("UPDATE orgs SET config_json=? WHERE org_id='solstitch'", (json.dumps(cfg),))
    db_conn.commit()
    _seed_pending(db_conn, 1, kind="community_tweet",
                  payload=_ct_payload(author="pa", text_="the Prometheus swarm beta popped off"))
    _seed_pending(db_conn, 2, kind="community_tweet",
                  payload=_ct_payload(author="pb", text_="Prometheus public release soon"))
    _seed_pending(db_conn, 3, kind="community_tweet",
                  payload=_ct_payload(author="oa", text_="just bought more $tig"))
    i = _interaction(_member(1, role_ids=()), channel_id=700)
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()
    authors = {f.name.split("@")[1] for f in i.channel.send.call_args.kwargs["embed"].fields if "@" in f.name}
    assert authors <= {"pa", "pb"}  # never the off-topic card


async def test_topic_channel_refuses_with_label_when_thin(monkeypatch, duel_env, db_conn):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["1"]})
    _set_unlimited(db_conn, [1])
    cfg = json.loads(db_conn.execute("SELECT config_json FROM orgs WHERE org_id='solstitch'").fetchone()[0])
    cfg["duel_channels"] = json.dumps({"700": {"require_terms": ["prometheus"], "label": "Prometheus"}})
    db_conn.execute("UPDATE orgs SET config_json=? WHERE org_id='solstitch'", (json.dumps(cfg),))
    db_conn.commit()
    _seed_pending(db_conn, 1, kind="community_tweet",
                  payload=_ct_payload(author="pa", text_="Prometheus beta"))  # only ONE match
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="oa", text_="gm $tig"))
    i = _interaction(_member(1, role_ids=()), channel_id=700)
    await mod._handle_duel(i)
    i.channel.send.assert_not_called()
    assert "not enough fresh Prometheus content" in _sent_text(i)


async def test_general_channel_empty_profile_serves_anything(monkeypatch, duel_env, db_conn):
    monkeypatch.setattr(mod, "DUEL_STARTERS", {"100": ["1"]})
    _set_channels(db_conn, {"702": {}}, default={"require_terms": ["prometheus"]})
    _set_unlimited(db_conn, [1])
    cfg = json.loads(db_conn.execute("SELECT config_json FROM orgs WHERE org_id='solstitch'").fetchone()[0])
    cfg["duel_channels"] = json.dumps({"702": {}})
    cfg["duel_kinds"] = '["community_tweet"]'
    db_conn.execute("UPDATE orgs SET config_json=? WHERE org_id='solstitch'", (json.dumps(cfg),))
    db_conn.commit()
    _seed_pending(db_conn, 1, kind="community_tweet", payload=_ct_payload(author="a", text_="gm $tig"))
    _seed_pending(db_conn, 2, kind="community_tweet", payload=_ct_payload(author="b", text_="wagmi $tig"))
    i = _interaction(_member(1, role_ids=()), channel_id=702)  # general — no topic filter
    await mod._handle_duel(i)
    i.channel.send.assert_called_once()  # off-topic cards are fine in general


# --- restart durability (persistent view + durable close sweep, mig 084) ------

def test_persistent_view_has_static_custom_ids():
    """The view is registered ONCE (client.add_view) and routes clicks on any duel message
    by static custom_id — the whole point of surviving a restart."""
    view = mod._DuelView()
    assert view.timeout is None  # persistent
    cids = {c.custom_id for c in view.children if isinstance(c, discord.ui.Button)}
    assert cids == {"duel:vote:a", "duel:vote:b"}


async def test_vote_works_on_a_fresh_view_instance(duel_env):
    """After a 'restart' the original view object is gone — a click on the old message goes
    to a FRESH _DuelView() and must still record (state is looked up by message_id)."""
    mid = await _open_duel(duel_env)
    # a brand-new view instance (what add_view holds post-restart) handles the click
    vi = _vote_interaction(_member(10, role_ids=()), mid)
    await mod._DuelView()._vote(vi, "a")
    n = duel_env.execute("SELECT COUNT(*) FROM content_deck_decisions").fetchone()[0]
    assert n == 1


async def test_startup_sweep_closes_a_duel_that_expired_during_downtime(duel_env):
    """The close is driven by the durable sweep, not a view timeout — a duel whose deadline
    passed WHILE THE BOT WAS DOWN is revealed on the next sweep (find due → close each)."""
    mid = await _open_duel(duel_env)
    await _cast(duel_env, _member(10, role_ids=()), "a", mid)
    # its deadline lapsed during downtime
    duel_env.execute("UPDATE content_duels SET deadline='2000-01-01T00:00:00Z' WHERE message_id=?",
                     (mid,))
    duel_env.commit()
    # the sweep body: find due, close each
    due = mod.cduels.list_due_duels(duel_env)
    assert [d["message_id"] for d in due] == [mid]
    edited = MagicMock(spec=discord.Message); edited.edit = AsyncMock()
    await mod._close_one(_fake_client(edited), due[0])
    assert edited.edit.called
    closed = edited.edit.call_args.kwargs["embed"]
    assert any("🅰 1 — 0 🅱" in f.value for f in closed.fields)  # revealed the real tally
    assert mod.cduels.get_duel(duel_env, mid)["status"] == "closed"


async def test_reveal_edit_failure_still_marks_closed(duel_env):
    """A deleted message / kicked bot at close: the row is still marked closed (no infinite
    retry) and votes were already durable."""
    mid = await _open_duel(duel_env)
    duel = mod.cduels.get_duel(duel_env, mid)
    client = MagicMock()
    channel = MagicMock()
    resp = MagicMock(); resp.status = 404
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(resp, "gone"))
    client.get_channel = MagicMock(return_value=channel)
    await mod._close_one(client, duel)
    assert mod.cduels.get_duel(duel_env, mid)["status"] == "closed"
