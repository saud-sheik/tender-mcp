"""
scrapers/hmel.py  –  HMEL e-procurement (mjunction) scraper
=============================================================
URL: https://hmeleprocurement.mjunction.in/epspropartner/hmel/login-screen

Uses Playwright (async) because the portal is fully JS-rendered.

Login flow:
  1. Navigate to login-screen
  2. Fill username + password from HMEL_USERNAME / HMEL_PASSWORD env vars
  3. Submit form
  4. Check for OTP/CAPTCHA prompt → if found, raise LoginError immediately
  5. On success, save storage state to playwright_state.json for reuse
  6. Navigate to the open/live tenders listing
  7. Extract tender cards

Session handling:
  - On startup we try to load playwright_state.json
  - If the session is still valid (no login redirect), we skip login
  - If we get redirected to login mid-scrape → re-login once → retry
  - Second failure → raise SessionExpiredError with clear message

OTP/CAPTCHA policy:
  If any element matching "otp|captcha" appears after login, we STOP and
  raise LoginError with instructions. We do NOT try to solve or bypass.

HEADLESS mode:
  Set env var HEADLESS=false to watch the browser. Default: true.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOGIN_URL = "https://hmeleprocurement.mjunction.in/epspropartner/hmel/login-screen"
TENDERS_URL = "https://hmeleprocurement.mjunction.in/epspropartner/hmel/tender/open-tenders"
STATE_PATH = Path(__file__).parent.parent / "playwright_state.json"


class LoginError(RuntimeError):
    """Raised when login fails or OTP/CAPTCHA is detected."""


class SessionExpiredError(RuntimeError):
    """Raised when re-login also fails."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_tenders() -> list[dict[str, Any]]:
    """
    Main entry point. Returns list of tender dicts from HMEL.
    Raises LoginError if OTP/CAPTCHA encountered or credentials invalid.
    Raises SessionExpiredError if session cannot be restored.
    """
    from tender_mcp.config import get_hmel_credentials, is_hmel_locked, lock_hmel

    locked, reason = is_hmel_locked()
    if locked:
        logger.warning(
            "hmel: Account lockout protection active (%s) – skipping HMEL until .hmel_locked is removed",
            reason,
        )
        return []

    username, password = get_hmel_credentials()
    if not username or not password:
        logger.warning(
            "hmel: HMEL_USERNAME / HMEL_PASSWORD not configured in .env – skipping HMEL"
        )
        return []

    headless = os.environ.get("HEADLESS", "true").lower() != "false"

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("hmel: playwright not installed – run: playwright install chromium")
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context_kwargs: dict[str, Any] = {
            "user_agent": (
                "TenderAggregator/1.0 (Swamina International; "
                "contact: tender-bot@swamina.example)"
            )
        }

        # Try loading saved state
        if STATE_PATH.exists():
            logger.info("hmel: loading saved browser state from %s", STATE_PATH)
            context_kwargs["storage_state"] = str(STATE_PATH)

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        try:
            # ---------- Attempt with existing session ----------
            tenders = await _navigate_to_tenders(page)
            if tenders is not None:
                return tenders

            # Redirected to login – do fresh login
            logger.info("hmel: session expired; logging in fresh")
            try:
                await _do_login(page, username, password)
            except LoginError as exc:
                lock_hmel(str(exc))
                raise
            except Exception as exc:
                lock_hmel(f"Login failed unexpectedly: {exc}")
                raise LoginError(f"HMEL: Login failed: {exc}") from exc

            await context.storage_state(path=str(STATE_PATH))
            logger.info("hmel: saved new session state to %s", STATE_PATH)

            tenders = await _navigate_to_tenders(page)
            if tenders is None:
                raise SessionExpiredError(
                    "HMEL: After re-login, still redirected to login page. "
                    "Check credentials or if portal has changed."
                )
            return tenders

        finally:
            await context.close()
            await browser.close()


