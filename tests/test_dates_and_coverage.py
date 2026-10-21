"""
tests/test_dates_and_coverage.py
================================
Tests for:
1. Date parsing and ISO 8601 conversion for both formats seen:
   - "12-10-2026 5:45 PM"
   - "21-Oct-2026 01:00 PM"
2. Expired tenders filtering in DB queries.
3. Database migration of non-ISO dates and CPPP duplicate rows.
4. CPPP real tender ID parsing from row content.
"""
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from tender_mcp.dates import to_iso8601, parse_datetime, is_expired
import tender_mcp.db as db_module
from scrapers.cppp import parse as parse_cppp


def test_date_format_gem():
    """Test format: 12-10-2026 5:45 PM (GeM style, IST to UTC)."""
    raw = "12-10-2026 5:45 PM"
    iso = to_iso8601(raw)
    assert iso == "2026-10-12T12:15:00Z"
    dt = parse_datetime(raw)
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 10
    assert dt.day == 12
    assert dt.hour == 12
    assert dt.minute == 15


def test_date_format_cppp():
    """Test format: 21-Oct-2026 01:00 PM (CPPP style, IST to UTC)."""
    raw = "21-Oct-2026 01:00 PM"
    iso = to_iso8601(raw)
    assert iso == "2026-10-21T07:30:00Z"
    dt = parse_datetime(raw)
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 10
    assert dt.day == 21
    assert dt.hour == 7
    assert dt.minute == 30


def test_cppp_real_id_extracted():
    """Verify CPPP parsing extracts real tender reference/ID rather than URL token."""
    sample_html = """
    <table id="activeTenders">
      <tbody>
        <tr>
          <td>1</td>
          <td>12-Oct-2026 05:21 PM</td>
          <td>28-Oct-2026 03:00 PM</td>
          <td>29-Oct-2026 03:30 PM</td>
          <td><a href="https://eprocure.gov.in/cppp/tendersfullview/MTQxOTYwOTA=A13h1OGQ2NzAxYTMwZTJhNTIxMGNiNmEwM2EzNmNhYWZhODk=">Supply of Natural Gas Valves</a>/REF/2026/GAS/001/2026_CPPP_99999_1</td>
          <td>GAIL India Ltd</td>
          <td>Corrigendum 1</td>
        </tr>
      </tbody>
    </table>
    """
    tenders = parse_cppp(sample_html)
    assert len(tenders) == 1
    t = tenders[0]
    assert t["id"] == "cppp:2026_CPPP_99999_1"
    assert t["published_date"] == "2026-10-12T11:51:00Z"
    assert t["closing_date"] == "2026-10-28T09:30:00Z"
    assert t["raw"]["ref_no"] == "REF/2026/GAS/001"
    assert t["raw"]["tender_id"] == "2026_CPPP_99999_1"


def test_db_migration_normalizes_dates_and_cppp_ids(tmp_path, monkeypatch):
    """Test that init_db() migrates unnormalized dates and replaces old token CPPP rows."""
    test_db = tmp_path / "migration_test.db"
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    db_module._local.__dict__.clear()

    # Pre-populate database with unmigrated schema and legacy data
    conn = sqlite3.connect(test_db)
    with conn:
        conn.executescript("""
            CREATE TABLE tenders (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                organisation TEXT,
                category TEXT,
                published_date TEXT,
                closing_date TEXT,
                opening_date TEXT,
                emd_value TEXT,
                tender_value TEXT,
                link TEXT,
                status TEXT DEFAULT 'active',
                relevance_score REAL DEFAULT 0.0,
                matched_keywords TEXT DEFAULT '[]',
                raw_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO tenders (id, source, title, published_date, closing_date, raw_json)
            VALUES (
                'cppp:MTQxOTYwOTA=',
                'cppp',
                'Legacy CPPP Tender',
                '21-Oct-2026 01:00 PM',
                '12-10-2026 5:45 PM',
                '{"tender_id": "2026_MIGRATED_ID_1"}'
            );
            INSERT INTO tenders (id, source, title, published_date, closing_date, raw_json)
            VALUES (
                'gem:GEM/2026/B/1234',
                'gem',
                'Legacy GeM Tender',
                '12-10-2026 5:45 PM',
                '21-Oct-2026 01:00 PM',
                '{}'
            );
        """)
    conn.close()

    # Run init_db which triggers migrations
    db_module.init_db()

    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, source, published_date, closing_date FROM tenders").fetchall()
    conn.close()

    rows_by_source = {r["source"]: r for r in rows}

    # Verify CPPP ID was migrated from token to real ID
    cppp_row = rows_by_source["cppp"]
    assert cppp_row["id"] == "cppp:2026_MIGRATED_ID_1"
    assert cppp_row["published_date"] == "2026-10-21T07:30:00Z"
    assert cppp_row["closing_date"] == "2026-10-12T12:15:00Z"

    # Verify GeM dates were normalized to ISO 8601
    gem_row = rows_by_source["gem"]
    assert gem_row["published_date"] == "2026-10-12T12:15:00Z"
    assert gem_row["closing_date"] == "2026-10-21T07:30:00Z"


