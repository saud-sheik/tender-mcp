#!/usr/bin/env python3
"""
scripts/refresh.py  –  CLI wrapper for the tender refresh pipeline
===================================================================
Designed for cron use every 3 hours:
    0 */3 * * * /path/to/venv/bin/python /path/to/scripts/refresh.py >> /var/log/tender-refresh.log 2>&1

Usage:
    python scripts/refresh.py
    python scripts/refresh.py --sources cppp,gem
    python scripts/refresh.py --sources hmel --headless false
    python scripts/refresh.py --cppp-pages 5 --gem-pages 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Make the project root importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender_mcp.config import ENV_PATH
from dotenv import load_dotenv

load_dotenv(ENV_PATH)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("scripts.refresh")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh tender database from all portals")
    parser.add_argument(
        "--sources",
        default=None,
        help="Comma-separated sources to refresh (default: cppp,gem,hmel)",
    )
    parser.add_argument(
        "--cppp-pages",
        type=int,
        default=None,
        help="Max pages to fetch from CPPP (default: 40 if backfill else CPPP_MAX_PAGES)",
    )
    parser.add_argument(
        "--gem-pages",
        type=int,
        default=int(os.environ.get("GEM_MAX_PAGES", "3")),
        help="Max pages to fetch from GeM",
    )
    parser.add_argument(
        "--headless",
        default=None,
        help="Override HEADLESS env var for Playwright (true/false)",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        default=False,
        help="Disable CPPP early stop and read up to CPPP_MAX_PAGES",
    )
    args = parser.parse_args()

    # Allow --headless to override env
    if args.headless is not None:
        os.environ["HEADLESS"] = args.headless

    cppp_pages = args.cppp_pages
    if cppp_pages is None:
        cppp_pages = 40 if args.backfill else int(os.environ.get("CPPP_MAX_PAGES", "40"))

    source_list = (
        [s.strip() for s in args.sources.split(",") if s.strip()]
        if args.sources
        else None
    )

    from tender_mcp.refresh import run_refresh_sync

    logger.info(
        "Starting refresh: sources=%s cppp_pages=%d gem_pages=%d backfill=%s",
        source_list or "all",
        cppp_pages,
        args.gem_pages,
        args.backfill,
    )

    summary = run_refresh_sync(
        sources=source_list,
        cppp_max_pages=cppp_pages,
        gem_max_pages=args.gem_pages,
        backfill=args.backfill,
    )

    # Print summary to stdout (cron-friendly)
    print(json.dumps(summary, indent=2))

    total_new = summary.get("total_new", 0)
    total_updated = summary.get("total_updated", 0)
    errors = [
        f"{src}: {info.get('error') or 'Fetched 0 tenders'}"
        for src, info in summary.get("sources", {}).items()
        if info.get("error") or info.get("fetched", 0) == 0
    ]

    logger.info(
        "Refresh complete: new=%d updated=%d total_relevant=%d errors=%d",
        total_new, total_updated, summary.get("total_relevant", 0), len(errors),
    )
    for src, info in summary.get("sources", {}).items():
        logger.info(
            "Source %s summary: fetched=%d valid=%d relevant=%d (score>=min_score) new=%d updated=%d",
            src.upper(),
            info.get("fetched", 0),
            info.get("valid", 0),
            info.get("relevant", 0),
            info.get("new", 0),
            info.get("updated", 0),
        )
    for err in errors:
        logger.error("Source error: %s", err)

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
