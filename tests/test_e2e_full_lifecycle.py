"""
tests/test_e2e_full_lifecycle.py
================================
Comprehensive End-to-End (E2E) Test Suite:
Simulates a complete real-world operational lifecycle across all subsystems:
  1. Ingestion of raw records from all 3 portals (GeM, CPPP, HMEL)
  2. Matching, scoring, noise filtering, buyer classification
  3. Automatic notification / instant alert dispatch on discovery
  4. Web dashboard operations (Auth, list, filters, watchlist, detail, status update, CSV export, settings)
  5. MCP server protocol tools
  6. Database consistency & integrity verification
"""
from __future__ import annotations

import base64
import json
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
from fastapi.testclient import TestClient

import tender_mcp.db as db_mod
from tender_mcp.web.app import app
from tender_mcp.refresh import refresh_all
from tender_mcp.notifications import send_tier_a_alert
from tender_mcp.server import (
    search_tenders,
    get_tender,
    export_tenders,
    tender_stats,
    toggle_watchlist,
    get_watchlist,
    configure_and_fetch_hmel,
)
from scripts.check_consistency import check_consistency


@pytest.fixture(autouse=True)
def e2e_environment(monkeypatch, tmp_path):
    """Setup isolated test database and test configuration."""
    test_db = tmp_path / "e2e_test.db"
    db_mod.DB_PATH = test_db
    db_mod._local.__dict__.clear()
    db_mod.init_db()

    env_path = tmp_path / ".env"
    monkeypatch.setattr("tender_mcp.config.ENV_PATH", env_path)

    monkeypatch.setenv("DASHBOARD_USER", "ops_admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "super_secret_dashboard_pw_999")
    monkeypatch.setenv("HMEL_USERNAME", "client_hmel_vendor")
    monkeypatch.setenv("HMEL_PASSWORD", "client_hmel_secret")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/E2E/TEST/WEBHOOK")
    monkeypatch.setenv("AUTO_REFRESH_INTERVAL_MINUTES", "180")

    yield

    db_mod._local.__dict__.clear()


def _auth_header() -> dict[str, str]:
    tok = base64.b64encode(b"ops_admin:super_secret_dashboard_pw_999").decode()
    return {"Authorization": f"Basic {tok}"}


@pytest.mark.asyncio
async def test_e2e_full_lifecycle():
    """Execute complete end-to-end integration workflow."""
    
    # -------------------------------------------------------------------------
    # Phase 1: Mock Ingestion from GeM, CPPP, and HMEL
    # -------------------------------------------------------------------------
    mock_gem_tenders = [
        {
            "id": "gem:GEM_2026_B_9001",
            "source": "gem",
            "title": "Supply and Installation of Coriolis Mass Flowmeters",
            "organisation": "Ministry of Petroleum and Natural Gas Bharat Petroleum Corporation Ltd",
            "category": "Goods",
            "published_date": "2026-11-01T00:00:00Z",
            "closing_date": "2026-12-10T15:00:00Z",
            "relevance_score": 85,
            "tier": "A",
            "tender_kind": "product",
            "bid_number": "GEM/2026/B/9001",
            "emd_amount_inr": 150000.0,
            "link": "https://bidplus.gem.gov.in/showbidDocument/9001",
            "raw": {"bid_id": "9001"},
        }
    ]

    mock_cppp_tenders = [
        {
            "id": "cppp:IOCL_2026_CPPP_9002",
            "source": "cppp",
            "title": "Supply of High Pressure Gate Valves for Panipat Refinery",
            "organisation": "Indian Oil Corporation Limited",
            "category": "Goods",
            "published_date": "2026-11-02T00:00:00Z",
            "closing_date": "2026-12-12T15:00:00Z",
            "relevance_score": 90,
            "tier": "A",
            "tender_kind": "product",
            "bid_number": "IOCL/PANIPAT/2026/9002",
            "link": "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata",
            "raw": {},
        }
    ]

    mock_hmel_tenders = [
        {
            "id": "hmel:HMEL_RFQ_9003",
            "source": "hmel",
            "title": "Procurement of API 610 Centrifugal Pumps for Polypropylene Unit",
            "organisation": "HPCL-Mittal Energy Limited",
            "category": "Goods",
            "published_date": "2026-11-03T00:00:00Z",
            "closing_date": "2026-12-15T15:00:00Z",
            "relevance_score": 95,
            "tier": "A",
            "tender_kind": "product",
            "bid_number": "HMEL/RFQ/9003",
            "link": "https://hmeleprocurement.mjunction.in/epspropartner/hmel/login-screen",
            "raw": {},
        }
    ]

    # Patch the scrapers to return our realistic portal data
    with patch("tender_mcp.refresh._run_gem", new_callable=AsyncMock, return_value=mock_gem_tenders), \
         patch("tender_mcp.refresh._run_cppp", new_callable=AsyncMock, return_value=mock_cppp_tenders), \
         patch("tender_mcp.refresh._run_hmel", new_callable=AsyncMock, return_value=(mock_hmel_tenders, None)), \
         patch("tender_mcp.notifications.send_tier_a_alert") as mock_alert_dispatch:

        summary = await refresh_all(sources=["gem", "cppp", "hmel"])

        # Verify summary stats
        assert summary["total_fetched"] == 3
        assert summary["total_new"] == 3
        assert summary["sources"]["gem"]["new"] == 1
        assert summary["sources"]["cppp"]["new"] == 1
        assert summary["sources"]["hmel"]["new"] == 1

        # Verify instant Slack notification was triggered for newly discovered Tier A bids
        assert mock_alert_dispatch.called
        dispatched_tenders = mock_alert_dispatch.call_args[0][0]
        assert len(dispatched_tenders) >= 3

    # -------------------------------------------------------------------------
    # Phase 2: Web Dashboard & Endpoints E2E
    # -------------------------------------------------------------------------
    client = TestClient(app)

    # 1. Health endpoint
    r_health = client.get("/health")
    assert r_health.status_code == 200
    health_data = r_health.json()
    assert health_data["status"] == "healthy"
    assert "sources" in health_data

    # 2. Main List Page
    r_list = client.get("/", headers=_auth_header())
    assert r_list.status_code == 200
    assert "Coriolis Mass Flowmeters" in r_list.text
    assert "High Pressure Gate Valves" in r_list.text
    assert "Centrifugal Pumps" in r_list.text
    assert "⭐ Watched" in r_list.text

    # 3. Source Filtering
    r_hmel_filter = client.get("/?source=hmel", headers=_auth_header())
    assert r_hmel_filter.status_code == 200
    assert "Centrifugal Pumps" in r_hmel_filter.text
    assert "High Pressure Gate Valves" not in r_hmel_filter.text

    # 4. Watchlist Toggle E2E
    # Initially none are watched
    r_empty_watched = client.get("/?status=watched", headers=_auth_header())
    assert r_empty_watched.status_code == 200
    assert "No matching tenders found" in r_empty_watched.text

    # Star the HMEL tender via API
    r_watch = client.post("/tenders/hmel:HMEL_RFQ_9003/toggle-watch", headers=_auth_header())
    assert r_watch.status_code == 200
    assert r_watch.json()["is_watched"] is True

    # Now verify it appears under status=watched
    r_watched_list = client.get("/?status=watched", headers=_auth_header())
    assert r_watched_list.status_code == 200
    assert "Centrifugal Pumps" in r_watched_list.text
    assert "High Pressure Gate Valves" not in r_watched_list.text

    # 5. Detail View
    r_detail = client.get("/tenders/hmel:HMEL_RFQ_9003", headers=_auth_header())
    assert r_detail.status_code == 200
    assert "HPCL-Mittal Energy Limited" in r_detail.text
    assert "hmeleprocurement.mjunction.in" in r_detail.text
    assert "Open HMEL portal ↗" in r_detail.text

    # 6. Status, Owner, and Notes Update
    r_update = client.post(
        "/tenders/hmel:HMEL_RFQ_9003/update",
        data={"status": "bidding", "owner": "Saud", "notes": "Approved for bidding by management"},
        headers=_auth_header(),
        follow_redirects=True,
    )
    assert r_update.status_code == 200
    assert "bidding" in r_update.text
    assert "Saud" in r_update.text
    assert "Approved for bidding by management" in r_update.text

    # 7. CSV Export
    r_csv = client.get("/export?tier=all", headers=_auth_header())
    assert r_csv.status_code == 200
    csv_text = r_csv.text
    assert "id,source,title" in csv_text
    assert "hmel:HMEL_RFQ_9003" in csv_text
    assert "gem:GEM_2026_B_9001" in csv_text
    assert "cppp:IOCL_2026_CPPP_9002" in csv_text

    # 8. Settings Page & Credentials Update
    r_settings = client.get("/settings", headers=_auth_header())
    assert r_settings.status_code == 200
    assert "Auto-Refresh Scheduler" in r_settings.text
    assert "Instant Slack Webhook Alerts" in r_settings.text

    # Save new HMEL credentials
    r_save_hmel = client.post(
        "/settings/hmel",
        data={"username": "new_vendor_user", "password": "new_vendor_pw_123"},
        headers=_auth_header(),
        follow_redirects=True,
    )
    assert r_save_hmel.status_code == 200
    assert "HMEL mjunction credentials saved successfully" in r_save_hmel.text

    # Unlock lockout protection
    r_unlock = client.post("/settings/hmel/unlock", headers=_auth_header(), follow_redirects=True)
    assert r_unlock.status_code == 200

    # -------------------------------------------------------------------------
    # Phase 3: MCP Server Tools E2E
    # -------------------------------------------------------------------------
    # 1. search_tenders
    res_search = json.loads(search_tenders(keyword="Flowmeter"))
    assert len(res_search) == 1
    assert res_search[0]["id"] == "gem:GEM_2026_B_9001"

    # 2. get_tender
    res_get = json.loads(get_tender("hmel:HMEL_RFQ_9003"))
    assert res_get["status"] == "bidding"
    assert res_get["owner"] == "Saud"

    # 3. get_watchlist
    res_wl = json.loads(get_watchlist())
    assert res_wl["count"] == 1
    assert res_wl["tenders"][0]["id"] == "hmel:HMEL_RFQ_9003"

    # 4. toggle_watchlist via MCP
    res_toggle = json.loads(toggle_watchlist("gem:GEM_2026_B_9001"))
    assert res_toggle["is_watched"] is True
    res_wl2 = json.loads(get_watchlist())
    assert res_wl2["count"] == 2

    # -------------------------------------------------------------------------
    # Phase 4: Database Consistency Verification
    # -------------------------------------------------------------------------
    is_consistent, violations = check_consistency()
    assert is_consistent is True
    assert all(len(v) == 0 for v in violations.values())
