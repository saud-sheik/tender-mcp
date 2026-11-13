"""
tender_mcp/web/app.py  –  FastAPI Web Dashboard for Tender MCP
============================================================
Provides:
  - List view with filters (tier, kind, source, match_basis, extended, window, eligibility, status, free text)
  - Detail view for enriched fields, date history, eligibility, status updates, owner assignment, notes
  - CSV export of filtered view
  - Health strip and /health status endpoint
  - HTTP Basic Auth with DASHBOARD_USER and DASHBOARD_PASSWORD
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tender_mcp.config import (
    get_dashboard_credentials,
    get_dashboard_owners,
    get_hmel_credentials,
    set_hmel_credentials,
    is_hmel_locked,
    unlock_hmel,
    mask_credential,
    get_slack_webhook_url,
    set_slack_webhook_url,
    get_auto_refresh_interval,
    set_auto_refresh_interval,
)
from tender_mcp.dates import get_hours_to_close, to_ist_display
from tender_mcp.db import (
    VALID_STATUSES,
    get_closing_date_history,
    get_conn,
    get_tender_by_id,
    get_tender_status_history,
    update_tender_status,
    toggle_tender_watched,
    set_tender_watched,
    get_watched_tenders,
)
from tender_mcp.matcher import compute_bid_window, is_single_tender, check_mr_title_and_category

logger = logging.getLogger(__name__)

# Verify credentials on module load / startup
USER, PASSWORD = get_dashboard_credentials()
if not USER or not PASSWORD:
    raise RuntimeError(
        "DASHBOARD_USER and DASHBOARD_PASSWORD must be set in .env to start the dashboard."
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _auto_refresh_task
    _auto_refresh_task = asyncio.create_task(_background_scheduler_loop())
    yield
    if _auto_refresh_task and not _auto_refresh_task.done():
        _auto_refresh_task.cancel()
        try:
            await _auto_refresh_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Swamina Tender Aggregator Dashboard", lifespan=lifespan)

SESSION_COOKIE = "tender_session"

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)
jinja_env.filters["to_ist"] = to_ist_display


def create_session_token(username: str) -> str:
    """Create a signed HMAC session token for dashboard login."""
    _, current_pw = get_dashboard_credentials()
    secret = (current_pw or "swamina-tender-default-secret").encode("utf-8")
    ts = str(int(datetime.now(timezone.utc).timestamp()))
    payload = f"{username}:{ts}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_session_token(token: str) -> str | None:
    """Validate signed HMAC session token and enforce expiration (14 days)."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        user, ts_str, sig = parts
        current_user, current_pw = get_dashboard_credentials()
        secret = (current_pw or "swamina-tender-default-secret").encode("utf-8")
        payload = f"{user}:{ts_str}"
        expected_sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected_sig):
            return None
        if not current_user or not secrets.compare_digest(user, current_user):
            return None
        ts = int(ts_str)
        now = int(datetime.now(timezone.utc).timestamp())
        if (now - ts) > (14 * 86400) or ts > (now + 300):
            return None
        return user
    except Exception:
        return None


class NeedsLoginException(Exception):
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


@app.exception_handler(NeedsLoginException)
async def handle_needs_login(request: Request, exc: NeedsLoginException):
    """Redirect browser cleanly to the login page without triggering native HTTP Basic modal."""
    target = exc.next_url or "/"
    login_url = f"/login?next={urlencode({'next': target})}" if target != "/" else "/login"
    return RedirectResponse(url=login_url, status_code=status.HTTP_303_SEE_OTHER)


