"""Tests for the /burn-me roast generation pipeline (B5).

Covers plan §7 pipeline matrix:
- generate_roast happy path: cost row + fitcheck_roast_generated audit row,
  Sonnet pricing within ±5% of plan §5 ($3 in / $15 out / $3.75 cache_write /
  $0.30 cache_read per MTok).
- Refusal ("pass" / "pass."): None returned, cost row with call_status='refused',
  fitcheck_roast_skipped audit row, refused=True in detail.
- BadRequestError: None returned, fitcheck_roast_skipped audit row with
  bad_request:* reason, no cost row.
- Generic exception: None returned, fitcheck_roast_skipped audit row with
  exception:<ClassName> reason, no cost row.
- Quote-stripping: model's wrapping quotes get trimmed.
- System prompt is sent with cache_control type=ephemeral (prompt caching live).
- _fetch_image_bytes: happy path, oversize (>5MB), SVG, HTTPException → None.
- _compute_cost: Sonnet rate, Haiku rate, unknown model → 0.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from anthropic import BadRequestError

from sable_roles.features import burn_me as bm

from tests.conftest import fetch_audit_rows


# --- Fixture helpers ---


def _make_db_context(db_conn):
    class _Ctx:
        def __enter__(self_inner):
            return db_conn

        def __exit__(self_inner, exc_type, exc_val, exc_tb):
            return False

    return _Ctx


def _patch_db(monkeypatch, db_conn) -> None:
    monkeypatch.setattr(bm, "get_db", lambda: _make_db_context(db_conn)())


def _patch_client(monkeypatch, response=None, raises: Exception | None = None):
    """Install a fake AsyncAnthropic with one configured `.messages.create`.

    Returns the mock so tests can inspect kwargs the pipeline passed in.
    """
    create = AsyncMock(return_value=response) if raises is None else AsyncMock(side_effect=raises)
    fake = MagicMock()
    fake.messages = MagicMock()
    fake.messages.create = create
    monkeypatch.setattr(bm, "_anthropic_client", fake)
    return fake


def _make_response(
    *,
    text: str,
    input_tokens: int = 1500,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_write: int = 0,
):
    """Build a stand-in for anthropic.types.Message — only the fields the
    pipeline reads (usage + content[].text)."""
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    content = [SimpleNamespace(text=text)]
    return SimpleNamespace(usage=usage, content=content)


def _make_bad_request_error(message: str = "bad image"):
    response = httpx.Response(
        status_code=400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    return BadRequestError(message=message, response=response, body={"error": message})


def _fetch_cost_rows(db_conn) -> list[dict]:
    rows = db_conn.execute(
        "SELECT org_id, call_type, model, input_tokens, output_tokens, cost_usd, call_status"
        " FROM cost_events"
    ).fetchall()
    return [dict(r._mapping) if hasattr(r, "_mapping") else dict(r) for r in rows]


# --- _fetch_image_bytes ---


def _make_attachment(
    *,
    size: int = 100,
    content_type: str | None = "image/png",
    data: bytes = b"\x89PNG\r\n\x1a\n",
    read_raises: BaseException | None = None,
):
    att = MagicMock(spec=discord.Attachment)
    att.size = size
    att.content_type = content_type
    if read_raises is not None:
        att.read = AsyncMock(side_effect=read_raises)
    else:
        att.read = AsyncMock(return_value=data)
    return att


@pytest.mark.asyncio
async def test_fetch_image_bytes_happy_path_returns_bytes_and_media_type():
    att = _make_attachment(content_type="image/jpeg", data=b"jpegdata")
    result = await bm._fetch_image_bytes(att)
    assert result == (b"jpegdata", "image/jpeg")


@pytest.mark.asyncio
async def test_fetch_image_bytes_oversize_returns_none_without_read():
    """5 MB cap is enforced before .read() is awaited."""
    att = _make_attachment(size=6 * 1024 * 1024)
    assert await bm._fetch_image_bytes(att) is None
    att.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_image_bytes_svg_returns_none():
    att = _make_attachment(content_type="image/svg+xml", data=b"<svg/>")
    assert await bm._fetch_image_bytes(att) is None


@pytest.mark.asyncio
async def test_fetch_image_bytes_strips_content_type_params():
    """Discord sometimes attaches `; charset=utf-8` onto content_type."""
    att = _make_attachment(content_type="image/png; charset=binary")
    result = await bm._fetch_image_bytes(att)
    assert result is not None
    _, mt = result
    assert mt == "image/png"


@pytest.mark.asyncio
async def test_fetch_image_bytes_http_exception_returns_none():
    err = discord.HTTPException(MagicMock(), "fetch failed")
    att = _make_attachment(read_raises=err)
    assert await bm._fetch_image_bytes(att) is None


@pytest.mark.asyncio
async def test_fetch_image_bytes_not_found_returns_none():
    err = discord.NotFound(MagicMock(), "gone")
    att = _make_attachment(read_raises=err)
    assert await bm._fetch_image_bytes(att) is None


@pytest.mark.asyncio
async def test_fetch_image_bytes_missing_content_type_falls_back_to_jpeg():
    """No content_type → assume image/jpeg (mirrors Anthropic's default-friendly format)."""
    att = _make_attachment(content_type=None, data=b"raw")
    result = await bm._fetch_image_bytes(att)
    assert result == (b"raw", "image/jpeg")


@pytest.mark.asyncio
async def test_fetch_image_bytes_sniff_overrides_lying_content_type():
    """Discord sometimes mislabels (e.g. declares webp on actual PNG bytes).
    Anthropic strict-validates and 400s on mismatch; magic-byte sniff wins.
    Regression: real bot log 2026-05-16 req_011Cb5yifXRS84ZBXqQCW8hL."""
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    att = _make_attachment(content_type="image/webp", data=png_bytes)
    result = await bm._fetch_image_bytes(att)
    assert result == (png_bytes, "image/png")


def test_sniff_image_type_recognises_all_four_anthropic_formats():
    assert bm._sniff_image_type(b"\x89PNG\r\n\x1a\nXX") == "image/png"
    assert bm._sniff_image_type(b"\xff\xd8\xff\xe0...") == "image/jpeg"
    assert bm._sniff_image_type(b"GIF89a....") == "image/gif"
    assert bm._sniff_image_type(b"GIF87a....") == "image/gif"
    assert bm._sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert bm._sniff_image_type(b"unknownbytes") is None
    assert bm._sniff_image_type(b"") is None


# --- _compute_cost ---


def test_compute_cost_sonnet_rate_matches_plan():
    # 1500 plain in + 50 out + 0 cache.
    # Expected: (1500 * 3 + 50 * 15) / 1_000_000 = (4500 + 750) / 1e6 = 0.00525
    cost = bm._compute_cost("claude-sonnet-4-6", plain_in=1500, out=50, cache_read=0, cache_write=0)
    assert cost == pytest.approx(0.00525, rel=1e-9)


def test_compute_cost_sonnet_with_cache_write_and_read():
    # cache_write 1000 + cache_read 500 → 1000 * 3.75 + 500 * 0.30 = 3750 + 150 = 3900 micro-cents
    # plain_in 200 + out 50 → 200 * 3 + 50 * 15 = 600 + 750 = 1350
    # Total → (3900 + 1350) / 1e6 = 0.00525
    cost = bm._compute_cost(
        "claude-sonnet-4-6", plain_in=200, out=50, cache_read=500, cache_write=1000
    )
    assert cost == pytest.approx((1000 * 3.75 + 500 * 0.30 + 200 * 3.0 + 50 * 15.0) / 1_000_000)


def test_compute_cost_haiku_rate_matches_plan():
    # 1500 * 0.80 + 50 * 4 = 1200 + 200 = 1400 → 0.0014
    cost = bm._compute_cost("claude-haiku-4-5", plain_in=1500, out=50, cache_read=0, cache_write=0)
    assert cost == pytest.approx(0.0014, rel=1e-9)


def test_compute_cost_unknown_model_returns_zero():
    cost = bm._compute_cost("gpt-4", plain_in=1000, out=100, cache_read=0, cache_write=0)
    assert cost == 0.0


# --- generate_roast: happy path ---


@pytest.mark.asyncio
async def test_generate_roast_happy_path_writes_audit_and_cost(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)
    response = _make_response(text="bold of you to wear that brown with that brown.", input_tokens=1500, output_tokens=20)
    _patch_client(monkeypatch, response=response)

    result = await bm.generate_roast(
        org_id="solstitch",
        guild_id="100",
        user_id="555",
        post_id="abc",
        image_bytes=b"jpegdata",
        media_type="image/jpeg",
        author_display_name="tester",
        invocation_path="optin_once",
    )

    assert result is not None
    roast, audit_id = result
    assert roast == "bold of you to wear that brown with that brown."
    assert isinstance(audit_id, int) and audit_id > 0

    cost_rows = _fetch_cost_rows(db_conn)
    assert len(cost_rows) == 1
    cost = cost_rows[0]
    assert cost["org_id"] == "solstitch"
    assert cost["call_type"] == "sable_roles_burn"
    assert cost["model"] == "claude-sonnet-4-6"
    assert cost["input_tokens"] == 1500
    assert cost["output_tokens"] == 20
    assert cost["call_status"] == "success"
    # Plan §5 Sonnet pricing: 1500 * 3 + 20 * 15 = 4800 → 0.0048. Within ±5%.
    expected = 0.0048
    assert cost["cost_usd"] == pytest.approx(expected, rel=0.05)

    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_roast_generated"]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["guild_id"] == "100"
    assert detail["user_id"] == "555"
    assert detail["post_id"] == "abc"
    assert detail["invocation_path"] == "optin_once"
    assert detail["model"] == "claude-sonnet-4-6"
    assert detail["refused"] is False
    assert audits[0]["actor"] == "discord:bot:auto"
    assert audits[0]["org_id"] == "solstitch"
    assert audits[0]["source"] == "sable-roles"


@pytest.mark.asyncio
async def test_generate_roast_sends_cache_controlled_system_prompt(monkeypatch, db_conn):
    """The system message MUST carry cache_control ephemeral so prompt caching kicks in."""
    _patch_db(monkeypatch, db_conn)
    fake = _patch_client(monkeypatch, response=_make_response(text="roast"))

    await bm.generate_roast(
        org_id="solstitch",
        guild_id="100",
        user_id="555",
        post_id="abc",
        image_bytes=b"img",
        media_type="image/png",
        author_display_name="tester",
        invocation_path="optin_persist",
    )

    create = fake.messages.create
    assert create.await_count == 1
    _, kwargs = create.call_args
    assert kwargs["model"] == "claude-sonnet-4-6"
    system = kwargs["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "fashion community" in system[0]["text"]
    # And the user content carries the image block first, text second.
    user_content = kwargs["messages"][0]["content"]
    assert user_content[0]["type"] == "image"
    assert user_content[0]["source"]["media_type"] == "image/png"
    assert user_content[1]["type"] == "text"
    assert "tester" in user_content[1]["text"]
    assert "optin_persist" in user_content[1]["text"]


@pytest.mark.asyncio
async def test_generate_roast_invocation_path_random_bypass_stamps_audit(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)
    _patch_client(monkeypatch, response=_make_response(text="ok"))

    await bm.generate_roast(
        org_id="solstitch",
        guild_id="100",
        user_id="555",
        post_id="abc",
        image_bytes=b"img",
        media_type="image/png",
        author_display_name="tester",
        invocation_path="random_bypass",
    )

    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_roast_generated"]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["invocation_path"] == "random_bypass"


@pytest.mark.asyncio
async def test_generate_roast_strips_wrapping_quotes(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)
    _patch_client(monkeypatch, response=_make_response(text='"that hoodie is loud"'))

    result = await bm.generate_roast(
        org_id="solstitch", guild_id="100", user_id="555", post_id="abc",
        image_bytes=b"img", media_type="image/png",
        author_display_name="tester", invocation_path="optin_once",
    )

    assert result is not None
    roast, _audit_id = result
    assert roast == "that hoodie is loud"


@pytest.mark.asyncio
async def test_generate_roast_cache_token_costing_in_audit_detail(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)
    _patch_client(
        monkeypatch,
        response=_make_response(text="ok", input_tokens=50, output_tokens=10, cache_read=1500, cache_write=0),
    )

    await bm.generate_roast(
        org_id="solstitch", guild_id="100", user_id="555", post_id="abc",
        image_bytes=b"img", media_type="image/png",
        author_display_name="tester", invocation_path="optin_persist",
    )

    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_roast_generated"]
    detail = json.loads(audits[0]["detail_json"])
    assert detail["cache_read_tokens"] == 1500
    assert detail["cache_write_tokens"] == 0
    # 50 * 3 + 10 * 15 + 1500 * 0.30 = 150 + 150 + 450 = 750 → 0.00075
    assert detail["cost_usd"] == pytest.approx(0.00075, rel=1e-6)


# --- generate_roast: refusal ---


@pytest.mark.asyncio
async def test_generate_roast_refusal_pass_returns_none_logs_refused_cost(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)
    _patch_client(monkeypatch, response=_make_response(text="pass"))

    roast = await bm.generate_roast(
        org_id="solstitch", guild_id="100", user_id="555", post_id="abc",
        image_bytes=b"img", media_type="image/png",
        author_display_name="tester", invocation_path="optin_once",
    )

    assert roast is None

    cost_rows = _fetch_cost_rows(db_conn)
    assert len(cost_rows) == 1
    assert cost_rows[0]["call_status"] == "refused"
    assert cost_rows[0]["call_type"] == "sable_roles_burn"

    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_roast_skipped"]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["refused"] is True
    # No bad_request / exception reason on a clean model refusal — refusal goes
    # through the cost branch, not _audit_skipped.
    assert "reason" not in detail
    # And no fitcheck_roast_generated audit slipped in.
    generated = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_roast_generated"]
    assert generated == []


@pytest.mark.asyncio
async def test_generate_roast_refusal_with_trailing_period_also_refused(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)
    _patch_client(monkeypatch, response=_make_response(text="Pass."))

    roast = await bm.generate_roast(
        org_id="solstitch", guild_id="100", user_id="555", post_id="abc",
        image_bytes=b"img", media_type="image/png",
        author_display_name="tester", invocation_path="optin_once",
    )

    assert roast is None
    cost_rows = _fetch_cost_rows(db_conn)
    assert cost_rows[0]["call_status"] == "refused"


# --- generate_roast: error paths ---


@pytest.mark.asyncio
async def test_generate_roast_bad_request_returns_none_no_cost(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)
    _patch_client(monkeypatch, raises=_make_bad_request_error("unsupported media"))

    roast = await bm.generate_roast(
        org_id="solstitch", guild_id="100", user_id="555", post_id="abc",
        image_bytes=b"img", media_type="image/png",
        author_display_name="tester", invocation_path="optin_once",
    )

    assert roast is None

    # No cost row for error paths.
    assert _fetch_cost_rows(db_conn) == []

    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_roast_skipped"]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["reason"].startswith("bad_request:")
    assert detail["invocation_path"] == "optin_once"
    assert detail["post_id"] == "abc"


@pytest.mark.asyncio
async def test_generate_roast_generic_exception_returns_none_no_cost(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)
    _patch_client(monkeypatch, raises=RuntimeError("network down"))

    roast = await bm.generate_roast(
        org_id="solstitch", guild_id="100", user_id="555", post_id="abc",
        image_bytes=b"img", media_type="image/png",
        author_display_name="tester", invocation_path="random_bypass",
    )

    assert roast is None
    assert _fetch_cost_rows(db_conn) == []

    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_roast_skipped"]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["reason"] == "exception:RuntimeError"


# --- _audit_skipped direct ---


def test_audit_skipped_writes_row_with_full_detail(monkeypatch, db_conn):
    _patch_db(monkeypatch, db_conn)

    bm._audit_skipped(
        org_id="solstitch",
        guild_id="100",
        user_id="555",
        post_id="abc",
        invocation_path="optin_persist",
        reason="exception:Boom",
    )

    audits = [a for a in fetch_audit_rows(db_conn) if a["action"] == "fitcheck_roast_skipped"]
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["reason"] == "exception:Boom"
    assert detail["invocation_path"] == "optin_persist"
    assert detail["post_id"] == "abc"
    assert detail["user_id"] == "555"
    assert detail["guild_id"] == "100"
    assert audits[0]["actor"] == "discord:bot:auto"
    assert audits[0]["org_id"] == "solstitch"
    assert audits[0]["source"] == "sable-roles"


# --- system prompt import shape ---


def test_system_prompt_importable_from_prompts_module():
    """B5 pass criterion: system prompt loaded via the expected import path."""
    from sable_roles.prompts.burn_me_system import SYSTEM_PROMPT

    assert "fashion community" in SYSTEM_PROMPT
    assert "pass" in SYSTEM_PROMPT
    # Voice rail is the load-bearing part; if this changes someone forgot to grill.
    assert "All lowercase" in SYSTEM_PROMPT
