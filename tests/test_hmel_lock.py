"""
tests/test_hmel_lock.py  –  Tests for HMEL lockout protection
============================================================
"""
from __future__ import annotations

import pytest

from tender_mcp.config import is_hmel_locked, lock_hmel, unlock_hmel, LOCK_FILE
from scrapers.hmel import fetch_tenders


@pytest.fixture(autouse=True)
def clean_lock_file():
    unlock_hmel()
    yield
    unlock_hmel()


def test_lock_and_unlock_cycle():
    assert is_hmel_locked()[0] is False
    lock_hmel("Test OTP detected")
    is_locked, reason = is_hmel_locked()
    assert is_locked is True
    assert "Test OTP detected" in reason
    assert LOCK_FILE.exists()

    unlocked = unlock_hmel()
    assert unlocked is True
    assert is_hmel_locked()[0] is False
    assert not LOCK_FILE.exists()


@pytest.mark.asyncio
async def test_fetch_tenders_skips_when_locked():
    lock_hmel("Simulated captcha lockout")
    tenders = await fetch_tenders()
    assert tenders == [], "fetch_tenders must immediately return empty list when locked"