def test_expired_tenders_filtering(tmp_path, monkeypatch):
    """Test that query_tenders hides expired tenders by default unless include_expired=True."""
    test_db = tmp_path / "filter_test.db"
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    db_module._local.__dict__.clear()
    db_module.init_db()

    now = datetime.now(timezone.utc)
    future_date = (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past_date = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Future tender
    db_module.upsert_tender({
        "id": "gem:active_future",
        "source": "gem",
        "title": "Future Active Tender",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": future_date,
        "link": "https://example.com/1",
        "status": "active",
    })

    # Past tender (expired)
    db_module.upsert_tender({
        "id": "gem:expired_past",
        "source": "gem",
        "title": "Expired Past Tender",
        "published_date": "2026-09-01T00:00:00Z",
        "closing_date": past_date,
        "link": "https://example.com/2",
        "status": "active",
    })

    # Tender with no closing date
    db_module.upsert_tender({
        "id": "gem:no_closing_date",
        "source": "gem",
        "title": "Tender Without Closing Date",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "",
        "link": "https://example.com/3",
        "status": "active",
    })

    # By default, expired tenders should be hidden
    results_active = db_module.query_tenders(include_expired=False)
    ids_active = {r["id"] for r in results_active}
    assert "gem:active_future" in ids_active
    assert "gem:no_closing_date" in ids_active
    assert "gem:expired_past" not in ids_active

    # When include_expired=True, all should be returned
    results_all = db_module.query_tenders(include_expired=True)
    ids_all = {r["id"] for r in results_all}
    assert "gem:active_future" in ids_all
    assert "gem:no_closing_date" in ids_all
    assert "gem:expired_past" in ids_all


def test_hours_to_close_and_closing_soon(tmp_path, monkeypatch):
    """Test hours_to_close calculation, default hiding <6h, and flagging <=48h."""
    from tender_mcp.dates import get_hours_to_close
    import json
    from tender_mcp.server import search_tenders

    test_db = tmp_path / "hours_test.db"
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    db_module._local.__dict__.clear()
    db_module.init_db()

    now = datetime.now(timezone.utc)
    in_3h = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    in_24h = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    in_72h = (now + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert get_hours_to_close(in_24h) == pytest.approx(24.0, abs=0.2)
    assert get_hours_to_close("") is None

    db_module.upsert_tender({
        "id": "gem:closing_in_3h",
        "source": "gem",
        "title": "Supply of crude oil pumps",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": in_3h,
        "link": "https://example.com/3h",
        "status": "active",
        "relevance_score": 80,
    })
    db_module.upsert_tender({
        "id": "gem:closing_in_24h",
        "source": "gem",
        "title": "Supply of crude oil valves",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": in_24h,
        "link": "https://example.com/24h",
        "status": "active",
        "relevance_score": 80,
    })
    db_module.upsert_tender({
        "id": "gem:closing_in_72h",
        "source": "gem",
        "title": "Supply of crude oil pipes",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": in_72h,
        "link": "https://example.com/72h",
        "status": "active",
        "relevance_score": 80,
    })

    # Default search: hides tenders closing within 6 hours
    res = json.loads(search_tenders())
    res_ids = {r["id"] for r in res}
    assert "gem:closing_in_3h" not in res_ids
    assert "gem:closing_in_24h" in res_ids
    assert "gem:closing_in_72h" in res_ids

    # Check 24h tender: flagged as closing_soon (<= 48h)
    t24 = next(r for r in res if r["id"] == "gem:closing_in_24h")
    assert t24["closing_soon"] is True
    assert t24["hours_to_close"] == pytest.approx(24.0, abs=0.5)

    # Check 72h tender: closing_soon is False (> 48h)
    t72 = next(r for r in res if r["id"] == "gem:closing_in_72h")
    assert t72["closing_soon"] is False
    assert t72["hours_to_close"] == pytest.approx(72.0, abs=0.5)

    # When min_hours_to_close=0, 3h tender is included
    res_all = json.loads(search_tenders(min_hours_to_close=0))
    res_all_ids = {r["id"] for r in res_all}
    assert "gem:closing_in_3h" in res_all_ids
