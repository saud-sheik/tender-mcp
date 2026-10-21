"""
scrapers/cppp.py  –  Central Public Procurement Portal scraper
===============================================================
URL: https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata

Table columns (confirmed from live HTML, Oct 2026):
  0  Sl.No
  1  e-Published Date
  2  Bid Submission Closing Date
  3  Tender Opening Date
  4  Title/Ref.No./Tender Id  (contains <a href="…"> link)
  5  Organisation Name
  6  Corrigendum

Pagination:
  The page URLs are base64-encoded in the ?url= query param.
  Decode e.g. aHR0c...  → https://...?page=2
  We just iterate the decoded ?page=N form directly.

CAPTCHA:
  The search form (POST) requires an image CAPTCHA.
  The default GET listing at /cpppdata has NO captcha – it just returns
  the first page of all active tenders sorted by published date desc.
  We therefore use GET-only, never the search form.

Terms-of-use note:
  CPPP is a Government of India public portal. The page carries
  <meta name="robots" content="noindex"> (suppresses search-engine indexing)
  but does NOT forbid programmatic reading of public data. No robots.txt
  was found (404). We access only the public listing, politely.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"
HEADERS = {
    "User-Agent": (
        "TenderAggregator/1.0 (Swamina International internal tool; "
        "contact: tender-bot@swamina.example)"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_DELAY = 2.0  # seconds between pages


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(html: str, source_url: str = BASE_URL) -> list[dict[str, Any]]:
    """
    Parse CPPP tender table HTML into a list of tender dicts.
    Works on both live pages and saved fixture HTML.
    Extracts the real tender reference/ID number from the row.
    """
    from tender_mcp.dates import to_iso8601

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "table"}) or soup.find(
        "table", {"class": "list_table"}
    ) or soup.find("table")
    if not table:
        logger.warning("cppp: no table found in HTML (len=%d)", len(html))
        return []

    tenders: list[dict[str, Any]] = []
    for tbody in table.find_all("tbody") or [table]:
        for row in tbody.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            # col 4: anchor with title + link
            link_cell = cells[4]
            anchor = link_cell.find("a")
            link = anchor["href"].strip() if anchor and anchor.get("href") else ""
            # Make absolute
            if link and link.startswith("/"):
                link = "https://eprocure.gov.in" + link

            anchor_text = anchor.get_text(strip=True) if anchor else ""
            full_cell_text = link_cell.get_text(" ", strip=True)

            # Table column is: Title/Ref.No./Tender Id
            # Format: <a href="...">Title</a>/Ref.No./Tender Id
            remainder = (
                full_cell_text[len(anchor_text):].strip().lstrip("/")
                if full_cell_text.startswith(anchor_text)
                else full_cell_text
            )
            ref_no, sep, tender_id = remainder.rpartition("/")
            ref_no = ref_no.strip()
            tender_id = tender_id.strip()

            # Real tender reference / ID number from the row (not URL token)
            if tender_id.isdigit() and len(tender_id) <= 3 and ref_no:
                real_id = f"{ref_no}_{tender_id}"
            else:
                real_id = tender_id or ref_no or _extract_id_from_link(link) or _hash_title(anchor_text)
            composite_id = f"cppp:{real_id}"

            # Convert dates to ISO 8601
            published_date = to_iso8601(cells[1].get_text(strip=True))
            closing_date = to_iso8601(cells[2].get_text(strip=True))
            opening_date = to_iso8601(cells[3].get_text(strip=True))

            title = anchor_text or full_cell_text or ref_no

            row_data: dict[str, Any] = {
                "id": composite_id,
                "source": "cppp",
                "title": title,
                "organisation": cells[5].get_text(strip=True),
                "published_date": published_date,
                "closing_date": closing_date,
                "opening_date": opening_date,
                "corrigendum": cells[6].get_text(strip=True) if len(cells) > 6 else "",
                "link": link,
                "status": "new",
                "category": "",
                "emd_value": "",
                "tender_value": "",
                "raw": {
                    "sl_no": cells[0].get_text(strip=True),
                    "title_full": full_cell_text,
                    "anchor_text": anchor_text,
                    "ref_no": ref_no,
                    "tender_id": tender_id,
                    "source_url": source_url,
                },
            }
            tenders.append(row_data)

    logger.info("cppp: parsed %d tenders from %s", len(tenders), source_url)
    return tenders


from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    reraise=True,
)
async def _fetch_url_with_retry(client: httpx.AsyncClient, url: str) -> httpx.Response:
    resp = await client.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp


import base64
import urllib.parse


async def fetch_pages(
    client: httpx.AsyncClient,
    max_pages: int = 40,
    is_already_stored: Any = None,
    backfill: bool = False,
    stop_after_stored_pages: int = 2,
) -> list[dict[str, Any]]:
    """
    Fetch up to *max_pages* of CPPP active tenders with exponential backoff retry.
    Normal run: stops early only after *stop_after_stored_pages* consecutive pages
    whose rows are all already stored.
    backfill=True: the already-stored early stop is disabled; reads up to max_pages.
    Returns list of tender dicts.
    """
    all_tenders: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    consecutive_stored_pages = 0

    for page in range(1, max_pages + 1):
        if page == 1:
            url = BASE_URL
        else:
            target_url = f"{BASE_URL}?page={page}"
            b64_str = base64.b64encode(target_url.encode()).decode()
            url = f"{BASE_URL}?url={urllib.parse.quote_plus(b64_str)}"

        logger.info("cppp: fetching page %d → %s", page, url)

        try:
            resp = await _fetch_url_with_retry(client, url)
        except Exception as exc:
            logger.error("cppp: failed to fetch page %d after retries: %s", page, exc)
            break

        page_tenders = parse(resp.text, source_url=url)
        stored_count = (
            sum(1 for t in page_tenders if is_already_stored(t["id"]))
            if is_already_stored
            else 0
        )
        logger.info(
            "cppp: page %d rows_parsed=%d rows_new=%d rows_already_stored=%d",
            page,
            len(page_tenders),
            len(page_tenders) - stored_count,
            stored_count,
        )

        if not page_tenders:
            logger.info("cppp: no tenders on page %d – stopping", page)
            break

        new_on_page = 0
        for t in page_tenders:
            if t["id"] not in seen_ids:
                seen_ids.add(t["id"])
                all_tenders.append(t)
                new_on_page += 1

        if new_on_page == 0:
            # Portal returned the same rows as an earlier page in this run (end of pagination)
            logger.info("cppp: all tenders on page %d already seen in this run – stopping", page)
            break

        if is_already_stored and stored_count == len(page_tenders):
            consecutive_stored_pages += 1
        else:
            consecutive_stored_pages = 0

        if not backfill and consecutive_stored_pages >= stop_after_stored_pages:
            logger.info(
                "cppp: %d consecutive pages fully stored in DB (last page %d) – stopping early",
                consecutive_stored_pages,
                page,
            )
            break

        if page < max_pages:
            await asyncio.sleep(REQUEST_DELAY)

    return all_tenders


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_id_from_link(link: str) -> str:
    """
    Extract a stable unique token from the CPPP tendersfullview URL.
    URL pattern: /cppp/tendersfullview/<base64-encoded-id>A13h1<more>
    We take the first base64 segment as the ID key.
    """
    m = re.search(r"/tendersfullview/([A-Za-z0-9+/=]+)", link)
    if m:
        return m.group(1)[:32]  # truncate to keep IDs manageable
    return ""


def _hash_title(title: str) -> str:
    return hashlib.sha1(title.encode()).hexdigest()[:12]
