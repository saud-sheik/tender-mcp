#!/usr/bin/env python3
"""
scripts/spot_check.py  –  Audit 10 random stored tenders for data quality
========================================================================
Picks 10 random tenders from tenders.db and displays:
  - id
  - title
  - issuer (organisation)
  - closing date
  - direct link
  - manual check URL
  - relevance score & matched keywords

Allows sales engineers at Swamina International to manually cross-verify
stored records against live government portals (CPPP, GeM, HMEL).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender_mcp.db import DB_PATH, get_conn, init_db
from tender_mcp.matcher import score_tender


def get_manual_check_url(tender: dict) -> str:
    """Generate a portal verification URL for manual human checking."""
    source = tender.get("source", "")
    link = tender.get("link", "")
    tid = tender.get("id", "")

    if link and link.startswith("http"):
        return link

    if source == "cppp":
        return "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"
    elif source == "gem":
        bid_no = tid.replace("gem:", "").replace("_", "/")
        return f"https://bidplus.gem.gov.in/all-bids?SearchBid={bid_no}"
    elif source == "hmel":
        return "https://hmeleprocurement.mjunction.in/epspropartner/hmel/login-screen"
    return link or "N/A"


def main() -> int:
    init_db()

    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
        if total == 0:
            print("No tenders currently in database. Run `python scripts/refresh.py` first.")
            return 1

        rows = conn.execute(
            """
            SELECT id, source, title, organisation, category,
                   published_date, closing_date, link, status, relevance_score, raw_json
            FROM tenders
            ORDER BY RANDOM()
            LIMIT 10
            """
        ).fetchall()

    tenders = [dict(r) for r in rows]

    print("=" * 80)
    print(f"  SWAMINA INTERNATIONAL - TENDER QUALITY SPOT CHECK ({len(tenders)} of {total} total)")
    print("=" * 80)

    for i, t in enumerate(tenders, start=1):
        manual_url = get_manual_check_url(t)
        scoring = score_tender(t)
        score = t.get("relevance_score") or scoring["score"]
        terms = scoring["matched_terms"] or ["none"]

        print(f"\n[{i:02d}/10] Tender ID : {t['id']}")
        print(f"      Source    : {t['source'].upper()}")
        print(f"      Title     : {t.get('title') or '(No Title)'}")
        print(f"      Issuer    : {t.get('organisation') or '(Not specified)'}")
        print(f"      Closing   : {t.get('closing_date') or '(Not specified)'}")
        print(f"      Score     : {score}/100 ({scoring['assessment']})")
        print(f"      Keywords  : {', '.join(terms)}")
        print(f"      Doc Link  : {t.get('link') or 'N/A'}")
        print(f"      MANUAL VERIFY URL: {manual_url}")
        print("-" * 80)

    print("\nSpot check complete. Verify any link above in your web browser.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
