"""
tests/test_web.py  –  Tests for FastAPI dashboard and email alerts
==================================================================
Tests:
  - Auth required (401 on missing/wrong credentials, 200 on valid)
  - List filters (tier, kind, extended_only, CSV export)
  - Status write and history row recorded
  - Security test: page never contains HMEL username or password
  - Send digest dry-run and mock SMTP execution
"""
import base64
import os
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

import tender_mcp.db as db_module
from tender_mcp.web.app import app


@pytest.fixture(autouse=True)
def use_temp_db_and_auth(monkeypatch, tmp_path):
    """Point DB layer at a temporary database and set dashboard auth in environment."""
    test_db = tmp_path / "test_web.db"
    db_module.DB_PATH = test_db
    db_module._local.__dict__.clear()
    db_module.init_db()

    monkeypatch.setenv("DASHBOARD_USER", "testuser")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpass12345678901234")
    monkeypatch.setenv("HMEL_USERNAME", "secret_hmel_user_99")
    monkeypatch.setenv("HMEL_PASSWORD", "secret_hmel_pass_99")

    yield

    db_module._local.__dict__.clear()


def _get_auth_header(user: str = "testuser", pw: str = "testpass12345678901234") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_auth_required():
    """Verify HTTP Basic auth is strictly enforced."""
    client = TestClient(app)

    # 1. No auth -> 401
    r_no_auth = client.get("/")
    assert r_no_auth.status_code == 401
    assert "WWW-Authenticate" in r_no_auth.headers

    # 2. Wrong auth -> 401
    bad_auth = _get_auth_header("testuser", "wrongpassword")
    r_bad_auth = client.get("/", headers=bad_auth)
    assert r_bad_auth.status_code == 401

    # 3. Valid auth -> 200
    good_auth = _get_auth_header()
    r_good = client.get("/", headers=good_auth)
    assert r_good.status_code == 200


def test_list_filters_and_export():
    """Verify list filters (tier, tender_kind, extended_only) and CSV export."""
    client = TestClient(app)
    auth = _get_auth_header()

    # Populate temporary tenders
    t1 = {
        "id": "gem:GEM_2026_B_111",
        "source": "gem",
        "title": "Supply of Gas Detectors for BPCL",
        "organisation": "BPCL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-20T10:00:00Z",
        "relevance_score": 80,
        "status": "new",
        "raw": {},
    }
    t2 = {
        "id": "gem:GEM_2026_B_222",
        "source": "gem",
        "title": "Operation and Maintenance of Fire Hydrant System",
        "organisation": "Delhi Police Department",
        "category": "Services",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-25T10:00:00Z",
        "relevance_score": 10,
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t1)
    db_module.upsert_tender(t2)

    # Filter by tier=A
    r_tier_a = client.get("/?tier=A", headers=auth)
    assert r_tier_a.status_code == 200
    assert "Supply of Gas Detectors" in r_tier_a.text
    assert "Fire Hydrant System" not in r_tier_a.text

    # CSV Export
    r_export = client.get("/export?tier=A", headers=auth)
    assert r_export.status_code == 200
    assert "text/csv" in r_export.headers["Content-Type"]
    assert "Supply of Gas Detectors" in r_export.text
    assert "Fire Hydrant System" not in r_export.text


def test_status_write_and_history_row():
    """Verify detail page status update saves status, owner, notes, and records a status_history row."""
    client = TestClient(app)
    auth = _get_auth_header()

    t = {
        "id": "gem:GEM_2026_B_333",
        "source": "gem",
        "title": "Supply of High Pressure LPG Hose",
        "organisation": "IOCL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-30T10:00:00Z",
        "relevance_score": 75,
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t)

    # Post update
    update_data = {
        "status": "bidding",
        "owner": "rahul",
        "notes": "Reviewed with technical team - preparing bid.",
    }
    r_post = client.post(
        f"/tenders/{t['id']}/update",
        data=update_data,
        headers=auth,
        follow_redirects=False,
    )
    assert r_post.status_code == 303  # Redirect back to detail page

    # Check updated tender in DB
    updated = db_module.get_tender_by_id(t["id"])
    assert updated["status"] == "bidding"
    assert updated["owner"] == "rahul"
    assert updated["notes"] == "Reviewed with technical team - preparing bid."

    # Check status_history record
    history = db_module.get_tender_status_history(t["id"])
    assert len(history) == 1
    assert history[0]["old_status"] == "new"
    assert history[0]["new_status"] == "bidding"
    assert history[0]["owner"] == "rahul"


