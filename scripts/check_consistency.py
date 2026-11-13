#!/usr/bin/env python3
"""
scripts/check_consistency.py
============================
Consistency checks for tenders database:
  1. buyer_match = 1 and tier C (must be at least tier B).
  2. category_noise_reason and tier A (capped/demoted to tier B).
  3. match_basis = 'location_only' and tier A (location-only can never be tier A).
  4. date_check = 'check_needed' with pdf_parse_status = 'none' (only tenders with parsed PDF can have check_needed).
  5. status outside allowed set ('new', 'interested', 'bidding', 'skipped', 'submitted').
  6. open tenders with closing_date in the past.
  7. dashboard lane filter count differs from SQL count.

Exits 0 if all checks pass.
Exits 1 (non-zero) if any inconsistency is found.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender_mcp.db import get_conn


def check_consistency() -> tuple[bool, dict[str, list[dict]]]:
    """
    Run 7 consistency checks against tenders table and dashboard query logic.
    Returns (is_consistent, violations_dict).
    """
    violations: dict[str, list[dict[str, Any]]] = {
        "buyer_match_tier_c": [],
        "category_noise_tier_a": [],
        "location_only_tier_a": [],
        "check_needed_unparsed_pdf": [],
        "status_invalid": [],
        "open_tenders_past_closing": [],
        "lane_filter_count_mismatch": [],
    }

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        # Check 1: buyer_match = 1 with tier C
        rows1 = conn.execute("""
            SELECT id, title, organisation, relevance_score, tier, buyer_match
            FROM tenders
            WHERE buyer_match = 1 AND tier = 'C'
        """).fetchall()
        for r in rows1:
            violations["buyer_match_tier_c"].append(dict(r))

        # Check 2: category_noise_reason with tier A
        rows2 = conn.execute("""
            SELECT id, title, organisation, relevance_score, tier, category_noise_reason
            FROM tenders
            WHERE category_noise_reason IS NOT NULL AND category_noise_reason != '' AND tier = 'A'
        """).fetchall()
        for r in rows2:
            violations["category_noise_tier_a"].append(dict(r))

        # Check 3: match_basis = location_only with tier A
        rows3 = conn.execute("""
            SELECT id, title, organisation, relevance_score, tier, match_basis
            FROM tenders
            WHERE match_basis = 'location_only' AND tier = 'A'
        """).fetchall()
        for r in rows3:
            violations["location_only_tier_a"].append(dict(r))

        # Check 4: date_check = 'check_needed' with pdf_parse_status = 'none'
        rows4 = conn.execute("""
            SELECT id, title, organisation, date_check, pdf_parse_status
            FROM tenders
            WHERE date_check = 'check_needed' AND (pdf_parse_status = 'none' OR pdf_parse_status IS NULL OR pdf_parse_status = '')
        """).fetchall()
        for r in rows4:
            violations["check_needed_unparsed_pdf"].append(dict(r))

        # Check 5: status outside new/interested/bidding/skipped/submitted
        rows5 = conn.execute("""
            SELECT id, title, organisation, status
            FROM tenders
            WHERE status NOT IN ('new', 'interested', 'bidding', 'skipped', 'submitted')
        """).fetchall()
        for r in rows5:
            violations["status_invalid"].append(dict(r))

        # Check 6: open tenders with closing_date in the past
        from tender_mcp.web.app import query_filtered_tenders
        open_tenders = query_filtered_tenders(tier="all", min_hours=0.0)
        for t in open_tenders:
            c_date = t.get("closing_date")
            if c_date and c_date <= now_iso:
                violations["open_tenders_past_closing"].append({
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "closing_date": c_date,
                    "now_iso": now_iso,
                })

        # Check 7: dashboard lane filter count differs from SQL count
        sql_lane_row = conn.execute("""
            SELECT count(*)
            FROM tenders
            WHERE buyer_match = 1 AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """).fetchone()
        sql_lane_count = sql_lane_row[0] if sql_lane_row else 0
        dash_lane_tenders = query_filtered_tenders(tier="buyer_match", min_hours=0.0)
        dash_lane_count = len(dash_lane_tenders)

        if sql_lane_count != dash_lane_count:
            violations["lane_filter_count_mismatch"].append({
                "sql_count": sql_lane_count,
                "dashboard_count": dash_lane_count,
                "difference": abs(sql_lane_count - dash_lane_count),
            })

    is_consistent = not any(len(v) > 0 for v in violations.values())
    return is_consistent, violations


def main() -> int:
    is_consistent, violations = check_consistency()

    total_violations = sum(len(v) for v in violations.values())
    if is_consistent:
        print("[CONSISTENCY CHECK PASSED]")
        print("  1. buyer_match = 1 and tier C: 0 violations")
        print("  2. category_noise_reason and tier A: 0 violations")
        print("  3. match_basis = location_only and tier A: 0 violations")
        print("  4. date_check = 'check_needed' with pdf_parse_status = 'none': 0 violations")
        print("  5. status outside allowed set: 0 violations")
        print("  6. open tenders with closing_date in past: 0 violations")
        print("  7. dashboard lane filter count vs SQL count mismatch: 0 violations")
        return 0

    print(f"[CONSISTENCY CHECK FAILED] Found {total_violations} total violations:")

    for check_name, vlist in violations.items():
        if vlist:
            print(f"\n--- {check_name} ({len(vlist)} violations) ---")
            for item in vlist[:10]:
                print(f"  {item}")
            if len(vlist) > 10:
                print(f"  ... and {len(vlist) - 10} more")

    return 1


if __name__ == "__main__":
    sys.exit(main())
