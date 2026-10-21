"""
tests/test_fabrication.py
Verification tests ensuring no fabricated or hardcoded values:
1. Every field in search_tenders output equals the stored value in tenders.db.
2. Every field in export_tenders CSV output equals the stored value in tenders.db.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tender_mcp.db import get_conn, upsert_tender, save_gem_pdf_enrichment
from tender_mcp.server import search_tenders, export_tenders


@pytest.fixture(autouse=True)
def use_temp_db(monkeypatch, tmp_path):
    import tender_mcp.db as db_module
    db_module.DB_PATH = tmp_path / "test_fabrication.db"
    db_module._local.__dict__.clear()
    db_module.init_db()
    yield
    db_module._local.__dict__.clear()


def test_search_and_export_match_database_strictly(tmp_path, monkeypatch):
    import tender_mcp.server as srv_module
    monkeypatch.setattr(srv_module, "EXPORTS_DIR", tmp_path / "exports")

    # Insert a tender with complete enriched details
    tender_raw = {
        "id": "gem:GEM_2026_B_9990001",
        "source": "gem",
        "title": "Supply of Pressure Gauges and Valves",
        "organisation": "Bharat Petroleum Corporation Ltd",
        "category": "Mechanical",
        "published_date": "2026-10-01T10:00:00Z",
        "closing_date": "2026-11-15T15:00:00Z",
        "opening_date": "2026-11-15T15:30:00Z",
        "emd_value": "50000",
        "tender_value": "2500000",
        "link": "https://bidplus.gem.gov.in/showbidDocument/9990001",
        "status": "active",
        "raw": {"test": "data"},
    }
    tid, _ = upsert_tender(tender_raw)

    enrichment_fields = {
        "bid_number": "GEM/2026/B/9990001",
        "bid_end_datetime": "15-11-2026 15:00:00",
        "bid_opening_datetime": "15-11-2026 15:30:00",
        "bid_offer_validity_days": 90,
        "ministry": "Ministry of Petroleum and Natural Gas",
        "department": "Bharat Petroleum Corporation Ltd",
        "office_name": "Mumbai Refinery",
        "item_category": "Pressure Gauges and Valves",
        "total_quantity": 50,
        "estimated_bid_value_inr": 2500000.0,
        "emd_required": True,
        "emd_amount_inr": None,
        "emd_amount_inr_min": 25000.0,
        "emd_amount_inr_max": 50000.0,
        "emd_schedules": [{"schedule": "Schedule 1", "amount": 25000.0}, {"schedule": "Schedule 2", "amount": 50000.0}],
        "epbg_required": True,
        "epbg_percent": 3.0,
        "epbg_months": 12,
        "mse_reserved": False,
        "mse_purchase_preference": "yes at 15%",
        "mse_experience_turnover_relaxation": True,
        "startup_relaxation": True,
        "mii_class_restriction": "Class 1 and 2 local suppliers only",
        "mii_preference": "yes",
        "bis_required": 1,
        "min_turnover_lakh": 15.5,
        "years_experience_required": 3,
        "past_performance_percent": 10.0,
        "oem_average_turnover_lakh": 50.0,
        "oem_authorization_required": True,
        "bid_type": "two packet",
        "reverse_auction": True,
        "short_duration_bid": False,
        "inspection_required": True,
        "contract_period": "3 Months",
        "delivery_days": 90,
        "consignee_city": "Mumbai",
        "bid_splitting": "not_stated",
        "auto_extend_min_bids": 3,
        "auto_extend_days": 7,
        "auto_extend_max_count": 2,
        "pdf_parse_status": "ok",
        "date_check": "unchanged",
        "corrigendum_date": "not_stated",
    }
    save_gem_pdf_enrichment(tid, enrichment_fields)

    # Fetch stored DB row directly
    with get_conn() as conn:
        db_row = conn.execute("SELECT * FROM tenders WHERE id = ?", (tid,)).fetchone()
        assert db_row is not None
        db_dict = dict(db_row)

    # 1. Check search_tenders output against DB
    search_res_json = search_tenders(keyword="Pressure Gauges", include_low=True, include_tier_c=True)
    search_list = json.loads(search_res_json)
    assert len(search_list) == 1
    search_item = search_list[0]

    # Every DB column that appears in search_item must equal the value in DB
    for key, search_val in search_item.items():
        if key in ("hours_to_close", "bid_window", "closing_soon"):
            continue  # derived UI convenience fields
        if key in ("eligibility_reasons", "flags", "emd_schedules", "floor_price_schedules", "pbg_months_per_lot"):
            # in DB stored as JSON text, parsed in search_item
            db_val = json.loads(db_dict[key]) if isinstance(db_dict[key], str) and db_dict[key] else db_dict[key]
            assert search_val == db_val, f"Mismatch on {key}: search={search_val} vs db={db_val}"
        elif key in db_dict:
            db_val = db_dict[key]
            if isinstance(search_val, bool) and isinstance(db_val, int):
                assert search_val == bool(db_val), f"Mismatch on {key}: {search_val} vs {db_val}"
            elif db_val is None:
                assert search_val is None, f"Mismatch on {key}: expected None, got {search_val}"
            else:
                assert search_val == db_val, f"Mismatch on {key}: {search_val} vs {db_val}"

    # 2. Check export_tenders output against DB
    export_res_json = export_tenders(include_tier_c=True, min_hours_to_close=0)
    export_info = json.loads(export_res_json)
    csv_path = Path(export_info["path"])
    assert csv_path.exists()

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        csv_rows = list(reader)
    assert len(csv_rows) == 1
    csv_row = csv_rows[0]

    for key, csv_val in csv_row.items():
        if key in ("hours_to_close", "bid_window", "closing_soon"):
            continue
        if key in db_dict:
            db_val = db_dict[key]
            if db_val is None:
                assert csv_val == "", f"Mismatch on {key}: expected empty CSV value, got {csv_val}"
            elif csv_val in ("True", "False"):
                assert csv_val == str(bool(db_val)), f"Mismatch on {key}: {csv_val} vs {db_val}"
            elif isinstance(db_val, (int, float)):
                assert float(csv_val) == float(db_val), f"Mismatch on {key}: {csv_val} vs {db_val}"
            else:
                assert csv_val == str(db_val), f"Mismatch on {key}: {csv_val} vs {db_val}"
