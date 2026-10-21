#!/usr/bin/env python3
"""
scripts/backup_db.py  –  Nightly SQLite backup script
===================================================
Uses SQLite online backup API to copy tenders.db safely while in WAL mode.
Keeps the last 14 copies in ./backups/, pruning older ones.
"""
import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tender_mcp.db import DB_PATH

logger = logging.getLogger("backup_db")
BACKUPS_DIR = PROJECT_ROOT / "backups"


def perform_backup(keep_count: int = 14) -> Path:
    """Create a new backup and prune old ones beyond keep_count."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found at {DB_PATH}")

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUPS_DIR / f"tenders_{timestamp}.db"

    # Use SQLite online backup API
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup_file))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    # Prune older backups
    all_backups = sorted(BACKUPS_DIR.glob("tenders_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    pruned = 0
    if len(all_backups) > keep_count:
        for old in all_backups[keep_count:]:
            old.unlink(missing_ok=True)
            pruned += 1

    remaining = min(len(all_backups), keep_count)
    print(f"Backup created: {backup_file.name} (retained {remaining}, pruned {pruned})")
    return backup_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup tenders SQLite database.")
    parser.add_argument("--keep", type=int, default=14, help="Number of backups to keep (default: 14)")
    args = parser.parse_args()

    try:
        perform_backup(keep_count=args.keep)
    except Exception as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