def verify_auth(request: Request) -> str:
    """
    Authenticate request via session cookie or Authorization header.
    Redirects unauthenticated browser requests to /login.
    Does NOT return WWW-Authenticate header to prevent native browser modal.
    """
    current_user, current_pw = get_dashboard_credentials()
    if not current_user or not current_pw:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Dashboard credentials unconfigured.",
        )

    # 1. Check session cookie
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user = verify_session_token(token)
        if user:
            return user

    # 2. Check Authorization header (HTTP Basic Auth for API/scripts/tests)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            encoded = auth_header.split(" ", 1)[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            u, p = decoded.split(":", 1)
            if secrets.compare_digest(u, current_user) and secrets.compare_digest(p, current_pw):
                return u
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    # 3. Not authenticated
    accept = request.headers.get("accept", "")
    if "text/html" in accept or request.method == "GET":
        next_url = request.url.path
        if request.url.query:
            next_url += f"?{request.url.query}"
        raise NeedsLoginException(next_url=next_url)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def get_health_data() -> dict[str, Any]:
    """Calculate health status and per-source refresh stats from refresh_log."""
    now_utc = datetime.now(timezone.utc)
    with get_conn() as conn:
        latest_success = conn.execute(
            """
            SELECT finished_at FROM refresh_log
            WHERE finished_at IS NOT NULL AND (error_msg IS NULL OR error_msg = '')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

        latest_logs = conn.execute(
            """
            SELECT source, started_at, finished_at, fetched_count, new_count, updated_count, error_msg
            FROM refresh_log ORDER BY id DESC LIMIT 20
            """
        ).fetchall()

    age_hours: float = 999.0
    last_run_ist = "Never"
    if latest_success and latest_success[0]:
        try:
            finished_str = latest_success[0]
            if finished_str.endswith("Z"):
                finished_dt = datetime.fromisoformat(finished_str.rstrip("Z")).replace(tzinfo=timezone.utc)
            else:
                finished_dt = datetime.fromisoformat(finished_str)
                if finished_dt.tzinfo is None:
                    finished_dt = finished_dt.replace(tzinfo=timezone.utc)
            age_hours = round((now_utc - finished_dt).total_seconds() / 3600.0, 1)
            last_run_ist = to_ist_display(finished_str)
        except Exception:
            pass

    # Group by source
    source_stats = {
        "cppp": {"fetched": 0, "new": 0, "errors": None},
        "gem": {"fetched": 0, "new": 0, "errors": None},
        "hmel": {"fetched": 0, "new": 0, "errors": None},
    }
    seen_sources = set()
    for row in latest_logs:
        src = row["source"]
        if src in source_stats and src not in seen_sources:
            seen_sources.add(src)
            source_stats[src] = {
                "fetched": row["fetched_count"] or 0,
                "new": row["new_count"] or 0,
                "errors": row["error_msg"],
            }

    is_stale = age_hours > 6.0
    return {
        "age_hours": age_hours,
        "is_stale": is_stale,
        "last_run_ist": last_run_ist,
        "sources": source_stats,
    }


def query_filtered_tenders(
    tier: str = "A",
    status_filter: str = "all",
    tender_kind: str = "all",
    source: str = "all",
    closing_window: str = "all",
    eligibility: str = "all",
    match_basis: str = "all",
    extended_only: bool = False,
    q: str = "",
    min_hours: float | None = 24.0,
) -> list[dict[str, Any]]:
    """Execute SQL query with filters for list view and CSV export."""
    clauses = []
    params = []

    if tier and tier != "all":
        if tier == "buyer_match":
            clauses.append("buyer_match = 1")
        elif tier == "watched":
            clauses.append("is_watched = 1")
        else:
            clauses.append("tier = ?")
            params.append(tier)

    if status_filter and status_filter != "all":
        if status_filter == "watched":
            clauses.append("is_watched = 1")
        else:
            clauses.append("status = ?")
            params.append(status_filter)

    if tender_kind:
        if tender_kind in ("product", "products", "products only", "products_only"):
            clauses.append("tender_kind = 'product'")
        elif tender_kind in ("service", "services", "services only", "services_only"):
            clauses.append("tender_kind = 'service'")
        elif tender_kind in ("both", "all", ""):
            pass
        else:
            clauses.append("tender_kind = ?")
            params.append(tender_kind)

    if source and source != "all":
        clauses.append("source = ?")
        params.append(source)

    if match_basis and match_basis != "all":
        if match_basis == "buyer_match":
            clauses.append("buyer_match = 1")
        else:
            clauses.append("match_basis = ?")
            params.append(match_basis)

    if extended_only:
        clauses.append("(is_extended = 1 OR date_check IN ('extended_auto', 'extended_by_corrigendum'))")

    if eligibility and eligibility != "all":
        clauses.append("eligibility = ?")
        params.append(eligibility)

    if q.strip():
        clauses.append("(title LIKE ? OR organisation LIKE ? OR item_category LIKE ? OR matched_terms LIKE ? OR bid_number LIKE ? OR id LIKE ?)")
        term = f"%{q.strip()}%"
        params.extend([term, term, term, term, term, term])

    # Base query sorted by closing_date asc
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM tenders {where_sql} ORDER BY closing_date ASC"

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Post-process hours left, window, and min_hours filter
    # Review lane ("buyer_match") shows all open tenders regardless of 24h cutoff
    effective_min_hours = 0.0 if (tier == "buyer_match" or match_basis == "buyer_match") else min_hours
    results = []
    for r in rows:
        htc = get_hours_to_close(r.get("closing_date"))
        r["hours_to_close"] = htc
        r["bid_window"] = compute_bid_window(htc)
        r["closing_ist"] = to_ist_display(r.get("closing_date", ""))
        r["is_single_tender"] = is_single_tender(r)

        mr_info = check_mr_title_and_category(r.get("title"), r.get("item_category"), r.get("pdf_parse_status"))
        r["is_mr_title"] = mr_info["is_mr_title"]
        r["pdf_item_category"] = mr_info["pdf_item_category"]
        r["is_generic_category"] = mr_info["is_generic_category"]

        # Default view: open tenders closing in more than 24 hours (or open if effective_min_hours is 0.0)
        if effective_min_hours is not None:
            if htc is None or htc < effective_min_hours:
                continue

        # Closing window filter
        if closing_window and closing_window != "all":
            if r["bid_window"] != closing_window:
                continue

        for c in ("emd_value", "tender_value", "possible_latest_end_utc"):
            r.pop(c, None)

        results.append(r)

    return results


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Lightweight liveness probe for container orchestrators (Render, Docker, Kubernetes)."""
    return {"status": "ok"}


@app.get("/health")
def health_endpoint() -> JSONResponse:
    """Return 200 only if the last successful refresh is under 6 hours old, else 503."""
    data = get_health_data()
    status_code = status.HTTP_200_OK if not data["is_stale"] else status.HTTP_503_SERVICE_UNAVAILABLE
    payload = {
        "status": "healthy" if not data["is_stale"] else "unhealthy",
        "last_refresh_age_hours": data["age_hours"],
        "last_refresh_ist": data["last_run_ist"],
        "sources": data["sources"],
    }
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = Query("/"),
    error: str = Query(""),
) -> Response:
    """Render modern login page, or redirect to home if already authenticated."""
    token = request.cookies.get(SESSION_COOKIE)
    if token and verify_session_token(token):
        return RedirectResponse(url=next if (next and next.startswith("/")) else "/", status_code=status.HTTP_303_SEE_OTHER)

    current_user, _ = get_dashboard_credentials()
    template = jinja_env.get_template("login.html")
    rendered = template.render(
        next_url=next or "/",
        error=error,
        configured_user=current_user,
        username=request.query_params.get("username", ""),
    )
    return HTMLResponse(rendered)


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
) -> Response:
    """Validate credentials, set secure session cookie, and redirect to destination."""
    current_user, current_pw = get_dashboard_credentials()
    is_user_ok = bool(current_user and secrets.compare_digest(username.strip(), current_user))
    is_pw_ok = bool(current_pw and secrets.compare_digest(password.strip(), current_pw))

    if not (is_user_ok and is_pw_ok):
        template = jinja_env.get_template("login.html")
        rendered = template.render(
            next_url=next or "/",
            error="Invalid username or password. Please try again.",
            configured_user=current_user,
            username=username.strip(),
        )
        return HTMLResponse(content=rendered, status_code=status.HTTP_401_UNAUTHORIZED)

    token = create_session_token(current_user)
    target = next if (next and next.startswith("/")) else "/"
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=14 * 86400,
        path="/",
    )
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    """Clear session cookie and redirect to login page."""
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def list_page(
    request: Request,
    tier: str = Query("A"),
    status: str = Query("all"),
    tender_kind: str = Query("both"),
    source: str = Query("all"),
    closing_window: str = Query("all"),
    eligibility: str = Query("all"),
    match_basis: str = Query("all"),
    extended_only: int = Query(0),
    q: str = Query(""),
    min_hours: float = Query(24.0),
    username: str = Depends(verify_auth),
) -> HTMLResponse:
    """List tenders with filters."""
    is_ext = bool(extended_only)
    tenders = query_filtered_tenders(
        tier=tier,
        status_filter=status,
        tender_kind=tender_kind,
        source=source,
        closing_window=closing_window,
        eligibility=eligibility,
        match_basis=match_basis,
        extended_only=is_ext,
        q=q,
        min_hours=min_hours,
    )

    health = get_health_data()
    template = jinja_env.get_template("list.html")

    # Build query string for CSV export link
    export_params = {
        "tier": tier,
        "status": status,
        "tender_kind": tender_kind,
        "source": source,
        "closing_window": closing_window,
        "eligibility": eligibility,
        "match_basis": match_basis,
        "extended_only": 1 if is_ext else 0,
        "q": q,
        "min_hours": min_hours,
    }
    export_query = urlencode(export_params)

    # Compute status counts for KPI stat cards (OPEN tenders closing in the future)
    all_stored = query_filtered_tenders(tier="all", min_hours=None)
    open_tenders = [t for t in all_stored if t.get("hours_to_close") is not None and t.get("hours_to_close") >= 0.0]
    status_counts = {
        "all": len(open_tenders),
        "total_stored": len(all_stored),
        "new": 0,
        "interested": 0,
        "bidding": 0,
        "submitted": 0,
        "skipped": 0,
        "watched": 0,
    }
    for t in open_tenders:
        st = t.get("status")
        if st in status_counts and st not in ("all", "total_stored", "watched"):
            status_counts[st] += 1
        if t.get("is_watched"):
            status_counts["watched"] += 1

    rendered = template.render(
        tenders=tenders,
        total_count=len(tenders),
        health=health,
        filters=export_params,
        export_query=export_query,
        status_counts=status_counts,
        current_user=username,
    )
    return HTMLResponse(rendered)


