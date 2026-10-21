#!/usr/bin/env bash
# scripts/smoke_test.sh  –  Post-deployment host verification script
# ===================================================================
# Runs on Linux deployment host after container / systemd setup:
#   1. Check /health endpoint
#   2. Run one refresh with --sources cppp
#   3. Run a digest --dry-run
#   4. Run a database backup
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== 1. Checking /health Endpoint ==="
if command -v curl >/dev/null 2>&1; then
    curl -s http://127.0.0.1:8000/health || echo "Health check curl failed (dashboard may not be up yet)"
    echo ""
else
    echo "curl not installed, skipping /health HTTP check"
fi

echo "=== 2. Running CPPP Refresh ==="
python3 -m scripts.refresh --sources cppp

echo "=== 3. Running Digest Dry Run ==="
python3 scripts/send_digest.py --dry-run

echo "=== 4. Running Database Backup ==="
python3 scripts/backup_db.py --keep 14

echo "=== Smoke Test Complete ==="
