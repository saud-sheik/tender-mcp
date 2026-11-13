#!/usr/bin/env python3
"""
scripts/fetch_portal.py  –  Interactive portal scraper & credential helper
========================================================================
Run directly from terminal / IDE without opening a browser or localhost server:
    python scripts/fetch_portal.py

Allows choosing a portal (HMEL, GeM, CPPP), entering credentials if required,
and immediately pulling live tenders into tenders.db.
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender_mcp.config import (
    get_hmel_credentials,
    set_hmel_credentials,
    is_hmel_locked,
    unlock_hmel,
)
from tender_mcp.db import init_db, get_conn, upsert_tender


async def pull_hmel(username: str | None = None, password: str | None = None) -> None:
    if username and password:
        set_hmel_credentials(username, password)
        print("  ✓ Saved HMEL credentials to local .env.")

    # Check lockout
    is_locked, reason = is_hmel_locked()
    if is_locked:
        print(f"\n  ⚠️ Account lockout protection is active: {reason}")
        ans = input("  Would you like to clear the lockout now? [y/N]: ").strip().lower()
        if ans == "y":
            unlock_hmel()
            print("  ✓ Lockout file cleared.")
        else:
            print("  Skipping HMEL fetch.")
            return

    print("\n  ▶ Launching Playwright to scrape hmeleprocurement.mjunction.in...")
    from tender_mcp.refresh import _run_hmel
    tenders, err = await _run_hmel()

    if err:
        print(f"\n  ❌ Failed to fetch HMEL tenders: {err}")
        return

    new_count = 0
    updated_count = 0
    for t in tenders:
        _, is_new = upsert_tender(t)
        if is_new:
            new_count += 1
        else:
            updated_count += 1

    print(f"\n  ✅ Successfully fetched {len(tenders)} HMEL tenders ({new_count} new, {updated_count} updated).")


async def main() -> None:
    init_db()

    print("=" * 65)
    print("  SWAMINA TENDER AGGREGATOR - DIRECT PORTAL FETCHER")
    print("=" * 65)
    print("  Choose a portal to pull tenders from:")
    print("    [1] HMEL (hmeleprocurement.mjunction.in) - Requires Vendor Login")
    print("    [2] GeM  (bidplus.gem.gov.in)           - Public")
    print("    [3] CPPP (eprocure.gov.in)              - Public")
    print("    [4] All Portals")
    print("=" * 65)

    choice = input("  Select [1-4] (default 1): ").strip() or "1"

    if choice == "1":
        curr_user, _ = get_hmel_credentials()
        print("\n  HMEL (Guru Gobind Singh Refineries via mjunction)")
        if curr_user:
            print(f"  Current configured user: {curr_user}")
            use_existing = input("  Use current saved credentials? [Y/n]: ").strip().lower() != "n"
            if use_existing:
                await pull_hmel()
                return

        u = input("  Enter HMEL mjunction Username: ").strip()
        p = getpass.getpass("  Enter HMEL mjunction Password: ").strip()
        if not u or not p:
            print("  ❌ Username and password are required.")
            return
        await pull_hmel(username=u, password=p)

    elif choice == "2":
        print("\n  ▶ Pulling tenders from GeM...")
        from tender_mcp.refresh import refresh_all
        summary = await refresh_all(sources=["gem"])
        gem_stats = summary.get("sources", {}).get("gem", {})
        print(f"  ✅ GeM complete: fetched {gem_stats.get('fetched', 0)} ({gem_stats.get('new', 0)} new).")

    elif choice == "3":
        print("\n  ▶ Pulling tenders from CPPP...")
        from tender_mcp.refresh import refresh_all
        summary = await refresh_all(sources=["cppp"])
        cppp_stats = summary.get("sources", {}).get("cppp", {})
        print(f"  ✅ CPPP complete: fetched {cppp_stats.get('fetched', 0)} ({cppp_stats.get('new', 0)} new).")

    elif choice == "4":
        print("\n  ▶ Refreshing all portals...")
        from tender_mcp.refresh import refresh_all
        summary = await refresh_all()
        print(f"  ✅ Total fetched: {summary.get('total_fetched', 0)} ({summary.get('total_new', 0)} new).")

    else:
        print("  Invalid selection.")


if __name__ == "__main__":
    asyncio.run(main())

