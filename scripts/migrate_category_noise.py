"""
scripts/migrate_category_noise.py
Migrates tenders.db:
1. Adds similar_category and category_noise_reason columns via init_db().
2. Re-parses cached PDFs for all enriched bids (no network downloads).
3. Extracts similar_category and checks pdf_category_noise.
4. If item_category or similar_category matches pdf_category_noise, caps score at 30
   and sets category_noise_reason = "buyer category: <matched text>".
5. Updates tier according to existing tier logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import re
from tender_mcp.db import get_conn, init_db, save_gem_pdf_enrichment
from tender_mcp.gem_pdf import parse_gem_pdf

CACHE_DIR = Path("cache/bids")

def migrate_and_rescore():
    init_db()
    
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, link, bid_number FROM tenders WHERE pdf_parse_status != 'none'"
        ).fetchall()
        
    print(f"Found {len(rows)} enriched tenders to check.")
    reparsed_count = 0
    noise_count = 0
    
    for r in rows:
        tid = r["id"]
        link = r["link"] or ""
        bid_no = r["bid_number"]
        
        # Link typically has format: .../showbidDocument/<doc_id>
        doc_id = link.rstrip("/").split("/")[-1]
        pdf_path = CACHE_DIR / f"{doc_id}.pdf"
        if not pdf_path.exists():
            # Try by numeric id from tid or link
            m = re.search(r"\d+", doc_id)
            if m:
                pdf_path = CACHE_DIR / f"{m.group(0)}.pdf"
                
        if not pdf_path.exists():
            print(f"Warning: cached PDF not found for {tid} at {pdf_path}")
            continue
            
        parsed = parse_gem_pdf(pdf_path, expected_bid_number=bid_no)
        if parsed.get("pdf_parse_status") in ("ok", "partial"):
            save_gem_pdf_enrichment(tid, parsed)
            reparsed_count += 1
            with get_conn() as conn:
                check = conn.execute(
                    "SELECT similar_category, category_noise_reason, relevance_score, tier FROM tenders WHERE id = ?",
                    (tid,)
                ).fetchone()
                if check and check["category_noise_reason"]:
                    noise_count += 1
                    print(f"Noise hit on {tid}: {check['category_noise_reason']} (score={check['relevance_score']}, tier={check['tier']})")
                    
    print(f"Migration complete: {reparsed_count} reparsed, {noise_count} noise categories flagged.")

if __name__ == "__main__":
    migrate_and_rescore()
