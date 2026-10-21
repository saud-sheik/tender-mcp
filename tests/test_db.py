"""
tests/test_db.py  –  Database layer tests
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def use_temp_db(monkeypatch, tmp_path):
    """Point the DB layer at a temp file for each test."""
    import tender_mcp.db as db_module
    db_module.DB_PATH = tmp_path / "test_tenders.db"
    # Reset thread-local connection so it picks up the new path
    db_module._local.__dict__.clear()
    db_module.init_db()
    yield
    db_module._local.__dict__.clear()


def _sample_tender(n: int = 1) -> dict:
    return {
        "id": f"cppp:test{n:04d}",
        "source": "cppp",
        "title": f"Test Tender {n}",
        "organisation": f"Test Org {n}",
        "category": "Engineering",
        "published_date": "12-Oct-2026",
        "closing_date": "2026-11-30T00:00:00Z",
        "opening_date": "",
        "emd_value": "",
        "tender_value": "",
        "link": f"https://eprocure.gov.in/cppp/tender/{n}",
        "status": "active",
        "raw": {"source": "test"},
    }


from tender_mcp.db import upsert_tender, query_tenders, get_tender_by_id


def test_upsert_new_tender():
    t = _sample_tender(1)
    tid, is_new = upsert_tender(t)
    assert tid == "cppp:test0001"
    assert is_new is True


def test_upsert_existing_tender():
    t = _sample_tender(1)
    upsert_tender(t)
    t["title"] = "Updated Title"
    tid, is_new = upsert_tender(t)
    assert is_new is False


def test_get_tender_by_id():
    t = _sample_tender(2)
    upsert_tender(t)
    result = get_tender_by_id("cppp:test0002")
    assert result is not None
    assert result["title"] == "Test Tender 2"


def test_get_tender_not_found():
    result = get_tender_by_id("cppp:nonexistent")
    assert result is None


def test_query_all():
    for i in range(1, 4):
        upsert_tender(_sample_tender(i))
    results = query_tenders()
    assert len(results) == 3


def test_query_by_source():
    upsert_tender({**_sample_tender(1), "source": "gem", "id": "gem:test001"})
    upsert_tender({**_sample_tender(2), "source": "cppp", "id": "cppp:test002"})
    results = query_tenders(source="gem")
    assert len(results) == 1
    assert results[0]["source"] == "gem"


def test_query_by_status():
    upsert_tender({**_sample_tender(1), "status": "active"})
    upsert_tender({**_sample_tender(2), "id": "cppp:test0002", "status": "closed"})
    active = query_tenders(status="active")
    assert all(r["status"] == "active" for r in active)


def test_ids_are_unique_after_multiple_upserts():
    t = _sample_tender(5)
    upsert_tender(t)
    upsert_tender(t)
    upsert_tender(t)
    all_rows = query_tenders()
    ids = [r["id"] for r in all_rows]
    assert len(ids) == len(set(ids))


def test_matched_terms_stored_and_retrieved():
    t = {
        "id": "gem:test_matched",
        "source": "gem",
        "title": "Natural gas pipeline valves and compressors",
        "organisation": "GAIL India Limited",
        "category": "Piping",
        "published_date": "2026-10-14T00:00:00Z",
        "closing_date": "2026-11-15T00:00:00Z",
    }
    upsert_tender(t)
    saved = get_tender_by_id("gem:test_matched")
    assert saved is not None
    assert "pipeline" in saved["matched_terms"]
    assert "gas" in saved["matched_terms"]
    assert saved["relevance_score"] >= 70


def test_closing_date_history_and_extensions_seen():
    """Verify closing_date_history records changes and increments extensions_seen on upward shift."""
    from tender_mcp.db import get_conn
    t = _sample_tender(10)
    t["closing_date"] = "2026-10-20T10:00:00Z"
    upsert_tender(t)

    # First update: upward extension
    t["closing_date"] = "2026-10-25T10:00:00Z"
    upsert_tender(t)

    saved = get_tender_by_id(t["id"])
    assert saved["closing_date"] == "2026-10-25T10:00:00Z"
    assert saved["extensions_seen"] == 1
    assert saved["was_extended"] == 1

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM closing_date_history WHERE tender_id = ?", (t["id"],)).fetchall()
        assert len(rows) == 1
        assert rows[0]["old_value"] == "2026-10-20T10:00:00Z"
        assert rows[0]["new_value"] == "2026-10-25T10:00:00Z"


def test_refresh_recalculates_date_check_for_enriched_tender():
    """
    Starts with a tender with a PDF (status 'unchanged'),
    moves closing_date later in a refresh, and ends with the correct status ('extended_auto', then 'check_needed').
    """
    from tender_mcp.db import save_gem_pdf_enrichment

    tender = {
        "id": "gem:GEM_2026_B_1234567",
        "source": "gem",
        "title": "Supply of Gas Detectors",
        "organisation": "BPCL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-10-13T09:30:00Z",
        "opening_date": "",
        "emd_value": "",
        "tender_value": "",
        "link": "https://bidplus.gem.gov.in/showbidDocument/1234567",
        "status": "active",
        "raw": {},
    }
    upsert_tender(tender)

    # Store enrichment from PDF: bid ends 13-10-2026 15:00:00 IST (2026-10-13T09:30:00Z UTC)
    # auto extend 7 days x 2 = limit 2026-10-27T09:30:00Z
    enrichment = {
        "bid_number": "GEM/2026/B/1234567",
        "bid_end_datetime": "13-10-2026 15:00:00",
        "auto_extend_days": 7,
        "auto_extend_max_count": 2,
        "pdf_parse_status": "ok",
    }
    save_gem_pdf_enrichment(tender["id"], enrichment)

    saved = get_tender_by_id(tender["id"])
    assert saved["original_end_utc"] == "2026-10-13T09:30:00Z"
    assert saved["auto_extension_limit_utc"] == "2026-10-27T09:30:00Z"
    assert saved["date_check"] == "unchanged"
    assert saved["is_extended"] == 0
    assert saved["extensions_seen"] == 0

    # Refresh 1: Portal lists new closing_date 19-10-2026 (moved later, within auto limit)
    tender["closing_date"] = "2026-10-19T09:30:00Z"
    upsert_tender(tender)

    saved_after_refresh1 = get_tender_by_id(tender["id"])
    assert saved_after_refresh1["closing_date"] == "2026-10-19T09:30:00Z"
    assert saved_after_refresh1["date_check"] == "extended_auto"
    assert saved_after_refresh1["is_extended"] == 1
    assert saved_after_refresh1["extensions_seen"] == 1
    assert saved_after_refresh1["extension_days_observed"] == 6.0

    # Refresh 2: Portal lists new closing_date 2026-11-05 (beyond auto limit, no corrigendum)
    tender["closing_date"] = "2026-11-05T09:30:00Z"
    upsert_tender(tender)

    saved_after_refresh2 = get_tender_by_id(tender["id"])
    assert saved_after_refresh2["closing_date"] == "2026-11-05T09:30:00Z"
    assert saved_after_refresh2["date_check"] == "check_needed"
    assert saved_after_refresh2["is_extended"] == 1
    assert saved_after_refresh2["extensions_seen"] == 2


def test_not_checked_status_for_unparsed_or_cppp():
    """Unparsed tenders (pdf_parse_status='none') and CPPP rows must have date_check='not_checked'."""
    cppp_tender = _sample_tender(20)
    upsert_tender(cppp_tender)
    saved_cppp = get_tender_by_id(cppp_tender["id"])
    assert saved_cppp["date_check"] == "not_checked"

    gem_tender = {
        "id": "gem:GEM_2026_B_9999001",
        "source": "gem",
        "title": "Supply of valves",
        "organisation": "ONGC",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-10-20T00:00:00Z",
        "status": "active",
        "raw": {},
    }
    upsert_tender(gem_tender)
    saved_gem = get_tender_by_id(gem_tender["id"])
    assert saved_gem["date_check"] == "not_checked"


def test_refresh_never_overwrites_status_notes_or_owner():
    """Verify that a portal refresh never overwrites workflow status, notes or owner on an existing row."""
    from tender_mcp.db import update_tender_status

    t = _sample_tender(30)
    upsert_tender(t)

    # User customizes status, owner, and notes
    update_tender_status(
        tender_id=t["id"],
        new_status="bidding",
        owner="rahul",
        notes="High priority tender - prepare bid document",
    )

    saved_before = get_tender_by_id(t["id"])
    assert saved_before["status"] == "bidding"
    assert saved_before["owner"] == "rahul"
    assert saved_before["notes"] == "High priority tender - prepare bid document"

    # Simulate portal refresh updating closing_date, title, and passing status='active'
    refresh_update = dict(t)
    refresh_update["title"] = "Updated Title From Scraper Refresh"
    refresh_update["closing_date"] = "2026-12-15T00:00:00Z"
    refresh_update["status"] = "active"
    refresh_update["owner"] = ""
    refresh_update["notes"] = ""

    upsert_tender(refresh_update)

    saved_after = get_tender_by_id(t["id"])
    assert saved_after["title"] == "Updated Title From Scraper Refresh"
    assert saved_after["closing_date"] == "2026-12-15T00:00:00Z"
    # Status, owner, and notes MUST remain untouched!
    assert saved_after["status"] == "bidding"
    assert saved_after["owner"] == "rahul"
    assert saved_after["notes"] == "High priority tender - prepare bid document"


def test_status_history_and_transitions():
    """Verify status transitions and status_history table entries."""
    from tender_mcp.db import update_tender_status, get_tender_status_history

    t = _sample_tender(31)
    t["status"] = "new"
    upsert_tender(t)

    # Initial status is 'new'
    saved = get_tender_by_id(t["id"])
    assert saved["status"] == "new"
    assert len(get_tender_status_history(t["id"])) == 0

    # 1. Transition to 'interested'
    update_tender_status(t["id"], new_status="interested", owner="amit")
    h1 = get_tender_status_history(t["id"])
    assert len(h1) == 1
    assert h1[0]["old_status"] == "new"
    assert h1[0]["new_status"] == "interested"
    assert h1[0]["owner"] == "amit"

    # 2. Transition to 'submitted'
    update_tender_status(t["id"], new_status="submitted", owner="amit")
    h2 = get_tender_status_history(t["id"])
    assert len(h2) == 2
    assert h2[1]["old_status"] == "interested"
    assert h2[1]["new_status"] == "submitted"

    # 3. Invalid status validation
    with pytest.raises(ValueError):
        update_tender_status(t["id"], new_status="invalid_status_xyz")