@app.get("/export")
def export_csv(
    tier: str = Query("all"),
    status: str = Query("all"),
    tender_kind: str = Query("both"),
    source: str = Query("all"),
    closing_window: str = Query("all"),
    eligibility: str = Query("all"),
    match_basis: str = Query("all"),
    extended_only: int = Query(0),
    q: str = Query(""),
    min_hours: float | None = Query(None),
    username: str = Depends(verify_auth),
) -> StreamingResponse:
    """Export current filtered view to CSV."""
    is_ext = bool(extended_only)
    tenders = query_filtered_tenders(
        tier=tier,
        status_filter=status,
        tender_kind=tender_kind,
        source=source,
        closing_window=closing_window,
        eligibility=eligibility,
        match_basis=match_basis,
        extended_only=is_ext,
        q=q,
        min_hours=min_hours,
    )

    output = io.StringIO()
    fieldnames = [
        "id", "source", "title", "organisation", "tier", "tender_kind", "is_watched", "single_tender", "buyer_match", "buyer_match_label", "status", "owner",
        "relevance_score", "matched_terms", "closing_date", "closing_ist", "hours_to_close",
        "is_extended", "date_check", "extensions_seen", "emd_display", "estimated_bid_value_inr",
        "floor_price_inr_min", "floor_price_inr_max", "buyer_overrides_gem_fields", "buyer_overrides_badge",
        "similar_category", "category_noise_reason", "notes"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for t in tenders:
        t["single_tender"] = 1 if (t.get("is_single_tender") or is_single_tender(t)) else 0
        if t.get("buyer_match"):
            t["buyer_match_label"] = "Oil/gas buyer, supply tender"
        else:
            t["buyer_match_label"] = ""
        if t.get("emd_amount_inr"):
            t["emd_display"] = f"₹{t['emd_amount_inr']:,.0f}"
        elif t.get("emd_amount_inr_min") and t.get("emd_amount_inr_max") and t.get("emd_amount_inr_min") == t.get("emd_amount_inr_max"):
            t["emd_display"] = f"₹{t['emd_amount_inr_min']:,.0f} per schedule"
        elif t.get("emd_amount_inr_min") or t.get("emd_amount_inr_max"):
            t["emd_display"] = f"₹{t.get('emd_amount_inr_min', 0):,.0f} - ₹{t.get('emd_amount_inr_max', 0):,.0f}"
        else:
            t["emd_display"] = "Nil / Exempt"

        if t.get("buyer_overrides_gem_fields"):
            t["buyer_overrides_badge"] = "EMD/turnover in buyer documents"
        else:
            t["buyer_overrides_badge"] = ""
        writer.writerow(t)

    output.seek(0)
    filename = f"tenders_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/tenders/{tender_id:path}", response_class=HTMLResponse)
