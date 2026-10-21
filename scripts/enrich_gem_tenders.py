#!/usr/bin/env python3
"""
scripts/enrich_gem_tenders.py
Enrich all open tier A and B GeM tenders with bid details, extension tracking, and eligibility.
Uses a 2-second delay between downloads and stops immediately on any block/captcha.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender_mcp.db import get_conn, save_gem_pdf_enrichment
from tender_mcp.gem_pdf import download_gem_pdf, parse_gem_pdf, load_profile, GemBlockedError, CACHE_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("enrich_gem")


def enrich_all_open_gem_tenders() -> None:
    profile = load_profile()

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, link, closing_date, bid_number
            FROM tenders
            WHERE source = 'gem'
              AND tier IN ('A', 'B')
              AND (closing_date IS NULL OR closing_date >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+24 hours'))
            ORDER BY closing_date ASC
        """).fetchall()

    logger.info("Found %d open tier A & B GeM tenders to enrich", len(rows))

    success_count = 0
    fail_count = 0

    for idx, r in enumerate(rows, 1):
        tid = r["id"]
        link = r["link"]
        close_iso = r["closing_date"]
        exp_bid_no = r["bid_number"] or tid.replace("gem:", "").replace("_", "/")
        doc_id = link.rstrip("/").split("/")[-1] if link else ""

        if not doc_id:
            logger.warning("[%d/%d] Tender %s has no document id in link: %s", idx, len(rows), tid, link)
            continue

        try:
            pdf_path = download_gem_pdf(
                link=link,
                bid_doc_id=doc_id,
                delay=2.0,
                closing_date_iso=close_iso,
            )
        except GemBlockedError as e:
            logger.error("STOPPING ENRICHMENT: Block/Captcha detected on tender %s: %s", tid, e)
            break
        except Exception as e:
            logger.error("[%d/%d] Error downloading tender %s (%s): %s", idx, len(rows), tid, doc_id, e)
            fail_count += 1
            continue

        if not pdf_path or not pdf_path.exists():
            logger.warning("[%d/%d] PDF not available for tender %s (%s)", idx, len(rows), tid, doc_id)
            fail_count += 1
            continue

        parsed = parse_gem_pdf(pdf_path, expected_bid_number=exp_bid_no)
        save_gem_pdf_enrichment(tid, parsed, profile=profile)
        success_count += 1
        logger.info(
            "[%d/%d] Enriched %s: status=%s, bid_end=%s",
            idx,
            len(rows),
            exp_bid_no,
            parsed.get("pdf_parse_status"),
            parsed.get("bid_end_datetime"),
        )

    logger.info("Enrichment finished. Processed: %d, Failed: %d", success_count, fail_count)


if __name__ == "__main__":
    enrich_all_open_gem_tenders()
