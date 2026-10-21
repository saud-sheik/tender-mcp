"""
tests/test_gem.py  –  Tests for GeM scraper
============================================
The GeM fixture was captured from a GET request to the static page.
Since GeM requires JS to render bids, the fixture will have the skeleton
but not actual bid data. We test:
  1. parse() handles empty/skeleton HTML gracefully
  2. parse() correctly extracts GEM/ bid numbers if present
  3. Helper functions work correctly

For live bid data, a Playwright-rendered fixture would be needed.
The browser fetch is tested via integration test (marked as slow/integration).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scrapers.gem import parse, _regex, _parse_by_gem_numbers

FIXTURE = Path(__file__).parent / "fixtures" / "gem_page1.html"


@pytest.fixture(scope="module")
def gem_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture file tests (static/skeleton HTML)
# ---------------------------------------------------------------------------

def test_fixture_exists():
    assert FIXTURE.exists(), f"Fixture file not found: {FIXTURE}"


def test_parse_empty_returns_list(gem_html):
    result = parse(gem_html)
    assert isinstance(result, list), "parse() should always return a list"


def test_parse_does_not_crash_on_skeleton(gem_html):
    """Static HTML has no bid data; parse should return [] without crashing."""
    result = parse(gem_html)
    # Either empty or contains partial data from GEM/ number extraction
    assert result is not None


# ---------------------------------------------------------------------------
# Synthetic bid card tests (test parser logic without network)
# ---------------------------------------------------------------------------

SAMPLE_BID_HTML = """
<html><body>
<div class="bid-list-box clearfix" id="bidlist">
  <div class="bid-info">
    <span class="bid-no">GEM/2024/B/5123456</span>
    <span class="bid-title">Supply of Petroleum Equipments</span>
    <div class="bid-org">Ministry of Petroleum and Natural Gas</div>
    <div class="bid-dates">
      Start Date: 01-Nov-2024 | End Date: 30-Nov-2024
    </div>
    <a href="/bidlisting/GEM/2024/B/5123456">View Bid</a>
  </div>
</div>
<div class="bid-list-box clearfix">
  <div class="bid-info">
    <span class="bid-no">GEM/2024/B/9999999</span>
    <span class="bid-title">LPG Cylinder Supply</span>
    <div class="bid-org">Department of Petroleum</div>
    <div class="bid-dates">
      Start Date: 15-Oct-2024 | End Date: 14-Nov-2024
    </div>
    <a href="/bidlisting/GEM/2024/B/9999999">View Bid</a>
  </div>
</div>
</body></html>
"""


def test_parse_synthetic_bid_cards():
    result = parse(SAMPLE_BID_HTML, source_url="https://bidplus.gem.gov.in/all-bids?page_no=1")
    # Should find the GEM bid numbers
    ids = [r["id"] for r in result]
    assert any("5123456" in i for i in ids) or any("gem:" in i for i in ids), (
        f"Expected GEM bid IDs in result. Got: {ids}"
    )


def test_parse_returns_correct_source():
    result = parse(SAMPLE_BID_HTML)
    for t in result:
        assert t["source"] == "gem"


def test_parse_ids_unique():
    result = parse(SAMPLE_BID_HTML)
    ids = [t["id"] for t in result]
    assert len(ids) == len(set(ids)), "IDs must be unique"


def test_parse_ids_start_with_gem():
    result = parse(SAMPLE_BID_HTML)
    for t in result:
        assert t["id"].startswith("gem:"), f"Expected 'gem:' prefix, got: {t['id']}"


def test_parse_links_absolute():
    result = parse(SAMPLE_BID_HTML)
    for t in result:
        link = t.get("link", "")
        if link:
            assert link.startswith("http"), f"Link should be absolute: {link}"


# ---------------------------------------------------------------------------
# GEM number extraction test
# ---------------------------------------------------------------------------

SAMPLE_GEM_TEXT_HTML = """
<html><body>
<p>Active bids available: GEM/2024/B/5000001, GEM/2024/S/3000002</p>
<div>Bid GEM/2025/B/1234567 open for bidding</div>
</body></html>
"""


def test_parse_by_gem_numbers():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(SAMPLE_GEM_TEXT_HTML, "html.parser")
    result = _parse_by_gem_numbers(soup, "https://bidplus.gem.gov.in/all-bids")
    assert len(result) == 3, f"Expected 3 bids, got {len(result)}: {[r['id'] for r in result]}"


# ---------------------------------------------------------------------------
# Regex helper tests
# ---------------------------------------------------------------------------

def test_regex_finds_match():
    text = "Department: Ministry of Petroleum and Natural Gas"
    result = _regex(text, r"Department:\s*(.+)")
    assert result == "Ministry of Petroleum and Natural Gas"


def test_regex_returns_empty_on_no_match():
    result = _regex("no match here", r"xyz:\s*(.+)")
    assert result == ""