def detail_page(
    tender_id: str,
    request: Request,
    username: str = Depends(verify_auth),
) -> HTMLResponse:
    """Detail view for a tender."""
    tender = get_tender_by_id(tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="Tender not found")

    # Parse JSON lists safely
    flags = []
    if tender.get("flags"):
        try:
            flags = json.loads(tender["flags"])
        except Exception:
            flags = []

    if tender.get("buyer_overrides_gem_fields"):
        override_msg = "EMD / turnover set in buyer documents, check them before relying on these numbers"
        if override_msg not in flags:
            flags.append(override_msg)

    eligibility_reasons = []
    if tender.get("eligibility_reasons"):
        try:
            eligibility_reasons = json.loads(tender["eligibility_reasons"])
        except Exception:
            eligibility_reasons = []

    emd_schedules = []
    if tender.get("emd_schedules"):
        try:
            emd_schedules = json.loads(tender["emd_schedules"])
        except Exception:
            emd_schedules = []

    floor_price_schedules = []
    if tender.get("floor_price_schedules"):
        try:
            floor_price_schedules = json.loads(tender["floor_price_schedules"])
        except Exception:
            floor_price_schedules = []

    pbg_months_per_lot = []
    if tender.get("pbg_months_per_lot"):
        try:
            pbg_months_per_lot = json.loads(tender["pbg_months_per_lot"])
        except Exception:
            pbg_months_per_lot = []

    tender["emd_schedules"] = emd_schedules
    tender["floor_price_schedules"] = floor_price_schedules
    tender["pbg_months_per_lot"] = pbg_months_per_lot
    tender["hours_to_close"] = get_hours_to_close(tender.get("closing_date"))
    tender["closing_ist"] = to_ist_display(tender.get("closing_date", ""))
    tender["original_end_ist"] = to_ist_display(tender.get("original_end_utc", ""))
    tender["auto_extension_limit_ist"] = to_ist_display(tender.get("auto_extension_limit_utc", ""))
    tender["first_seen_ist"] = to_ist_display(tender.get("first_seen_at", ""))
    tender["last_seen_ist"] = to_ist_display(tender.get("last_seen_at", ""))
    tender["is_single_tender"] = is_single_tender(tender)
    mr_info = check_mr_title_and_category(tender.get("title"), tender.get("item_category"), tender.get("pdf_parse_status"))
    tender["is_mr_title"] = mr_info["is_mr_title"]
    tender["pdf_item_category"] = mr_info["pdf_item_category"]
    tender["is_generic_category"] = mr_info["is_generic_category"]

    for c in ("emd_value", "tender_value", "possible_latest_end_utc"):
        tender.pop(c, None)

    bid_no = tender.get("bid_no") or tender.get("bid_number")
    raw = tender.get("raw")
    if not bid_no and isinstance(raw, dict):
        bid_no = raw.get("bid_no") or raw.get("bid_number") or raw.get("tender_id")
    if not bid_no and ":" in str(tender.get("id", "")):
        raw_id = tender["id"].split(":", 1)[1]
        if tender.get("source") == "gem" and raw_id.startswith("GEM_"):
            bid_no = raw_id.replace("_", "/")
        else:
            bid_no = raw_id
    tender["bid_number"] = bid_no or tender.get("id", "")

    status_history = get_tender_status_history(tender_id)
    closing_history = get_closing_date_history(tender_id)
    owners = get_dashboard_owners()
    health = get_health_data()

    template = jinja_env.get_template("detail.html")
    rendered = template.render(
        tender=tender,
        flags=flags,
        eligibility_reasons=eligibility_reasons,
        status_history=status_history,
        closing_history=closing_history,
        owners=owners,
        health=health,
        current_user=username,
    )
    return HTMLResponse(rendered)


