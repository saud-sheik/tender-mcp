"""
scripts/rescore_all_tenders.py
Rescores all tenders in tenders.db using updated keywords, product-supply protection,
narrowed cleaning noise, and category noise rules.
Tracks tier changes for open tenders (closing_date > now).
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timezone
from tender_mcp.db import get_conn
from tender_mcp.matcher import score_tender

def run_rescore():
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, title, category, item_category, similar_category,
                   organisation, tier, relevance_score, category_noise_reason,
                   closing_date, status
            FROM tenders
        """).fetchall()

    print(f"Total tenders to rescore: {len(rows)}")

    tier_changes = []
    updated_count = 0

    with get_conn() as conn:
        for r in rows:
            tid = r["id"]
            title = r["title"] or ""
            cat = r["category"] or ""
            item_cat = r["item_category"] or ""
            sim_cat = r["similar_category"] or ""
            org = r["organisation"] or ""
            old_tier = r["tier"]
            old_score = r["relevance_score"]
            old_noise = r["category_noise_reason"]
            closing = r["closing_date"] or ""

            eval_title = title
            if title.startswith("[Items not listed]") and item_cat:
                eval_title = f"{item_cat} ({title})"

            scoring = score_tender({
                "title": eval_title,
                "category": item_cat or cat,
                "item_category": item_cat or cat,
                "similar_category": sim_cat,
                "organisation": org,
                "status": r["status"],
            })

            new_score = scoring["score"]
            new_tier = scoring["tier"]
            new_kind = scoring["tender_kind"]
            new_terms = scoring["matched_terms_str"]
            new_match_basis = scoring["match_basis"]
            new_noise = scoring.get("category_noise_reason")
            new_buyer_match = scoring.get("buyer_match", 0)

            conn.execute("""
                UPDATE tenders SET
                    relevance_score = ?,
                    tier = ?,
                    tender_kind = ?,
                    matched_terms = ?,
                    match_basis = ?,
                    category_noise_reason = ?,
                    buyer_match = ?
                WHERE id = ?
            """, (new_score, new_tier, new_kind, new_terms, new_match_basis, new_noise, new_buyer_match, tid))
            updated_count += 1

            # Check open tenders for tier change
            is_open = closing > now_iso
            if is_open and old_tier != new_tier:
                new_weak = [t for t in ["pipe", "flowmeter", "flameproof", "skid", "coalescer", "reactor", "lubrication oil", "fccu", "hose"] if any(w.startswith(t) for w in scoring.get("matched_weak", []))]
                if new_buyer_match:
                    reason = "Oil/gas buyer, supply tender"
                elif new_weak:
                    reason = f"New weak term(s) matched: {', '.join(new_weak)} (score {old_score} -> {new_score})"
                elif old_tier == "C" and new_tier in ("A", "B"):
                    reason = f"Score change ({old_score} -> {new_score})"
                elif old_tier == "A" and new_tier == "B":
                    reason = f"Category noise ({new_noise})"
                elif old_tier == "B" and new_tier == "A":
                    reason = f"Oil & gas buyer match / un-capped (score {old_score} -> {new_score})"
                else:
                    reason = f"Score change ({old_score} -> {new_score})"

                tier_changes.append({
                    "id": tid,
                    "title": title,
                    "organisation": org,
                    "old_tier": old_tier,
                    "new_tier": new_tier,
                    "reason": reason,
                    "score": new_score,
                })

        conn.commit()

    print(f"Rescore complete. {updated_count} tenders updated.")
    print(f"Tier changes on open tenders: {len(tier_changes)}")
    for tc in tier_changes:
        print(f"Title: {tc['title']}\nOrganisation: {tc['organisation']}\nOld Tier: {tc['old_tier']} -> New Tier: {tc['new_tier']}\nReason: {tc['reason']}\n")

if __name__ == "__main__":
    run_rescore()
