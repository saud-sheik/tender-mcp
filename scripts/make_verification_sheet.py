#!/usr/bin/env python3
"""
scripts/make_verification_sheet.py
Generate exports/verification_sheet.csv with randomly sampled open tenders:
  - 5 open tier A
  - 2 open tier B
  - 1 open tier A closing soonest
  - 3 open CPPP
Columns:
  id, source, bid_number, portal_link, title, organisation, closing_date_IST,
  quantity, emd, min_turnover_lakh, years_experience, portal_closing, portal_emd,
  portal_other, match_yes_no, notes
"""
from __future__ import annotations

import csv
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import tender_mcp.db as db_mod
from tender_mcp.dates import to_ist_display


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_mod.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def format_emd(r: sqlite3.Row) -> str:
    if r["emd_amount_inr"]:
        return f"₹{r['emd_amount_inr']:,.0f}"
    if r["emd_amount_inr_min"] or r["emd_amount_inr_max"]:
        return f"₹{r['emd_amount_inr_min'] or 0:,.0f} - ₹{r['emd_amount_inr_max'] or 0:,.0f}"
    if r["emd_value"]:
        return str(r["emd_value"])
    return ""


def generate_verification_sheet(output_path: Path | str | None = None) -> Path:
    out_file = Path(output_path) if output_path else PROJECT_ROOT / "exports" / "verification_sheet.csv"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        # 1. 1 open tier A closing soonest
        soonest_row = conn.execute(
            """
            SELECT * FROM tenders
            WHERE tier = 'A' AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            ORDER BY closing_date ASC LIMIT 1
            """
        ).fetchone()

        soonest_id = soonest_row["id"] if soonest_row else None

        # 2. 5 open tier A (excluding the soonest one if present to avoid duplicate)
        if soonest_id:
            tier_a_rows = conn.execute(
                """
                SELECT * FROM tenders
                WHERE tier = 'A' AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                  AND id != ?
                ORDER BY RANDOM() LIMIT 5
                """,
                (soonest_id,),
            ).fetchall()
        else:
            tier_a_rows = conn.execute(
                """
                SELECT * FROM tenders
                WHERE tier = 'A' AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                ORDER BY RANDOM() LIMIT 5
                """
            ).fetchall()

        # 3. 2 open tier B
        tier_b_rows = conn.execute(
            """
            SELECT * FROM tenders
            WHERE tier = 'B' AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            ORDER BY RANDOM() LIMIT 2
            """
        ).fetchall()

        # 4. 3 open CPPP
        cppp_rows = conn.execute(
            """
            SELECT * FROM tenders
            WHERE source = 'cppp' AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            ORDER BY RANDOM() LIMIT 3
            """
        ).fetchall()

    rows_to_write = []
    if soonest_row:
        rows_to_write.append(soonest_row)
    rows_to_write.extend(tier_a_rows)
    rows_to_write.extend(tier_b_rows)
    rows_to_write.extend(cppp_rows)

    fieldnames = [
        "id",
        "source",
        "bid_number",
        "portal_link",
        "title",
        "organisation",
        "closing_date_IST",
        "quantity",
        "emd",
        "min_turnover_lakh",
        "years_experience",
        "portal_closing",
        "portal_emd",
        "portal_other",
        "match_yes_no",
        "notes",
    ]

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for r in rows_to_write:
            bid_no = r["bid_number"]
            if not bid_no and ":" in str(r["id"]):
                raw_id = r["id"].split(":", 1)[1]
                if r["source"] == "gem" and raw_id.startswith("GEM_"):
                    bid_no = raw_id.replace("_", "/")
                else:
                    bid_no = raw_id
            if not bid_no:
                bid_no = r["id"]

            writer.writerow({
                "id": r["id"],
                "source": r["source"],
                "bid_number": bid_no,
                "portal_link": r["link"] or "",
                "title": r["title"] or "",
                "organisation": r["organisation"] or "",
                "closing_date_IST": to_ist_display(r["closing_date"]) if r["closing_date"] else "",
                "quantity": r["total_quantity"] if r["total_quantity"] is not None else "",
                "emd": format_emd(r),
                "min_turnover_lakh": r["min_turnover_lakh"] if r["min_turnover_lakh"] is not None else "",
                "years_experience": r["years_experience_required"] if r["years_experience_required"] is not None else "",
                "portal_closing": "",
                "portal_emd": "",
                "portal_other": "",
                "match_yes_no": "",
                "notes": "",
            })

    print(f"Wrote {len(rows_to_write)} tenders to {out_file}")
    return out_file


if __name__ == "__main__":
    generate_verification_sheet()