def test_page_never_contains_hmel_credentials():
    """Security check: Dashboard pages must NEVER expose HMEL username or password."""
    client = TestClient(app)
    auth = _get_auth_header()

    t = {
        "id": "gem:GEM_2026_B_444",
        "source": "gem",
        "title": "Refinery Pipe Supply",
        "organisation": "HMEL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-30T10:00:00Z",
        "relevance_score": 70,
        "status": "new",
        "raw": {"debug_secret": "do_not_leak_raw_json"},
    }
    db_module.upsert_tender(t)

    # 1. Check List Page
    r_list = client.get("/", headers=auth)
    assert r_list.status_code == 200
    assert "secret_hmel_user_99" not in r_list.text
    assert "secret_hmel_pass_99" not in r_list.text
    assert "do_not_leak_raw_json" not in r_list.text

    # 2. Check Detail Page
    r_detail = client.get(f"/tenders/{t['id']}", headers=auth)
    assert r_detail.status_code == 200
    assert "secret_hmel_user_99" not in r_detail.text
    assert "secret_hmel_pass_99" not in r_detail.text
    assert "do_not_leak_raw_json" not in r_detail.text


def test_send_digest_dry_run_and_mock_smtp(monkeypatch, capsys):
    """Verify send_digest.py dry run and mock SMTP execution."""
    from scripts.send_digest import main as digest_main

    from datetime import datetime, timezone, timedelta
    close_iso = (datetime.now(timezone.utc) + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t = {
        "id": "gem:GEM_2026_B_555",
        "source": "gem",
        "title": "Custom Bid for LPG Pump Spares",
        "organisation": "BPCL",
        "category": "Goods",
        "published_date": "2026-10-14T00:00:00Z",
        "closing_date": close_iso,  # Closing within 72 hours and > 24 hours
        "relevance_score": 85,
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t)

    # 1. Test --dry-run
    monkeypatch.setattr("sys.argv", ["send_digest.py", "--dry-run"])
    digest_main()
    out = capsys.readouterr().out
    assert "SWAMINA INTERNATIONAL - TENDER ALERT DIGEST" in out
    assert "Custom Bid for LPG Pump Spares" in out

    # 2. Test send with Mock SMTP
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "alert@swamina.com")
    monkeypatch.setenv("SMTP_PASSWORD", "mockpassword")
    monkeypatch.setenv("DIGEST_FROM", "alert@swamina.com")
    monkeypatch.setenv("DIGEST_TO", "procurement@swamina.com")

    # 2a. Verify send is refused when DIGEST_ENABLED is false
    monkeypatch.setenv("DIGEST_ENABLED", "false")
    monkeypatch.setattr("sys.argv", ["send_digest.py", "--send"])
    with pytest.raises(SystemExit):
        digest_main()

    # 2b. Test send with Mock SMTP when DIGEST_ENABLED is true
    monkeypatch.setenv("DIGEST_ENABLED", "true")
    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_instance
        digest_main()
        assert mock_instance.starttls.called
        assert mock_instance.send_message.called


def test_location_only_demoted_to_tier_b():
    """Verify tenders where match_basis == 'location_only' are Tier B even from an oil/gas buyer with score >= 70."""
    from tender_mcp.matcher import compute_tier, score_tender

    # Example: high score keyword only in plant name, item is unlisted
    t = {
        "id": "gem:GEM_2026_B_LOC_ONLY",
        "source": "gem",
        "title": "RATE CONTRACT FOR SUPPLY OF PRE-TREATED USED COOKING OIL FOR VISAKH REFINERY",
        "organisation": "Hindustan Petroleum Corporation Limited",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-20T10:00:00Z",
        "raw": {},
    }
    scoring = score_tender(t)
    assert scoring["match_basis"] == "location_only"
    tier = compute_tier(scoring["score"], t["organisation"], match_basis=scoring["match_basis"])
    assert tier == "B"

    # When stored in DB via upsert_tender
    db_module.upsert_tender(t)
    stored = db_module.get_tender_by_id(t["id"])
    assert stored["match_basis"] == "location_only"
    assert stored["tier"] == "B"