@app.post("/tenders/{tender_id:path}/update")
def update_status(
    tender_id: str,
    status: str | None = Form(None),
    owner: str | None = Form(None),
    notes: str | None = Form(None),
    username: str = Depends(verify_auth),
) -> RedirectResponse:
    """Handle status, owner, and notes update from dashboard form."""
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    updated = update_tender_status(
        tender_id=tender_id,
        new_status=status,
        owner=owner,
        notes=notes,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Tender not found")

    return RedirectResponse(url=f"/tenders/{tender_id}", status_code=303)


# ---------------------------------------------------------------------------
# Settings & Portal Credentials Management
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    message: str | None = None,
    msg_type: str | None = None,
    user_auth: str = Depends(verify_auth),
) -> HTMLResponse:
    """Settings page for configuring portal credentials, especially HMEL/mjunction."""
    template = jinja_env.get_template("settings.html")
    health = get_health_data()

    with get_conn() as conn:
        rows = conn.execute("SELECT source, count(*) FROM tenders GROUP BY source").fetchall()
        counts_dict = {str(r[0]).lower(): r[1] for r in rows}

    counts = {
        "gem": counts_dict.get("gem", 0),
        "cppp": counts_dict.get("cppp", 0),
        "hmel": counts_dict.get("hmel", 0) + counts_dict.get("hmel_email", 0) + counts_dict.get("hmel (email)", 0),
    }

    from scrapers.hmel import STATE_PATH
    hmel_user, hmel_pass = get_hmel_credentials()
    is_locked, lock_reason = is_hmel_locked()
    has_session = STATE_PATH.exists()

    hmel_info = {
        "configured": bool(hmel_user and hmel_pass),
        "username_masked": mask_credential(hmel_user) if hmel_user else "",
        "raw_username": hmel_user,
        "is_locked": is_locked,
        "lock_reason": lock_reason,
        "has_session": has_session,
    }

    slack_url = get_slack_webhook_url()
    slack_info = {
        "configured": bool(slack_url),
        "url_masked": mask_credential(slack_url) if slack_url else "",
        "raw_url": slack_url,
    }

    scheduler_minutes = get_auto_refresh_interval()
    scheduler_info = {
        "interval_minutes": scheduler_minutes,
        "is_active": scheduler_minutes > 0,
    }

    rendered = template.render(
        health=health,
        counts=counts,
        hmel=hmel_info,
        slack=slack_info,
        scheduler=scheduler_info,
        message=message,
        msg_type=msg_type or "info",
        current_user=user_auth,
    )
    return HTMLResponse(rendered)


