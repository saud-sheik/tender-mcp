"""
tender_mcp/refresh.py  –  Core refresh orchestrator
=====================================================
Called by both the MCP server (refresh_tenders tool) and the CLI script
(scripts/refresh.py) to pull new data from all three sources and persist
it to the database.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
import re
from typing import Any
from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from tender_mcp.db import (
    init_db,
    log_refresh_end,
    log_refresh_start,
    upsert_tender,
    get_previous_refresh_stats,
)
from tender_mcp.config import LOG_FILE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> datetime | None:
    if not date_str or not date_str.strip():
        return None
    cleaned = re.sub(r"\s+", " ", date_str.strip())
    formats = [
        "%d-%b-%Y %I:%M %p",
        "%d-%m-%Y %I:%M %p",
        "%d-%m-%Y %H:%M",
        "%d-%b-%Y %H:%M",
        "%d-%b-%Y",
        "%d-%m-%Y",
        "%d/%m/%Y %I:%M %p",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue

    m = re.search(r"(\d{2})[-/](\d{2}|\w{3})[-/](\d{4})", cleaned)
    if m:
        d_str = m.group(0)
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(d_str, fmt)
            except ValueError:
                continue
    return None


def validate_tender(tender: dict[str, Any]) -> tuple[bool, str | None]:
    """
    Validate a tender record before ingest.
    Returns (is_valid, skip_reason).
    Rejects:
      - Empty tender id
      - Empty title
      - Closing date more than 1 day in the past
    """
    tid = str(tender.get("id") or "").strip()
    if not tid:
        return False, "missing tender id"

    title = str(tender.get("title") or "").strip()
    if not title:
        return False, "missing tender title"

    closing_str = str(tender.get("closing_date") or "").strip()
    if closing_str:
        closing_dt = _parse_date(closing_str)
        if closing_dt:
            # Compare with now - 1 day
            now = datetime.now()
            cutoff = now - timedelta(days=1)
            if closing_dt < cutoff:
                return False, f"closing date ({closing_str}) is more than 1 day in the past"

    return True, None


# ---------------------------------------------------------------------------
# Per-source fetchers
# ---------------------------------------------------------------------------

async def _run_cppp(client: httpx.AsyncClient, max_pages: int, backfill: bool = False) -> list[dict[str, Any]]:
    from scrapers.cppp import fetch_pages
    from tender_mcp.db import get_tender_by_id

    def _is_stored(tender_id: str) -> bool:
        return get_tender_by_id(tender_id) is not None

    return await fetch_pages(client, max_pages=max_pages, is_already_stored=_is_stored, backfill=backfill)


async def _run_gem(client: httpx.AsyncClient, max_pages: int) -> list[dict[str, Any]]:
    from scrapers.gem import fetch_pages
    return await fetch_pages(client, max_pages=max_pages)


async def _run_hmel() -> tuple[list[dict[str, Any]], str | None]:
    from scrapers.hmel import fetch_tenders, LoginError, SessionExpiredError
    try:
        tenders = await fetch_tenders()
        return tenders, None
    except (LoginError, SessionExpiredError) as exc:
        logger.warning("hmel: %s", exc)
        return [], str(exc)
    except Exception as exc:
        logger.exception("hmel: unexpected error: %s", exc)
        return [], str(exc)


# ---------------------------------------------------------------------------
# Main refresh entry point
# ---------------------------------------------------------------------------

async def refresh_all(
    sources: list[str] | None = None,
    cppp_max_pages: int | None = None,
    gem_max_pages: int | None = None,
    backfill: bool = False,
) -> dict[str, Any]:
    """
    Refresh tenders from all (or specified) sources.
    Performs ingest validation, layout-change monitoring, and writes health summary to refresh.log.
    """
    init_db()

    if cppp_max_pages is None:
        cppp_max_pages = int(os.environ.get("CPPP_MAX_PAGES", "40"))
    if gem_max_pages is None:
        gem_max_pages = int(os.environ.get("GEM_MAX_PAGES", "3"))

    sources_to_run = sources or ["cppp", "gem", "hmel"]
    started_at = datetime.now(timezone.utc).isoformat()

    summary: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": "",
        "sources": {},
        "total_fetched": 0,
        "total_relevant": 0,
        "total_new": 0,
        "total_updated": 0,
    }

    from tender_mcp.matcher import get_min_score
    min_score = get_min_score()

    async with httpx.AsyncClient() as client:
        for source in sources_to_run:
            rowid = log_refresh_start(source, started_at)
            src_fetched = 0
            src_relevant = 0
            src_new = 0
            src_updated = 0
            src_error = None

            try:
                if source == "cppp":
                    raw_tenders = await _run_cppp(client, cppp_max_pages, backfill=backfill)
                elif source == "gem":
                    raw_tenders = await _run_gem(client, gem_max_pages)
                elif source == "hmel":
                    raw_tenders, src_error = await _run_hmel()
                else:
                    logger.warning("refresh: unknown source '%s'", source)
                    continue

                src_fetched = len(raw_tenders)

                # Ingest validation
                valid_tenders = []
                for t in raw_tenders:
                    is_valid, reason = validate_tender(t)
                    if not is_valid:
                        logger.info(
                            "validation: skipped tender '%s' from %s (%s)",
                            t.get("id", "UNKNOWN"), source, reason,
                        )
                        continue
                    valid_tenders.append(t)

                for t in valid_tenders:
                    _, is_new = upsert_tender(t)
                    if is_new:
                        src_new += 1
                    else:
                        src_updated += 1
                    if t.get("relevance_score", 0) >= min_score:
                        src_relevant += 1

                logger.info(
                    "refresh: %s → fetched=%d valid=%d relevant=%d (score>=%d) new=%d updated=%d",
                    source, src_fetched, len(valid_tenders), src_relevant, min_score, src_new, src_updated,
                )

                if src_fetched == 0:
                    logger.error("refresh: source '%s' fetched 0 tenders", source)
                    if not src_error:
                        src_error = f"Source '{source}' fetched 0 tenders"

                # Check if 0 tenders returned twice in a row
                prev = get_previous_refresh_stats(source, rowid)
                if prev and prev.get("fetched_count", 0) == 0 and src_fetched == 0:
                    logger.error(
                        "possible site layout change: source '%s' returned 0 tenders twice in a row",
                        source,
                    )

            except Exception as exc:
                src_error = str(exc)
                logger.exception("refresh: source '%s' failed: %s", source, exc)

            finished = datetime.now(timezone.utc).isoformat()
            log_refresh_end(rowid, finished, src_fetched, src_new, src_updated, src_error)

            summary["sources"][source] = {
                "fetched": src_fetched,
                "valid": len(valid_tenders),
                "relevant": src_relevant,
                "new": src_new,
                "updated": src_updated,
                "error": src_error,
            }
            summary["total_fetched"] += src_fetched
            summary["total_relevant"] += src_relevant
            summary["total_new"] += src_new
            summary["total_updated"] += src_updated

    finished_at = datetime.now(timezone.utc).isoformat()
    summary["finished_at"] = finished_at

    # Write health summary to refresh.log
    _write_health_log(summary)

    return summary


def _write_health_log(summary: dict[str, Any]) -> None:
    """Append a human-readable health summary to refresh.log."""
    try:
        lines = [
            f"--- REFRESH HEALTH SUMMARY [{summary['started_at']} -> {summary['finished_at']}] ---",
        ]
        for src, data in summary.get("sources", {}).items():
            err_str = f" | ERROR: {data['error']}" if data.get("error") else ""
            lines.append(
                f"  [{src.upper()}] fetched={data['fetched']} valid={data.get('valid', data['fetched'])} relevant={data.get('relevant', 0)} new={data['new']} updated={data['updated']}{err_str}"
            )
        lines.append(
            f"  [TOTAL] fetched={summary['total_fetched']} relevant={summary.get('total_relevant', 0)} new={summary['total_new']} updated={summary['total_updated']}\n"
        )
        report = "\n".join(lines)

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(report)
    except Exception as exc:
        logger.error("Failed to write to refresh.log: %s", exc)


def run_refresh_sync(
    sources: list[str] | None = None,
    cppp_max_pages: int | None = None,
    gem_max_pages: int | None = None,
    backfill: bool = False,
) -> dict[str, Any]:
    """Synchronous wrapper for use in non-async contexts."""
    return asyncio.run(
        refresh_all(sources=sources, cppp_max_pages=cppp_max_pages, gem_max_pages=gem_max_pages, backfill=backfill)
    )
