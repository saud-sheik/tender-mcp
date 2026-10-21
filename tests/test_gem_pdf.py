"""
tests/test_gem_pdf.py
Tests for GeM PDF download, extraction, integrity check, and eligibility evaluation.
Uses local fixture PDFs and temporary database, never the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tender_mcp.gem_pdf import parse_gem_pdf, evaluate_eligibility
from tender_mcp.db import init_db, get_conn, save_gem_pdf_enrichment

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "bids"


def test_sample_9944887_extraction():
    """Verify sample 9944887 (GEM/2026/B/8081986, ONGC Jorhat, plunger pump)."""
    p = FIXTURES_DIR / "9944887.pdf"
    assert p.exists(), "Sample PDF 9944887.pdf must exist in tests/fixtures/bids/"

    res = parse_gem_pdf(p, expected_bid_number="GEM/2026/B/8081986")
    assert res["pdf_parse_status"] == "ok"
    assert res["bid_number"] == "GEM/2026/B/8081986"
    assert res["bid_end_datetime"] == "20-10-2026 15:00:00"
    assert res["emd_required"] is True
    assert res["emd_amount_inr"] == 84400.0
    assert res["epbg_required"] is True
    assert res["epbg_percent"] == 3.0
    assert res["epbg_months"] == 21
    assert "yes at 15%" in res["mse_purchase_preference"].lower()
    assert res["mii_preference"] == "no"
    assert res["total_quantity"] == 1
    assert res["oem_authorization_required"] is True
    assert "two packet" in res["bid_type"].lower()
    assert res["inspection_required"] is True
    assert res["delivery_days"] == 210
    assert res["estimated_bid_value_inr"] is None
    assert res["min_turnover_lakh"] == "not_stated"
    assert res["years_experience_required"] == "not_stated"
    assert res["mii_class_restriction"] == "not_stated"
    assert res["bis_required"] == "not_stated"
    assert res["emd_amount_inr_min"] == 84400.0
    assert res["emd_amount_inr_max"] == 84400.0
    assert res["emd_schedules"] == []


def test_sample_9939832_extraction():
    """Verify sample 9939832 (GEM/2026/B/8077594, BPCL Dharwad LPG plant painting)."""
    p = FIXTURES_DIR / "9939832.pdf"
    assert p.exists()

    res = parse_gem_pdf(p, expected_bid_number="GEM/2026/B/8077594")
    assert res["pdf_parse_status"] == "ok"
    assert res["bid_number"] == "GEM/2026/B/8077594"
    assert res["bid_end_datetime"] == "15-10-2026 13:00:00"
    assert res["estimated_bid_value_inr"] == 2234646.0
    assert res["emd_required"] is False
    assert res["epbg_required"] is False
    assert res["min_turnover_lakh"] == 6.0
    assert res["years_experience_required"] == 3
    assert res["mse_experience_turnover_relaxation"] is True
    assert res["startup_relaxation"] is False
    assert "1 Month(s) 15 Day(s)" in res["contract_period"]
    assert res["mii_preference"] == "yes"
    assert "two packet" in res["bid_type"].lower()


def test_sample_9993139_extraction():
    """Verify sample 9993139 (GEM/2026/B/8123846, HPCL bitumen drum)."""
    p = FIXTURES_DIR / "9993139.pdf"
    assert p.exists()

    res = parse_gem_pdf(p, expected_bid_number="GEM/2026/B/8123846")
    assert res["pdf_parse_status"] == "ok"
    assert res["bid_number"] == "GEM/2026/B/8123846"
    assert res["bid_end_datetime"] == "20-10-2026 17:00:00"
    assert res["total_quantity"] == 500000
    assert res["mse_reserved"] is True
    assert res["startup_relaxation"] is True
    assert res["emd_required"] is False
    assert res["epbg_required"] is True
    assert res["epbg_percent"] == 2.0
    assert res["epbg_months"] == 27
    assert res["reverse_auction"] is True
    assert res["mii_class_restriction"] == "Class 1 and 2 local suppliers only"
    assert res["bis_required"] is True
    assert res["delivery_days"] == 720
    assert res["short_duration_bid"] is True
    assert res["bid_splitting"] == "90:10"
    assert res["consignee_city"] == "Mumbai"


def test_sample_9903155_multi_schedule_emd():
    """Verify schedule-wise EMD in 9903155.pdf: min, max, schedules list, do not sum."""
    p = FIXTURES_DIR / "9903155.pdf"
    assert p.exists()

    res = parse_gem_pdf(p, expected_bid_number="GEM/2026/B/8045390")
    assert res["bid_number"] == "GEM/2026/B/8045390"
    assert res["emd_required"] is True
    assert res["emd_amount_inr"] is None  # Do not sum across schedules!
    assert res["emd_amount_inr_min"] == 182000.0
    assert res["emd_amount_inr_max"] == 213000.0
    assert len(res["emd_schedules"]) == 2
    assert res["emd_schedules"][0] == {"schedule": "Schedule 1", "amount": 213000.0}
    assert res["emd_schedules"][1] == {"schedule": "Schedule 2", "amount": 182000.0}


def test_integrity_check_mismatch():
    """Integrity check must return mismatch status when expected bid number doesn't match."""
    p = FIXTURES_DIR / "9944887.pdf"
    res = parse_gem_pdf(p, expected_bid_number="GEM/2026/B/9999999")
    assert res["pdf_parse_status"] == "mismatch"
    assert res["bid_number"] == "GEM/2026/B/8081986"


