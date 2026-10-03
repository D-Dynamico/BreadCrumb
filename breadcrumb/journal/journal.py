"""The journal: one durable entry per side effect, with every state it reached.

An entry is written before its effect touches the world (INTENDED, then
DISPATCHED) and again when the outcome is known. Each `record` call is one
committed transaction, so whatever the process does next, the entry on disk says
how far the effect got.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from breadcrumb.journal.machine import RETRYABLE, State, check_transition
from breadcrumb.runs.store import RunStore, iso


@dataclass
class Entry:
    entry_id: str
    run_id: str
    step_no: int
    attempt_no: int
    action: str  # the declared action that carries the effect, e.g. browser_submit
    kind: str  # create, update, send or other
    description: str  # the change, in the worker's plain words
    key: dict[str, Any]  # the natural key: the business identity of the effect
    values: dict[str, Any]  # other values the effect writes, for reconcile to compare
    lookup: dict[str, Any] | None  # how to look the effect up later, or None
    idempotency_key: str
    state: State = State.PROPOSED
    history: list[list[str]] = field(default_factory=list)  # [state, at, note]
    evidence: dict[str, Any] = field(default_factory=dict)
    before: dict[str, Any] | None = None  # for updates: the values before the change

    @classmethod
    def new(cls, **fields: Any) -> Entry:
        entry = cls(entry_id="je-" + secrets.token_hex(4), attempt_no=1, **fields)
        entry.history.append([State.PROPOSED.value, iso(), ""])
        return entry

    @property
    def note(self) -> str:
        return self.history[-1][2] if self.history else ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, text: str) -> Entry:
        data = json.loads(text)
        return cls(**{**data, "state": State(data["state"])})


class Journal:
    def __init__(self, store: RunStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    def record(
        self,
        entry: Entry,
        new: State,
        note: str = "",
        evidence: dict[str, Any] | None = None,
        step_no: int | None = None,
    ) -> Entry:
        """Move an entry to a new state and commit it to disk before returning."""
        check_transition(entry.state, new)
        attempt = entry.attempt_no
        if entry.state in RETRYABLE and new is State.INTENDED:
            attempt += 1
        moved = replace(
            entry,
            state=new,
            attempt_no=attempt,
            step_no=entry.step_no if step_no is None else step_no,
            history=[*entry.history, [new.value, iso(), note]],
            evidence={**entry.evidence, **(evidence or {})},
        )
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO journal (entry_id, run_id, idempotency_key, step_no, attempt_no,"
                " state, entry_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (entry_id) DO UPDATE SET step_no = excluded.step_no,"
                " attempt_no = excluded.attempt_no, state = excluded.state,"
                " entry_json = excluded.entry_json, updated_at = excluded.updated_at",
                (
                    moved.entry_id,
                    self.run_id,
                    moved.idempotency_key,
                    moved.step_no,
                    moved.attempt_no,
                    moved.state.value,
                    moved.to_json(),
                    iso(),
                ),
            )
        # Only now, with the row committed, does the caller's entry change.
        entry.__dict__.update(moved.__dict__)
        return entry

    def find(self, idempotency_key: str) -> Entry | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT entry_json FROM journal WHERE run_id = ? AND idempotency_key = ?",
                (self.run_id, idempotency_key),
            ).fetchone()
        return Entry.from_json(row["entry_json"]) if row else None

    def entries(self) -> list[Entry]:
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT entry_json FROM journal WHERE run_id = ? ORDER BY rowid", (self.run_id,)
            ).fetchall()
        return [Entry.from_json(r["entry_json"]) for r in rows]