@app.post("/settings/hmel")
def update_hmel_settings(
    username: str = Form(""),
    password: str = Form(""),
    user_auth: str = Depends(verify_auth),
) -> RedirectResponse:
    """Save HMEL credentials to local .env and environment."""
    from urllib.parse import quote_plus

    if not username.strip() and not password.strip():
        msg = quote_plus("No credentials provided.")
        return RedirectResponse(url=f"/settings?message={msg}&msg_type=error", status_code=303)

    set_hmel_credentials(username=username, password=password)
    msg = quote_plus("HMEL mjunction credentials saved successfully to .env.")
    return RedirectResponse(url=f"/settings?message={msg}&msg_type=success", status_code=303)


@app.post("/settings/hmel/unlock")
def unlock_hmel_settings(
    user_auth: str = Depends(verify_auth),
) -> RedirectResponse:
    """Clear .hmel_locked lockout protection."""
    from urllib.parse import quote_plus
    unlocked = unlock_hmel()
    if unlocked:
        msg = quote_plus("HMEL account lockout protection cleared.")
        msg_type = "success"
    else:
        msg = quote_plus("No active HMEL lockout file found.")
        msg_type = "info"
    return RedirectResponse(url=f"/settings?message={msg}&msg_type={msg_type}", status_code=303)