def test_eligibility_evaluation_rules():
    """Test eligibility evaluation across profile scenarios."""
    tender_mse_reserved = {"mse_reserved": True}

    # Profile with is_msme = False -> likely_not_eligible
    status, reasons, flags = evaluate_eligibility(tender_mse_reserved, {"is_msme": False, "annual_turnover_lakh": 100.0, "years_in_business": 10, "is_startup": False})
    assert status == "likely_not_eligible"
    assert "reserved for MSEs" in reasons
    assert "MSE reserved" in flags

    # Profile with is_msme = None -> unknown (never eligible with any null value)
    status, reasons, flags = evaluate_eligibility(tender_mse_reserved, {"is_msme": None, "annual_turnover_lakh": 100.0, "years_in_business": 10})
    assert status == "unknown"
    assert "missing profile is_msme" in reasons
    assert "MSE reserved" in flags

    # Fully filled profile with no blockers -> no_blockers_found (never 'eligible')
    full_profile = {
        "is_msme": True,
        "is_startup": True,
        "is_manufacturer": True,
        "is_trader": False,
        "is_service_provider": True,
        "annual_turnover_lakh": 500.0,
        "years_in_business": 10,
        "has_oem_authorization": True,
        "products": ["cables"],
    }
    status, reasons, flags = evaluate_eligibility(tender_mse_reserved, full_profile)
    assert status == "no_blockers_found"

    # Turnover test: required 10L, profile has 5L
    tender_to = {"min_turnover_lakh": 10.0, "mse_experience_turnover_relaxation": False}
    status, reasons, flags = evaluate_eligibility(tender_to, {**full_profile, "annual_turnover_lakh": 5.0})
    assert status == "likely_not_eligible"

    # IOCL copper cable bid and 24,800 MT bitumen bid test:
    # Must give "unknown" with flags while my_profile.json is empty
    empty_profile = {}
    bitumen_tender = {
        "title": "Supply of 24800 MT Bitumen",
        "bis_required": True,
        "mii_class_restriction": "Class 1 and 2 local suppliers only",
        "reverse_auction": True,
    }
    b_status, b_reasons, b_flags = evaluate_eligibility(bitumen_tender, empty_profile)
    assert b_status == "unknown"
    assert "BIS required" in b_flags
    assert "Class 1/2 local suppliers only" in b_flags
    assert "reverse auction" in b_flags

    copper_cable_tender = {
        "title": "Supply of Copper Cable for IOCL",
        "oem_authorization_required": True,
        "short_duration_bid": True,
    }
    c_status, c_reasons, c_flags = evaluate_eligibility(copper_cable_tender, empty_profile)
    assert c_status == "unknown"
    assert "OEM authorization needed" in c_flags
    assert "short duration" in c_flags


def test_turnover_units_crore_and_lakh():
    """Confirm Lakh and Crore are both handled, with test that 2.47 Crore(s) gives 247.0 lakh."""
    from tender_mcp.gem_pdf import parse_currency_to_lakh
    assert parse_currency_to_lakh("2.47", "Crore(s)") == 247.0
    assert parse_currency_to_lakh("3.51", "Lakh (s)") == 3.51
    assert parse_currency_to_lakh("57", "Lakh (s)") == 57.0