def parse_tender_cards(html: str) -> list[dict[str, Any]]:
    """
    Parse HMEL tender listing HTML (for testing with saved fixtures).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    return _extract_tenders_from_soup(soup, source_url=TENDERS_URL)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _navigate_to_tenders(page: Any) -> list[dict[str, Any]] | None:
    """
    Navigate to the open tenders page.
    Returns None if we're redirected to login (session expired).
    Returns tender list on success.
    """
    await page.goto(TENDERS_URL, wait_until="networkidle", timeout=30000)
    current_url = page.url

    if "login" in current_url.lower():
        logger.info("hmel: redirected to login (url=%s)", current_url)
        return None

    logger.info("hmel: on tenders page (url=%s)", current_url)
    # Wait for tender content to load
    try:
        await page.wait_for_selector(
            "table, .tender-card, .tender-list, [class*=tender]",
            timeout=15000,
        )
    except Exception:
        logger.warning("hmel: timeout waiting for tender selector; parsing anyway")

    html = await page.content()
    tenders = _extract_tenders_from_soup_html(html, TENDERS_URL)
    return tenders


async def _do_login(page: Any, username: str, password: str) -> None:
    """Perform login. Raises LoginError on OTP/CAPTCHA or wrong credentials."""
    logger.info("hmel: navigating to login page")
    await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)

    # Fill username
    try:
        await page.fill('input[name="username"], input[id*="user"], input[type="text"]', username)
    except Exception as exc:
        raise LoginError(f"HMEL: Could not find username field: {exc}") from exc

    # Fill password
    try:
        await page.fill('input[name="password"], input[id*="pass"], input[type="password"]', password)
    except Exception as exc:
        raise LoginError(f"HMEL: Could not find password field: {exc}") from exc

    # Click submit
    try:
        await page.click('button[type="submit"], input[type="submit"], button:has-text("Login")')
    except Exception as exc:
        raise LoginError(f"HMEL: Could not click login button: {exc}") from exc

    # Wait for navigation
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    # Check for OTP / CAPTCHA
    page_text = (await page.content()).lower()
    if re.search(r"\botp\b|\bcaptcha\b|\bone.time\b|\bverification code\b", page_text):
        raise LoginError(
            "HMEL login page is showing an OTP or CAPTCHA prompt. "
            "This cannot be automated. Please:\n"
            "1. Log in manually at https://hmeleprocurement.mjunction.in/epspropartner/hmel/login-screen\n"
            "2. Export session cookies and place them in playwright_state.json, OR\n"
            "3. Set up a tender-alert email subscription as a fallback data source."
        )

    # Check for error messages
    if re.search(r"invalid\s+credentials|login\s+fail|incorrect\s+password|user\s+not\s+found", page_text):
        raise LoginError(
            "HMEL: Login failed – invalid credentials. "
            "Check HMEL_USERNAME and HMEL_PASSWORD in your .env file."
        )

    current_url = page.url
    if "login" in current_url.lower():
        raise LoginError(
            f"HMEL: Still on login page after submit (url={current_url}). "
            "Login may have failed silently."
        )

    logger.info("hmel: login successful (url=%s)", current_url)


def _extract_tenders_from_soup_html(html: str, source_url: str) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    return _extract_tenders_from_soup(soup, source_url)


def _extract_tenders_from_soup(soup: Any, source_url: str) -> list[dict[str, Any]]:
    """Extract tenders from parsed BeautifulSoup object."""
    tenders: list[dict[str, Any]] = []

    # Strategy 1: table rows
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        headers: list[str] = []
        for row in rows:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            texts = [c.get_text(strip=True) for c in cells]

            # Detect header row
            if not headers and any(
                h in " ".join(texts).lower()
                for h in ("tender", "closing", "id", "title", "no.")
            ):
                headers = texts
                continue

            if len(texts) < 2:
                continue

            t = _build_tender_from_row(texts, headers, cells, source_url)
            if t:
                tenders.append(t)

    if tenders:
        logger.info("hmel: extracted %d tenders via table strategy", len(tenders))
        return tenders

    # Strategy 2: generic divs with tender-like content
    for div in soup.find_all(["div", "li", "article"]):
        text = div.get_text(" ", strip=True)
        if len(text) < 20:
            continue
        tid = _regex_find(text, r"Tender\s*(?:No|ID|Number)\s*[:\-]?\s*([A-Z0-9/\-]+)")
        if not tid:
            continue
        t = _build_tender_from_text(text, div, source_url)
        if t:
            tenders.append(t)

    logger.info("hmel: extracted %d tenders via div strategy", len(tenders))
    return tenders


def _build_tender_from_row(
    texts: list[str],
    headers: list[str],
    cells: list[Any],
    source_url: str,
) -> dict[str, Any] | None:
    def col(names: list[str]) -> str:
        for name in names:
            for i, h in enumerate(headers):
                if name.lower() in h.lower() and i < len(texts):
                    return texts[i]
        return ""

    tender_id = col(["tender id", "tender no", "ref no", "no.", "id"]) or texts[0]
    if not tender_id or tender_id.lower() in ("", "s.no", "sl no"):
        return None

    title = col(["title", "description", "work"]) or texts[1] if len(texts) > 1 else tender_id
    closing = col(["closing", "due date", "bid end", "last date"])
    published = col(["publish", "start", "open date", "issue"])
    emd = col(["emd", "earnest"])
    value = col(["value", "estimated", "amount"])

    anchor = None
    for cell in cells:
        a = cell.find("a")
        if a:
            anchor = a
            break

    link = ""
    if anchor:
        href = anchor.get("href", "")
        link = href if href.startswith("http") else f"https://hmeleprocurement.mjunction.in{href}"

    composite_id = f"hmel:{tender_id.replace('/', '_').replace(' ', '_')}"
    return {
        "id": composite_id,
        "source": "hmel",
        "title": title,
        "organisation": "HMEL (Guru Gobind Singh Refineries)",
        "category": col(["category", "type"]),
        "published_date": published,
        "closing_date": closing,
        "opening_date": col(["opening", "open"]),
        "emd_value": emd,
        "tender_value": value,
        "link": link,
        "status": "new",
        "raw": {"source_url": source_url},
    }


def _build_tender_from_text(
    text: str,
    tag: Any,
    source_url: str,
) -> dict[str, Any] | None:
    tender_id = _regex_find(text, r"Tender\s*(?:No|ID|Number)\s*[:\-]?\s*([A-Z0-9/\-]+)")
    if not tender_id:
        return None

    title = _regex_find(text, r"(?:Title|Description|Work)\s*[:\-]?\s*([^\n\|]{5,100})")
    closing = _regex_find(text, r"(?:Closing|Due|Last Date)\s*[:\-]?\s*(\d{1,2}[/-]\w{2,3}[/-]\d{2,4}[^\n\|]*)")
    published = _regex_find(text, r"(?:Published|Issue|Start)\s*[:\-]?\s*(\d{1,2}[/-]\w{2,3}[/-]\d{2,4}[^\n\|]*)")
    emd = _regex_find(text, r"EMD\s*[:\-]?\s*([\d,. ]+(?:Lakhs?|Crore|Rs\.?)?)")
    value = _regex_find(text, r"(?:Tender Value|Estimated Cost)\s*[:\-]?\s*([\d,. ]+(?:Lakhs?|Crore|Rs\.?)?)")

    anchor = tag.find("a")
    link = ""
    if anchor:
        href = anchor.get("href", "")
        link = href if href.startswith("http") else f"https://hmeleprocurement.mjunction.in{href}"

    composite_id = f"hmel:{tender_id.replace('/', '_').replace(' ', '_')}"
    return {
        "id": composite_id,
        "source": "hmel",
        "title": title or tender_id,
        "organisation": "HMEL (Guru Gobind Singh Refineries)",
        "category": "",
        "published_date": published or "",
        "closing_date": closing or "",
        "opening_date": "",
        "emd_value": emd or "",
        "tender_value": value or "",
        "link": link,
        "status": "new",
        "raw": {"source_url": source_url},
    }


def _regex_find(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.I)
    return m.group(1).strip() if m else ""
