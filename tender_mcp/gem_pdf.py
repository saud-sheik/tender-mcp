"""
tender_mcp/gem_pdf.py
Download, cache, and extract bid details and extension tracking from GeM tender PDFs.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pypdf

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "bids"
PROFILE_PATH = Path(__file__).resolve().parent.parent / "my_profile.json"
IST = ZoneInfo("Asia/Kolkata")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "(GeM Bid Enrichment; Swamina International; ops@swaminaintl.com)"
)


class GemBlockedError(Exception):
    """Raised when GeM returns a block page, captcha, or access denied."""
    pass


def load_profile(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else PROFILE_PATH
    if not p.exists():
        return {
            "is_msme": None,
            "is_startup": None,
            "is_manufacturer": None,
            "is_trader": None,
            "is_service_provider": None,
            "annual_turnover_lakh": None,
            "years_in_business": None,
            "has_oem_authorization": None,
            "products": [],
        }
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load profile from %s: %s", p, e)
        return {}


def download_gem_pdf(
    link: str,
    bid_doc_id: str,
    cache_dir: Path | None = None,
    delay: float = 2.0,
    timeout: float = 20.0,
    closing_date_iso: str | None = None,
) -> Path | None:
    """
    Download PDF from GeM showbidDocument link into cache_dir/<bid_doc_id>.pdf.
    Re-download if cached file is older than 24h and bid closes within 7 days.
    Never re-downloads otherwise.
    Raises GemBlockedError on block, captcha, or access-denied responses.
    """
    out_dir = cache_dir or CACHE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    target_path = out_dir / f"{bid_doc_id}.pdf"

    if target_path.exists() and target_path.stat().st_size > 0:
        file_age_sec = time.time() - target_path.stat().st_mtime
        should_refresh = False
        if file_age_sec > 86400 and closing_date_iso:
            try:
                dt_close = datetime.strptime(closing_date_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                hours_to_close = (dt_close - now_utc).total_seconds() / 3600.0
                if 0 <= hours_to_close <= 168.0:
                    should_refresh = True
            except Exception:
                pass
        if not should_refresh:
            return target_path

    if not link or not link.startswith("http"):
        return None

    # Enforce delay between requests
    if delay > 0:
        time.sleep(delay)

    headers = {"User-Agent": USER_AGENT}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(link, headers=headers)
    except Exception as e:
        logger.error("Network error downloading %s: %s", link, e)
        return None

    # Check for blocking or access denied
    if resp.status_code in (401, 403, 429):
        raise GemBlockedError(f"GeM returned HTTP {resp.status_code} for {link}")

    content = resp.content
    content_lower = content[:2000].lower()
    if (
        b"<html" in content_lower
        and any(
            b in content_lower
            for b in [b"captcha", b"access denied", b"blocked", b"cloudflare", b"forbidden"]
        )
    ):
        raise GemBlockedError(f"GeM returned captcha or access denied block page for {link}")

    if not content.startswith(b"%PDF"):
        logger.warning("Response from %s is not a valid PDF (status %d)", link, resp.status_code)
        return None

    target_path.write_bytes(content)
    logger.info("Saved PDF for %s (%d bytes)", bid_doc_id, len(content))
    return target_path


def parse_currency_to_lakh(text_val: str, unit: str) -> float | None:
    """Convert amount to Lakh. 1 Crore = 100 Lakh."""
    try:
        val = float(text_val.replace(",", "").strip())
    except ValueError:
        return None
    if "crore" in unit.lower():
        return round(val * 100.0, 4)
    return val


def parse_gem_pdf(
    pdf_path: Path | str,
    expected_bid_number: str | None = None,
) -> dict[str, Any]:
    """
    Extract structured fields from a GeM bid PDF.
    Sets pdf_parse_status='partial' if any of:
      item_category, total_quantity, consignee_city, or emd_amount_inr (when emd_required is true) is NULL.
    """
    p = Path(pdf_path)
    if not p.exists():
        return {"pdf_parse_status": "failed", "error": f"File {pdf_path} not found"}

    try:
        reader = pypdf.PdfReader(str(p))
        text = "\n".join([page.extract_text() or "" for page in reader.pages])
    except Exception as e:
        return {"pdf_parse_status": "failed", "error": f"PDF parse error: {e}"}

    if not text.strip():
        return {"pdf_parse_status": "failed", "error": "No text extracted from PDF"}

    # 1. Bid Number
    m_bid = re.search(r"Bid\s+Number\s*:\s*\n\s*(GEM/\d{4}/[A-Z]/\d+)", text, re.IGNORECASE)
    if not m_bid:
        m_bid = re.search(r"\b(GEM/\d{4}/[A-Z]/\d+)\b", text)
    bid_number = m_bid.group(1).strip() if m_bid else None

    # Integrity Check
    if expected_bid_number:
        norm_expected = expected_bid_number.replace("_", "/").strip().upper()
        if "GEM:" in norm_expected:
            norm_expected = norm_expected.split("GEM:")[-1]
        norm_parsed = (bid_number or "").replace("_", "/").strip().upper()

        if norm_parsed != norm_expected:
            logger.warning(
                "Integrity mismatch: parsed bid number '%s' != expected '%s'",
                norm_parsed,
                norm_expected,
            )
            return {
                "pdf_parse_status": "mismatch",
                "bid_number": bid_number,
                "expected_bid_number": expected_bid_number,
            }

    # 2. Dates
    m_end = re.search(r"Bid\s+End\s+Date/Time\s*\n\s*([0-9-]+\s+[0-9:]+)", text, re.IGNORECASE)
    bid_end_datetime = m_end.group(1).strip() if m_end else None

    m_open = re.search(r"Bid\s+Opening\s+Date/Time\s*\n\s*([0-9-]+\s+[0-9:]+)", text, re.IGNORECASE)
    bid_opening_datetime = m_open.group(1).strip() if m_open else None

    # 3. Validity
    m_val = re.search(r"Bid\s+Offer\s+Validity\s*\(From\s+End\s+Date\)\s*\n\s*(\d+)", text, re.IGNORECASE)
    bid_offer_validity_days = int(m_val.group(1)) if m_val else None

    # 4. Ministry, Department, Organisation, Office
    def get_header_field(lbl: str) -> str | None:
        m = re.search(rf"{lbl}\s*\n\s*([^\n/]+)", text, re.IGNORECASE)
        if not m:
            return None
        v = m.group(1).strip()
        return None if v.upper() in ("NA", "N/A", "") else v

    ministry = get_header_field("Ministry/State Name")
    department = get_header_field("Department Name")
    organisation = get_header_field("Organisation Name")
    office_name = get_header_field("Office Name")

    # 5. Item Category
    m_cat = re.search(
        r"/Item\s+Category\s*\n\s*([^\n]+(?:\n[^\n]+)*?)(?=\n\s*(?:[\u0900-\u097F\s]*/|GeMARPTS|MSE|Startup|Similar|Contract|\())",
        text,
        re.IGNORECASE,
    )
    if m_cat:
        raw_cat = " ".join(m_cat.group(1).split())
        item_category = re.split(r"[\u0900-\u097F]", raw_cat)[0].strip() or None
    else:
        item_category = None

    # 5b. Similar Category
    m_sim = re.search(
        r"(?:/|\b)Similar\s+Category\s*\n\s*([^\n]+(?:\n[^\n]+)*?)(?=\n\s*(?:[\u0900-\u097F\s]*/|/Contract|Contract|GeMARPTS|MSE|Startup|\())",
        text,
        re.IGNORECASE,
    )
    similar_category = None
    if m_sim:
        raw_sim = m_sim.group(1)
        sim_lines = raw_sim.splitlines()
        seen_sim = []
        for l in sim_lines:
            clean = l.strip()
            clean_eng = re.split(r"[\u0900-\u097F]", clean)[0].strip()
            if clean_eng and clean_eng not in seen_sim:
                seen_sim.append(clean_eng)
        if seen_sim:
            similar_category = "; ".join(seen_sim)

    # 6. Total Quantity
    m_qty = re.search(r"/Total\s+Quantity\s*\n\s*(\d+)", text, re.IGNORECASE)
    if m_qty:
        total_quantity = int(m_qty.group(1))
    elif re.search(r"Project\s*/\s*Lumpsum", text, re.IGNORECASE):
        total_quantity = 1
    else:
        total_quantity = None

    # 7. Estimated Bid Value
    m_est = re.search(r"Estimated\s+Bid\s+Value\s+in\s+INR.*?\n\s*(\d+)", text, re.DOTALL | re.IGNORECASE)
    estimated_bid_value_inr = float(m_est.group(1)) if m_est else None

    # 7b. Minimum Floor Price (if defined by Buyer)
    floor_price_matches = re.findall(
        r"Minimum Floor Price\s*(?:defined by Buyer)?\s*(?:Yes)?[\s\S]*?(?:Parameter\s*Values[\s\S]*?Core|Core)\s*\n\s*(\d+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    floor_prices = [float(x) for x in floor_price_matches]
    floor_price_schedules: list[dict[str, Any]] = []
    if floor_prices:
        floor_price_inr_min = min(floor_prices)
        floor_price_inr_max = max(floor_prices)
        if len(floor_prices) > 1:
            floor_price_schedules = [{"schedule": f"Schedule {i+1}", "amount": amt} for i, amt in enumerate(floor_prices)]
        else:
            floor_price_schedules = [{"schedule": "Main", "amount": floor_prices[0]}]
    else:
        floor_price_inr_min = None
        floor_price_inr_max = None

    # 8. EMD Detail
    emd_block = re.search(r"EMD\s+Detail.*?(?=(?:ePBG\s+Detail|\Z))", text, re.DOTALL | re.IGNORECASE)
    emd_schedules: list[dict[str, Any]] = []
    emd_amount_inr_min: float | None = None
    emd_amount_inr_max: float | None = None
    if emd_block:
        eb = emd_block.group(0)
        emd_req = not bool(re.search(r"/Required\s*\n\s*No", eb, re.IGNORECASE))
        if emd_req:
            emd_required = True
            # Check for schedule-wise EMD
            sched_matches = re.findall(
                r"(Schedule\s+\d+)[^\n]*\n.*?EMD\s+Amount(?:\s*\(In INR\))?\s*\n\s*(\d+(?:\.\d+)?)",
                eb,
                re.DOTALL | re.IGNORECASE,
            )
            if sched_matches:
                emd_schedules = [{"schedule": s.strip(), "amount": float(a)} for s, a in sched_matches]
                amts = [s["amount"] for s in emd_schedules]
                emd_amount_inr = None  # Do not sum across schedules!
                emd_amount_inr_min = min(amts) if amts else None
                emd_amount_inr_max = max(amts) if amts else None
            else:
                m_single = re.search(r"EMD\s+Amount(?:\s*\(In INR\))?\s*\n\s*(\d+(?:\.\d+)?)", eb, re.IGNORECASE)
                if m_single:
                    amt = float(m_single.group(1))
                    emd_amount_inr = amt
                    emd_amount_inr_min = amt
                    emd_amount_inr_max = amt
                else:
                    emd_amount_inr = None
                    emd_amount_inr_min = None
                    emd_amount_inr_max = None
        else:
            emd_required = False
            emd_amount_inr = None
            emd_amount_inr_min = None
            emd_amount_inr_max = None
    else:
        emd_required = None
        emd_amount_inr = None
        emd_amount_inr_min = None
        emd_amount_inr_max = None

    # 9. ePBG Detail
    epbg_block = re.search(r"ePBG\s+Detail.*?(?=(?:Bid\s+splitting|Splitting|MII|\Z))", text, re.DOTALL | re.IGNORECASE)
    if epbg_block:
        epb = epbg_block.group(0)
        epbg_req = not bool(re.search(r"/Required\s*\n\s*No", epb, re.IGNORECASE))
        if epbg_req:
            m_pct = re.search(r"/ePBG\s+Percentage\(%\)\s*\n\s*(\d+(?:\.\d+)?)", epb, re.IGNORECASE)
            epbg_percent = float(m_pct.group(1)) if m_pct else None
            m_mo = re.search(r"/Duration\s+of\s+ePBG\s+required\s*\(Months\)\.?\s*\n\s*(\d+)", epb, re.IGNORECASE)
            epbg_months = int(m_mo.group(1)) if m_mo else None
            epbg_required = True
        else:
            epbg_required = False
            epbg_percent = "not_stated"
            epbg_months = None
    else:
        epbg_required = None
        epbg_percent = "not_stated"
        epbg_months = None

    # 9b. PBG duration per lot in buyer terms
    pbg_lines = [l for l in text.split("\n") if "pbg" in l.lower() and "lot" in l.lower()]
    pbg_months_per_lot: list[dict[str, Any]] = []
    for line in pbg_lines:
        matches = re.findall(r"(LOT\s*\d+)[^\d\n]*?(\d+)\s*Months?", line, re.IGNORECASE)
        for lot, m in matches:
            norm_lot = " ".join(lot.strip().upper().replace("LOT", "LOT ").split())
            pbg_months_per_lot.append({"lot": norm_lot, "months": int(m)})

    # 10. MSE Reserved
    m_mseres = re.search(r"Reserved\s+for\s+MSE\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    if m_mseres:
        mse_reserved = True if m_mseres.group(1).lower() == "yes" else False
    elif re.search(r"Procurement\s+under\s+this\s+bid\s+is\s+reserved\s+for\s+purchase\s+from\s+Micro\s+and\s+Small\s+Enterprises", text, re.IGNORECASE):
        mse_reserved = True
    else:
        mse_reserved = False

    # 11. MSE Purchase Preference (true/false and percent)
    m_msepref = re.search(r"/MSE\s+Purchase\s+Preference\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    if m_msepref and m_msepref.group(1).lower() == "yes":
        m_pct = re.search(r"Purchase\s+Preference\s+to\s+MSE.*?L1\+X%\s*\n\s*(\d+)", text, re.DOTALL | re.IGNORECASE)
        mse_purchase_preference = f"yes at {m_pct.group(1)}%" if m_pct else "yes"
    elif m_msepref and m_msepref.group(1).lower() == "no":
        mse_purchase_preference = "no"
    elif re.search(r"Not Applicable as this is a works contract|no purchase preference applicable to MSE", text, re.IGNORECASE):
        mse_purchase_preference = "no"
    else:
        mse_purchase_preference = None

    # 12. MSE & Startup Relaxation
    m_mserel = re.search(r"MSE\s+Relaxation\s+for\s+Years\s+Of\s+Experience\s+and\s+Turnover\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    mse_experience_turnover_relaxation = True if (m_mserel and m_mserel.group(1).lower() == "yes") else (False if m_mserel else None)

    m_startrel = re.search(r"Startup\s+Relaxation\s+for\s+Years\s+Of\s+Experience\s+and\s+Turnover\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    startup_relaxation = True if (m_startrel and m_startrel.group(1).lower() == "yes") else (False if m_startrel else None)

    # 13. MII Class Restriction & Preference
    if re.search(r"Allow\s+participation\s+only\s+from\s+Class\s+1/Class\s+2\s+local\s+suppliers", text, re.IGNORECASE):
        mii_class_restriction = "Class 1 and 2 local suppliers only"
    else:
        mii_class_restriction = "not_stated"

    m_miipref = re.search(r"MII\s+Purchase\s+Preference\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    m_miicomp = re.search(r"MII\s+Compliance\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    if m_miipref:
        mii_preference = m_miipref.group(1).lower()
    elif m_miicomp:
        mii_preference = m_miicomp.group(1).lower()
    else:
        mii_preference = None

    # 14. BIS Required
    m_bis = re.search(r"Bis\s+Required\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    if m_bis:
        bis_required = True if m_bis.group(1).lower() == "yes" else False
    elif re.search(r"Standard\s+Marking\s+as\s+per\s+BIS\s*\n\s*Yes", text, re.IGNORECASE):
        bis_required = True
    else:
        bis_required = "not_stated"

    # 15. Min Turnover & Experience
    if re.search(r"Minimum\s+Average\s+Annual\s+Turnover", text, re.IGNORECASE):
        m_to = re.search(
            r"Minimum\s+Average\s+Annual\s+Turnover[^\n]*\s*(?:\n[^\n]*?)*?\n\s*(\d+(?:\.\d+)?)\s*(Lakh|Crore)",
            text,
            re.IGNORECASE,
        )
        min_turnover_lakh = parse_currency_to_lakh(m_to.group(1), m_to.group(2)) if m_to else None
    else:
        min_turnover_lakh = "not_stated"

    if re.search(r"Years\s+of\s+Past\s+Experience\s+Required", text, re.IGNORECASE):
        m_exp = re.search(r"Years\s+of\s+Past\s+Experience\s+Required[^\n]*\s*(?:\n[^\n]*?)*?\n\s*(\d+)\s*Year", text, re.IGNORECASE)
        years_experience_required = int(m_exp.group(1)) if m_exp else None
    else:
        years_experience_required = "not_stated"

    # Past performance & OEM Average Turnover
    m_pp = re.search(r"Past\s+Performance\s*\n\s*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    past_performance_percent = float(m_pp.group(1)) if m_pp else None

    m_oem_to = re.search(
        r"OEM\s+Average\s+Turnover[^\n]*\s*(?:\n[^\n]*?)*?\n\s*(\d+(?:\.\d+)?)\s*(Lakh|Crore)",
        text,
        re.IGNORECASE,
    )
    oem_average_turnover_lakh = parse_currency_to_lakh(m_oem_to.group(1), m_oem_to.group(2)) if m_oem_to else None

    # 16. OEM Authorization Required
    m_oem = re.search(r"OEM\s+Authorization\s+Certificate", text, re.IGNORECASE)
    oem_authorization_required = True if m_oem else False

    # 17. Bid Type
    m_bt = re.search(r"Type\s+of\s+Bid\s*\n\s*([^\n/]+)", text, re.IGNORECASE)
    bid_type = m_bt.group(1).strip().lower() if m_bt else None

    # 18. Reverse Auction
    m_ra = re.search(r"Bid\s+to\s+RA\s+enabled\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    reverse_auction = True if (m_ra and m_ra.group(1).lower() == "yes") else (False if m_ra else None)

    # 19. Short Duration Bid
    short_duration_bid = bool(re.search(r"Short\s+Duration\s+Bid\s+has\s+been\s+published", text, re.IGNORECASE))

    # 20. Inspection Required
    m_insp = re.search(r"Inspection\s+Required\s*\(By\s+Empanelled[^\)]*\)\s*\n\s*(Yes|No)", text, re.IGNORECASE)
    inspection_required = True if (m_insp and m_insp.group(1).lower() == "yes") else (False if m_insp else None)

    # 21. Contract Period
    m_cp = re.search(r"Contract\s+Period\s*\n\s*([^\n/]+)", text, re.IGNORECASE)
    contract_period = m_cp.group(1).strip() if m_cp else None

    # 22. Consignees Table: City and Delivery Days
    m_cons = re.search(
        r"Consignees/Reporting\s+Officer.*?(?=(?:Buyer\s+added|Special\s+terms|Advisory|Technical|Document\s+Title|Item\s+Wise|\Z))",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    consignee_city = None
    delivery_days = None
    if m_cons:
        ctext = m_cons.group(0)
        if re.search(r"Project\s*/\s*Lumpsum\s*Based", ctext, re.IGNORECASE):
            delivery_days = None
            consignee_city = "not_stated"
        else:
            num_matches = re.findall(r"(?:^|\n)\s*(\d{1,4})\s*(?=\n|$)", ctext)
            valid_days = [int(n) for n in num_matches if 1 <= int(n) <= 1200]
            if valid_days:
                delivery_days = valid_days[-1]

            # City extraction
            cities = [
                "Mumbai", "Dharwad", "Jorhat", "CINNAMARA", "Guwahati", "Bina",
                "Jaipur", "Ranchi", "Mangalore", "Jobner", "Vadodara", "Panipat",
                "Mathura", "Bhitoni", "Barauni", "Visakhapatnam", "Kheda", "Kolkata",
                "Delhi", "Chennai", "Bengaluru", "Ahmedabad", "Pune", "Hyderabad",
                "Cochin", "Kochi", "Udyamperoor", "Doranda", "Haldia", "Bongaigaon",
                "Numaligarh", "Bathinda", "Paradip", "Nagapattinam", "Tatipaka", "Koyali",
                "Jamnagar", "Silvassa", "Surat", "Bhubaneswar", "Raipur", "Bhopal",
                "Gwalior", "Indore", "Nagpur", "Kanpur", "Lucknow", "Varanasi",
                "Patna", "Ernakulam", "Ambala", "Faridabad", "Gurugram", "Noida",
                "Ghaziabad", "Roorkee", "Dehradun", "Dibrugarh", "Sibsagar", "Sivasagar",
                "Nazira", "Ankleshwar", "Mehsana", "Hazira", "Dahej", "Gandhinagar",
                "Rajkot", "Barmer", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner",
                "Alwar", "Kakinada", "Rajahmundry", "Vijayawada", "Guntur", "Tirupati",
                "Secunderabad", "Warangal", "Coimbatore", "Madurai", "Tiruchirappalli",
                "Salem", "Tirunelveli", "Mangaluru", "Mysuru", "Hubballi", "Belagavi",
                "Kalaburagi", "Dhanbad", "Jamshedpur", "Bokaro", "Rourkela", "Cuttack",
                "Sambalpur", "Asansol", "Durgapur", "Siliguri", "Agartala", "Shillong",
                "Imphal", "Aizawl", "Kohima", "Itanagar", "Gangtok", "Raiganj",
                "Trivandrum", "Thiruvananthapuram", "Kozhikode", "Palakkad", "Kollam",
                "Thrissur", "Kannur", "Alappuzha", "Kottayam", "Malappuram", "Kasaragod",
                "Pathanamthitta", "Idukki", "Wayanad"
            ]
            for cand in cities:
                if re.search(r"\b" + re.escape(cand) + r"\b", ctext, re.IGNORECASE):
                    consignee_city = cand
                    break
            if not consignee_city and office_name:
                for cand in cities:
                    if re.search(r"\b" + re.escape(cand) + r"\b", office_name, re.IGNORECASE):
                        consignee_city = cand
                        break
            if not consignee_city:
                # If consignee table has no recognized city
                consignee_city = "not_stated"
    else:
        consignee_city = "not_stated"

    # 23. Bid Splitting
    m_split = re.search(r"Split\s+Criteria\s+based.*?\n\s*(\d+:\d+)", text, re.DOTALL | re.IGNORECASE)
    bid_splitting = m_split.group(1).strip() if m_split else None

    # 24. Auto-extension fields
    m_min = re.search(r"Minimum\s+number\s+of\s+bids\s+required\s+to\s+disable\s+automatic\s+bid\s+extension[^\n]*\n\s*(\d+)", text, re.IGNORECASE)
    auto_extend_min_bids = int(m_min.group(1)) if m_min else None

    m_days = re.search(r"Number\s+of\s+days\s+for\s+which\s+Bid\s+would\s+be\s+auto[-\s]*extended\s*\n\s*(\d+)", text, re.IGNORECASE)
    auto_extend_days = int(m_days.group(1)) if m_days else None

    m_cnt = re.search(r"Number\s+of\s+Auto\s+Extension\s+count[^\n]*\n\s*(\d+)", text, re.IGNORECASE)
    auto_extend_max_count = int(m_cnt.group(1)) if m_cnt else None

    # 25. Buyer override flag
    # If buyer-added terms say to ignore GeM's EMD, turnover or consignee fields
    override_patterns = [
        r"ignore\s+(?:the\s+)?(?:gem\s+)?(?:emd|annual\s+turnover|turnover|consignee)",
        r"ignore\s+emd\s*/\s*annual\s+turnover",
        r"ignore\s+(?:gem\s+)?consignee",
        r"bqc\s+emd(?:/at)?\s+packet",
        r"kindly\s+ignore\s+(?:the\s+)?gem",
        r"kindly\s+ignore\s+(?:emd|turnover|consignee)",
        r"consignee\s+details\s+shall\s+be\s+ignored",
    ]
    buyer_overrides_gem_fields = bool(any(re.search(pat, text, re.IGNORECASE) for pat in override_patterns))

    # Parse status check:
    # Partial if item_category is None, or total_quantity is None,
    # or (emd_required is True and emd_amount_inr is None and emd_amount_inr_min is None),
    # or min_turnover_lakh is None (label present but failed),
    # or years_experience_required is None (label present but failed),
    # or epbg_percent is None (ePBG required but percent failed).
    if (
        item_category is None
        or total_quantity is None
        or (emd_required is True and emd_amount_inr is None and emd_amount_inr_min is None)
        or min_turnover_lakh is None
        or years_experience_required is None
        or epbg_percent is None
        or consignee_city is None
    ):
        pdf_parse_status = "partial"
    else:
        pdf_parse_status = "ok"

    return {
        "pdf_parse_status": pdf_parse_status,
        "bid_number": bid_number,
        "bid_end_datetime": bid_end_datetime,
        "bid_opening_datetime": bid_opening_datetime,
        "bid_offer_validity_days": bid_offer_validity_days,
        "ministry": ministry,
        "department": department,
        "organisation": organisation,
        "office_name": office_name,
        "item_category": item_category,
        "similar_category": similar_category,
        "total_quantity": total_quantity,
        "estimated_bid_value_inr": estimated_bid_value_inr,
        "floor_price_inr_min": floor_price_inr_min,
        "floor_price_inr_max": floor_price_inr_max,
        "floor_price_schedules": floor_price_schedules,
        "pbg_months_per_lot": pbg_months_per_lot,
        "buyer_overrides_gem_fields": buyer_overrides_gem_fields,
        "emd_required": emd_required,
        "emd_amount_inr": emd_amount_inr,
        "emd_amount_inr_min": emd_amount_inr_min,
        "emd_amount_inr_max": emd_amount_inr_max,
        "emd_schedules": emd_schedules,
        "epbg_required": epbg_required,
        "epbg_percent": epbg_percent,
        "epbg_months": epbg_months,
        "mse_reserved": mse_reserved,
        "mse_purchase_preference": mse_purchase_preference,
        "mse_experience_turnover_relaxation": mse_experience_turnover_relaxation,
        "startup_relaxation": startup_relaxation,
        "mii_class_restriction": mii_class_restriction,
        "mii_preference": mii_preference,
        "bis_required": bis_required,
        "min_turnover_lakh": min_turnover_lakh,
        "years_experience_required": years_experience_required,
        "past_performance_percent": past_performance_percent,
        "oem_average_turnover_lakh": oem_average_turnover_lakh,
        "oem_authorization_required": oem_authorization_required,
        "bid_type": bid_type,
        "reverse_auction": reverse_auction,
        "short_duration_bid": short_duration_bid,
        "inspection_required": inspection_required,
        "contract_period": contract_period,
        "delivery_days": delivery_days,
        "consignee_city": consignee_city,
        "bid_splitting": bid_splitting,
        "auto_extend_min_bids": auto_extend_min_bids,
        "auto_extend_days": auto_extend_days,
        "auto_extend_max_count": auto_extend_max_count,
    }


def compute_extension_fields(
    bid_end_datetime: str | None,
    closing_date_iso: str | None,
    auto_extend_days: int | None,
    auto_extend_max_count: int | None,
    corrigendum_date: str | None = None,
    has_corrigendum: bool | None = None,
) -> dict[str, Any]:
    """
    Calculate extension tracking fields:
      - original_end_utc: PDF "Bid End Date/Time" converted from IST to UTC ISO8601.
      - auto_extension_limit_utc: original_end_utc + auto_extend_days x auto_extend_max_count.
      - is_extended: 1 if closing_date is > 1 min after original_end_utc, else 0.
      - extension_days_observed: (closing_date - original_end_utc) in days (decimal).
      - date_check: 'unchanged', 'extended_auto', 'extended_by_corrigendum', 'check_needed', or 'not_checked'.
    """
    if not bid_end_datetime:
        return {
            "original_end_utc": None,
            "auto_extension_limit_utc": None,
            "possible_latest_end_utc": None,
            "is_extended": 0,
            "extension_days_observed": None,
            "date_check": "not_checked",
        }

    try:
        s = bid_end_datetime.strip()
        if "T" in s or s.endswith("Z"):
            dt_utc = datetime.strptime(s.replace("Z", "+00:00"), "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
        else:
            dt_ist = datetime.strptime(s, "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)
            dt_utc = dt_ist.astimezone(timezone.utc)
        original_end_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return {
            "original_end_utc": None,
            "auto_extension_limit_utc": None,
            "possible_latest_end_utc": None,
            "is_extended": 0,
            "extension_days_observed": None,
            "date_check": "not_checked",
        }

    ext_days = (auto_extend_days or 0) * (auto_extend_max_count or 0)
    dt_latest_utc = dt_utc + timedelta(days=ext_days)
    auto_extension_limit_utc = dt_latest_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not closing_date_iso:
        return {
            "original_end_utc": original_end_utc,
            "auto_extension_limit_utc": auto_extension_limit_utc,
            "possible_latest_end_utc": auto_extension_limit_utc,
            "is_extended": 0,
            "extension_days_observed": None,
            "date_check": "not_checked",
        }

    try:
        s_close = closing_date_iso.strip()
        if "T" in s_close or s_close.endswith("Z"):
            dt_closing = datetime.strptime(s_close.replace("Z", "+00:00"), "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
        else:
            dt_closing = datetime.strptime(s_close, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return {
            "original_end_utc": original_end_utc,
            "auto_extension_limit_utc": auto_extension_limit_utc,
            "possible_latest_end_utc": auto_extension_limit_utc,
            "is_extended": 0,
            "extension_days_observed": None,
            "date_check": "check_needed",
        }

    diff_sec = (dt_closing - dt_utc).total_seconds()
    extension_days_observed = round(diff_sec / 86400.0, 2)
    is_extended = 1 if diff_sec > 60 else 0

    if has_corrigendum is not None:
        corrigendum_found = bool(has_corrigendum)
    else:
        c_str = str(corrigendum_date or "").strip().lower()
        corrigendum_found = bool(c_str and c_str not in ("not_stated", "none", "false", "0"))

    # Determine date_check:
    # - unchanged
    # - extended_auto (within the auto limit)
    # - extended_by_corrigendum (beyond it, with a corrigendum found)
    # - check_needed (earlier than the original date, or later than the limit with no corrigendum found)
    if diff_sec < -60:
        date_check = "check_needed"
    elif abs(diff_sec) <= 60:
        date_check = "unchanged"
    elif auto_extend_days is None or auto_extend_max_count is None:
        date_check = "extended_by_corrigendum" if corrigendum_found else "check_needed"
    elif diff_sec > 60 and (dt_closing - dt_latest_utc).total_seconds() <= 60:
        date_check = "extended_auto"
    else:
        # Closing date is beyond the auto extension limit
        # "A closing_date beyond it is not an error."
        date_check = "extended_by_corrigendum" if corrigendum_found else "check_needed"

    return {
        "original_end_utc": original_end_utc,
        "auto_extension_limit_utc": auto_extension_limit_utc,
        "possible_latest_end_utc": auto_extension_limit_utc,
        "is_extended": is_extended,
        "extension_days_observed": extension_days_observed,
        "date_check": date_check,
    }


def evaluate_eligibility(
    tender: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> tuple[str, list[str], list[str]]:
    """
    Evaluate tender eligibility against my_profile.json rules.
    Returns: (eligibility, reasons, flags)
      - eligibility: 'likely_not_eligible', 'unknown', or 'no_blockers_found' (never 'eligible').
        With any null profile value the result must be 'unknown', never 'no_blockers_found'.
      - flags: separate list of tags (Class 1/2 local suppliers only, BIS required,
        OEM authorization needed, MSE reserved, reverse auction, short duration).
    """
    prof = profile if profile is not None else load_profile()
    reasons: list[str] = []
    flags: list[str] = []
    missing_fields: list[str] = []
    likely_not_eligible = False

    # Collect flags
    if tender.get("mii_class_restriction") and tender.get("mii_class_restriction") != "not_stated":
        flags.append("Class 1/2 local suppliers only")
    if tender.get("bis_required") is True or tender.get("bis_required") == 1:
        flags.append("BIS required")
    if tender.get("oem_authorization_required"):
        flags.append("OEM authorization needed")
    if tender.get("mse_reserved"):
        flags.append("MSE reserved")
    if tender.get("reverse_auction"):
        flags.append("reverse auction")
    if tender.get("short_duration_bid"):
        flags.append("short duration")
    if tender.get("buyer_overrides_gem_fields"):
        flags.append("EMD / turnover set in buyer documents, check them before relying on these numbers")

    # 1. MSE Reservation
    if tender.get("mse_reserved"):
        is_msme = prof.get("is_msme")
        if is_msme is False:
            reasons.append("reserved for MSEs")
            likely_not_eligible = True
        elif is_msme is None:
            missing_fields.append("is_msme")

    # 2. Min Turnover
    min_to = tender.get("min_turnover_lakh")
    if min_to is not None and min_to != "not_stated" and isinstance(min_to, (int, float)) and min_to > 0:
        relaxed = False
        if tender.get("mse_experience_turnover_relaxation") and prof.get("is_msme") is True:
            relaxed = True
        if tender.get("startup_relaxation") and prof.get("is_startup") is True:
            relaxed = True

        if not relaxed:
            p_to = prof.get("annual_turnover_lakh")
            if p_to is None:
                missing_fields.append("annual_turnover_lakh")
            elif p_to < min_to:
                reasons.append(f"turnover {p_to}L < required {min_to}L")
                likely_not_eligible = True

    # 3. Years of Past Experience
    exp_req = tender.get("years_experience_required")
    if exp_req is not None and exp_req != "not_stated" and isinstance(exp_req, (int, float)) and exp_req > 0:
        relaxed = False
        if tender.get("mse_experience_turnover_relaxation") and prof.get("is_msme") is True:
            relaxed = True
        if tender.get("startup_relaxation") and prof.get("is_startup") is True:
            relaxed = True

        if not relaxed:
            p_exp = prof.get("years_in_business")
            if p_exp is None:
                missing_fields.append("years_in_business")
            elif p_exp < exp_req:
                reasons.append(f"experience {p_exp}y < required {exp_req}y")
                likely_not_eligible = True

    # Check if ANY profile value is null or missing
    required_keys = {
        "is_msme",
        "is_startup",
        "is_manufacturer",
        "is_trader",
        "is_service_provider",
        "annual_turnover_lakh",
        "years_in_business",
        "has_oem_authorization",
    }
    has_any_null_profile = not prof or any(prof.get(k) is None for k in required_keys)

    if likely_not_eligible:
        return "likely_not_eligible", reasons, flags

    if missing_fields or has_any_null_profile:
        for f in missing_fields:
            if f"missing profile {f}" not in reasons:
                reasons.append(f"missing profile {f}")
        return "unknown", reasons, flags

    return "no_blockers_found", reasons, flags
