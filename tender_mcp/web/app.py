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

import csv
import io
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tender_mcp.config import get_dashboard_credentials, get_dashboard_owners
from tender_mcp.dates import get_hours_to_close, to_ist_display
from tender_mcp.db import (
    VALID_STATUSES,
    get_closing_date_history,
    get_conn,
    get_tender_by_id,
    get_tender_status_history,
    update_tender_status,
)
from tender_mcp.matcher import compute_bid_window

# Verify credentials on module load / startup
USER, PASSWORD = get_dashboard_credentials()
if not USER or not PASSWORD:
    raise RuntimeError(
        "DASHBOARD_USER and DASHBOARD_PASSWORD must be set in .env to start the dashboard."
    )

app = FastAPI(title="Swamina Tender Aggregator Dashboard")
security = HTTPBasic()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def verify_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Verify HTTP Basic credentials in constant time."""
    current_user, current_pw = get_dashboard_credentials()
    if not current_user or not current_pw:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Dashboard credentials unconfigured.",
        )

    is_user_ok = secrets.compare_digest(credentials.username, current_user)
    is_pw_ok = secrets.compare_digest(credentials.password, current_pw)
    if not (is_user_ok and is_pw_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": 'Basic realm="Swamina Tender Aggregator"'},
        )
    return credentials.username


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
        clauses.append("tier = ?")
        params.append(tier)

    if status_filter and status_filter != "all":
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
        clauses.append("match_basis = ?")
        params.append(match_basis)

    if extended_only:
        clauses.append("(is_extended = 1 OR date_check IN ('extended_auto', 'extended_by_corrigendum'))")

    if eligibility and eligibility != "all":
        clauses.append("eligibility = ?")
        params.append(eligibility)

    if q.strip():
        clauses.append("(title LIKE ? OR organisation LIKE ? OR item_category LIKE ? OR matched_terms LIKE ?)")
        term = f"%{q.strip()}%"
        params.extend([term, term, term, term])

    # Base query sorted by closing_date asc
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM tenders {where_sql} ORDER BY closing_date ASC"

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Post-process hours left, window, and min_hours filter
    results = []
    for r in rows:
        htc = get_hours_to_close(r.get("closing_date"))
        r["hours_to_close"] = htc
        r["bid_window"] = compute_bid_window(htc)
        r["closing_ist"] = to_ist_display(r.get("closing_date", ""))

        # Default view: open tenders closing in more than 24 hours
        if min_hours is not None:
            if htc is None or htc < min_hours:
                continue

        # Closing window filter
        if closing_window and closing_window != "all":
            if r["bid_window"] != closing_window:
                continue

        results.append(r)

    return results


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

    # Compute status counts for KPI stat cards
    all_tenders_for_counts = query_filtered_tenders(tier="all", min_hours=None)
    status_counts = {
        "all": len(all_tenders_for_counts),
        "new": 0,
        "interested": 0,
        "bidding": 0,
        "submitted": 0,
        "skipped": 0,
    }
    for t in all_tenders_for_counts:
        st = t.get("status")
        if st in status_counts and st != "all":
            status_counts[st] += 1

    rendered = template.render(
        tenders=tenders,
        total_count=len(tenders),
        health=health,
        filters=export_params,
        export_query=export_query,
        status_counts=status_counts,
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
        "id", "source", "title", "organisation", "tier", "tender_kind", "buyer_match", "buyer_match_label", "status", "owner",
        "relevance_score", "matched_terms", "closing_date", "closing_ist", "hours_to_close",
        "is_extended", "date_check", "extensions_seen", "emd_value", "estimated_bid_value_inr",
        "floor_price_inr_min", "floor_price_inr_max", "buyer_overrides_gem_fields",
        "similar_category", "category_noise_reason", "notes"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for t in tenders:
        if t.get("buyer_match"):
            t["buyer_match_label"] = "Oil/gas buyer, supply tender"
        else:
            t["buyer_match_label"] = ""
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
