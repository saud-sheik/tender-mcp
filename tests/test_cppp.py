"""
tests/test_cppp.py  –  Tests for CPPP scraper
==============================================
Uses the real HTML snapshot saved in tests/fixtures/cppp_page1.html.
No network calls in these tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scrapers.cppp import parse, _extract_id_from_link, _hash_title

FIXTURE = Path(__file__).parent / "fixtures" / "cppp_page1.html"


@pytest.fixture(scope="module")
def cppp_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed(cppp_html: str) -> list:
    return parse(cppp_html)


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

def test_parse_returns_list(parsed):
    assert isinstance(parsed, list), "parse() should return a list"


def test_parse_finds_tenders(parsed):
    assert len(parsed) >= 5, (
        f"Expected at least 5 tenders from fixture, got {len(parsed)}"
    )


def test_tender_has_required_fields(parsed):
    required = {"id", "source", "title", "organisation", "published_date",
                "closing_date", "link", "status"}
    for tender in parsed[:3]:
        missing = required - set(tender.keys())
        assert not missing, f"Tender missing fields: {missing}\nTender: {tender}"


# ---------------------------------------------------------------------------
# Field values
# ---------------------------------------------------------------------------

def test_source_is_cppp(parsed):
    for t in parsed:
        assert t["source"] == "cppp", f"Expected source='cppp', got {t['source']}"


def test_id_starts_with_cppp(parsed):
    for t in parsed:
        assert t["id"].startswith("cppp:"), (
            f"Expected id to start with 'cppp:', got {t['id']}"
        )


def test_ids_are_unique(parsed):
    ids = [t["id"] for t in parsed]
    assert len(ids) == len(set(ids)), "Tender IDs should be unique"


def test_links_are_absolute(parsed):
    for t in parsed:
        link = t.get("link", "")
        if link:
            assert link.startswith("http"), (
                f"Link should be absolute URL, got: {link}"
            )


def test_links_point_to_eprocure(parsed):
    for t in parsed:
        link = t.get("link", "")
        if link:
            assert "eprocure.gov.in" in link, (
                f"Link should point to eprocure.gov.in, got: {link}"
            )


def test_published_date_format(parsed):
    """Published dates should be converted to ISO 8601 format."""
    import re
    iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    for t in parsed[:5]:
        pub_date = t.get("published_date", "")
        if pub_date:
            assert iso_pattern.match(pub_date), (
                f"Unexpected date format: {pub_date}"
            )


def test_organisation_not_empty(parsed):
    """Organisation should not be empty for most tenders."""
    non_empty = [t for t in parsed if t.get("organisation")]
    assert len(non_empty) >= len(parsed) * 0.8, (
        "At least 80% of tenders should have an organisation"
    )


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

def test_extract_id_from_link_valid():
    link = (
        "https://eprocure.gov.in/cppp/tendersfullview/"
        "MTQxOTYwOTA=A13h1OGQ2NzAxYTMwZTJhNTIxMGNiNmEwM2EzNmNhYWZhODk="
    )
    result = _extract_id_from_link(link)
    assert result, "Should extract an ID from a valid CPPP link"
    assert len(result) <= 32


def test_extract_id_from_link_invalid():
    result = _extract_id_from_link("https://example.com/no/match")
    assert result == ""


def test_hash_title():
    h = _hash_title("Some Tender Title")
    assert len(h) == 12
    assert h == _hash_title("Some Tender Title"), "Same title → same hash"
    assert h != _hash_title("Different Title"), "Different title → different hash"


# ---------------------------------------------------------------------------
# Pagination data sanity
# ---------------------------------------------------------------------------

def test_pagination_present_in_html(cppp_html: str):
    """Page 1 HTML should include pagination links for page 2."""
    assert "paginate_button" in cppp_html or "page_parination" in cppp_html, (
        "Expected pagination elements in CPPP page 1 HTML"
    )


def test_total_tenders_count_in_html(cppp_html: str):
    """The HTML should mention a total tenders count."""
    import re
    assert re.search(r"Total Tenders\s*:\s*\d+", cppp_html), (
        "Expected 'Total Tenders: N' in CPPP HTML"
    )
