"""Tests for vibe_observer (R10) — listener + rollup + GC + kill switch."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sable_platform.db import discord_roast, discord_user_vibes
from sable_roles.features import vibe_observer


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


@pytest.fixture
def patched_observer(monkeypatch, db_conn):
    monkeypatch.setattr(vibe_observer, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(vibe_observer, "OBSERVATION_CHANNELS", {})
    monkeypatch.setattr(vibe_observer, "VIBE_OBSERVATION_ENABLED", True)
    monkeypatch.setattr(vibe_observer, "VIBE_OBSERVATION_WINDOW_DAYS", 30)
    monkeypatch.setattr(
        vibe_observer, "get_db", lambda: _make_db_context(db_conn)()
    )
    return vibe_observer


def _make_message(
    *,
    author_id: int = 555,
    bot: bool = False,
    guild_id: int | None = 100,
    channel_id: int = 200,
    text_channel: bool = True,
    message_id: int = 700,
    content: str = "test content",
    created_at: datetime | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    author = MagicMock()
    author.id = author_id
    author.bot = bot
    msg.author = author
    if guild_id is None:
        msg.guild = None
    else:
        msg.guild = MagicMock()
        msg.guild.id = guild_id
    if text_channel:
        msg.channel = MagicMock(spec=discord.TextChannel)
    else:
        msg.channel = MagicMock(spec=discord.Thread)
    msg.channel.id = channel_id
    msg.id = message_id
    msg.content = content
    msg.created_at = (
        created_at or datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    )
    return msg


def _make_reaction_payload(
    *,
    guild_id: int | None = 100,
    user_id: int = 777,
    channel_id: int = 200,
    message_id: int = 700,
    emoji: str = "🔥",
) -> MagicMock:
    p = MagicMock()
    p.guild_id = guild_id
    p.user_id = user_id
    p.channel_id = channel_id
    p.message_id = message_id
    emoji_obj = MagicMock()
    emoji_obj.__str__ = lambda self: emoji
    p.emoji = emoji_obj
    return p


# ---------------------------------------------------------------------------
# _observe_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_message_inserts_raw_row(patched_observer, db_conn):
    msg = _make_message(content="bold fit")
    await vibe_observer._observe_message(msg)
    rows = db_conn.execute(
        "SELECT user_id, guild_id, channel_id, content_truncated"
        " FROM discord_message_observations"
    ).fetchall()
    rs = [dict(r._mapping if hasattr(r, "_mapping") else r) for r in rows]
    assert len(rs) == 1
    assert rs[0]["user_id"] == "555"
    assert rs[0]["guild_id"] == "100"
    assert rs[0]["content_truncated"] == "bold fit"


@pytest.mark.asyncio
async def test_observe_message_truncates_long_content(patched_observer, db_conn):
    msg = _make_message(content="x" * 1000)
    await vibe_observer._observe_message(msg)
    row = db_conn.execute(
        "SELECT content_truncated FROM discord_message_observations"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert len(rd["content_truncated"]) == 500


@pytest.mark.asyncio
async def test_observe_message_skips_bot(patched_observer, db_conn):
    msg = _make_message(bot=True)
    await vibe_observer._observe_message(msg)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_observe_message_skips_dm(patched_observer, db_conn):
    msg = _make_message(guild_id=None)
    await vibe_observer._observe_message(msg)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_observe_message_skips_unconfigured_guild(
    patched_observer, db_conn
):
    msg = _make_message(guild_id=999)
    await vibe_observer._observe_message(msg)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_observe_message_skips_non_text_channel(patched_observer, db_conn):
    msg = _make_message(text_channel=False)
    await vibe_observer._observe_message(msg)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_observe_message_skips_out_of_scope_channel(
    monkeypatch, patched_observer, db_conn
):
    """OBSERVATION_CHANNELS allowlist set → off-allowlist channel skipped."""
    monkeypatch.setattr(
        vibe_observer, "OBSERVATION_CHANNELS", {"100": ["200"]}
    )
    msg = _make_message(channel_id=999)  # not in allowlist
    await vibe_observer._observe_message(msg)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_observe_message_skips_blocklisted_user(
    patched_observer, db_conn
):
    discord_roast.insert_blocklist(db_conn, "100", "555")
    msg = _make_message(author_id=555)
    await vibe_observer._observe_message(msg)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_observe_message_honors_kill_switch(
    monkeypatch, patched_observer, db_conn
):
    monkeypatch.setattr(vibe_observer, "VIBE_OBSERVATION_ENABLED", False)
    msg = _make_message()
    await vibe_observer._observe_message(msg)
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


# ---------------------------------------------------------------------------
# _observe_reaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_reaction_merges_emoji_count(patched_observer, db_conn):
    msg = _make_message(message_id=700)
    await vibe_observer._observe_message(msg)
    payload = _make_reaction_payload(message_id=700, emoji="🔥")
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 999999
    # Channel-cache lookup returns a real TextChannel double so the new
    # symmetric channel-type filter (mirrors _observe_message) passes.
    text_channel = MagicMock(spec=discord.TextChannel)
    fake_client.get_channel = MagicMock(return_value=text_channel)
    await vibe_observer._observe_reaction(payload, client=fake_client)
    row = db_conn.execute(
        "SELECT reactions_given_json FROM discord_message_observations"
        " WHERE message_id='700'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    parsed = json.loads(rd["reactions_given_json"])
    assert parsed == {"🔥": 1}


@pytest.mark.asyncio
async def test_observe_reaction_skips_non_text_channel(
    patched_observer, db_conn
):
    """Mirrors _observe_message's text-channel filter — reactions on
    threads / voice / category channels are NOT merged (defense in
    depth; _observe_message already blocks the row from existing)."""
    msg = _make_message(message_id=700)
    await vibe_observer._observe_message(msg)
    payload = _make_reaction_payload(message_id=700, emoji="🔥")
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 999999
    # Thread, not TextChannel → filter blocks the merge.
    fake_client.get_channel = MagicMock(
        return_value=MagicMock(spec=discord.Thread)
    )
    await vibe_observer._observe_reaction(payload, client=fake_client)
    row = db_conn.execute(
        "SELECT reactions_given_json FROM discord_message_observations"
        " WHERE message_id='700'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    # Reactions never merged — column stays NULL.
    assert rd["reactions_given_json"] is None


@pytest.mark.asyncio
async def test_observe_reaction_skips_bot_self_reaction(
    patched_observer, db_conn
):
    msg = _make_message(message_id=700)
    await vibe_observer._observe_message(msg)
    payload = _make_reaction_payload(user_id=111, message_id=700)
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 111  # bot is the reactor
    await vibe_observer._observe_reaction(payload, client=fake_client)
    row = db_conn.execute(
        "SELECT reactions_given_json FROM discord_message_observations"
        " WHERE message_id='700'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["reactions_given_json"] is None


@pytest.mark.asyncio
async def test_observe_reaction_skips_dm(patched_observer, db_conn):
    payload = _make_reaction_payload(guild_id=None)
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 999999
    # No exception, no DB writes
    await vibe_observer._observe_reaction(payload, client=fake_client)


@pytest.mark.asyncio
async def test_observe_reaction_skips_blocklisted_reactor(
    patched_observer, db_conn
):
    msg = _make_message(message_id=700)
    await vibe_observer._observe_message(msg)
    discord_roast.insert_blocklist(db_conn, "100", "777")
    payload = _make_reaction_payload(user_id=777, message_id=700)
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 999999
    await vibe_observer._observe_reaction(payload, client=fake_client)
    row = db_conn.execute(
        "SELECT reactions_given_json FROM discord_message_observations"
        " WHERE message_id='700'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["reactions_given_json"] is None


@pytest.mark.asyncio
async def test_observe_reaction_kill_switch(
    monkeypatch, patched_observer, db_conn
):
    msg = _make_message(message_id=700)
    await vibe_observer._observe_message(msg)
    monkeypatch.setattr(vibe_observer, "VIBE_OBSERVATION_ENABLED", False)
    payload = _make_reaction_payload(message_id=700)
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 999999
    await vibe_observer._observe_reaction(payload, client=fake_client)
    row = db_conn.execute(
        "SELECT reactions_given_json FROM discord_message_observations"
        " WHERE message_id='700'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["reactions_given_json"] is None


# ---------------------------------------------------------------------------
# _rollup_pass
# ---------------------------------------------------------------------------


def _seed_observation(
    db_conn, *, user_id: str, message_id: str,
    content: str | None = "msg", reactions: dict | None = None,
    posted_at: str = "2026-05-15T12:00:00Z",
    guild_id: str = "100", channel_id: str = "200",
):
    db_conn.execute(
        "INSERT INTO discord_message_observations"
        " (guild_id, channel_id, message_id, user_id, content_truncated,"
        "  reactions_given_json, posted_at, captured_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, channel_id, message_id, user_id, content,
         json.dumps(reactions) if reactions else None,
         posted_at, posted_at),
    )
    db_conn.commit()


@pytest.mark.asyncio
async def test_rollup_pass_creates_rollup_row(patched_observer, db_conn):
    _seed_observation(db_conn, user_id="555", message_id="m1", content="hello")
    _seed_observation(db_conn, user_id="555", message_id="m2", content="world")
    await vibe_observer._rollup_pass()
    row = db_conn.execute(
        "SELECT message_count, sample_messages_json, channels_active_in_json"
        " FROM discord_user_observations WHERE user_id='555'"
    ).fetchone()
    assert row is not None
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    assert rd["message_count"] == 2
    samples = json.loads(rd["sample_messages_json"])
    assert set(samples) == {"hello", "world"}
    channels = json.loads(rd["channels_active_in_json"])
    assert channels == ["200"]


@pytest.mark.asyncio
async def test_rollup_pass_merges_reactions(patched_observer, db_conn):
    _seed_observation(
        db_conn, user_id="555", message_id="m1",
        reactions={"🔥": 2, "💯": 1},
    )
    _seed_observation(
        db_conn, user_id="555", message_id="m2",
        reactions={"🔥": 1, "💀": 3},
    )
    await vibe_observer._rollup_pass()
    row = db_conn.execute(
        "SELECT reaction_emojis_given_json FROM discord_user_observations"
        " WHERE user_id='555'"
    ).fetchone()
    rd = dict(row._mapping if hasattr(row, "_mapping") else row)
    merged = json.loads(rd["reaction_emojis_given_json"])
    assert merged == {"🔥": 3, "💯": 1, "💀": 3}


@pytest.mark.asyncio
async def test_rollup_pass_isolates_per_user(patched_observer, db_conn):
    _seed_observation(db_conn, user_id="555", message_id="m1")
    _seed_observation(db_conn, user_id="666", message_id="m2")
    await vibe_observer._rollup_pass()
    rows = db_conn.execute(
        "SELECT user_id FROM discord_user_observations"
    ).fetchall()
    ids = {dict(r._mapping if hasattr(r, "_mapping") else r)["user_id"] for r in rows}
    assert ids == {"555", "666"}


@pytest.mark.asyncio
async def test_rollup_pass_kill_switch(
    monkeypatch, patched_observer, db_conn
):
    _seed_observation(db_conn, user_id="555", message_id="m1")
    monkeypatch.setattr(vibe_observer, "VIBE_OBSERVATION_ENABLED", False)
    await vibe_observer._rollup_pass()
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_user_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_rollup_pass_isolates_per_guild(patched_observer, db_conn):
    """A second guild not in GUILD_TO_ORG must NOT roll up."""
    _seed_observation(
        db_conn, user_id="555", message_id="m1", guild_id="100",
    )
    _seed_observation(
        db_conn, user_id="555", message_id="m2", guild_id="999",
    )
    await vibe_observer._rollup_pass()
    rows = db_conn.execute(
        "SELECT guild_id FROM discord_user_observations"
    ).fetchall()
    guilds = {dict(r._mapping if hasattr(r, "_mapping") else r)["guild_id"] for r in rows}
    assert guilds == {"100"}


# ---------------------------------------------------------------------------
# _gc_pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gc_pass_drops_old_rows(patched_observer, db_conn):
    """Seed an observation captured 40 days ago → GC drops it
    (WINDOW=30 + headroom=7 → cutoff is 37 days)."""
    old_iso = (
        datetime.now(timezone.utc) - timedelta(days=40)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_conn.execute(
        "INSERT INTO discord_message_observations"
        " (guild_id, channel_id, message_id, user_id, posted_at, captured_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("100", "200", "old_m", "555", old_iso, old_iso),
    )
    db_conn.commit()
    await vibe_observer._gc_pass()
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 0


@pytest.mark.asyncio
async def test_gc_pass_keeps_recent_rows(patched_observer, db_conn):
    _seed_observation(db_conn, user_id="555", message_id="m1")
    await vibe_observer._gc_pass()
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 1


@pytest.mark.asyncio
async def test_gc_pass_kill_switch(
    monkeypatch, patched_observer, db_conn
):
    """Kill switch off → GC also halts. Stale data preserved for re-enable."""
    old_iso = (
        datetime.now(timezone.utc) - timedelta(days=40)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_conn.execute(
        "INSERT INTO discord_message_observations"
        " (guild_id, channel_id, message_id, user_id, posted_at, captured_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("100", "200", "old_m", "555", old_iso, old_iso),
    )
    db_conn.commit()
    monkeypatch.setattr(vibe_observer, "VIBE_OBSERVATION_ENABLED", False)
    await vibe_observer._gc_pass()
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM discord_message_observations"
    ).fetchone()
    assert dict(n._mapping if hasattr(n, "_mapping") else n)["n"] == 1


# ---------------------------------------------------------------------------
# Register composition + channel scope helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composes_with_existing_handlers(
    patched_observer, db_conn
):
    client = discord.Client(intents=discord.Intents.default())
    msg_calls: list = []
    react_calls: list = []

    @client.event
    async def on_message(message):
        msg_calls.append(message)

    @client.event
    async def on_raw_reaction_add(payload):
        react_calls.append(payload)

    vibe_observer.register(client)

    msg = _make_message()
    await client.on_message(msg)
    assert len(msg_calls) == 1

    payload = _make_reaction_payload()
    fake_client = MagicMock()
    fake_client.user = MagicMock()
    fake_client.user.id = 999999
    # The on_raw_reaction_add closure captured `client` (the real one), but
    # the message-id 700 has no observation row seeded, so merge is a no-op.
    await client.on_raw_reaction_add(payload)
    assert len(react_calls) == 1


def test_channel_in_scope_empty_allowlist_observes_all(patched_observer):
    assert vibe_observer._channel_in_scope("100", 200) is True
    assert vibe_observer._channel_in_scope("100", 999) is True


def test_channel_in_scope_with_allowlist(monkeypatch, patched_observer):
    monkeypatch.setattr(
        vibe_observer, "OBSERVATION_CHANNELS", {"100": ["200"]}
    )
    assert vibe_observer._channel_in_scope("100", 200) is True
    assert vibe_observer._channel_in_scope("100", 999) is False


# ---------------------------------------------------------------------------
# Pure summarizer
# ---------------------------------------------------------------------------


def test_summarize_empty():
    out = vibe_observer._summarize_observations([])
    assert out["message_count"] == 0
    assert out["sample_messages"] == []
    assert out["channels_active_in"] == []


def test_summarize_caps_sample_at_20(monkeypatch):
    rows = [
        {"content_truncated": f"msg{i}", "channel_id": "200",
         "reactions_given_json": None, "posted_at": "2026-05-15T12:00:00Z"}
        for i in range(50)
    ]
    out = vibe_observer._summarize_observations(rows)
    assert out["message_count"] == 50
    assert len(out["sample_messages"]) == 20
