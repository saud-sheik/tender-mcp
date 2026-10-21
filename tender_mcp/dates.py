"""
tender_mcp/dates.py  –  Robust ISO 8601 date parsing & normalization
====================================================================
Standardizes closing and published dates across all portals:
  - GeM: "12-10-2026 5:45 PM" -> "2026-10-12T17:45:00Z"
  - CPPP: "21-Oct-2026 01:00 PM" -> "2026-10-21T13:00:00Z"
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any


DATE_FORMATS = [
    # 12-10-2026 5:45 PM / 12-Oct-2026 01:00 PM
    "%d-%m-%Y %I:%M %p",
    "%d-%b-%Y %I:%M %p",
    "%d-%m-%Y %H:%M",
    "%d-%b-%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
    # Slash separated
    "%d/%m/%Y %I:%M %p",
    "%d/%b/%Y %I:%M %p",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    # Date only
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%d/%m/%Y",
    "%d/%b/%Y",
    # ISO formats
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
]


IST = timezone(timedelta(hours=5, minutes=30))


def parse_datetime(date_str: str | None) -> datetime | None:
    """
    Parse a date string from Indian portals (CPPP, GeM, HMEL) into a UTC datetime.
    Indian portals display time in IST (UTC+05:30). This function localizes
    IST inputs and converts them to UTC before attaching the 'Z' designation.
    """
    if not date_str or not str(date_str).strip():
        return None

    cleaned = str(date_str).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    # If it's already an ISO string with Z, it is already UTC
    if cleaned.endswith("Z"):
        try:
            return datetime.fromisoformat(cleaned.rstrip("Z")).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # If it has an explicit timezone offset like +05:30 or -00:00
    try:
        dt_iso = datetime.fromisoformat(cleaned)
        if dt_iso.tzinfo is not None:
            return dt_iso.astimezone(timezone.utc)
    except ValueError:
        pass

    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(cleaned, fmt)
            # Indian portal dates are in IST (UTC+05:30)
            return dt.replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue

    # Fallback: extract date portion via regex DD-MM-YYYY or DD-Mon-YYYY
    m = re.search(r"(\d{2})[-/](\d{2}|\w{3})[-/](\d{4})(?:\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?))?", cleaned, re.I)
    if m:
        sub_str = m.group(0)
        for fmt in DATE_FORMATS:
            try:
                dt = datetime.strptime(sub_str, fmt)
                return dt.replace(tzinfo=IST).astimezone(timezone.utc)
            except ValueError:
                continue

    return None


def to_iso8601(date_str: str | None) -> str:
    """
    Convert any portal date format into a standard ISO 8601 string:
    'YYYY-MM-DDTHH:MM:SSZ'
    Returns empty string if unparseable or empty.
    """
    dt = parse_datetime(date_str)
    if dt is None:
        return str(date_str).strip() if date_str else ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_expired(closing_date_str: str | None, now: datetime | None = None) -> bool:
    """
    Check if a closing date has already passed.
    Returns False if closing date is empty or cannot be parsed.
    """
    dt = parse_datetime(closing_date_str)
    if dt is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    return dt < current_time


def get_hours_to_close(closing_date_str: str | None, now: datetime | None = None) -> float | None:
    """
    Calculate hours remaining until closing time.
    Returns None if closing date is empty or cannot be parsed.
    """
    dt = parse_datetime(closing_date_str)
    if dt is None:
        return None
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    diff = (dt - current_time).total_seconds() / 3600.0
    return round(diff, 1)


def to_ist_display(date_str: str | None) -> str:
    """
    Format a UTC date string into an IST display string: 'DD-MM-YYYY hh:mm AM/PM IST'.
    """
    if not date_str:
        return "N/A"
    dt = parse_datetime(date_str)
    if dt is None:
        return str(date_str)
    ist_dt = dt.astimezone(IST)
    return ist_dt.strftime("%d-%m-%Y %I:%M %p IST")

