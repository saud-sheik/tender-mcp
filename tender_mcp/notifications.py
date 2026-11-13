"""
tender_mcp/notifications.py  –  Instant Slack & Webhook Alerts
=============================================================
Sends real-time notifications when new high-value or Tier A tenders
are ingested by the aggregator.
"""
from __future__ import annotations

import json
import logging
from typing import Any
import urllib.request
import urllib.error

from tender_mcp.config import get_slack_webhook_url
from tender_mcp.dates import to_ist_display, get_hours_to_close

logger = logging.getLogger(__name__)


def send_slack_message(text: str, webhook_url: str | None = None) -> bool:
    """
    Send a raw text/markdown payload to a Slack incoming webhook.
    Returns True if successful, False otherwise. Never raises an exception.
    """
    url = (webhook_url or get_slack_webhook_url()).strip()
    if not url:
        logger.debug("notifications: SLACK_WEBHOOK_URL is not configured; skipping notification.")
        return False

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status == 200:
                logger.info("notifications: Slack alert sent successfully.")
                return True
            logger.warning("notifications: Slack webhook responded with HTTP %d", resp.status)
            return False
    except Exception as exc:
        logger.warning("notifications: Failed to deliver Slack alert: %s", exc)
        return False


def format_tender_summary(t: dict[str, Any]) -> str:
    """Format an individual tender for Slack alert."""
    title = t.get("title", "Untitled Tender")
    org = t.get("organisation", "Unknown Organisation")
    score = t.get("relevance_score", 0)
    tier = t.get("tier", "A")
    source = (t.get("source") or "").upper()
    bid_num = t.get("bid_number") or t.get("id", "")
    link = t.get("link") or ""

    closing_iso = t.get("closing_date") or ""
    closing_ist = to_ist_display(closing_iso) if closing_iso else "N/A"
    htc = get_hours_to_close(closing_iso) if closing_iso else None
    htc_str = f"{round(htc, 1)}h remaining" if htc is not None else ""

    emd_str = "Nil / Exempt"
    if t.get("emd_amount_inr"):
        emd_str = f"₹{t['emd_amount_inr']:,.0f}"
    elif t.get("emd_amount_inr_min") or t.get("emd_amount_inr_max"):
        emd_str = f"₹{t.get('emd_amount_inr_min', 0):,.0f} - ₹{t.get('emd_amount_inr_max', 0):,.0f}"

    lines = [
        f"• *<{link}|{title}>*" if link else f"• *{title}*",
        f"  *Buyer:* {org} | *Source:* {source} | *Score:* {score} (Tier {tier})",
        f"  *Bid #:* `{bid_num}` | *EMD:* {emd_str}",
        f"  *Deadline:* {closing_ist} ({htc_str})" if htc_str else f"  *Deadline:* {closing_ist}",
    ]
    return "\n".join(lines)


def send_tier_a_alert(tenders: list[dict[str, Any]], webhook_url: str | None = None) -> bool:
    """
    Format and send an instant alert for a list of newly ingested high-value / Tier A tenders.
    Returns True if an alert was sent, False if empty or unconfigured.
    """
    if not tenders:
        return False

    url = (webhook_url or get_slack_webhook_url()).strip()
    if not url:
        logger.debug("notifications: No webhook configured for %d new tenders.", len(tenders))
        return False

    count = len(tenders)
    heading = (
        f"⚡ *[Swamina Tender Alert]* Found {count} new high-priority tender{'s' if count > 1 else ''}:"
    )

    tender_lines = [format_tender_summary(t) for t in tenders[:10]]
    if count > 10:
        tender_lines.append(f"\n_...and {count - 10} more tenders in dashboard._")

    full_message = f"{heading}\n\n" + "\n\n".join(tender_lines)
    return send_slack_message(full_message, webhook_url=url)