def test_extension_tracking_computation():
    """Test original_end_utc, auto_extension_limit_utc, is_extended, extension_days_observed, date_check."""
    from tender_mcp.gem_pdf import compute_extension_fields

    # GEM/2026/B/8069375: PDF says 13-10-2026 15:00:00 IST, closing_date is 19-10-2026 15:00:00 IST
    # auto_extend_days=7, auto_extend_max_count=2 -> auto limit is 27-10-2026
    res = compute_extension_fields(
        bid_end_datetime="13-10-2026 15:00:00",
        closing_date_iso="2026-10-19T09:30:00Z",
        auto_extend_days=7,
        auto_extend_max_count=2,
    )
    assert res["original_end_utc"] == "2026-10-13T09:30:00Z"
    assert res["auto_extension_limit_utc"] == "2026-10-27T09:30:00Z"
    assert res["possible_latest_end_utc"] == "2026-10-27T09:30:00Z"
    assert res["is_extended"] == 1
    assert res["extension_days_observed"] == 6.0
    assert res["date_check"] == "extended_auto"

    # Unchanged (within 1 min)
    res_unchanged = compute_extension_fields(
        bid_end_datetime="13-10-2026 15:00:00",
        closing_date_iso="2026-10-13T09:30:30Z",
        auto_extend_days=7,
        auto_extend_max_count=2,
    )
    assert res_unchanged["is_extended"] == 0
    assert res_unchanged["date_check"] == "unchanged"

    # Later than auto_extension_limit_utc with NO corrigendum -> check_needed
    res_check = compute_extension_fields(
        bid_end_datetime="13-10-2026 15:00:00",
        closing_date_iso="2026-11-01T09:30:00Z",
        auto_extend_days=7,
        auto_extend_max_count=2,
        corrigendum_date=None,
    )
    assert res_check["is_extended"] == 1
    assert res_check["date_check"] == "check_needed"

    # Later than auto_extension_limit_utc WITH corrigendum -> extended_by_corrigendum
    res_corrigendum = compute_extension_fields(
        bid_end_datetime="13-10-2026 15:00:00",
        closing_date_iso="2026-11-01T09:30:00Z",
        auto_extend_days=7,
        auto_extend_max_count=2,
        corrigendum_date="2026-10-25 10:00:00",
    )
    assert res_corrigendum["is_extended"] == 1
    assert res_corrigendum["date_check"] == "extended_by_corrigendum"

    # Earlier than original_end_utc -> check_needed
    res_early = compute_extension_fields(
        bid_end_datetime="13-10-2026 15:00:00",
        closing_date_iso="2026-10-10T09:30:00Z",
        auto_extend_days=7,
        auto_extend_max_count=2,
    )
    assert res_early["is_extended"] == 0
    assert res_early["date_check"] == "check_needed"


def test_sample_9986574_floor_prices_and_buyer_override():
    """Verify sample 9986574 (GEM/2026/B/8118096): floor prices 10336231.18 & 6278350.41,
    PBG months per lot (LOT 1: 37, LOT 2: 20), and buyer_overrides_gem_fields = True."""
    p = FIXTURES_DIR / "9986574.pdf"
    assert p.exists()

    res = parse_gem_pdf(p, expected_bid_number="GEM/2026/B/8118096")
    assert res["pdf_parse_status"] == "ok"
    assert res["bid_number"] == "GEM/2026/B/8118096"
    assert res["buyer_overrides_gem_fields"] is True
    assert res["floor_price_inr_min"] == 6278350.41
    assert res["floor_price_inr_max"] == 10336231.18
    assert res["floor_price_schedules"] == [
        {"schedule": "Schedule 1", "amount": 10336231.18},
        {"schedule": "Schedule 2", "amount": 6278350.41},
    ]
    assert res["pbg_months_per_lot"] == [
        {"lot": "LOT 1", "months": 37},
        {"lot": "LOT 2", "months": 20},
    ]

    # Test eligibility evaluation includes override warning flag
    status, reasons, flags = evaluate_eligibility(res, {})
    assert "EMD / turnover set in buyer documents, check them before relying on these numbers" in flags

    # Test DB roundtrip with isolated temp DB
    from tender_mcp.db import upsert_tender, get_tender_by_id, get_conn
    upsert_tender({
        "id": "gem:GEM_2026_B_8118096",
        "source": "gem",
        "bid_number": "GEM/2026/B/8118096",
        "title": "Hiring of 02 Nos OFC Operations Maintenance Teams",
        "organization": "GAIL",
        "closing_date": "2026-10-23T15:00:00Z",
        "tier": "A",
        "link": "https://bidplus.gem.gov.in/showbidDocument/9986574",
    })
    save_gem_pdf_enrichment("gem:GEM_2026_B_8118096", res)

    row = get_tender_by_id("gem:GEM_2026_B_8118096")
    assert row["buyer_overrides_gem_fields"] == 1
    assert row["floor_price_inr_min"] == 6278350.41
    assert row["floor_price_inr_max"] == 10336231.18
    assert json.loads(row["floor_price_schedules"]) == [
        {"schedule": "Schedule 1", "amount": 10336231.18},
        {"schedule": "Schedule 2", "amount": 6278350.41},
    ]
    assert json.loads(row["pbg_months_per_lot"]) == [
        {"lot": "LOT 1", "months": 37},
        {"lot": "LOT 2", "months": 20},
    ]
    assert row["similar_category"] == "DGR Empanelled ESM Security Services; Security Manpower Service (Version 2.0)"
    assert row["category_noise_reason"] == "Buyer category: Security Manpower Service in similar_category"
    assert row["relevance_score"] <= 30
    assert row["tier"] == "B"