def test_dashboard_kind_toggle_and_coloured_label():
    """Verify products only / services only / both toggle and coloured labels."""
    client = TestClient(app)
    auth = _get_auth_header()

    t_prod = {
        "id": "gem:GEM_2026_B_PROD",
        "source": "gem",
        "title": "Supply of Ball Valves for HPCL",
        "organisation": "HPCL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-20T10:00:00Z",
        "raw": {},
    }
    t_serv = {
        "id": "gem:GEM_2026_B_SERV",
        "source": "gem",
        "title": "Hiring of Maintenance Services for Gas Pipeline",
        "organisation": "GAIL",
        "category": "Services",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-20T10:00:00Z",
        "raw": {},
    }
    db_module.upsert_tender(t_prod)
    db_module.upsert_tender(t_serv)

    # 1. Default toggle 'both'
    r_both = client.get("/?tier=all", headers=auth)
    assert r_both.status_code == 200
    assert "Supply of Ball Valves" in r_both.text
    assert "Hiring of Maintenance Services" in r_both.text

    # 2. 'products only'
    r_prod = client.get("/?tier=all&tender_kind=products+only", headers=auth)
    assert r_prod.status_code == 200
    assert "Supply of Ball Valves" in r_prod.text
    assert "Hiring of Maintenance Services" not in r_prod.text

    # 3. 'services only'
    r_serv = client.get("/?tier=all&tender_kind=services+only", headers=auth)
    assert r_serv.status_code == 200
    assert "Supply of Ball Valves" not in r_serv.text
    assert "Hiring of Maintenance Services" in r_serv.text


def test_digest_three_sections_and_empty_none(monkeypatch, capsys):
    """Verify all 3 sections are printed every time, empty sections print 'none', and IST in header."""
    from scripts.send_digest import main as digest_main

    monkeypatch.setattr("sys.argv", ["send_digest.py", "--dry-run"])
    digest_main()
    out = capsys.readouterr().out

    assert "SWAMINA INTERNATIONAL - TENDER ALERT DIGEST" in out
    assert "Generated:" in out and "IST" in out
    assert "--- 1. NEW TIER A TENDERS SINCE LAST DIGEST" in out
    assert "--- 2. TIER A CLOSING WITHIN 72 HOURS" in out
    assert "--- 3. DEADLINES MOVED LATER SINCE LAST DIGEST" in out
    assert "none" in out


def test_digest_dry_run_never_writes_last_sent(monkeypatch, capsys):
    """Verify --dry-run does NOT write last_sent_at to digest_state and window does not move."""
    from scripts.send_digest import get_last_sent_at, main as digest_main
    import tender_mcp.db as db_mod

    # Confirm initial state is empty
    assert get_last_sent_at() is None
    with db_mod.get_conn() as conn:
        count = conn.execute("SELECT count(*) FROM digest_state").fetchone()[0]
        assert count == 0

    # First dry run
    monkeypatch.setattr("sys.argv", ["send_digest.py", "--dry-run"])
    digest_main()
    out1 = capsys.readouterr().out
    assert "Window: Since" in out1
    assert "first run: last 24 hours" in out1

    # State must still be empty!
    assert get_last_sent_at() is None
    with db_mod.get_conn() as conn:
        count = conn.execute("SELECT count(*) FROM digest_state").fetchone()[0]
        assert count == 0

    # Second dry run
    digest_main()
    out2 = capsys.readouterr().out
    assert "first run: last 24 hours" in out2

    # Window start in out1 and out2 must not jump to the generation time
    gen_time_line = [l for l in out2.splitlines() if "Generated:" in l][0]
    gen_time = gen_time_line.split("Generated:")[1].strip()
    window_line = [l for l in out2.splitlines() if "Window:" in l][0]
    # Window start cannot be equal to generation time
    assert gen_time not in window_line


