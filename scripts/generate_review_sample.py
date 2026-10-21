#!/usr/bin/env python3
"""
scripts/generate_review_sample.py
Regenerate ./exports/review_sample.csv:
  - Top 60 open tier A and B tenders closing in > 24h (match_basis == 'item'),
    sorted: tier A (product, mixed, unknown) ordered by bid_window (urgent last),
    then tier A (service), then tier B.
  - Plus 10 extra rows (a separate block) of location_only tenders so the sales team can judge them.
Total: 70 rows.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender_mcp.db import get_conn
from tender_mcp.dates import parse_datetime, get_hours_to_close, IST
from tender_mcp.matcher import compute_bid_window, score_tender

OUT_FILE = PROJECT_ROOT / "exports" / "review_sample.csv"


def generate() -> int:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, title, organisation, tier, tender_kind, is_consumable, match_basis,
                   relevance_score AS score, matched_terms, closing_date, link,
                   category, raw_json,
                   estimated_bid_value_inr, emd_amount_inr, mse_reserved,
                   min_turnover_lakh, years_experience_required, eligibility,
                   original_end_utc, auto_extension_limit_utc, is_extended,
                   extension_days_observed, date_check, extensions_seen, flags,
                   buyer_overrides_gem_fields, floor_price_inr_min, floor_price_inr_max,
                   similar_category, category_noise_reason, buyer_match
            FROM tenders
            WHERE tier IN ('A', 'B')
              AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+24 hours')
        """).fetchall()

    items = []
    window_order = {"soon": 0, "normal": 1, "urgent": 2, "unknown": 3}

    for r in rows:
        d = dict(r)
        d["item_term"] = ""
        d["match_basis"] = d.get("match_basis") or "item"

        if d.get("buyer_overrides_gem_fields"):
            import json
            flags_list = []
            if d.get("flags"):
                try:
                    flags_list = json.loads(d["flags"]) if isinstance(d["flags"], str) else list(d["flags"])
                except Exception:
                    flags_list = []
            override_msg = "EMD / turnover set in buyer documents, check them before relying on these numbers"
            if override_msg not in flags_list:
                flags_list.append(override_msg)
            d["flags"] = json.dumps(flags_list)

        htc = get_hours_to_close(d.get("closing_date"))
        d["hours_to_close"] = round(htc, 1) if htc is not None else None
        d["bid_window"] = compute_bid_window(htc)

        dt = parse_datetime(d.get("closing_date"))
        if dt:
            dt_ist = dt.astimezone(IST)
            d["closing_IST"] = dt_ist.strftime("%Y-%m-%d %H:%M IST")
        else:
            d["closing_IST"] = ""

        d["is_consumable"] = bool(d.get("is_consumable"))
        d["buyer_match_label"] = "Oil/gas buyer, supply tender" if d.get("buyer_match") else ""
        d["relevant_yes_no"] = ""
        d["notes"] = ""
        items.append(d)

    def sort_key(x):
        tier = x.get("tier", "B")
        kind = x.get("tender_kind", "unknown")
        if tier == "A" and kind in ("product", "mixed", "unknown"):
            group = 0
        elif tier == "A" and kind == "service":
            group = 1
        else:
            group = 2

        win_rank = window_order.get(x.get("bid_window", "unknown"), 3)
        htc = x.get("hours_to_close") or 999999
        score = -(x.get("score") or 0)
        return (group, win_rank, htc, score)

    item_rows = [x for x in items if x.get("match_basis") != "location_only"]
    loc_rows = [x for x in items if x.get("match_basis") == "location_only"]

    item_rows.sort(key=sort_key)
    loc_rows.sort(key=sort_key)

    top60 = item_rows[:60]
    loc10 = loc_rows[:10]

    all_rows = top60 + loc10

    fieldnames = [
        "id", "title", "organisation", "tier", "tender_kind", "buyer_match", "buyer_match_label", "match_basis", "item_term", "is_consumable",
        "score", "matched_terms", "hours_to_close", "bid_window",
        "original_end_utc", "closing_date", "is_extended", "extension_days_observed", "auto_extension_limit_utc", "date_check", "extensions_seen",
        "estimated_bid_value_inr", "floor_price_inr_min", "floor_price_inr_max", "buyer_overrides_gem_fields",
        "similar_category", "category_noise_reason",
        "emd_amount_inr", "mse_reserved", "min_turnover_lakh", "years_experience_required", "eligibility", "flags",
        "closing_IST", "link", "relevant_yes_no", "notes"
    ]

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Wrote {len(all_rows)} rows ({len(top60)} item + {len(loc10)} location_only) to {OUT_FILE}")
    return len(all_rows)


if __name__ == "__main__":
    generate()
