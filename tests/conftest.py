"""
tests/conftest.py
"""
import sys
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
import tender_mcp.db as db_module


@pytest.fixture(autouse=True)
def isolate_test_database(tmp_path, monkeypatch):
    """
    Ensure every test uses an isolated temporary SQLite database,
    protecting production tenders.db from any test mutations.
    """
    temp_db = tmp_path / "test_isolated.db"
    monkeypatch.setattr(db_module, "DB_PATH", temp_db)
    db_module._local.__dict__.clear()
    db_module.init_db()
    yield temp_db
    db_module._local.__dict__.clear()
