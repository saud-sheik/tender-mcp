#!/usr/bin/env python3
"""
scripts/send_digest.py  –  Tender Alerts Email & Slack Digest
=============================================================
Sends or previews daily tender digest:
  - Section 1: New tier A tenders since last digest (closing in > 24h)
  - Section 2: Tier A closing within 72 hours but not under 24
  - Section 3: Deadlines moved later since last digest

Safety constraints:
  - Real send strictly requires DIGEST_ENABLED=true AND --send flag AND all SMTP keys.
  - Otherwise refuses with error explanation and exits non-zero.
  - Times in header and lines stated in IST.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import logging
from pathlib import Path
import smtplib
import sqlite3
import sys
from typing import Any
import urllib.request

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import tender_mcp.db as db_mod
from tender_mcp.config import (
    get_smtp_config,
    get_slack_webhook_url,
    get_digest_enabled,
    get_digest_min_hours,
    get_digest_first_window_hours,
)
from tender_mcp.dates import to_ist_display, get_hours_to_close

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_mod.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_last_sent_at() -> str | None:
    """Read ISO timestamp of last successfully sent digest from digest_state table."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM digest_state WHERE key = 'last_sent_at'").fetchone()
        return row[0] if row and row[0] else None


def set_last_sent_at(timestamp_iso: str) -> None:
    """Store ISO timestamp of successful digest transmission in digest_state table."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO digest_state (key, value) VALUES ('last_sent_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (timestamp_iso,),
        )
        conn.commit()


def build_digest_data(min_hours: int = 24) -> dict[str, Any]:
    """
    Query DB for:
      1. New tier A tenders since last digest (closing in > min_hours)
      2. Tier A tenders closing within 72h but not under 24h
      3. Deadlines moved later since last digest
    """
    last_sent = get_last_sent_at()
    is_first_run = False
    first_window_hours = get_digest_first_window_hours()
    if not last_sent:
        is_first_run = True
        last_sent = (datetime.now(timezone.utc) - timedelta(hours=first_window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        # 1. New tier A tenders since last digest closing in > min_hours
        new_tier_a_rows = conn.execute(
            """
            SELECT * FROM tenders
            WHERE tier = 'A'
              AND first_seen_at >= ?
              AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+' || ? || ' hours')
            ORDER BY closing_date ASC
            """,
            (last_sent, str(min_hours)),
        ).fetchall()
        new_tier_a = [dict(r) for r in new_tier_a_rows]

        # 2. Tier A tenders closing within 72 hours but not under min_hours (24h)
        closing_soon_rows = conn.execute(
            """
            SELECT * FROM tenders
            WHERE tier = 'A'
              AND closing_date != ''
              AND closing_date > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+' || ? || ' hours')
              AND closing_date <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+72 hours')
            ORDER BY closing_date ASC
            """,
            (str(min_hours),),
        ).fetchall()
        closing_soon = [dict(r) for r in closing_soon_rows]

        # 3. Tenders with deadline moved later since last digest
        extended_rows = conn.execute(
            """
            SELECT t.*, c.old_value, c.new_value, c.seen_at
            FROM tenders t
            JOIN closing_date_history c ON t.id = c.tender_id
            WHERE c.new_value > c.old_value AND c.seen_at >= ?
            ORDER BY c.seen_at DESC
            """,
            (last_sent,),
        ).fetchall()
        extended = [dict(r) for r in extended_rows]

    return {
        "last_sent": last_sent,
        "is_first_run": is_first_run,
        "first_window_hours": first_window_hours,
        "min_hours": min_hours,
        "new_tier_a": new_tier_a,
        "closing_soon": closing_soon,
        "extended": extended,
    }


def format_tender_line(t: dict[str, Any], extra: str = "") -> str:
    """Format one tender line in IST."""
    htc = get_hours_to_close(t.get("closing_date"))
    htc_str = f"{round(htc, 1)}h left" if htc is not None else "N/A"
    ist_time = to_ist_display(t.get("closing_date", ""))

    emd_str = t.get("emd_value") or ""
    if t.get("emd_amount_inr"):
        emd_str = f"₹{t['emd_amount_inr']:,.0f}"
    elif t.get("emd_amount_inr_min") or t.get("emd_amount_inr_max"):
        emd_str = f"₹{t.get('emd_amount_inr_min', 0):,.0f} - ₹{t.get('emd_amount_inr_max', 0):,.0f}"
    if not emd_str:
        emd_str = "Nil / Exempt"

    score = t.get("relevance_score", 0)
    link = t.get("link") or f"https://bidplus.gem.gov.in/showbidDocument/{t.get('id', '').split(':')[-1]}"
    title = t.get("title", "Untitled")
    org = t.get("organisation", "N/A")

    labels = []
    if t.get("match_basis") == "location_only":
        labels.append("location only")
    if t.get("buyer_match"):
        labels.append("Oil/gas buyer, supply tender")
    label_str = f" [{', '.join(labels)}]" if labels else ""

    return (
        f"- {title}{label_str}\n"
        f"  Org: {org} | Score: {score} | EMD: {emd_str}\n"
        f"  Closing: {ist_time} ({htc_str}){extra}\n"
        f"  Link: {link}"
    )


def format_digest_text(data: dict[str, Any]) -> str:
    """Format full plain text digest with IST times and all three sections."""
    now_utc = datetime.now(timezone.utc)
    now_ist_str = to_ist_display(now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    last_sent_ist = to_ist_display(data.get("last_sent", ""))

    if data.get("is_first_run"):
        window_line = f" Window: Since {last_sent_ist} (first run: last {data.get('first_window_hours', 24)} hours)"
    else:
        window_line = f" Window: Since last digest at {last_sent_ist}"

    lines = [
        "================================================================================",
        " SWAMINA INTERNATIONAL - TENDER ALERT DIGEST",
        f" Generated: {now_ist_str}",
        window_line,
        f" Minimum Closing Threshold: {data.get('min_hours', 24)} Hours (DIGEST_MIN_HOURS)",
        "================================================================================\n",
    ]

    # Section 1
    lines.append(f"--- 1. NEW TIER A TENDERS SINCE LAST DIGEST ({len(data['new_tier_a'])}) ---")
    if data["new_tier_a"]:
        for t in data["new_tier_a"]:
            lines.append(format_tender_line(t) + "\n")
    else:
        lines.append("none\n")

    # Section 2
    min_h = data.get("min_hours", 24)
    lines.append(f"--- 2. TIER A CLOSING WITHIN 72 HOURS (NOT UNDER {min_h} HOURS) ({len(data['closing_soon'])}) ---")
    if data["closing_soon"]:
        for t in data["closing_soon"]:
            lines.append(format_tender_line(t) + "\n")
    else:
        lines.append("none\n")

    # Section 3
    lines.append(f"--- 3. DEADLINES MOVED LATER SINCE LAST DIGEST ({len(data['extended'])}) ---")
    if data["extended"]:
        for t in data["extended"]:
            old_ist = to_ist_display(t.get("old_value", ""))
            extra = f" [Extended from {old_ist}]"
            lines.append(format_tender_line(t, extra=extra) + "\n")
    else:
        lines.append("none\n")

    lines.append("--------------------------------------------------------------------------------")
    lines.append("Deadlines can be extended. Confirm the live deadline on GeM before bidding.")
    lines.append("================================================================================")
    return "\n".join(lines)


def send_email(subject: str, body: str, smtp_cfg: dict[str, str]) -> None:
    """Send digest email via smtplib."""
    host = smtp_cfg["SMTP_HOST"]
    port = int(smtp_cfg.get("SMTP_PORT") or 587)
    user = smtp_cfg["SMTP_USER"]
    password = smtp_cfg["SMTP_PASSWORD"]
    from_addr = smtp_cfg["DIGEST_FROM"]
    to_addrs = [addr.strip() for addr in smtp_cfg["DIGEST_TO"].split(",") if addr.strip()]

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            if user and password:
                server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)


def send_slack(text: str, webhook_url: str) -> None:
    """Send digest notification to Slack webhook."""
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            logger.warning("Slack webhook returned HTTP %d", resp.status)


def main() -> None:
    default_min = get_digest_min_hours()
    parser = argparse.ArgumentParser(description="Send or preview tender alerts digest.")
    parser.add_argument("--dry-run", action="store_true", help="Print digest preview and do not send email or slack.")
    parser.add_argument("--send", action="store_true", help="Send digest via email and slack (requires DIGEST_ENABLED=true and SMTP keys).")
    parser.add_argument("--min-hours", type=int, default=default_min, help=f"Minimum hours to close threshold (default: {default_min}).")
    args = parser.parse_args()

    # Safety checks
    if args.dry_run:
        data = build_digest_data(min_hours=args.min_hours)
        digest_text = format_digest_text(data)
        print("=== DRY RUN MODE: DIGEST PREVIEW ===")
        print(digest_text)
        print("=== END DRY RUN ===")
        return

    if not args.send:
        print(
            "Refused: Neither --send nor --dry-run was specified.\n"
            "Usage:\n"
            "  python3 scripts/send_digest.py --dry-run   (Preview output safely)\n"
            "  python3 scripts/send_digest.py --send      (Attempt live transmission)",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate DIGEST_ENABLED switch
    if not get_digest_enabled():
        print(
            "Refused: DIGEST_ENABLED=false in configuration.\n"
            "Live transmission is blocked by safety switch. Set DIGEST_ENABLED=true in .env to allow sending.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate SMTP keys
    smtp_cfg = get_smtp_config()
    missing_smtp = [k for k in ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "DIGEST_FROM", "DIGEST_TO"] if not smtp_cfg.get(k)]
    if missing_smtp:
        print(
            f"Refused: Missing required SMTP keys in .env: {', '.join(missing_smtp)}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Generate content
    data = build_digest_data(min_hours=args.min_hours)
    digest_text = format_digest_text(data)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subject = f"[Tender Alert] Swamina Daily Digest - {datetime.now(timezone.utc).strftime('%d %b %Y')}"

    try:
        send_email(subject, digest_text, smtp_cfg)
        print("Email digest sent successfully.")
    except Exception as exc:
        print(f"Failed to send email: {exc}", file=sys.stderr)
        sys.exit(1)

    slack_url = get_slack_webhook_url()
    if slack_url:
        try:
            send_slack(digest_text, slack_url)
            print("Slack notification sent successfully.")
        except Exception as exc:
            logger.error("Failed to send Slack webhook: %s", exc)

    # Store last_sent_at on successful send
    set_last_sent_at(now_iso)


if __name__ == "__main__":
    main()
