"""
tender_mcp/server.py  –  MCP server for tender-aggregator
==========================================================
Run with:
    python -m tender_mcp.server
Or (for stdio transport, used by Claude Desktop):
    python tender_mcp/server.py

MCP tools exposed:
  - refresh_tenders       Pull fresh tenders from all sources
  - search_tenders        Query the local DB by keyword / source / status
  - get_tender            Fetch one tender by ID
  - export_tenders        Write filtered tenders to CSV, return path
  - suggest_relevance     Score a tender against configured KEYWORDS
  - tender_stats          Summary of DB state and last-refresh stats

IMPORTANT: All logging goes to stderr. stdout is the MCP stdio channel.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ── Load config & env relative to project root ─────────────────────────────
from tender_mcp.config import PROJECT_ROOT, DEFAULT_MIN_HOURS_TO_CLOSE
from tender_mcp.matcher import score_tender, get_min_score, load_keywords
from tender_mcp.dates import get_hours_to_close

# ── Logging to stderr only ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("tender_mcp.server")

# ── Late imports (after env is loaded) ──────────────────────────────────────
from tender_mcp.db import init_db, query_tenders, get_tender_by_id
from tender_mcp.refresh import refresh_all

# ── Server instance ──────────────────────────────────────────────────────────
mcp = FastMCP(
    name="tender-aggregator",
    instructions=(
        "This server aggregates tenders from Indian government portals "
        "(CPPP, GeM, HMEL) for Swamina International's sales team. "
        "Use refresh_tenders to fetch latest data, then search_tenders or "
        "export_tenders to surface relevant opportunities."
    ),
)

EXPORTS_DIR = PROJECT_ROOT / "exports"

# Ensure DB exists on import
init_db()


# ═══════════════════════════════════════════════════════════════════════════
# Tool: refresh_tenders
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def refresh_tenders(
    sources: str | None = None,
    cppp_max_pages: int = 40,
    gem_max_pages: int = 3,
) -> str:
    """
    Pull fresh tenders from all (or specified) portals and store them in the DB.

    Args:
        sources: Comma-separated list of sources to refresh.
                 Options: 'cppp', 'gem', 'hmel'. Default: all three.
        cppp_max_pages: Max pages to fetch from CPPP (default 40).
        gem_max_pages:  Max pages to fetch from GeM per keyword.

    Returns:
        JSON string with refresh summary: new counts, updated counts, errors.
    """
    source_list = (
        [s.strip() for s in sources.split(",") if s.strip()]
        if sources
        else None
    )

    logger.info("refresh_tenders called: sources=%s", source_list)
    summary = await refresh_all(
        sources=source_list,
        cppp_max_pages=cppp_max_pages,
        gem_max_pages=gem_max_pages,
    )
    return json.dumps(summary, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# Tool: search_tenders
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_tenders(
    keyword: str | None = None,
    source: str | None = None,
    status: str | None = None,
    closing_within_days: int | None = None,
    tender_kind: str | None = None,
    eligibility_not: str | None = None,
    extended_only: bool = False,
    limit: int = 50,
    include_low: bool = False,
    include_expired: bool = False,
    include_tier_c: bool = False,
    min_hours_to_close: float | None = DEFAULT_MIN_HOURS_TO_CLOSE,
) -> str:
    """
    Search local tender database.

    Args:
        keyword: Text to match against title + organisation (case-insensitive).
        source: Filter to one portal: 'cppp', 'gem', or 'hmel'.
        status: Filter by status (default 'active').
        closing_within_days: Only return tenders closing in the next N days.
        tender_kind: Filter by kind: 'service', 'product', 'mixed' or 'unknown'.
        eligibility_not: Filter out tenders matching this eligibility (e.g. 'likely_not_eligible').
        extended_only: Filter to tenders where is_extended is 1 (True).
        limit: Max results to return (default 50).
        include_low: If True, returns tenders below min_score too, including tier-C
                     rows capped by staffing_noise / logistics_noise / context_exclude.
        include_expired: If False, hides tenders whose closing time has passed.
        include_tier_c: If False (default), hides tier-C tenders (score < 30) unless include_low=True.
        min_hours_to_close: Minimum hours before closing (default 24.0).

    Returns:
        JSON array of matching tender records. For enriched GeM bids, includes:
        - 'is_extended': 1 if closing_date is past original_end_utc, else 0.
        - 'original_end_utc': original deadline from PDF in UTC ISO 8601.
        - 'auto_extension_limit_utc': latest allowed auto-extension deadline.
        - 'date_check': 'unchanged', 'extended_auto', 'extended_by_corrigendum', 'check_needed', or 'not_checked'.
        - 'extensions_seen': count of upward changes seen by refresh since tracking began (not total extensions).
        Along with 'tier', 'tender_kind', 'is_consumable', and 'bid_window' fields.
    """
    from typing import Any
    from tender_mcp.matcher import compute_bid_window
    min_rel = None if (include_low or status == "details needed") else get_min_score()

    rows = query_tenders(
        source=source,
        status=status,
        closing_within_days=closing_within_days,
        min_relevance=min_rel,
        include_expired=include_expired,
    )

    filtered: list[dict[str, Any]] = []
    for r in rows:
        htc = get_hours_to_close(r.get("closing_date"))
        r["hours_to_close"] = round(htc, 1) if htc is not None else None
        r["closing_soon"] = bool(htc is not None and 0 <= htc <= 48.0)
        r["bid_window"] = compute_bid_window(htc)
        r["is_consumable"] = bool(r.get("is_consumable"))
        r["match_basis"] = r.get("match_basis") or "item"

        # By default hide tenders closing within 24 hours (configurable)
        if not include_expired and min_hours_to_close is not None and htc is not None and htc < min_hours_to_close:
            continue

        # By default hide tier C (include_low=True returns capped / low tenders too)
        if not include_tier_c and not include_low and r.get("tier", "C") == "C":
            continue

        # Filter by extended_only if specified
        if extended_only and not r.get("is_extended"):
            continue

        # Filter by tender_kind if specified
        if tender_kind and r.get("tender_kind") != tender_kind:
            continue

        if eligibility_not and r.get("eligibility") == eligibility_not:
            continue

        if keyword:
            kw = keyword.lower()
            if (
                kw not in (r.get("title") or "").lower()
                and kw not in (r.get("organisation") or "").lower()
                and kw not in (r.get("category") or "").lower()
                and kw not in (r.get("bid_number") or "").lower()
                and kw not in (r.get("id") or "").lower()
            ):
                continue

        # Parse eligibility_reasons, flags, and emd_schedules if stringified
        if isinstance(r.get("eligibility_reasons"), str) and r["eligibility_reasons"]:
            try:
                r["eligibility_reasons"] = json.loads(r["eligibility_reasons"])
            except Exception:
                pass
        if isinstance(r.get("flags"), str) and r["flags"]:
            try:
                r["flags"] = json.loads(r["flags"])
            except Exception:
                pass
        if isinstance(r.get("emd_schedules"), str) and r["emd_schedules"]:
            try:
                r["emd_schedules"] = json.loads(r["emd_schedules"])
            except Exception:
                pass
        if isinstance(r.get("floor_price_schedules"), str) and r["floor_price_schedules"]:
            try:
                r["floor_price_schedules"] = json.loads(r["floor_price_schedules"])
            except Exception:
                pass
        if isinstance(r.get("pbg_months_per_lot"), str) and r["pbg_months_per_lot"]:
            try:
                r["pbg_months_per_lot"] = json.loads(r["pbg_months_per_lot"])
            except Exception:
                pass
        if r.get("buyer_overrides_gem_fields"):
            r["buyer_overrides_gem_fields"] = True
            r["buyer_override_warning"] = "EMD / turnover set in buyer documents, check them before relying on these numbers"
            if "flags" not in r or not isinstance(r["flags"], list):
                r["flags"] = []
            if "EMD / turnover set in buyer documents, check them before relying on these numbers" not in r["flags"]:
                r["flags"].append("EMD / turnover set in buyer documents, check them before relying on these numbers")

        if r.get("buyer_match"):
            r["buyer_match_label"] = "Oil/gas buyer, supply tender"

        filtered.append(r)

    # Sort: tier A by closing_date asc within bid_window, then by score desc
    window_order = {"urgent": 0, "soon": 1, "normal": 2, "unknown": 3}
    filtered.sort(key=lambda x: (
        0 if x.get("tier") == "A" else 1,
        window_order.get(x.get("bid_window", "unknown"), 3),
        x.get("hours_to_close") or 999999,
        -(x.get("relevance_score") or 0),
    ))

    filtered = filtered[:limit]
    return json.dumps(filtered, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# Tool: get_tender
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_tender(tender_id: str) -> str:
    """
    Fetch a single tender by its composite ID (e.g. 'cppp:MTQxOTYwOTA=…').

    Args:
        tender_id: The tender's composite ID from the database.

    Returns:
        JSON object with all tender fields, or an error message.
    """
    tender = get_tender_by_id(tender_id)
    if not tender:
        return json.dumps({"error": f"Tender '{tender_id}' not found."})
    if isinstance(tender.get("eligibility_reasons"), str) and tender["eligibility_reasons"]:
        try:
            tender["eligibility_reasons"] = json.loads(tender["eligibility_reasons"])
        except Exception:
            pass
    if isinstance(tender.get("flags"), str) and tender["flags"]:
        try:
            tender["flags"] = json.loads(tender["flags"])
        except Exception:
            pass
    if isinstance(tender.get("floor_price_schedules"), str) and tender["floor_price_schedules"]:
        try:
            tender["floor_price_schedules"] = json.loads(tender["floor_price_schedules"])
        except Exception:
            pass
    if isinstance(tender.get("pbg_months_per_lot"), str) and tender["pbg_months_per_lot"]:
        try:
            tender["pbg_months_per_lot"] = json.loads(tender["pbg_months_per_lot"])
        except Exception:
            pass
    if tender.get("buyer_overrides_gem_fields"):
        tender["buyer_overrides_gem_fields"] = True
        tender["buyer_override_warning"] = "EMD / turnover set in buyer documents, check them before relying on these numbers"
        if "flags" not in tender or not isinstance(tender["flags"], list):
            tender["flags"] = []
        if "EMD / turnover set in buyer documents, check them before relying on these numbers" not in tender["flags"]:
            tender["flags"].append("EMD / turnover set in buyer documents, check them before relying on these numbers")
    if tender.get("buyer_match"):
        tender["buyer_match_label"] = "Oil/gas buyer, supply tender"
    return json.dumps(tender, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# Tool: export_tenders
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def export_tenders(
    status: str | None = None,
    closing_within_days: int | None = None,
    source: str | None = None,
    tender_kind: str | None = None,
    eligibility_not: str | None = None,
    extended_only: bool = False,
    include_expired: bool = False,
    include_tier_c: bool = False,
    min_hours_to_close: float | None = DEFAULT_MIN_HOURS_TO_CLOSE,
) -> str:
    """
    Export filtered tenders to a CSV file in the ./exports/ directory.

    Args:
        status: Filter by status field (default 'active'; pass None for all).
        closing_within_days: Only include tenders closing within N days.
        source: Restrict to one portal: 'cppp', 'gem', or 'hmel'.
        tender_kind: Filter by kind: 'service', 'product', 'mixed' or 'unknown'.
        eligibility_not: Filter out tenders matching this eligibility (e.g. 'likely_not_eligible').
        extended_only: Filter to tenders where is_extended is 1 (True).
        include_expired: If False, hides tenders whose closing time has passed.
        include_tier_c: If False (default), hides tier-C tenders (score < 30).
        min_hours_to_close: Minimum hours before closing (default 24.0).

    Returns:
        Absolute path of the created CSV file. Enriched columns include 'is_extended',
        'original_end_utc', 'auto_extension_limit_utc', 'date_check', and 'extensions_seen'
        (which counts upward changes seen by refresh since tracking began, not total extensions).
    """
    from tender_mcp.matcher import compute_bid_window
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    rows = query_tenders(
        source=source,
        status=status,
        closing_within_days=closing_within_days,
        include_expired=include_expired,
    )

    filtered_rows: list[dict] = []
    for r in rows:
        htc = get_hours_to_close(r.get("closing_date"))
        r["hours_to_close"] = round(htc, 1) if htc is not None else None
        r["closing_soon"] = bool(htc is not None and 0 <= htc <= 48.0)
        r["bid_window"] = compute_bid_window(htc)
        r["is_consumable"] = bool(r.get("is_consumable"))
        r["match_basis"] = r.get("match_basis") or "item"

        if not include_expired and min_hours_to_close is not None and htc is not None and htc < min_hours_to_close:
            continue

        if not include_tier_c and r.get("tier", "C") == "C":
            continue

        if extended_only and not r.get("is_extended"):
            continue

        if tender_kind and r.get("tender_kind") != tender_kind:
            continue

        if eligibility_not and r.get("eligibility") == eligibility_not:
            continue

        if r.get("buyer_match"):
            r["buyer_match_label"] = "Oil/gas buyer, supply tender"
        else:
            r["buyer_match_label"] = ""

        filtered_rows.append(r)

    rows = filtered_rows

    timestamp = date.today().isoformat()
    fname = f"tenders_{timestamp}"
    if source:
        fname += f"_{source}"
    if closing_within_days:
        fname += f"_closing{closing_within_days}d"
    fname += ".csv"
    out_path = EXPORTS_DIR / fname

    if not rows:
        return json.dumps({"path": str(out_path), "count": 0, "note": "No matching tenders."})

    fieldnames = [
        "id", "source", "title", "organisation", "category",
        "tier", "tender_kind", "match_basis", "is_consumable", "buyer_is_oil_gas", "buyer_match", "buyer_match_label",
        "published_date", "closing_date", "opening_date",
        "original_end_utc", "auto_extension_limit_utc", "is_extended", "was_extended", "extension_days_observed", "date_check", "extensions_seen",
        "hours_to_close", "bid_window", "closing_soon",
        "emd_value", "tender_value", "link", "status",
        "relevance_score", "matched_terms",
        # GeM enrichment fields
        "bid_number", "estimated_bid_value_inr", "floor_price_inr_min", "floor_price_inr_max", "floor_price_schedules", "pbg_months_per_lot", "buyer_overrides_gem_fields", "total_quantity",
        "similar_category", "category_noise_reason",
        "emd_required", "emd_amount_inr", "emd_amount_inr_min", "emd_amount_inr_max", "emd_schedules",
        "epbg_required", "epbg_percent", "epbg_months",
        "mse_reserved", "mse_purchase_preference", "min_turnover_lakh", "years_experience_required",
        "past_performance_percent", "oem_average_turnover_lakh",
        "auto_extend_min_bids", "auto_extend_days", "auto_extend_max_count",
        "oem_authorization_required", "bid_type", "reverse_auction", "short_duration_bid",
        "delivery_days", "consignee_city", "bid_splitting",
        "eligibility", "eligibility_reasons", "flags", "corrigendum_date", "pdf_parse_status", "details_needed",
        "first_seen_at", "last_seen_at",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    logger.info("export_tenders: wrote %d rows to %s", len(rows), out_path)
    return json.dumps({"path": str(out_path), "count": len(rows)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool: suggest_relevance
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def suggest_relevance(tender_id: str) -> str:
    """
    Score a tender against keywords.json rules and return matched terms.

    Useful for helping a model judge whether a tender is relevant to
    Swamina International's petroleum & gas business.

    Args:
        tender_id: Composite tender ID (e.g. 'cppp:MTQxOTYwOTA=…').

    Returns:
        JSON with tender details plus matched_terms, relevance_score (0–100),
        and detailed assessment.
    """
    tender = get_tender_by_id(tender_id)
    if not tender:
        return json.dumps({"error": f"Tender '{tender_id}' not found."})

    scoring = score_tender(tender)
    result = {
        **tender,
        "relevance_score": scoring["score"],
        "matched_keywords": scoring["matched_terms"],
        "matched_terms": scoring["matched_terms"],
        "matched_strong": scoring["matched_strong"],
        "matched_weak": scoring["matched_weak"],
        "matched_exclude": scoring["matched_exclude"],
        "min_score": scoring["min_score"],
        "is_relevant": scoring["is_relevant"],
        "assessment": scoring["assessment"],
    }
    return json.dumps(result, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# Tool: tender_stats
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def tender_stats() -> str:
    """
    Return a summary of the tender database and the most recent refresh.

    Returns:
        JSON object with counts by source, recent refresh stats, and
        tenders closing in the next 7 days.
    """
    from tender_mcp.db import get_conn

    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
        by_source = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT source, COUNT(*) FROM tenders GROUP BY source"
            ).fetchall()
        }
        latest_logs = conn.execute(
            """
            SELECT source, started_at, finished_at, fetched_count, new_count, updated_count, error_msg
            FROM refresh_log ORDER BY id DESC LIMIT 10
            """
        ).fetchall()
        closing_soon = conn.execute(
            """
            SELECT COUNT(*) FROM tenders
            WHERE closing_date <= strftime('%Y-%m-%dT%H:%M:%SZ','now','+7 days')
              AND status = 'active'
            """
        ).fetchone()[0]
        details_needed_count = conn.execute(
            "SELECT COUNT(*) FROM tenders WHERE status = 'details needed'"
        ).fetchone()[0]

    last_refresh_by_source = {}
    for r in latest_logs:
        src = r["source"]
        if src not in last_refresh_by_source:
            last_refresh_by_source[src] = {
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "fetched": r["fetched_count"],
                "new": r["new_count"],
                "updated": r["updated_count"],
                "error": r["error_msg"],
            }

    return json.dumps(
        {
            "total_tenders": total,
            "by_source": by_source,
            "details_needed": details_needed_count,
            "closing_within_7_days": closing_soon,
            "last_refresh": last_refresh_by_source,
            "recent_refreshes": [dict(r) for r in latest_logs[:6]],
        },
        indent=2,
        default=str,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool: get_details_needed_tenders
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_details_needed_tenders(limit: int = 50) -> str:
    """
    Fetch tenders from Petroleum-ministry organisations where item details are not listed
    (flagged as 'details needed') so their item details can be filled from bid PDFs.
    """
    from tender_mcp.db import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, source, title, organisation, published_date, closing_date, link, status, relevance_score, matched_terms
            FROM tenders WHERE status = 'details needed'
            ORDER BY closing_date ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return json.dumps([dict(r) for r in rows], indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("tender-aggregator MCP server starting (stdio transport)")
    mcp.run(transport="stdio")
