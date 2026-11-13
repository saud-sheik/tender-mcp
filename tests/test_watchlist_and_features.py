"""
tests/test_watchlist_and_features.py
====================================
Tests for:
  - Watchlist (Star/unstar tenders, DB helpers, Web endpoint, KPI filtering)
  - Instant Slack / Webhook alert dispatch for Tier A tenders
  - Background auto-refresh scheduler settings & refresh-all endpoint
  - MCP tools for watchlist
"""
import base64
import json
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

import tender_mcp.db as db_module
from tender_mcp.web.app import app
from tender_mcp.notifications import format_tender_summary, send_tier_a_alert, send_slack_message
from tender_mcp.server import toggle_watchlist, get_watchlist


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch, tmp_path):
    """Temporary test database and environment credentials."""
    test_db = tmp_path / "test_features.db"
    db_module.DB_PATH = test_db
    db_module._local.__dict__.clear()
    db_module.init_db()

    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret1234567890123")
    monkeypatch.setenv("HMEL_USERNAME", "vendor1")
    monkeypatch.setenv("HMEL_PASSWORD", "pass1")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("AUTO_REFRESH_INTERVAL_MINUTES", "0")

    yield

    db_module._local.__dict__.clear()


def _auth_header() -> dict[str, str]:
    token = base64.b64encode(b"admin:secret1234567890123").decode()
    return {"Authorization": f"Basic {token}"}


def test_watchlist_db_and_api():
    """Verify watchlist DB functions and web toggle endpoint."""
    t = {
        "id": "gem:GEM/2026/B/99901",
        "source": "gem",
        "title": "Supply of Rotary Pumps",
        "organisation": "Indian Oil Corporation",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-12-01T15:00:00Z",
        "relevance_score": 85,
        "tier": "A",
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t)

    # 1. Initially is_watched is 0
    tender = db_module.get_tender_by_id(t["id"])
    assert tender["is_watched"] == 0

    # 2. Toggle via DB helper -> 1
    new_state = db_module.toggle_tender_watched(t["id"])
    assert new_state is True
    assert db_module.get_tender_by_id(t["id"])["is_watched"] == 1

    # 3. get_watched_tenders returns it
    watched = db_module.get_watched_tenders()
    assert len(watched) == 1
    assert watched[0]["id"] == t["id"]

    # 4. Toggle via web endpoint -> 0 (returns JSON)
    client = TestClient(app)
    resp = client.post(f"/tenders/{t['id']}/toggle-watch", headers=_auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["is_watched"] is False
    assert db_module.get_tender_by_id(t["id"])["is_watched"] == 0

    # 5. Toggle via web endpoint with redirect_url -> 1
    resp2 = client.post(
        f"/tenders/{t['id']}/toggle-watch",
        data={"redirect_url": f"/tenders/{t['id']}"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    assert resp2.headers["location"] == f"/tenders/{t['id']}"
    assert db_module.get_tender_by_id(t["id"])["is_watched"] == 1


def test_watchlist_filter_in_list_page():
    """Verify list page filters correctly when status=watched or tier=watched."""
    t1 = {
        "id": "gem:GEM/2026/B/801",
        "source": "gem",
        "title": "Butterfly Valves for HPCL",
        "organisation": "HPCL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-12-01T15:00:00Z",
        "relevance_score": 90,
        "tier": "A",
        "status": "new",
        "raw": {},
    }
    t2 = {
        "id": "cppp:CPPP_802",
        "source": "cppp",
        "title": "Industrial Hoses for GAIL",
        "organisation": "GAIL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-12-01T15:00:00Z",
        "relevance_score": 75,
        "tier": "A",
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t1)
    db_module.upsert_tender(t2)

    # Star only t1
    db_module.set_tender_watched(t1["id"], True)

    client = TestClient(app)

    # 1. Filter status=watched
    resp = client.get("/?status=watched", headers=_auth_header())
    assert resp.status_code == 200
    assert t1["title"] in resp.text
    assert t2["title"] not in resp.text

    # 2. Check KPI card counts in HTML
    assert "⭐ Watched" in resp.text


def test_instant_slack_notifications():
    """Verify instant Slack alert payload generation and safe delivery."""
    sample_tenders = [
        {
            "id": "hmel:5001",
            "title": "Supply of High Pressure Gaskets",
            "organisation": "HPCL-Mittal Energy Limited",
            "source": "hmel",
            "tier": "A",
            "relevance_score": 92,
            "bid_number": "HMEL/2026/005",
            "closing_date": "2026-11-28T14:00:00Z",
            "emd_amount_inr": 250000.0,
            "link": "https://hmeleprocurement.mjunction.in/epspropartner/hmel/login-screen",
        }
    ]

    # 1. format_tender_summary
    summary = format_tender_summary(sample_tenders[0])
    assert "High Pressure Gaskets" in summary
    assert "HMEL/2026/005" in summary
    assert "₹250,000" in summary
    assert "92 (Tier A)" in summary

    # 2. send_tier_a_alert when no webhook configured -> returns False safely
    assert send_tier_a_alert(sample_tenders, webhook_url="") is False

    # 3. send_tier_a_alert with mock webhook -> returns True
    with patch("tender_mcp.notifications.urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_open.return_value = mock_resp

        ok = send_tier_a_alert(sample_tenders, webhook_url="https://hooks.slack.com/services/mock/url")
        assert ok is True
        assert mock_open.called


def test_settings_slack_and_scheduler_routes(monkeypatch, tmp_path):
    """Verify /settings endpoints for Slack config, Scheduler config, and Refresh All."""
    env_file = tmp_path / ".env"
    monkeypatch.setattr("tender_mcp.config.ENV_PATH", env_file)

    client = TestClient(app)

    # 1. Update Slack webhook URL
    resp_slack = client.post(
        "/settings/slack",
        data={"webhook_url": "https://hooks.slack.com/services/TEST/123", "test_alert": 0},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert resp_slack.status_code == 303

    from tender_mcp.config import get_slack_webhook_url, get_auto_refresh_interval
    assert get_slack_webhook_url() == "https://hooks.slack.com/services/TEST/123"

    # 2. Update Scheduler interval to 180 min
    resp_sched = client.post(
        "/settings/scheduler",
        data={"interval_minutes": 180},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert resp_sched.status_code == 303
    assert get_auto_refresh_interval() == 180

    # 3. Trigger full refresh
    from unittest.mock import AsyncMock
    with patch("tender_mcp.refresh.refresh_all", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = {"total_fetched": 15, "total_new": 2, "total_updated": 13}
        resp_refresh = client.post("/settings/refresh-all", headers=_auth_header(), follow_redirects=False)
        assert resp_refresh.status_code == 303


def test_mcp_watchlist_tools():
    """Verify toggle_watchlist and get_watchlist MCP tools."""
    t = {
        "id": "gem:GEM/2026/B/777",
        "source": "gem",
        "title": "Explosion Proof Motors",
        "organisation": "ONGC",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-12-15T15:00:00Z",
        "relevance_score": 88,
        "tier": "A",
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t)

    # 1. Star via MCP tool
    res_str = toggle_watchlist(t["id"])
    res = json.loads(res_str)
    assert res["status"] == "success"
    assert res["is_watched"] is True

    # 2. Fetch via MCP tool
    watchlist_str = get_watchlist()
    wl = json.loads(watchlist_str)
    assert wl["status"] == "success"
    assert wl["count"] == 1
    assert wl["tenders"][0]["id"] == t["id"]

    # 3. Unstar via MCP tool
    res_str2 = toggle_watchlist(t["id"])
    res2 = json.loads(res_str2)
    assert res2["is_watched"] is False
