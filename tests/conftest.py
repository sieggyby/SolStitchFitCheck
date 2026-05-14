"""Shared fixtures for sable-roles tests."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection

from sable_platform.db.compat_conn import CompatConnection
from sable_platform.db.schema import metadata as sa_metadata

os.environ.setdefault("SABLE_OPERATOR_ID", "test")


@pytest.fixture
def db_conn() -> CompatConnection:
    """In-memory CompatConnection with schema + a `solstitch` test org."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, connection_record):  # noqa: ARG001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    sa_metadata.create_all(engine)
    sa_conn = engine.connect()
    conn = CompatConnection(sa_conn)
    conn.execute(
        "INSERT INTO orgs (org_id, display_name) VALUES (?, ?)",
        ("solstitch", "SolStitch Test Org"),
    )
    conn.commit()
    yield conn
    sa_conn.close()
    engine.dispose()


@pytest.fixture
def fitcheck_module(monkeypatch, db_conn):
    """Import fitcheck_streak with config + get_db patched for the test guild.

    Returns the module; tests can call `mod.on_message(message)` after patching
    `mod._client` if the audit-actor format matters.
    """
    from sable_roles.features import fitcheck_streak as mod

    monkeypatch.setattr(mod, "GUILD_TO_ORG", {"100": "solstitch"})
    monkeypatch.setattr(
        mod,
        "FITCHECK_CHANNELS",
        {"100": {"org_id": "solstitch", "channel_id": "200"}},
    )
    # Reverse-lookup tables are built at module-load from FITCHECK_CHANNELS;
    # tests must keep them in sync with the monkeypatched config (C5).
    monkeypatch.setattr(mod, "_FITCHECK_CHANNEL_IDS", {200})
    monkeypatch.setattr(mod, "_CHANNEL_TO_GUILD", {200: "100"})

    class _DBContext:
        def __enter__(self_inner) -> CompatConnection:
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb) -> bool:
            return False

    monkeypatch.setattr(mod, "get_db", lambda: _DBContext())
    monkeypatch.setattr(mod, "_dm_cooldown", {})
    monkeypatch.setattr(mod, "_pending_recomputes", {})
    bot_user = SimpleNamespace(id=99999)
    monkeypatch.setattr(mod, "_client", SimpleNamespace(user=bot_user))
    return mod


def make_attachment(
    *,
    filename: str = "fit.png",
    content_type: str | None = "image/png",
) -> SimpleNamespace:
    return SimpleNamespace(filename=filename, content_type=content_type)


def make_message(
    *,
    author_id: int = 555,
    author_display_name: str = "tester",
    author_bot: bool = False,
    guild_id: int | None = 100,
    channel_id: int = 200,
    channel_kind: str = "text",
    parent_id: int | None = None,
    attachments: list | None = None,
    message_id: int = 700,
    created_at: datetime | None = None,
    delete_raises: BaseException | None = None,
    dm_raises: BaseException | None = None,
    add_reaction_raises: BaseException | None = None,
    create_thread_raises: BaseException | None = None,
) -> MagicMock:
    """Build a discord.Message double sufficient for on_message tests."""
    import discord

    if channel_kind == "thread":
        channel = MagicMock(spec=discord.Thread)
        channel.parent_id = parent_id
        channel.id = channel_id
    else:
        channel = MagicMock()
        channel.id = channel_id

    author = MagicMock()
    author.id = author_id
    author.bot = author_bot
    author.display_name = author_display_name
    if dm_raises is not None:
        author.send = AsyncMock(side_effect=dm_raises)
    else:
        author.send = AsyncMock()

    guild = MagicMock() if guild_id is not None else None
    if guild is not None:
        guild.id = guild_id

    message = MagicMock()
    message.author = author
    message.guild = guild
    message.channel = channel
    message.id = message_id
    message.attachments = attachments or []
    message.created_at = created_at or datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    if delete_raises is not None:
        message.delete = AsyncMock(side_effect=delete_raises)
    else:
        message.delete = AsyncMock()
    if add_reaction_raises is not None:
        message.add_reaction = AsyncMock(side_effect=add_reaction_raises)
    else:
        message.add_reaction = AsyncMock()
    if create_thread_raises is not None:
        message.create_thread = AsyncMock(side_effect=create_thread_raises)
    else:
        message.create_thread = AsyncMock()
    return message


def fetch_audit_rows(conn: Connection) -> list[dict]:
    rows = conn.execute("SELECT actor, action, org_id, detail_json, source FROM audit_log").fetchall()
    return [dict(r._mapping) if hasattr(r, "_mapping") else dict(r) for r in rows]


def fetch_streak_rows(conn: Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM discord_streak_events").fetchall()
    return [dict(r._mapping) if hasattr(r, "_mapping") else dict(r) for r in rows]
