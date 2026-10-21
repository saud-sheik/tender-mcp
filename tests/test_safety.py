"""
tests/test_safety.py  –  Safety net unit tests
==============================================
Ensures:
1. All tests use a temporary database; tenders.db is never touched.
2. A test that fails if any test operation touches tenders.db.
3. Refresh logs an error and records an error when a source fetches 0 tenders.
"""
import os
import sys
from pathlib import Path
import pytest

from tender_mcp.config import PROJECT_ROOT
import tender_mcp.db as db_module


def test_tests_never_use_production_db():
    """Verify DB_PATH is diverted away from the real tenders.db."""
    prod_db = PROJECT_ROOT / "tenders.db"
    assert db_module.DB_PATH != prod_db, (
        f"Security violation: Test DB_PATH is pointing to production {prod_db}!"
    )
    assert "test" in str(db_module.DB_PATH), (
        f"Test DB path should be temporary, got {db_module.DB_PATH}"
    )


def test_tenders_db_untouched_after_crud_operations():
    """Verify inserting/updating data in tests does not modify production tenders.db."""
    prod_db = PROJECT_ROOT / "tenders.db"
    prod_mtime_before = prod_db.stat().st_mtime if prod_db.exists() else None
    prod_size_before = prod_db.stat().st_size if prod_db.exists() else None

    # Perform DB writes in test environment
    db_module.upsert_tender({
        "id": "gem:TEST_SAFETY_001",
        "source": "gem",
        "title": "Safety Test Tender",
        "published_date": "2026-10-14T10:00:00Z",
        "closing_date": "2026-10-20T10:00:00Z",
        "link": "https://example.com/safety",
        "status": "active",
    })
    results = db_module.query_tenders()
    assert any(t["id"] == "gem:TEST_SAFETY_001" for t in results)

    # Ensure production DB was NOT modified
    if prod_db.exists():
        assert prod_db.stat().st_mtime == prod_mtime_before, "Production tenders.db mtime changed during test!"
        assert prod_db.stat().st_size == prod_size_before, "Production tenders.db size changed during test!"


@pytest.mark.asyncio
async def test_refresh_records_error_when_source_fetches_zero(monkeypatch):
    """When a source returns 0 tenders, refresh_all must record an error."""
    import tender_mcp.refresh as refresh_module

    async def mock_cppp(*args, **kwargs):
        return []

    monkeypatch.setattr(refresh_module, "_run_cppp", mock_cppp)

    summary = await refresh_module.refresh_all(sources=["cppp"], cppp_max_pages=1)
    cppp_info = summary["sources"]["cppp"]
    assert cppp_info["fetched"] == 0
    assert cppp_info["error"] is not None
    assert "0 tenders" in cppp_info["error"]