def test_reciprocating_plunger_pump_score_and_tier():
    """'Procurement of Reciprocating Plunger Pump (Crude Oil Despatch Application)' must remain above 60 and tier A."""
    from tender_mcp.matcher import score_tender, compute_tier
    t = {
        "title": "Procurement of Reciprocating Plunger Pump (Crude Oil Despatch Application)",
        "organisation": "Oil and Natural Gas Corporation Ltd",
    }
    scoring = score_tender(t)
    assert scoring["score"] > 60
    assert scoring["category_noise_reason"] is None
    tier = compute_tier(scoring["score"], t["organisation"])
    assert tier == "A"


def test_refresh_does_not_clear_similar_category():
    """A refresh does not clear similar_category on a bid already enriched."""
    from tender_mcp.db import upsert_tender, save_gem_pdf_enrichment, get_tender_by_id
    tid = "gem:GEM_2026_B_8118096"
    p = FIXTURES_DIR / "9986574.pdf"
    assert p.exists()

    # Ingest initial tender
    upsert_tender({
        "id": tid,
        "source": "gem",
        "bid_number": "GEM/2026/B/8118096",
        "title": "Custom Bid for Services - Pipeline Monitoring, ROU surveillence for Mumbai Manmad Bijwasan Pipeline",
        "organisation": "Bharat Petroleum Corporation Ltd",
        "closing_date": "2026-10-26T12:30:00Z",
        "tier": "A",
        "link": "https://bidplus.gem.gov.in/showbidDocument/9986574",
    })

    # Enrich from PDF
    res = parse_gem_pdf(p, expected_bid_number="GEM/2026/B/8118096")
    save_gem_pdf_enrichment(tid, res)

    row1 = get_tender_by_id(tid)
    assert row1["similar_category"] == "DGR Empanelled ESM Security Services; Security Manpower Service (Version 2.0)"
    assert row1["category_noise_reason"] == "Buyer category: Security Manpower Service in similar_category"
    assert row1["relevance_score"] <= 30
    assert row1["tier"] == "B"

    # Simulate scraper refresh with raw listing data (no similar_category in payload)
    upsert_tender({
        "id": tid,
        "source": "gem",
        "title": "Custom Bid for Services - Pipeline Monitoring, ROU surveillence for Mumbai Manmad Bijwasan Pipeline",
        "organisation": "Bharat Petroleum Corporation Ltd",
        "closing_date": "2026-10-26T12:30:00Z",
        "link": "https://bidplus.gem.gov.in/showbidDocument/9986574",
    })

    row2 = get_tender_by_id(tid)
    # similar_category must NOT be cleared
    assert row2["similar_category"] == "DGR Empanelled ESM Security Services; Security Manpower Service (Version 2.0)"
    # category_noise_reason must still be preserved
    assert row2["category_noise_reason"] == "Buyer category: Security Manpower Service in similar_category"
    # relevance_score must remain capped at 30 and tier B
    assert row2["relevance_score"] <= 30
    assert row2["tier"] == "B"


