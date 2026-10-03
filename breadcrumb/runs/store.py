"""The run store: one SQLite file (`runs/runs.db`) holding runs, checkpoints, events
and, through the journal module, journal entries (DURABILITY.md, three layers).

WAL mode with full fsync, so a row that was committed survives the process being
killed. Every write is one short transaction. Only this module and
`breadcrumb.journal.journal` write to the file.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    RECONCILING = "RECONCILING"
    # "The worker says it is done". Becomes VERIFYING then DONE once the Phase 4
    # verifier exists.
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


LIVE = (RunStatus.RUNNING, RunStatus.RECONCILING)
RESUMABLE = (RunStatus.INTERRUPTED, *LIVE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    reference TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    heartbeat_at REAL NOT NULL,
    resumes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    step INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    idempotency_key TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    state TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, idempotency_key)
);
"""


class LeaseError(Exception):
    """The run cannot be taken over; the message says why."""


def iso(epoch: float | None = None) -> str:
    moment = datetime.fromtimestamp(epoch if epoch is not None else time.time(), UTC)
    return moment.isoformat(timespec="seconds")


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task: str
    reference: str
    status: RunStatus
    summary: str
    pid: int | None
    heartbeat_at: float
    resumes: int
    created_at: str
    updated_at: str


def _record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(**{**dict(row), "status": RunStatus(row["status"])})


class RunStore:
    def __init__(self, path: Path, lease_timeout: float) -> None:
        self.path = path
        self.lease_timeout = lease_timeout
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=15)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)  # commits on its own
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction, committed (and fsynced) on leaving the block."""
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # -- runs ------------------------------------------------------------------------
    def create_run(
        self, run_id: str, task: str, reference: str, pid: int, now: float | None = None
    ) -> RunRecord:
        now = time.time() if now is None else now
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, task, reference, status, pid, heartbeat_at,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, task, reference, RunStatus.RUNNING, pid, now, iso(now), iso(now)),
            )
        run = self.get(run_id)
        assert run is not None
        return run

    def get(self, run_id: str) -> RunRecord | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _record(row) if row else None

    def list_runs(self) -> list[RunRecord]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at, run_id").fetchall()
        return [_record(r) for r in rows]

    def set_status(self, run_id: str, status: RunStatus, summary: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, summary = COALESCE(?, summary), updated_at = ?"
                " WHERE run_id = ?",
                (status, summary, iso(), run_id),
            )

    def heartbeat(self, run_id: str, pid: int, now: float | None = None) -> bool:
        """Renew the lease. False if this worker no longer holds it."""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ? AND pid = ?"
                " AND status IN (?, ?)",
                (now, run_id, pid, *LIVE),
            ).rowcount
        return changed == 1

    def mark_stale(self, now: float | None = None) -> list[str]:
        """Runs whose worker stopped sending heartbeats were interrupted."""
        now = time.time() if now is None else now
        cutoff = now - self.lease_timeout
        with self.transaction() as conn:
            stale = [
                r["run_id"]
                for r in conn.execute(
                    "SELECT run_id FROM runs WHERE status IN (?, ?) AND heartbeat_at < ?",
                    (*LIVE, cutoff),
                )
            ]
            for run_id in stale:
                conn.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (RunStatus.INTERRUPTED, iso(now), run_id),
                )
                _event(conn, run_id, "interrupted", {"by": "lease expired"}, now)
        return stale

    def acquire(self, run_id: str, pid: int, now: float | None = None) -> RunRecord:
        """Take the lease to resume a run. The run moves to RECONCILING."""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise LeaseError(f"there is no run {run_id}")
            run = _record(row)
            if run.status not in RESUMABLE:
                raise LeaseError(
                    f"run {run_id} is {run.status.value}; only interrupted runs resume"
                )
            if run.status in LIVE and now - run.heartbeat_at < self.lease_timeout:
                raise LeaseError(
                    f"run {run_id} is still running (worker {run.pid}); "
                    "stop it first with `breadcrumb kill`"
                )
            conn.execute(
                "UPDATE runs SET status = ?, pid = ?, heartbeat_at = ?, resumes = resumes + 1,"
                " updated_at = ? WHERE run_id = ?",
                (RunStatus.RECONCILING, pid, now, iso(now), run_id),
            )
            if run.status in LIVE:
                _event(conn, run_id, "interrupted", {"by": "lease expired"}, now)
            _event(conn, run_id, "resumed", {"pid": pid}, now)
        got = self.get(run_id)
        assert got is not None
        return got

    # -- checkpoints and events -------------------------------------------------------
    def save_checkpoint(self, run_id: str, step: int, state: dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO checkpoints (run_id, step, state_json, saved_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (run_id) DO UPDATE SET step = excluded.step,"
                " state_json = excluded.state_json, saved_at = excluded.saved_at",
                (run_id, step, json.dumps(state, ensure_ascii=False), iso()),
            )

    def load_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state_json FROM checkpoints WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def add_event(self, run_id: str, kind: str, detail: dict[str, Any]) -> None:
        with self.transaction() as conn:
            _event(conn, run_id, kind, detail, time.time())

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT at, kind, detail_json FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [{"at": r["at"], "kind": r["kind"], **json.loads(r["detail_json"])} for r in rows]


def _event(
    conn: sqlite3.Connection, run_id: str, kind: str, detail: dict[str, Any], now: float
) -> None:
    conn.execute(
        "INSERT INTO events (run_id, at, kind, detail_json) VALUES (?, ?, ?, ?)",
        (run_id, iso(now), kind, json.dumps(detail, ensure_ascii=False, default=str)),
    )