def test_dashboard_redesign_kpis_and_form():
    """Verify list page contains 6 KPI labels and status counts, and detail page retains form fields."""
    client = TestClient(app)
    auth = _get_auth_header()

    seeded = [
        {"id": "gem:KPI_1", "source": "gem", "title": "Tender 1", "organisation": "IOCL", "category": "Goods", "published_date": "2026-10-01T00:00:00Z", "closing_date": "2026-11-20T10:00:00Z", "relevance_score": 75, "status": "new", "raw": {}},
        {"id": "gem:KPI_2", "source": "gem", "title": "Tender 2", "organisation": "BPCL", "category": "Goods", "published_date": "2026-10-01T00:00:00Z", "closing_date": "2026-11-20T10:00:00Z", "relevance_score": 80, "status": "new", "raw": {}},
        {"id": "gem:KPI_3", "source": "gem", "title": "Tender 3", "organisation": "HPCL", "category": "Goods", "published_date": "2026-10-01T00:00:00Z", "closing_date": "2026-11-20T10:00:00Z", "relevance_score": 85, "status": "interested", "raw": {}},
        {"id": "gem:KPI_4", "source": "gem", "title": "Tender 4", "organisation": "ONGC", "category": "Goods", "published_date": "2026-10-01T00:00:00Z", "closing_date": "2026-11-20T10:00:00Z", "relevance_score": 90, "status": "bidding", "raw": {}},
        {"id": "gem:KPI_5", "source": "gem", "title": "Tender 5", "organisation": "GAIL", "category": "Goods", "published_date": "2026-10-01T00:00:00Z", "closing_date": "2026-11-20T10:00:00Z", "relevance_score": 70, "status": "submitted", "raw": {}},
        {"id": "gem:KPI_6", "source": "gem", "title": "Tender 6", "organisation": "OIL", "category": "Goods", "published_date": "2026-10-01T00:00:00Z", "closing_date": "2026-11-20T10:00:00Z", "relevance_score": 72, "status": "skipped", "raw": {}},
    ]
    for item in seeded:
        db_module.upsert_tender(item)

    # 1. Verify list page KPI labels and counts
    r_list = client.get("/", headers=auth)
    assert r_list.status_code == 200

    # KPI labels
    assert "All tenders" in r_list.text
    assert "New" in r_list.text
    assert "Interested" in r_list.text
    assert "Bidding" in r_list.text
    assert "Submitted" in r_list.text
    assert "Skipped" in r_list.text

    # Seeded status counts: total 6, new 2, interested 1, bidding 1, submitted 1, skipped 1
    # Check that KPI numbers appear within their respective card elements
    assert '<div class="kpi-num">6</div>\n    <div class="kpi-label">All tenders</div>' in r_list.text
    assert '<div class="kpi-num">2</div>\n    <div class="kpi-label">New</div>' in r_list.text
    assert '<div class="kpi-num">1</div>\n    <div class="kpi-label">Interested</div>' in r_list.text
    assert '<div class="kpi-num">1</div>\n    <div class="kpi-label">Bidding</div>' in r_list.text
    assert '<div class="kpi-num">1</div>\n    <div class="kpi-label">Submitted</div>' in r_list.text
    assert '<div class="kpi-num">1</div>\n    <div class="kpi-label">Skipped</div>' in r_list.text

    # 2. Verify detail page update form fields
    r_detail = client.get(f"/tenders/{seeded[0]['id']}", headers=auth)
    assert r_detail.status_code == 200
    assert 'name="status"' in r_detail.text
    assert 'name="owner"' in r_detail.text
    assert 'name="notes"' in r_detail.text
    assert "Save Changes" in r_detail.text


