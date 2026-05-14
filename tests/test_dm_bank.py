"""DM bank content + random.choice integration tests."""
from __future__ import annotations

import random

import pytest

from sable_roles.config import DM_BANK


def test_dm_bank_has_four_lines() -> None:
    assert len(DM_BANK) == 4


def test_dm_bank_lines_all_strings_nonempty() -> None:
    for line in DM_BANK:
        assert isinstance(line, str)
        assert line.strip() == line
        assert len(line) > 0


def test_dm_bank_lines_have_no_mentions_or_role_pings() -> None:
    for line in DM_BANK:
        assert "<@" not in line
        assert "@everyone" not in line
        assert "@here" not in line


def test_random_choice_does_not_crash_and_returns_member() -> None:
    rng = random.Random(0)
    for _ in range(50):
        line = rng.choice(DM_BANK)
        assert line in DM_BANK


def test_at_least_one_line_backtick_formats_channel_name() -> None:
    """Plan §0: channel name backtick-formatted."""
    backticked = [line for line in DM_BANK if "`#fitcheck`" in line]
    assert len(backticked) >= 1


def test_no_emoji_in_dm_body() -> None:
    """Plan §0: 'No emoji in DM body.' Reject any chars in common emoji ranges."""

    def has_emoji(s: str) -> bool:
        for ch in s:
            cp = ord(ch)
            if 0x1F300 <= cp <= 0x1FAFF:
                return True
            if 0x2600 <= cp <= 0x27BF:
                return True
            if cp == 0x2728 or cp == 0x2705:
                return True
        return False

    for line in DM_BANK:
        assert not has_emoji(line), f"emoji found in DM bank line: {line!r}"


@pytest.mark.asyncio
async def test_text_post_dm_uses_dm_bank_line(fitcheck_module) -> None:
    from tests.conftest import make_message

    msg = make_message(attachments=[])
    await fitcheck_module.on_message(msg)
    msg.author.send.assert_awaited_once()
    sent = msg.author.send.await_args.args[0]
    assert sent in DM_BANK
