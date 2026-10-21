"""
scripts/test_client.py  –  Quick smoke test for all MCP tools
=============================================================
Not a substitute for pytest – just verifies the server tools work
in-process without needing an MCP transport.

Run with:
    python scripts/test_client.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tender_mcp.config import ENV_PATH
from dotenv import load_dotenv
load_dotenv(ENV_PATH)

import tempfile
import tender_mcp.db as db_module

# Use a temporary database for test client to never touch production tenders.db
db_module.DB_PATH = Path(tempfile.gettempdir()) / "test_client_tenders.db"
if db_module.DB_PATH.exists():
    db_module.DB_PATH.unlink()
db_module._local.__dict__.clear()
from tender_mcp.db import init_db, upsert_tender

# Seed with one test tender
init_db()
upsert_tender({
    "id": "cppp:TESTID001",
    "source": "cppp",
    "title": "Supply of Petroleum Valves and Fittings",
    "organisation": "Oil India Limited",
    "category": "Engineering",
    "published_date": "12-Oct-2026",
    "closing_date": "2026-11-15T17:00:00Z",
    "opening_date": "2026-11-16T11:00:00Z",
    "emd_value": "5,00,000",
    "tender_value": "50,00,000",
    "link": "https://eprocure.gov.in/cppp/tendersfullview/TESTID001",
    "status": "active",
    "raw": {},
})

# Import tool functions directly
from tender_mcp.server import (
    search_tenders,
    get_tender,
    export_tenders,
    suggest_relevance,
    tender_stats,
)

print("=" * 60)
print("TEST: tender_stats")
result = tender_stats()
data = json.loads(result)
print(json.dumps(data, indent=2))
assert data["total_tenders"] >= 1, "Should have at least 1 tender"
print("PASS\n")

print("=" * 60)
print("TEST: search_tenders (keyword='petroleum')")
result = search_tenders(keyword="petroleum")
data = json.loads(result)
print(f"Found {len(data)} results")
assert len(data) >= 1, "Should find at least 1 petroleum tender"
print("PASS\n")

print("=" * 60)
print("TEST: get_tender")
result = get_tender("cppp:TESTID001")
data = json.loads(result)
assert data["title"] == "Supply of Petroleum Valves and Fittings"
print(f"Title: {data['title']}")
print("PASS\n")

print("=" * 60)
print("TEST: suggest_relevance")
result = suggest_relevance("cppp:TESTID001")
data = json.loads(result)
print(f"Matched keywords: {data['matched_keywords']}")
print(f"Score: {data['relevance_score']}")
print(f"Assessment: {data['assessment']}")
assert "petroleum" in data.get("matched_keywords", []) or data["relevance_score"] > 0
print("PASS\n")

print("=" * 60)
print("TEST: export_tenders")
result = export_tenders(status="active")
data = json.loads(result)
print(f"Exported to: {data['path']}")
print(f"Count: {data['count']}")
assert data["count"] >= 1
assert Path(data["path"]).exists()
print("PASS\n")

print("=" * 60)
print("ALL TOOL TESTS PASSED ✓")