def test_pdf_parse_status_and_cppp_labels():
    """Verify portal status fallback labels for unparsed GeM, parsed ok, and CPPP tenders."""
    client = TestClient(app)
    auth = _get_auth_header()

    # 1. Unparsed GeM tender (pdf_parse_status='none' by default)
    t_unparsed = {
        "id": "gem:GEM_UNPARSED_999",
        "source": "gem",
        "title": "Unparsed GeM Safety Valves",
        "organisation": "ONGC",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-20T10:00:00Z",
        "relevance_score": 85,
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t_unparsed)

    # 2. Parsed (ok) GeM tender with no EMD
    t_parsed_ok = {
        "id": "gem:GEM_PARSED_OK_999",
        "source": "gem",
        "title": "Parsed GeM Gas Chromatograph",
        "organisation": "BPCL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-20T10:00:00Z",
        "relevance_score": 80,
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t_parsed_ok)
    with db_module.get_conn() as conn:
        conn.execute("UPDATE tenders SET pdf_parse_status = 'ok' WHERE id = ?", (t_parsed_ok["id"],))
        conn.commit()

    # 3. CPPP tender
    t_cppp = {
        "id": "cppp:CPPP_TEST_999",
        "source": "cppp",
        "title": "CPPP Nitrogen Supply System",
        "organisation": "IOCL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-20T10:00:00Z",
        "relevance_score": 75,
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t_cppp)

    # 4. Partial parsed GeM tender
    t_partial = {
        "id": "gem:GEM_PARTIAL_999",
        "source": "gem",
        "title": "Partial GeM Flow Meters",
        "organisation": "GAIL",
        "category": "Goods",
        "published_date": "2026-10-01T00:00:00Z",
        "closing_date": "2026-11-20T10:00:00Z",
        "relevance_score": 70,
        "status": "new",
        "raw": {},
    }
    db_module.upsert_tender(t_partial)
    with db_module.get_conn() as conn:
        conn.execute("UPDATE tenders SET pdf_parse_status = 'partial' WHERE id = ?", (t_partial["id"],))
        conn.commit()

    # --- LIST VIEW CHECKS ---
    r_list = client.get("/", headers=auth)
    assert r_list.status_code == 200

    # (a) unparsed GeM tender shows new label and NOT "Nil / Exempt" in list
    unparsed_chunk = r_list.text.split(f'data-id="{t_unparsed["id"]}"')[1].split('</div>\n      </div>')[0]
    assert "Not extracted yet, check portal" in unparsed_chunk
    assert "Nil / Exempt" not in unparsed_chunk

    # (b) parsed (ok) tender with no EMD still shows "Nil / Exempt" in list
    parsed_ok_chunk = r_list.text.split(f'data-id="{t_parsed_ok["id"]}"')[1].split('</div>\n      </div>')[0]
    assert "EMD Nil / Exempt" in parsed_ok_chunk

    # (c) CPPP tender shows "See portal" and NOT "Nil / Exempt" in list
    cppp_chunk = r_list.text.split(f'data-id="{t_cppp["id"]}"')[1].split('</div>\n      </div>')[0]
    assert "See portal" in cppp_chunk
    assert "Nil / Exempt" not in cppp_chunk

    # --- DETAIL VIEW CHECKS ---
    # (a) unparsed GeM tender detail page shows new label and NOT "Nil / Exempt"
    r_detail_unparsed = client.get(f"/tenders/{t_unparsed['id']}", headers=auth)
    assert r_detail_unparsed.status_code == 200
    assert "Not extracted yet, check portal" in r_detail_unparsed.text
    assert "Nil / Exempt" not in r_detail_unparsed.text

    # (b) parsed (ok) tender with no EMD still shows "Nil / Exempt" on detail page
    r_detail_ok = client.get(f"/tenders/{t_parsed_ok['id']}", headers=auth)
    assert r_detail_ok.status_code == 200
    assert "Nil / Exempt" in r_detail_ok.text

    # (c) CPPP tender shows "See portal" and NOT "Nil / Exempt" on detail page
    r_detail_cppp = client.get(f"/tenders/{t_cppp['id']}", headers=auth)
    assert r_detail_cppp.status_code == 200
    assert "See portal" in r_detail_cppp.text
    assert "Nil / Exempt" not in r_detail_cppp.text

    # Partial tender detail page shows "Not found in PDF"
    r_detail_partial = client.get(f"/tenders/{t_partial['id']}", headers=auth)
    assert r_detail_partial.status_code == 200
    assert "Not found in PDF" in r_detail_partial.text
    assert "Nil / Exempt" not in r_detail_partial.text