@app.post("/settings/hmel/clear-session")
def clear_session_settings(
    user_auth: str = Depends(verify_auth),
) -> RedirectResponse:
    """Remove playwright_state.json to force a fresh login on next scrape."""
    from scrapers.hmel import STATE_PATH
    from urllib.parse import quote_plus
    if STATE_PATH.exists():
        STATE_PATH.unlink(missing_ok=True)
        msg = quote_plus("Saved browser session cleared. Next scraper run will perform a fresh login.")
        msg_type = "info"
    else:
        msg = quote_plus("No saved session file to clear.")
        msg_type = "info"
    return RedirectResponse(url=f"/settings?message={msg}&msg_type={msg_type}", status_code=303)


@app.post("/settings/hmel/refresh")
async def refresh_hmel_settings(
    user_auth: str = Depends(verify_auth),
) -> RedirectResponse:
    """Trigger HMEL scraper and ingest live tenders into database."""
    from tender_mcp.refresh import _run_hmel
    from tender_mcp.db import upsert_tender
    from urllib.parse import quote_plus

    tenders, err = await _run_hmel()
    if err:
        msg = quote_plus(f"HMEL scrape failed: {err}")
        return RedirectResponse(url=f"/settings?message={msg}&msg_type=error", status_code=303)

    new_count = 0
    updated_count = 0
    for t in tenders:
        _, is_new = upsert_tender(t)
        if is_new:
            new_count += 1
        else:
            updated_count += 1

    msg = quote_plus(f"HMEL fetch complete! Retrieved {len(tenders)} tenders ({new_count} new, {updated_count} updated).")
    return RedirectResponse(url=f"/settings?message={msg}&msg_type=success", status_code=303)


# ---------------------------------------------------------------------------
# Watchlist Endpoint
# ---------------------------------------------------------------------------

