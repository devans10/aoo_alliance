"""
Shared fixtures for the AoO Alliance test suite.
"""

import sqlite3
import sys
from pathlib import Path
from datetime import date, timedelta

import pytest

# Make shared/ and ocr/ importable without installing the package
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCHEMA_PATH = PROJECT_ROOT / "shared" / "db_schema.sql"


@pytest.fixture
def db_conn():
    """
    In-memory SQLite connection with the full project schema applied.
    Isolated per test — no side effects on the real alliance.db.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    yield conn
    conn.close()


@pytest.fixture
def today():
    return date.today().isoformat()


@pytest.fixture
def days_ago():
    """Return a callable: days_ago(n) → ISO date string n days in the past."""
    def _days_ago(n: int) -> str:
        return (date.today() - timedelta(days=n)).isoformat()
    return _days_ago
