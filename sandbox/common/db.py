"""SQLite access for the sandbox apps (WAL mode, shared by separate processes)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from sandbox.common.config import db_path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(*, readonly: bool = False) -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run `uv run tasks seed` first.")
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db(*, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """One connection per request. Commits if the block succeeds."""
    conn = connect(readonly=readonly)
    try:
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def create_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> date:
    """The sandbox's idea of today. `SANDBOX_TODAY` pins it for tests."""
    pinned = os.environ.get("SANDBOX_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()
