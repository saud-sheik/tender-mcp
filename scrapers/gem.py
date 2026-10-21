"""
scrapers/gem.py  –  Government e-Marketplace bid scraper
=========================================================
URL: https://bidplus.gem.gov.in/all-bids

Investigation results (Oct 2026):
  - The all-bids page renders bid data via JavaScript (Ajax POST to same URL)
  - The static HTML skeleton only has category links (filter-bid clearfix divs)
  - Actual bid cards appear after JS loads them into #all-bids-container or similar
  - The POST returns HTML partials, not JSON
  - A simple GET with page_no param returns only the skeleton (200 OK but no bids)

Strategy:
  1. PRIMARY: Use Playwright to render the JS and extract rendered bid cards
  2. FALLBACK: Try direct requests; if bids not found, log a warning

Bid card structure (from rendered DOM):
  - div.bid-list-box containing bid info
  - Bid No: shown as "GEM/2024/B/xxxxx"
  - Organisation / Ministry
  - Start Date, End Date
  - Items/Category text

Terms-of-use note:
  GeM is a Government of India public procurement portal. Public bid listings
  are intended for vendor discovery. No ToS prohibition on programmatic
  reading of public listings was found. We use polite rate limiting.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

BASE_URL = "https://bidplus.gem.gov.in/all-bids"
BID_DETAIL_BASE = "https://bidplus.gem.gov.in/bidlisting"

HEADERS = {
    "User-Agent": (
        "TenderAggregator/1.0 (Swamina International internal tool; "
        "contact: tender-bot@swamina.example)"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://bidplus.gem.gov.in/",
}
REQUEST_DELAY = float(os.environ.get("GEM_REQUEST_DELAY", "2.0"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(html: str, source_url: str = BASE_URL) -> list[dict[str, Any]]:
    """
    Parse GeM rendered-HTML (after JS execution) into tender dicts.
    Handles the rendered bid card structure from Playwright output.
    """
    soup = BeautifulSoup(html, "html.parser")
    tenders: list[dict[str, Any]] = []

    # Strategy 1: rendered bid cards (div.card or #bidCard .card)
    # These appear after JavaScript renders the page
    bid_containers = (
        soup.select("#bidCard div.card")
        or [c for c in soup.find_all("div", class_="card") if c.find("a", class_="bid_no_hover") or "bid" in c.get_text(" ", strip=True).lower()]
        or soup.find_all("div", class_=re.compile(r"bid.?list", re.I))
        or soup.find_all("div", class_=re.compile(r"all.?bid|bid.?box|bid.?item", re.I))
        or soup.find_all("div", attrs={"data-bid-no": True})
    )

    if bid_containers:
        for container in bid_containers:
            t = _parse_rendered_card(container, source_url)
            if t:
                tenders.append(t)

    # Strategy 2: look for GEM/ bid numbers in any div
    if not tenders:
        tenders = _parse_by_gem_numbers(soup, source_url)

    logger.info("gem: parsed %d bids from %s", len(tenders), source_url)
    return tenders


import urllib.parse
from tender_mcp.dates import to_iso8601


async def fetch_pages(
    client: httpx.AsyncClient,
    max_pages: int = 3,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch GeM bids by searching per keyword from the 'strong' and 'weak'
    lists in keywords.json. Deduplicates across keywords by bid number.
    """
    if keywords is None:
        from tender_mcp.matcher import load_keywords
        cfg = load_keywords()
        seen_kw = set()
        keywords = []
        for kw in cfg.get("strong", []) + cfg.get("weak", []):
            clean = kw.strip()
            if clean and clean.lower() not in seen_kw:
                seen_kw.add(clean.lower())
                keywords.append(clean)

    try:
        return await _fetch_via_playwright(max_pages, keywords)
    except ImportError:
        logger.warning("gem: playwright not available; falling back to direct HTTP (bids may be empty)")
        return await _fetch_via_httpx(client, max_pages, keywords)
    except Exception as exc:
        logger.error("gem: playwright fetch failed (%s); trying httpx fallback", exc)
        return await _fetch_via_httpx(client, max_pages, keywords)


# ---------------------------------------------------------------------------
# Playwright-based fetch (primary)
# ---------------------------------------------------------------------------

