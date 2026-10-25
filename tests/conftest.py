"""Pytest fixtures for Chronos."""

import sqlite3
import tempfile
from pathlib import Path
import pytest

from chronos.core.db import get_connection

@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary repository directory with .chronos/ setup."""
    chronos_dir = tmp_path / ".chronos"
    chronos_dir.mkdir(parents=True, exist_ok=True)
    db_file = chronos_dir / "chronos.db"
    con = get_connection(db_file)
    yield tmp_path, con, db_file
    con.close()

@pytest.fixture
def memory_db():
    """In-memory SQLite connection with full Chronos schema."""
    con = get_connection(":memory:")
    yield con
    con.close()