@app.post("/tenders/{tender_id:path}/toggle-watch")
def toggle_watch(
    tender_id: str,
    redirect_url: str | None = Form(None),
    user_auth: str = Depends(verify_auth),
) -> Response:
    """Toggle is_watched status on a tender. Returns JSON or redirects."""
    try:
        new_state = toggle_tender_watched(tender_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Tender not found")

    if redirect_url:
        return RedirectResponse(url=redirect_url, status_code=303)
    return JSONResponse({"success": True, "tender_id": tender_id, "is_watched": new_state})


# ---------------------------------------------------------------------------
# Slack & Notification Settings
# ---------------------------------------------------------------------------

@app.post("/settings/slack")
def update_slack_settings(
    webhook_url: str = Form(""),
    test_alert: int = Form(0),
    user_auth: str = Depends(verify_auth),
) -> RedirectResponse:
    """Save Slack webhook URL and optionally send a test alert."""
    from urllib.parse import quote_plus
    from tender_mcp.notifications import send_slack_message

    webhook_url = webhook_url.strip()
    if webhook_url:
        set_slack_webhook_url(webhook_url)

    if bool(test_alert):
        target = webhook_url or get_slack_webhook_url()
        if not target:
            msg = quote_plus("Cannot send test alert: No Slack webhook URL configured.")
            return RedirectResponse(url=f"/settings?message={msg}&msg_type=error", status_code=303)
        ok = send_slack_message(
            "🔔 *[Swamina Tender Aggregator]* Test alert received successfully! Instant Slack notifications are active.",
            webhook_url=target,
        )
        if ok:
            msg = quote_plus("Slack webhook saved and test alert sent successfully!")
            return RedirectResponse(url=f"/settings?message={msg}&msg_type=success", status_code=303)
        else:
            msg = quote_plus("Saved webhook, but test message delivery failed. Please verify the URL.")
            return RedirectResponse(url=f"/settings?message={msg}&msg_type=error", status_code=303)

    msg = quote_plus("Slack webhook settings saved successfully.")
    return RedirectResponse(url=f"/settings?message={msg}&msg_type=success", status_code=303)


# ---------------------------------------------------------------------------
# Background Auto-Refresh Scheduler & Endpoints
# ---------------------------------------------------------------------------

@app.post("/settings/scheduler")
def update_scheduler_settings(
    interval_minutes: int = Form(0),
    user_auth: str = Depends(verify_auth),
) -> RedirectResponse:
    """Configure auto-refresh background scheduler interval."""
    from urllib.parse import quote_plus

    set_auto_refresh_interval(interval_minutes)
    if interval_minutes > 0:
        msg = quote_plus(f"Auto-refresh scheduler enabled: will refresh all portals every {interval_minutes} minutes.")
    else:
        msg = quote_plus("Auto-refresh scheduler disabled (manual refresh only).")
    return RedirectResponse(url=f"/settings?message={msg}&msg_type=success", status_code=303)


@app.post("/settings/refresh-all")
async def refresh_all_settings(
    user_auth: str = Depends(verify_auth),
) -> RedirectResponse:
    """Trigger full refresh of GeM, CPPP, and HMEL immediately."""
    from tender_mcp.refresh import refresh_all
    from urllib.parse import quote_plus

    try:
        summary = await refresh_all()
        total_f = summary.get("total_fetched", 0)
        total_n = summary.get("total_new", 0)
        total_u = summary.get("total_updated", 0)
        msg = quote_plus(f"Full refresh complete: {total_f} tenders processed ({total_n} new, {total_u} updated).")
        return RedirectResponse(url=f"/settings?message={msg}&msg_type=success", status_code=303)
    except Exception as exc:
        msg = quote_plus(f"Full refresh failed: {exc}")
        return RedirectResponse(url=f"/settings?message={msg}&msg_type=error", status_code=303)


_auto_refresh_task: asyncio.Task | None = None

async def _background_scheduler_loop() -> None:
    """Periodically triggers refresh_all() if AUTO_REFRESH_INTERVAL_MINUTES > 0."""
    from tender_mcp.refresh import refresh_all
    logger.info("Background auto-refresh scheduler started.")
    while True:
        try:
            mins = get_auto_refresh_interval()
            if mins > 0:
                await asyncio.sleep(mins * 60)
                if get_auto_refresh_interval() > 0:
                    logger.info("Auto-refreshing all portals via background scheduler (interval %d min)...", mins)
                    await refresh_all()
            else:
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("Background auto-refresh loop cancelled.")
            break
        except Exception as exc:
            logger.error("Background auto-refresh encountered error: %s", exc)
            await asyncio.sleep(60)



