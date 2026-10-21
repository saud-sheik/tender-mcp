"""
tests/test_matcher.py  –  Unit tests for keyword matching, scoring, and ingest validation
========================================================================================
Tests the requirements:
  - "gasket supply" must not match "gas" (score low / 0)
  - "Natural gas pipeline EPC" matches strong & weak keywords with high score
  - "Office furniture for refinery guest house" matches exclude, capping score low
  - Ingest validation skips invalid or outdated tenders
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tender_mcp.matcher import score_text, score_tender, get_min_score, load_keywords
from tender_mcp.refresh import validate_tender


@pytest.fixture(scope="module")
def keywords_cfg():
    return load_keywords()


# ---------------------------------------------------------------------------
# Tricky keyword cases specified in requirements
# ---------------------------------------------------------------------------

def test_gasket_supply_does_not_match_gas(keywords_cfg):
    """
    Tricky case 1: 'gasket supply'
    'gas' should use word boundary so it does NOT match 'gasket'.
    Score must be 0 and below min_score.
    """
    result = score_text("Supply and installation of gasket supply for boiler unit", keywords_cfg)
    assert "gas" not in result["matched_terms"], "'gas' should not match inside 'gasket'"
    assert "gasket" not in result["matched_terms"]
    assert result["score"] < result["min_score"], f"Score {result['score']} should be below min_score"
    assert result["is_relevant"] is False


def test_natural_gas_pipeline_epc_high_score(keywords_cfg):
    """
    Tricky case 2: 'Natural gas pipeline EPC'
    Matches 'pipeline' (strong), 'gas' (weak), and 'EPC' (weak).
    Must achieve a high score (>= 70) and is_relevant True.
    """
    result = score_text("Natural gas pipeline EPC tender for cross-country line", keywords_cfg)
    assert "pipeline" in result["matched_strong"], "'pipeline' must match strong group"
    assert "gas" in result["matched_weak"], "'gas' must match weak group"
    assert "epc" in result["matched_weak"], "'epc' must match weak group"
    assert result["matched_exclude"] == [], "No exclude keywords should match"
    assert result["score"] >= 70, f"Score {result['score']} should be high (>= 70)"
    assert result["is_relevant"] is True


def test_office_furniture_for_refinery_guest_house_capped_low(keywords_cfg):
    """
    Tricky case 3: 'Office furniture for refinery guest house'
    Matches 'refinery' (strong), but matches 'office furniture' (exclude) and 'guest house' (exclude).
    The score must be capped low (<= 15) and below min_score.
    """
    result = score_text(
        "Procurement of Office furniture for refinery guest house at Bathinda",
        keywords_cfg,
    )
    assert "refinery" in result["matched_strong"], "'refinery' was present"
    assert "office furniture" in result["matched_exclude"], "'office furniture' must be in exclude"
    assert result["score"] <= 15, f"Score {result['score']} should be capped low (<= 15)"
    assert result["score"] < result["min_score"]
    assert result["is_relevant"] is False
    assert "EXCLUDED" in result["assessment"]


def test_vegas_and_biogas_word_boundaries(keywords_cfg):
    """Ensure substrings like Vegas or Biogas do not match 'gas' without word boundary."""
    res1 = score_text("Conference attendance in Las Vegas hotels", keywords_cfg)
    assert "gas" not in res1["matched_terms"]
    assert res1["score"] == 0

    res2 = score_text("Biogas digester cleaning", keywords_cfg)
    assert "gas" not in res2["matched_terms"]


def test_case_insensitivity(keywords_cfg):
    """Keywords must match case-insensitively."""
    upper = score_text("CRUDE OIL PIPELINE VALVES", keywords_cfg)
    lower = score_text("crude oil pipeline valves", keywords_cfg)
    assert upper["score"] == lower["score"]
    assert upper["matched_strong"] == lower["matched_strong"]


def test_weak_alone_gets_low_score(keywords_cfg):
    """A weak keyword alone (e.g. drilling or valve) without core petroleum terms scores low."""
    res = score_text("Supply of gate valve for municipal water plant", keywords_cfg)
    assert "valve" in res["matched_weak"]
    assert len(res["matched_strong"]) == 0
    assert res["score"] <= 30
    assert res["score"] <= res["min_score"]


# ---------------------------------------------------------------------------
# Ingest validation tests
# ---------------------------------------------------------------------------

def test_validate_tender_missing_id():
    tender = {"id": "", "title": "Supply of crude oil", "closing_date": "2026-11-01"}
    is_valid, reason = validate_tender(tender)
    assert is_valid is False
    assert "missing tender id" in reason


def test_validate_tender_missing_title():
    tender = {"id": "cppp:12345", "title": "   ", "closing_date": "2026-11-01"}
    is_valid, reason = validate_tender(tender)
    assert is_valid is False
    assert "missing tender title" in reason


def test_validate_tender_past_closing_date():
    # 5 days in the past
    past_date = (datetime.now() - timedelta(days=5)).strftime("%d-%b-%Y %I:%M %p")
    tender = {"id": "cppp:12345", "title": "Supply of crude oil", "closing_date": past_date}
    is_valid, reason = validate_tender(tender)
    assert is_valid is False
    assert "more than 1 day in the past" in reason


def test_validate_tender_future_closing_date():
    # 10 days in the future
    future_date = (datetime.now() + timedelta(days=10)).strftime("%d-%b-%Y %I:%M %p")
    tender = {"id": "cppp:12345", "title": "Supply of crude oil", "closing_date": future_date}
    is_valid, reason = validate_tender(tender)
    assert is_valid is True
    assert reason is None


def test_validate_tender_yesterday_within_1_day():
    # 12 hours ago (less than 1 day past)
    recent_date = (datetime.now() - timedelta(hours=12)).strftime("%d-%b-%Y %I:%M %p")
    tender = {"id": "cppp:12345", "title": "Supply of crude oil", "closing_date": recent_date}
    is_valid, reason = validate_tender(tender)
    assert is_valid is True


# ---------------------------------------------------------------------------
# Task 1: Specific scoring bug tests
# ---------------------------------------------------------------------------

def test_non_petroleum_titles_score_below_30():
    """
    The five titles identified in live data must all score below 30,
    even if raw_json contains SearchBid=petroleum or organisation is IOCL.
    """
    five_titles = [
        "Refined Sunflower Oil (V4)",
        "Wooden block",
        "Surveillance Drone",
        "Acyclovir eye drops",
        "Title1,Title2,Title3",
    ]
    for title in five_titles:
        tender = {
            "title": title,
            "organisation": "Ministry of Petroleum and Natural Gas INDIAN OIL CORPORATION LIMITED",
            "category": "General Goods",
            "raw_json": '{"bid_no": "GEM/2026/B/8065741", "source_url": "https://bidplus.gem.gov.in/all-bids?SearchBid=petroleum&page_no=1"}',
            "raw": {"source_url": "https://bidplus.gem.gov.in/all-bids?SearchBid=petroleum&page_no=1"},
        }
        res = score_tender(tender)
        assert res["score"] < 30, (
            f"Title '{title}' scored {res['score']}, expected < 30. Assessment: {res['assessment']}"
        )


def test_strong_petroleum_tenders_score_above_60():
    """
    'Supply of LPG cylinders to refinery' and 'Natural gas pipeline EPC'
    must score above 60.
    """
    t1 = {"title": "Supply of LPG cylinders to refinery"}
    res1 = score_tender(t1)
    assert res1["score"] > 60, f"Expected score > 60 for t1, got {res1['score']}"

    t2 = {"title": "Natural gas pipeline EPC"}
    res2 = score_tender(t2)
    assert res2["score"] > 60, f"Expected score > 60 for t2, got {res2['score']}"


def test_weak_words_alone_do_not_exceed_30():
    """
    Weak words alone (valve, gas, pump) must not exceed 30,
    even with organization bonus.
    """
    for weak_kw in ["valve", "gas", "pump"]:
        tender = {
            "title": f"Supply of {weak_kw} units",
            "organisation": "Indian Oil Corporation Limited",  # Matches petroleum org
        }
        res = score_tender(tender)
        assert res["score"] <= 30, (
            f"Weak word '{weak_kw}' scored {res['score']}, expected <= 30"
        )


def test_scorer_never_reads_source_url_or_raw_json():
    """
    Scorer must NEVER read raw_json, source_url, or search keyword.
    """
    tender = {
        "title": "Ordinary Office Tables and Chairs",
        "category": "Furniture",
        "raw_json": '{"bid_no": "GEM/123", "source_url": "https://bidplus.gem.gov.in/all-bids?SearchBid=petroleum&page_no=1"}',
        "raw": {"search_keyword": "petroleum", "source_url": "https://bidplus.gem.gov.in/all-bids?SearchBid=petroleum"},
    }
    res = score_tender(tender)
    assert res["score"] < 30
    assert "petroleum" not in res["matched_strong"]


def test_service_noise_capped_at_30():
    """
    Service tenders at petroleum sites (vehicle/cab hiring, manpower outsourcing,
    uniforms, UPS maintenance, catering, housekeeping) must not score above 30
    just because refinery or LPG plant is mentioned.
    """
    noise_examples = [
        "Monthly Basis Cab & Taxi Hiring Services for Mathura Refinery",
        "Providing Manpower Services for Equipment Maintenance at Nasik LPG Plant",
        "Providing Winter uniform and suiting for Panipat Refinery",
        "Annual Maintenance Contract (AMC) of Online UPS Systems for LPG Bottling Plant",
        "Catering and Housekeeping Services for Bathinda Refinery Guest House",
        "Vehicle Hiring Service for Pipeline Division",
    ]
    for title in noise_examples:
        tender = {
            "title": title,
            "organisation": "Ministry of Petroleum and Natural Gas INDIAN OIL CORPORATION LIMITED",
        }
        res = score_tender(tender)
        assert res["score"] <= 30, (
            f"Service tender '{title}' scored {res['score']}, expected <= 30"
        )
        assert len(res["matched_service_noise"]) > 0


def test_itemless_tenders_score_at_most_20():
    """
    A bid with no item text must score at most 20.
    """
    itemless_examples = [
        {"title": "[Items not listed] GEM/2026/B/8036278", "organisation": "Ministry of Petroleum and Natural Gas"},
        {"title": "GEM/2026/B/7832546", "organisation": "Indian Oil Corporation"},
        {"title": "", "organisation": "Bharat Petroleum Corporation Ltd"},
    ]
    for tender in itemless_examples:
        res = score_tender(tender)
        assert res["score"] <= 20, (
            f"Itemless tender '{tender.get('title')}' scored {res['score']}, expected <= 20"
        )


def test_scorer_ignores_department_names():
    """
    None of the scoring may rely on department names.
    """
    tender = {
        "title": "Supply of standard laboratory glassware",
        "organisation": "Ministry of Petroleum and Natural Gas - Indian Oil Corporation",
        "category": "Laboratory Supplies",
    }
    res = score_tender(tender)
    assert res["score"] == 0
    assert res["is_relevant"] is False


def test_negative_phrases_do_not_count():
    """
    Words in the wrong sense (e.g. 'gas metal arc', 'gas welding', 'gas cutting',
    'welding gas', 'gas stove', 'oxy gasoline') must not count towards petroleum keywords.
    """
    examples = [
        "plasma air cutting machine, Gas Metal Arc Welding (GMAW) Machine (V2), Oxy Gasoline Metal Cutting Tor",
        "Supply of gas welding equipment and accessories",
        "Gas cutting set with nozzles and hose",
        "Industrial welding gas regulator and cylinder",
        "Four burner commercial gas stove for mess",
        "Domestic gas geyser installation",
    ]
    for text in examples:
        res = score_text(text)
        assert res["score"] == 0, f"Expected 0 for '{text}', got {res['score']}"
        assert res["is_relevant"] is False


def test_itemless_petroleum_org_flagged_details_needed():
    """
    Itemless bids from Petroleum-ministry organisations must be flagged 'details needed'
    and score 20 instead of scoring 0.
    """
    petro_tender = {
        "title": "[Items not listed] GEM/2026/B/8141303",
        "category": "1200000960,1200000958",
        "organisation": "Ministry of Petroleum and Natural Gas Materials Department",
    }
    res = score_tender(petro_tender)
    assert res["score"] == 20
    assert res["is_details_needed"] is True
    assert res["suggested_status"] == "details needed"
    assert "DETAILS_NEEDED" in res["assessment"]


def test_itemless_non_petroleum_org_scores_zero():
    """
    Itemless bids from non-petroleum organisations score 0 and are not flagged details needed.
    """
    non_petro_tender = {
        "title": "[Items not listed] GEM/2026/B/12345",
        "category": "",
        "organisation": "Ministry of Power Contracts",
    }
    res = score_tender(non_petro_tender)
    assert res["score"] == 0
    assert res["is_details_needed"] is False
    assert res["suggested_status"] == "active"



# ---------------------------------------------------------------------------
# Item 4: context_exclude tests (real titles from live DB)
# ---------------------------------------------------------------------------

def test_context_exclude_lpg_steam_generator_below_30():
    """LPG STEAM GENERATOR with STEAM BOILER must be capped at 20 (context_exclude)."""
    title = "LPG STEAM GENERATOR, STEAM PIPELINE WITH INSULATION, STEAM BOILER 80 LTR CAPACITY, STEAM BOILER 50 LTR"
    res = score_tender({"title": title})
    assert res["score"] < 30, f"Expected < 30, got {res['score']}; assessment: {res['assessment']}"
    assert res["is_relevant"] is False


def test_context_exclude_co2_vacuum_gas_pipeline_below_30():
    """'SITC of CO2 & Vacuum Gas Pipeline' must be capped at 20 (context_exclude)."""
    title = "SITC of CO2 & Vacuum Gas Pipeline"
    res = score_tender({"title": title})
    assert res["score"] < 30, f"Expected < 30, got {res['score']}; assessment: {res['assessment']}"
    assert res["is_relevant"] is False


def test_context_exclude_pinion_brake_pneumatic_below_30():
    """'bush replace for pinion, valve brake pneumatic, ... lubricant' must be < 30."""
    title = "bush replace for pinion, valve brake pneumatic, sa of pipe oil for compressor lubricant, cylinder head"
    res = score_tender({"title": title})
    assert res["score"] < 30, f"Expected < 30, got {res['score']}; assessment: {res['assessment']}"
    assert res["is_relevant"] is False


def test_context_exclude_ajmer_lpg_compressor_above_60():
    """'SITC OF AIR COMPRESSOR AT AJMER LPG PLANT' must stay above 60 (no context_exclude match)."""
    title = "SITC OF AIR COMPRESSOR AT AJMER LPG PLANT"
    res = score_tender({"title": title})
    assert res["score"] > 60, f"Expected > 60, got {res['score']}; assessment: {res['assessment']}"
    assert res["is_relevant"] is True


def test_context_exclude_trivandrum_lpg_valves_above_60():
    """'Supply of Stem Raising Gate Valves for Fire water line at Trivandrum LPG' must stay above 60."""
    title = "Supply of Stem Raising Gate Valves for Fire water line at Trivandrum LPG"
    res = score_tender({"title": title})
    assert res["score"] > 60, f"Expected > 60, got {res['score']}; assessment: {res['assessment']}"
    assert res["is_relevant"] is True


# ---------------------------------------------------------------------------
# Item 2: buyer tier tests
# ---------------------------------------------------------------------------

def test_tier_A_oil_gas_buyer():
    """Score>=30 with an oil/gas org → tier A."""
    from tender_mcp.matcher import compute_tier
    assert compute_tier(75, "Ministry of Petroleum and Natural Gas INDIAN OIL CORPORATION LIMITED") == "A"
    assert compute_tier(30, "BPCL Bharat Petroleum Corporation Ltd") == "A"


def test_tier_B_non_oil_gas_buyer():
    """Score>=30 with a non-oil/gas org → tier B."""
    from tender_mcp.matcher import compute_tier
    assert compute_tier(75, "Ministry of Defence") == "B"
    assert compute_tier(30, "Ministry of Agriculture") == "B"


def test_tier_C_below_30():
    """Score < 30 → tier C regardless of org."""
    from tender_mcp.matcher import compute_tier
    assert compute_tier(0, "Ministry of Petroleum and Natural Gas") == "C"
    assert compute_tier(29, "IOCL") == "C"


def test_buyer_is_oil_gas():
    """is_oil_gas_buyer returns True for known buyers, False for others."""
    from tender_mcp.matcher import is_oil_gas_buyer
    assert is_oil_gas_buyer("Ministry of Petroleum and Natural Gas INDIAN OIL CORPORATION LIMITED") is True
    assert is_oil_gas_buyer("BPCL") is True
    assert is_oil_gas_buyer("HPCL Hindustan Petroleum Corporation Limited") is True
    assert is_oil_gas_buyer("Ministry of Defence") is False
    assert is_oil_gas_buyer("") is False

# ---------------------------------------------------------------------------
# Item 1: tender_kind classification tests
# ---------------------------------------------------------------------------

def test_classify_tender_kind_product():
    from tender_mcp.matcher import classify_tender_kind
    assert classify_tender_kind("Procurement of Reciprocating Plunger Pump") == "product"
    assert classify_tender_kind("Supply of Stem Raising Gate Valves for LPG") == "product"
    assert classify_tender_kind("Tamper Evident Seals for LPG cylinders") == "product"
    assert classify_tender_kind("Purchase of crude oil storage tank") == "product"


def test_classify_tender_kind_service():
    from tender_mcp.matcher import classify_tender_kind
    assert classify_tender_kind("Annual Maintenance Contract of Online UPS Systems for LPG Plant") == "service"
    assert classify_tender_kind("Providing courier services to deliver samples from Loni LPG plant") == "service"
    assert classify_tender_kind("Goods Transport Service - Packed Bitumen - Zone A") == "service"
    assert classify_tender_kind("SITC OF AIR COMPRESSOR AT AJMER LPG PLANT") == "service"


def test_classify_tender_kind_unknown():
    from tender_mcp.matcher import classify_tender_kind
    assert classify_tender_kind("Natural Gas Calibration Mixture") == "unknown"
    assert classify_tender_kind("") == "unknown"


def test_courier_services_below_30():
    """'Providing courier services to deliver samples from Loni LPG plant' must be below 30 (service_noise)."""
    res = score_tender({"title": "Providing Courier Services to Deliver Samples from Loni LPG Plant to LERC"})
    assert res["score"] < 30, f"Expected < 30, got {res['score']}; assessment: {res['assessment']}"


def test_goods_transport_bitumen_below_30():
    """'Goods Transport Service - Packed Bitumen' must be below 30 (service_noise)."""
    res = score_tender({"title": "Goods Transport Service Per MT per KM Based Service PACKED BITUMEN ZONE A Up to 700 Kms"})
    assert res["score"] < 30, f"Expected < 30, got {res['score']}; assessment: {res['assessment']}"


def test_tamper_evident_seals_product_visible():
    """'Tamper Evident Seals for LPG cylinders' must score >= 30, kind=product."""
    from tender_mcp.matcher import classify_tender_kind
    res = score_tender({"title": "Tamper Evident Seals for LPG cylinders"})
    assert res["score"] >= 30, f"Expected >= 30, got {res['score']}"
    assert res["is_relevant"] is True
    kind = classify_tender_kind("Tamper Evident Seals for LPG cylinders")
    assert kind == "product"


def test_ball_pigging_system_product():
    """'Procurement of ball pigging system for BPCL Mumbai Refinery' kind=product, matches item term 'system'."""
    from tender_mcp.matcher import classify_tender_kind
    res = score_tender({"title": "Procurement of ball pigging system for BPCL Mumbai Refinery"})
    assert res["score"] == 70
    assert "system" in res["matched_items"]
    kind = classify_tender_kind("Procurement of ball pigging system for BPCL Mumbai Refinery")
    assert kind == "product"


def test_location_vs_item_scoring():
    """Verify before and after scores for the 5 real titles specified by user."""
    # 1. Cooking oil for Visakh Refinery -> 40
    res1 = score_tender({"title": "RATE CONTRACT FOR SUPPLY OF PRE-TREATED USED COOKING OIL FOR VISAKH REFINERY"})
    assert res1["score"] == 40
    assert "LOCATION_ONLY" in res1["assessment"]

    # 2. Industrial DAP at IOCL Barauni Refinery -> 40
    res2 = score_tender({"title": "Procurement of Industrial DAP at IOCL Barauni Refinery"})
    assert res2["score"] == 40
    assert "LOCATION_ONLY" in res2["assessment"]

    # 3. VHF SET AT PANAGARH LPG Plant -> 70 (item 'set' / 'vhf' matched)
    res3 = score_tender({"title": "SUPPLY AND WPC LIASONING OF VHF SET AT PANAGARH LPG Plant"})
    assert res3["score"] == 70
    assert "set" in res3["matched_items"] or "vhf" in res3["matched_items"]

    # 4. Non-Sparking Tools for Bhitoni LPG Plant -> 70 (item 'tools' matched)
    res4 = score_tender({"title": "Supply of Non-Sparking Tools for Bhitoni LPG Plant"})
    assert res4["score"] == 70
    assert "tools" in res4["matched_items"]

    # 5. Plunger Pump (Crude Oil Despatch Application) -> 85 (must stay 85)
    res5 = score_tender({"title": "Procurement of Reciprocating Plunger Pump (Crude Oil Despatch Application)"})
    assert res5["score"] == 85
    assert "pump" in res5["matched_items"]


def test_bid_window_urgent():
    from tender_mcp.matcher import compute_bid_window
    assert compute_bid_window(0) == "urgent"
    assert compute_bid_window(24) == "urgent"
    assert compute_bid_window(47.9) == "urgent"


def test_bid_window_soon():
    from tender_mcp.matcher import compute_bid_window
    assert compute_bid_window(48) == "soon"
    assert compute_bid_window(96) == "soon"
    assert compute_bid_window(168) == "soon"


def test_bid_window_normal():
    from tender_mcp.matcher import compute_bid_window
    assert compute_bid_window(169) == "normal"
    assert compute_bid_window(500) == "normal"


def test_bid_window_unknown():
    from tender_mcp.matcher import compute_bid_window
    assert compute_bid_window(None) == "unknown"


def test_kind_precedence_six_real_titles():
    """Verify kind rule precedence on the 6 real titles specified by user."""
    from tender_mcp.matcher import classify_tender_kind
    # 1. Custom Bid for Services -> service
    assert classify_tender_kind(
        "Custom Bid for Services - Annual Rate Contract for Operation of Emergency Response Vehicle"
    ) == "service"

    # 2. Provision of drinking water in shed -> service (provision of is service word)
    assert classify_tender_kind(
        "Provision of drinking water in shed"
    ) == "service"

    # 3. Equipments for statutory testing and painting of LPG Cylinders -> mixed
    assert classify_tender_kind(
        "Equipments for statutory testing and painting of LPG Cylinders"
    ) == "mixed"

    # 4. Procurement of ball pigging system for BPCL Mumbai Refinery -> product
    assert classify_tender_kind(
        "Procurement of ball pigging system for BPCL Mumbai Refinery"
    ) == "product"

    # 5. SUPPLY & INSTALLATION OF NiCD BATTERY ... FOR GAS PIPELINE -> product
    assert classify_tender_kind(
        "SUPPLY & INSTALLATION OF NiCD BATTERY ALONG WITH BUYBACK OF OLD BATTERIES FOR GAS PIPELINE"
    ) == "product"

    # 6. Tamper Evident Seals for LPG cylinders -> product
    assert classify_tender_kind(
        "Tamper Evident Seals for LPG cylinders"
    ) == "product"


def test_is_consumable():
    from tender_mcp.matcher import is_consumable, score_tender
    assert is_consumable("Supply of Tamper Evident Seals") is True
    assert is_consumable("Supply of Spiral Wound Gaskets") is True
    assert is_consumable("Nitrile O-Rings for valves") is True
    assert is_consumable("Safety Hand Gloves") is True
    assert is_consumable("Teflon Gland Packing") is True
    assert is_consumable("Aluminium Inspection Tags") is True
    assert is_consumable("Centrifugal Pump Spares") is False

    # Consumables do not change score
    res_with_consumable = score_tender({"title": "Supply of Tamper Evident Seals for LPG cylinders"})
    assert res_with_consumable["is_consumable"] is True
    assert res_with_consumable["score"] >= 30


def test_staffing_and_logistics_noise_separate_caps():
    from tender_mcp.matcher import score_tender, load_keywords
    cfg = dict(load_keywords())
    cfg["staffing_noise_cap"] = 15
    cfg["logistics_noise_cap"] = 25

    # Staffing noise capped at 15
    res_staffing = score_tender(
        {"title": "Manpower outsourcing services for Mathura Refinery"},
        config=cfg,
    )
    assert res_staffing["score"] <= 15
    assert "manpower" in res_staffing["matched_staffing_noise"]

    # Logistics noise capped at 25
    res_logistics = score_tender(
        {"title": "Goods transport service for Panipat Refinery Packed Bitumen"},
        config=cfg,
    )
    assert res_logistics["score"] <= 25
    assert "goods transport" in res_logistics["matched_logistics_noise"]


def test_structural_steel_fabrication_erection_service():
    from tender_mcp.matcher import classify_tender_kind
    kind = classify_tender_kind(
        "Structural steel fabrication & erection of standard crossovers in shed at Raiganj LPG Plant"
    )
    assert kind in ("service", "mixed")


def test_item_terms_extension_and_plurals():
    """Verify new item terms and plural word-boundary matching."""
    from tender_mcp.matcher import score_tender

    # Plurals
    assert "pump" in score_tender({"title": "Supply of pumps for refinery"})["matched_items"]
    assert "set" in score_tender({"title": "Supply of sets for refinery"})["matched_items"]
    assert "fan" in score_tender({"title": "Supply of fans for refinery"})["matched_items"]
    assert "pipe" in score_tender({"title": "Supply of pipes for refinery"})["matched_items"]
    assert score_tender({"title": "Supply of pumps for refinery"})["score"] == 75

    # New item terms
    for term in [
        "equipment", "set", "system", "unit", "fan", "vhf", "uhf",
        "communication", "conveyor", "diverter", "purging", "tender",
        "analyzer", "detector", "hose", "tools", "spares", "instrument",
        "instrumentation", "transmitter", "sensor", "actuator", "gauge"
    ]:
        res = score_tender({"title": f"Procurement of {term} for site"})
        assert term in res["matched_items"] or term + "s" in res["matched_items"], f"Expected {term} to match item terms"


def test_seven_titles_score_and_match_basis():
    """Verify scores and match_basis for the 7 titles from user prompt."""
    from tender_mcp.matcher import score_tender

    # 1. VHF SET
    t1 = score_tender({"title": "SUPPLY AND WPC LIASONING OF VHF SET AT PANAGARH LPG Plant"})
    assert t1["score"] == 70
    assert t1["match_basis"] == "item"

    # 2. Flameproof Wall Mounted Fans
    t2 = score_tender({"title": "Supply of Flameproof (Ex-d) Wall Mounted Fans (PESO Approved) at Lalru LPG Plant"})
    assert t2["score"] == 75
    assert t2["match_basis"] == "item"

    # 3. Auto-diverter system for LPG Chain Conveyors
    t3 = score_tender({"title": "Supply & Installation of Auto-diverter system for LPG Chain Conveyors"})
    assert t3["score"] == 70
    assert t3["match_basis"] == "item"

    # 4. UHF Communication Equipment
    t4 = score_tender({"title": "Procurement of UHF Communication Equipment and Associated Services at Vadodara LPG Terminal"})
    assert t4["score"] == 70
    assert t4["match_basis"] == "item"

    # 5. PLC Based Purging Unit
    t5 = score_tender({"title": "Supply & Installation of PLC Based Purging Unit at Trivandrum LPG"})
    assert t5["score"] == 70
    assert t5["match_basis"] == "item"

    # 6. Cooking oil (stays capped at 40)
    t6 = score_tender({"title": "RATE CONTRACT FOR SUPPLY OF PRE-TREATED USED COOKING OIL FOR VISAKH REFINERY"})
    assert t6["score"] == 40
    assert t6["match_basis"] == "location_only"

    # 7. Industrial DAP (stays capped at 40)
    t7 = score_tender({"title": "Procurement of Industrial DAP at IOCL Barauni Refinery"})
    assert t7["score"] == 40
    assert t7["match_basis"] == "location_only"


def test_maintenance_jobs_facility_location_only():
    """MAINTENANCE JOBS IN LPG PH5 TDU PD FILLING FACILITIES AT BPCL MUMBAI REFINERY must score 40 as location_only."""
    res = score_tender({"title": "MAINTENANCE JOBS IN LPG PH5 TDU PD FILLING FACILITIES AT BPCL MUMBAI REFINERY"})
    assert res["score"] == 40
    assert res["match_basis"] == "location_only"
    assert res["matched_items"] == []


def test_cng_vehicle_hiring_noise_cap():
    """Hiring of CNG Driven Medium Goods Vehicle must be capped by logistics/staffing noise (score <= 20)."""
    res = score_tender({"title": "Hiring of CNG Driven Medium Goods Vehicle"})
    assert res["score"] <= 20
    assert "SERVICE_NOISE" in res["assessment"]
    assert len(res["matched_service_noise"]) > 0


def test_product_supply_protection_air_booster():
    """Air Booster bid with 'Supply of' keeps its normal score (70) and tier A."""
    from tender_mcp.matcher import score_tender
    title = (
        "Custom Bid for Services - Supply of Air Booster for pneumatic testing of LPG cylinder "
        "and Industrial Vacuum Cleaner for internal cleaning of LPG cylinders at BPCL Cherlapalli LPG Plant"
    )
    t = {
        "title": title,
        "organisation": "Bharat Petroleum Corporation Ltd",
        "category": title,
    }
    res = score_tender(t)
    assert res["score"] == 70
    assert res["tier"] == "A"
    assert res["category_noise_reason"] is None


def test_custom_bid_providing_manpower_still_capped():
    """'Custom Bid for Services - Providing Manpower Services at Nasik LPG Plant' is still capped at <= 20 and tier C."""
    from tender_mcp.matcher import score_tender
    title = "Custom Bid for Services - Providing Manpower Services at Nasik LPG Plant"
    t = {
        "title": title,
        "organisation": "Bharat Petroleum Corporation Ltd",
    }
    res = score_tender(t)
    assert res["score"] <= 20
    assert res["tier"] == "C"
    assert "manpower in title" in res["matched_terms_str"]


def test_industrial_vacuum_cleaner_not_capped():
    """'Industrial Vacuum Cleaner for internal cleaning of LPG cylinders' is not capped."""
    from tender_mcp.matcher import score_tender
    title = "Industrial Vacuum Cleaner for internal cleaning of LPG cylinders"
    t = {
        "title": title,
        "organisation": "Bharat Petroleum Corporation Ltd",
    }
    res = score_tender(t)
    assert res["score"] >= 70
    assert res["tier"] == "A"
    assert res["category_noise_reason"] is None


def test_housekeeping_services_at_lpg_plant_is_capped():
    """'Housekeeping services at LPG plant' is capped."""
    from tender_mcp.matcher import score_tender
    title = "Housekeeping services at LPG plant"
    t = {
        "title": title,
        "organisation": "Bharat Petroleum Corporation Ltd",
    }
    res = score_tender(t)
    assert res["score"] <= 20
    assert res["tier"] == "C"
    assert "housekeeping in title" in res["matched_terms_str"]


def test_category_noise_reason_field_attribution_and_tier_b():
    """Tenders with category noise must not be tier A; make them tier B with exact field attribution."""
    from tender_mcp.matcher import score_tender
    t = {
        "title": "Pipeline Monitoring for Gas Pipeline",
        "organisation": "Bharat Petroleum Corporation Ltd",
        "similar_category": "DGR Empanelled ESM Security Services; Security Manpower Service (Version 2.0)",
    }
    res = score_tender(t)
    assert res["score"] == 30
    assert res["tier"] == "B"
    assert res["category_noise_reason"] == "Buyer category: Security Manpower Service in similar_category"


def test_plural_matching_header_pipelines():
    """'Header Pipelines' matches strong term 'pipeline' and scores 60 or more."""
    from tender_mcp.matcher import score_tender
    res = score_tender({"title": "Header Pipelines"})
    assert res["score"] >= 60
    assert "pipeline (+70)" in res["matched_terms_detailed"]


def test_gas_does_not_match_gasket():
    """Keyword 'gas' must never match 'gasket' or 'gaskets'."""
    from tender_mcp.matcher import score_tender
    # Only gasket, no other keyword
    res = score_tender({"title": "Supply of Spiral Wound Gasket"})
    # 'gas' must NOT be in matched terms
    assert "gas" not in res["matched_strong"]
    assert "gas" not in res["matched_weak"]
    assert not any("gas" == t.split()[0] for t in res["matched_terms"])


def test_compound_terms_strong_and_weak():
    """Verify newly added compound terms match and score accurately."""
    from tender_mcp.matcher import score_tender
    # Strong compound terms (score >= 70)
    for term in ["LPG Bullets", "Mounded Bullets", "Tank Farms", "Floating Roofs", "Loading Arms"]:
        res = score_tender({"title": f"Supply of {term}"})
        assert res["score"] >= 70, f"Expected {term} to match strong keyword, got score {res['score']}"

    # Weak compound terms (score 20-30)
    for term in ["Tank Lorries", "Filling Gantries", "Dispensing Units", "Flow Meters", "Storage Tanks", "Fire Water System"]:
        res = score_tender({"title": f"Supply of {term}"})
        assert res["score"] >= 20, f"Expected {term} to match weak keyword, got score {res['score']}"


def test_buyer_match_lane():
    """Verify buyer-match lane rules: buyer_is_oil_gas, supply title prefix, no noise, score < 30."""
    from tender_mcp.matcher import score_tender

    # 1. Matches all 4 conditions -> tier B, buyer_match = 1, label present, score unchanged
    t1 = {
        "title": "Supply of Stainless Steel Bolts and Nuts",
        "organisation": "Indian Oil Corporation Limited",
    }
    r1 = score_tender(t1)
    assert r1["score"] == 0
    assert r1["buyer_match"] == 1
    assert r1["tier"] == "B"
    assert r1["buyer_match_label"] == "Oil/gas buyer, supply tender"

    # 2. Custom Bid for Services prefix stripped -> matches
    t2 = {
        "title": "Custom Bid for Services - Procurement of General Hardware Materials",
        "organisation": "Bharat Petroleum Corporation Limited",
    }
    r2 = score_tender(t2)
    assert r2["score"] == 0
    assert r2["buyer_match"] == 1
    assert r2["tier"] == "B"
    assert r2["buyer_match_label"] == "Oil/gas buyer, supply tender"

    # 3. Not an oil & gas buyer -> buyer_match = 0, tier C
    t3 = {
        "title": "Supply of Stainless Steel Bolts and Nuts",
        "organisation": "Ministry of Defence",
    }
    r3 = score_tender(t3)
    assert r3["buyer_match"] == 0
    assert r3["tier"] == "C"

    # 4. Noise phrase present -> buyer_match = 0
    t4 = {
        "title": "Supply of Manpower for Housekeeping at Refinery",
        "organisation": "Indian Oil Corporation Limited",
    }
    r4 = score_tender(t4)
    assert r4["buyer_match"] == 0

    # 5. Title does not start with Supply/Procurement/Purchase -> buyer_match = 0
    t5 = {
        "title": "Installation and Commissioning of Nitrogen Plant",
        "organisation": "Indian Oil Corporation Limited",
    }
    r5 = score_tender(t5)
    assert r5["buyer_match"] == 0

    # 6. Score >= 30 -> buyer_match = 0, normal tier A
    t6 = {
        "title": "Supply of LPG Cylinders",
        "organisation": "Indian Oil Corporation Limited",
    }
    r6 = score_tender(t6)
    assert r6["score"] >= 70
    assert r6["buyer_match"] == 0
    assert r6["tier"] == "A"



