"""Releasing a waiting run: approve, reject or answer (D35).

The one implementation the CLI, the harness and the Phase 5 UI all call. A decision
is written to `runs.db`; a worker that is still waiting picks it up within seconds,
and a worker that died picks it up when the run is resumed.
"""

from __future__ import annotations

from breadcrumb.runs.store import RunStore, Wait


class NothingPending(Exception):
    """The run has no wait of that kind; the message says what it has instead."""


def pending(store: RunStore, run_id: str, kind: str | None = None) -> list[Wait]:
    return [w for w in store.waits(run_id, "pending") if kind is None or w.kind == kind]


def _one(store: RunStore, run_id: str, kind: str) -> Wait:
    waits = pending(store, run_id, kind)
    if not waits:
        other = pending(store, run_id)
        has = f"; it is waiting for a {other[0].kind}" if other else ""
        raise NothingPending(f"run {run_id} has no pending {kind}{has}")
    return waits[0]


def approval_pending(store: RunStore, run_id: str) -> Wait:
    return _one(store, run_id, "approval")


def approve(store: RunStore, run_id: str) -> Wait:
    return store.decide_wait(approval_pending(store, run_id).wait_id, "approved")


def reject(store: RunStore, run_id: str, reason: str = "") -> Wait:
    """Refuse the pending action. The reason reaches the worker as a user answer."""
    return store.decide_wait(approval_pending(store, run_id).wait_id, "rejected", reason)


def answer(store: RunStore, run_id: str, text: str) -> Wait:
    return store.decide_wait(_one(store, run_id, "question").wait_id, "answered", text)


def describe(wait: Wait) -> str:
    """What a person needs to see to decide: the question, or the exact diff."""
    if wait.kind == "question":
        return f"Question: {wait.payload.get('question', '')}"
    p = wait.payload
    lines = [f"Approval needed: {p.get('description', '')}", f"  why: {p.get('reason', '')}"]
    lines.append(f"  deliverable: {p.get('deliverable', '')}")
    lines += [f"  key   {k} = {v}" for k, v in (p.get("key") or {}).items()]
    lines += [f"  value {k} = {v}" for k, v in (p.get("values") or {}).items()]
    lines += [f"  typed {label} = {value}" for label, value in p.get("typed") or []]
    return "\n".join(lines)