def _clean_gem_title(candidate: str, bid_no: str = "") -> str:
    """
    Build the title ONLY from the bid's real item names (card details, popover, category).
    If real item text cannot be retrieved, use '[Items not listed] GEM/2026/B/xxxx'.
    Never put the organisation or department into the title.
    """
    clean = candidate.strip()
    if clean.lower().startswith("items:"):
        clean = clean[6:].strip()

    if not clean:
        return f"[Items not listed] {bid_no}" if bid_no else "[Items not listed]"

    # Filter out pure numeric codes (e.g. 4125242564, 4125242294)
    parts = [p.strip() for p in clean.split(",") if p.strip()]
    descriptive = [p for p in parts if not re.match(r"^\d{5,}$", p)]
    if descriptive:
        candidate_text = ", ".join(descriptive)
    else:
        candidate_text = clean

    # If it has no alphabetic word of at least 3 letters
    if not re.search(r"[a-zA-Z]{3,}", candidate_text):
        return f"[Items not listed] {bid_no}" if bid_no else "[Items not listed]"

    return candidate_text


async def _fetch_via_playwright(max_pages: int, keywords: list[str]) -> list[dict[str, Any]]:
    """Use Playwright to search GeM via its search box, sort newest end date, and collect bids."""
    from playwright.async_api import async_playwright
    from tender_mcp.matcher import score_tender

    all_tenders: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    headless_mode = os.environ.get("HEADLESS", "true").lower() != "false"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless_mode)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        page = await context.new_page()

        try:
            logger.info("gem: navigating to %s (headless=%s)", BASE_URL, headless_mode)
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_selector("#searchBid", timeout=20000)

            # Sort by newest / latest end date, not soonest closing
            try:
                sort_btn = await page.query_selector("#currentSort")
                if sort_btn:
                    sort_text = await sort_btn.inner_text()
                    if "Latest First" not in sort_text:
                        await sort_btn.click()
                        await page.wait_for_timeout(400)
                        latest_opt = await page.query_selector('a[data-val="Bid-End-Date-Latest"]')
                        if latest_opt:
                            await latest_opt.click()
                            await page.wait_for_selector("#bidCard div.card, .card", timeout=15000)
                            logger.info("gem: selected sort 'Bid End Date: Latest First'")
            except Exception as e:
                logger.warning("gem: could not set sort to Latest First: %s", e)

            BROAD_KEYWORDS = {
                "petroleum", "gas", "valve", "valves", "pump", "compressor",
                "drilling", "diesel fuel", "atf", "aviation turbine fuel"
            }
            gem_broad_pages = int(os.environ.get("GEM_BROAD_PAGES", str(max_pages)))

            for kw_idx, keyword in enumerate(keywords, start=1):
                logger.info("gem: searching keyword [%d/%d]: '%s'", kw_idx, len(keywords), keyword)
                kw_tenders: list[dict[str, Any]] = []
                new_for_keyword = 0
                pages_read = 0
                total_bids = 0

                try:
                    search_inp = await page.query_selector("#searchBid")
                    if not search_inp:
                        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_selector("#searchBid", timeout=15000)
                        search_inp = await page.query_selector("#searchBid")

                    await search_inp.fill(keyword)
                    search_btn = await page.query_selector("#searchBidRA")
                    if search_btn:
                        await search_btn.click()
                    else:
                        await page.keyboard.press("Enter")

                    # Wait for bid cards to appear
                    try:
                        await page.wait_for_selector(
                            "#bidCard div.card, #bidCard .card, .card, [class*=not-found]",
                            timeout=15000,
                        )
                    except Exception:
                        pass

                    await asyncio.sleep(REQUEST_DELAY)

                    # Check for block or error page
                    body_text = await page.inner_text("body")
                    if "access denied" in body_text.lower() or "captcha" in body_text.lower() or "blocked" in body_text.lower():
                        logger.warning("gem: blocked or captcha encountered on keyword '%s' – stopping", keyword)
                        break

                    # Extract portal's reported total bids from .totalRecord
                    try:
                        total_record_el = await page.query_selector(".totalRecord")
                        if total_record_el:
                            tot_text = await total_record_el.inner_text()
                            m_tot = re.search(r"of\s+([\d,]+)\s+records", tot_text, re.I)
                            if m_tot:
                                total_bids = int(m_tot.group(1).replace(",", ""))
                    except Exception:
                        pass

                    # Parse page 1
                    html = await page.content()
                    source_url = f"{BASE_URL}?SearchBid={urllib.parse.quote_plus(keyword)}"
                    p1_tenders = parse(html, source_url=source_url)
                    pages_read = 1 if p1_tenders else 0
                    if total_bids == 0:
                        total_bids = len(p1_tenders)

                    for t in p1_tenders:
                        kw_tenders.append(t)
                        if t["id"] not in seen_ids:
                            seen_ids.add(t["id"])
                            all_tenders.append(t)
                            new_for_keyword += 1

                    # Determine target pages
                    # Targeted (total <= 250): up to 30 pages
                    # Broad (total > 250 or broad keywords): up to GEM_BROAD_PAGES (default 3)
                    is_broad = keyword.lower() in BROAD_KEYWORDS or total_bids > 250
                    if not is_broad and total_bids > 0:
                        target_pages = min(30, max(1, (total_bids + 9) // 10))
                    elif total_bids > 0:
                        target_pages = min(gem_broad_pages, max(1, (total_bids + 9) // 10))
                    else:
                        target_pages = 1

                    # Paginate remaining pages
                    for p_num in range(2, target_pages + 1):
                        await asyncio.sleep(REQUEST_DELAY)

                        # Find page link or next button
                        page_btn = await page.query_selector(f'a.page-link[href="#page-{p_num}"]')
                        if not page_btn:
                            page_btn = await page.query_selector("a.page-link.next")
                        if not page_btn:
                            logger.info("gem: keyword '%s': no pagination button for page %d – ending", keyword, p_num)
                            break

                        await page_btn.click()
                        try:
                            await page.wait_for_selector("#bidCard div.card, #bidCard .card, .card", timeout=15000)
                        except Exception:
                            pass

                        await asyncio.sleep(0.5)
                        # Check block or error
                        p_body = await page.inner_text("body")
                        if "access denied" in p_body.lower() or "captcha" in p_body.lower() or "blocked" in p_body.lower():
                            logger.warning("gem: blocked on page %d for '%s' – stopping", p_num, keyword)
                            break

                        p_html = await page.content()
                        p_url = f"{BASE_URL}?SearchBid={urllib.parse.quote_plus(keyword)}&page_no={p_num}"
                        p_tenders = parse(p_html, source_url=p_url)
                        if not p_tenders:
                            break

                        pages_read += 1
                        for t in p_tenders:
                            kw_tenders.append(t)
                            if t["id"] not in seen_ids:
                                seen_ids.add(t["id"])
                                all_tenders.append(t)
                                new_for_keyword += 1

                    relevant_count = sum(1 for t in kw_tenders if score_tender(t).get("is_relevant"))
                    logger.info(
                        "gem: keyword '%s': portal total=%d, pages read=%d, fetched=%d, new=%d, relevant=%d",
                        keyword, total_bids, pages_read, len(kw_tenders), new_for_keyword, relevant_count,
                    )

                except Exception as exc:
                    logger.error("gem: error searching keyword '%s': %s", keyword, exc)

        finally:
            await browser.close()

    return all_tenders


# ---------------------------------------------------------------------------
# httpx fallback (static HTML, may return empty if JS required)
# ---------------------------------------------------------------------------

async def _fetch_via_httpx(
    client: httpx.AsyncClient,
    max_pages: int,
    keywords: list[str],
) -> list[dict[str, Any]]:
    all_tenders: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for keyword in keywords:
        for page in range(1, max_pages + 1):
            url = f"{BASE_URL}?SearchBid={urllib.parse.quote_plus(keyword)}&page_no={page}"
            logger.info("gem: [httpx] '%s' page %d → %s", keyword, page, url)

            try:
                resp = await client.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
                resp.raise_for_status()
            except Exception as exc:
                logger.error("gem: HTTP error on page %d: %s", page, exc)
                break

            page_tenders = parse(resp.text, source_url=url)
            if not page_tenders:
                break

            for t in page_tenders:
                if t["id"] not in seen_ids:
                    seen_ids.add(t["id"])
                    all_tenders.append(t)

            await asyncio.sleep(REQUEST_DELAY)

    return all_tenders


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_rendered_card(card: Tag, source_url: str) -> dict[str, Any] | None:
    text = card.get_text(" ", strip=True)

    # 1. Bid number
    bid_a = card.find("a", class_="bid_no_hover")
    bid_no = ""
    if bid_a:
        bid_no = _regex(bid_a.get_text(strip=True), r"(GEM/[\d]+[A-Z]?/[BSRA]/[\d]+)")
    if not bid_no:
        bid_no = (
            _regex(text, r"(GEM/[\d]+[A-Z]?/[BSRA]/[\d]+)")
            or _regex(text, r"Bid\s*No\s*[:\-]?\s*([A-Z0-9/\-]+)")
            or card.get("data-bid-no", "")
        )
    if not bid_no:
        return None

    # 3. Department / Organisation (extracted early for title fallback)
    dept = (
        _regex(text, r"Department Name And Address\s*[:\-]?\s*(.*?)(?=\s*Start Date|\s*End Date|$)")
        or _regex(text, r"(?:Department|Ministry|Organisation)\s*[:\-]?\s*([^\n\|]{5,150})")
    )
    if not dept:
        org_el = card.find(class_=re.compile(r"bid-org|bid_org|department|ministry", re.I))
        if org_el:
            dept = org_el.get_text(strip=True)

    # 2. Items / Title
    items = ""
    items_a = card.find("a", attrs={"data-toggle": "popover"}) or card.find("a", attrs={"data-content": True})
    if items_a and items_a.get("data-content"):
        items = items_a["data-content"].strip()
    if not items:
        col4 = card.find("div", class_="col-md-4")
        if col4:
            for row in col4.find_all("div", class_="row"):
                text_row = row.get_text(strip=True)
                if "Items:" in text_row:
                    items = text_row.replace("Items:", "").strip()
                    break
    if not items:
        items = (
            _regex(text, r"Items\s*[:\-]?\s*(.*?)(?=\s*Quantity|\s*Department|\s*Start Date|\s*End Date|$)")
            or _regex(text, r"(?:Items?|Category)\s*[:\-]?\s*([^\n\|]{5,100})")
        )
    if not items:
        title_el = card.find(class_=re.compile(r"bid-title|bid_title", re.I))
        if title_el:
            items = title_el.get_text(strip=True)

    title = _clean_gem_title(items, bid_no=bid_no)

    # 4. Dates
    start = (
        _regex(text, r"Start Date\s*[:\-]?\s*([0-9]{2}-[0-9]{2}-[0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s+[AP]M)?)")
        or _regex(text, r"(?:Start|From)\s*[:\-]?\s*(\d{2}[/-]\w{2,3}[/-]\d{2,4}[^\n\|]*)")
    )
    end = (
        _regex(text, r"End Date\s*[:\-]?\s*([0-9]{2}-[0-9]{2}-[0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s+[AP]M)?)")
        or _regex(text, r"(?:End|Closing|To)\s*[:\-]?\s*(\d{2}[/-]\w{2,3}[/-]\d{2,4}[^\n\|]*)")
    )

    start_iso = to_iso8601(start) if start else ""
    end_iso = to_iso8601(end) if end else ""

    # Task 3: Skip any bid whose closing time has already passed
    from tender_mcp.dates import is_expired
    if end_iso and is_expired(end_iso):
        logger.debug("gem: skipping expired bid %s (closing=%s)", bid_no, end_iso)
        return None

    # 5. Link
    link = ""
    if bid_a and bid_a.get("href"):
        href = bid_a["href"].strip()
        link = href if href.startswith("http") else f"https://bidplus.gem.gov.in/{href.lstrip('/')}"
    if not link:
        anchor = card.find("a", href=re.compile(r"bidlisting|bid/view|tender|showbidDocument", re.I)) or card.find("a")
        if anchor and isinstance(anchor, Tag) and anchor.get("href"):
            href = anchor["href"].strip()
            link = href if href.startswith("http") else f"https://bidplus.gem.gov.in/{href.lstrip('/')}"
    if not link:
        link = f"{BID_DETAIL_BASE}/{bid_no}"

    composite_id = f"gem:{bid_no.replace('/', '_')}"
    return {
        "id": composite_id,
        "source": "gem",
        "title": title,
        "organisation": dept or "",
        "category": items or "",
        "published_date": start_iso,
        "closing_date": end_iso,
        "opening_date": "",
        "emd_value": "",
        "tender_value": "",
        "link": link,
        "status": "new",
        "raw": {"bid_no": bid_no, "source_url": source_url},
    }


def _parse_by_gem_numbers(soup: BeautifulSoup, source_url: str) -> list[dict[str, Any]]:
    """Scan for GEM/YYYY/B/NNNNN patterns in any rendered text."""
    tenders: list[dict[str, Any]] = []
    seen: set[str] = set()
    text = soup.get_text(" ")

    # GEM bid numbers follow: GEM/YYYY/B/NNNNN or GEM/YYYY/S/NNNNN etc.
    for m in re.finditer(r"(GEM/\d{4}/[BSRA]/\d+)", text):
        bid_no = m.group(1)
        if bid_no in seen:
            continue
        seen.add(bid_no)
        composite_id = f"gem:{bid_no.replace('/', '_')}"
        tenders.append({
            "id": composite_id,
            "source": "gem",
            "title": f"[Items not listed] {bid_no}",
            "organisation": "",
            "category": "",
            "published_date": "",
            "closing_date": "",
            "opening_date": "",
            "emd_value": "",
            "tender_value": "",
            "link": f"{BID_DETAIL_BASE}/{bid_no}",
            "status": "new",
            "raw": {"bid_no": bid_no, "source_url": source_url},
        })
    return tenders


def _regex(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.I)
    return m.group(1).strip() if m else ""
