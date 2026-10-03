"""Run store and journal on disk: lease, heartbeat, checkpoints, entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from breadcrumb.journal.journal import Entry, Journal
from breadcrumb.journal.machine import IllegalTransition, State
from breadcrumb.runs.store import LeaseError, RunStatus, RunStore


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs.db", lease_timeout=30)


def test_a_new_run_is_running_with_its_worker(store: RunStore) -> None:
    store.create_run("r1", "do it", "BC-AAAA", pid=111)
    run = store.get("r1")
    assert run is not None
    assert (run.status, run.pid, run.reference) == (RunStatus.RUNNING, 111, "BC-AAAA")
    assert [r.run_id for r in store.list_runs()] == ["r1"]


def test_a_silent_worker_is_marked_interrupted(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1, now=1000.0)
    store.create_run("r2", "t", "BC-B", pid=2, now=1000.0)
    store.heartbeat("r2", pid=2, now=1025.0)
    assert store.mark_stale(now=1040.0) == ["r1"]
    statuses = {r.run_id: r.status for r in store.list_runs()}
    assert statuses == {"r1": RunStatus.INTERRUPTED, "r2": RunStatus.RUNNING}


def test_only_the_lease_holder_can_heartbeat(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1)
    assert store.heartbeat("r1", pid=2) is False
    assert store.heartbeat("r1", pid=1) is True


def test_resume_takes_the_lease_of_an_interrupted_run(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1, now=1000.0)
    store.set_status("r1", RunStatus.INTERRUPTED)
    run = store.acquire("r1", pid=2, now=1001.0)
    assert (run.status, run.pid, run.resumes) == (RunStatus.RECONCILING, 2, 1)


def test_a_live_worker_keeps_its_lease(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1, now=1000.0)
    with pytest.raises(LeaseError, match="still running"):
        store.acquire("r1", pid=2, now=1010.0)
    # Once its heartbeat is stale, the run can be taken over.
    assert store.acquire("r1", pid=2, now=1031.0).pid == 2


@pytest.mark.parametrize("status", [RunStatus.FINISHED, RunStatus.FAILED, RunStatus.ESCALATED])
def test_ended_runs_cannot_be_resumed(store: RunStore, status: RunStatus) -> None:
    store.create_run("r1", "t", "BC-A", pid=1)
    store.set_status("r1", status, "why")
    with pytest.raises(LeaseError, match=status.value):
        store.acquire("r1", pid=2)


def test_unknown_run_cannot_be_resumed(store: RunStore) -> None:
    with pytest.raises(LeaseError, match="no run"):
        store.acquire("nope", pid=2)


def test_latest_checkpoint_wins(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1)
    assert store.load_checkpoint("r1") is None
    store.save_checkpoint("r1", 1, {"plan": ["a"]})
    store.save_checkpoint("r1", 2, {"plan": ["a", "b"]})
    assert store.load_checkpoint("r1") == {"plan": ["a", "b"]}


def test_events_are_kept_in_order(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1)
    store.add_event("r1", "interrupted", {"by": "kill"})
    store.add_event("r1", "resumed", {})
    assert [e["kind"] for e in store.events("r1")] == ["interrupted", "resumed"]


def _entry() -> Entry:
    return Entry.new(
        run_id="r1",
        step_no=4,
        action="browser_submit",
        kind="create",
        description="enter the invoice",
        key={"invoice_no": "INV-2"},
        values={"amount": "10.00"},
        lookup={"operation": "listPayables", "params": {}},
        idempotency_key="bc-123",
    )


def test_journal_entries_survive_a_new_process(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1)
    entry = _entry()
    Journal(store, "r1").record(entry, State.INTENDED)
    Journal(store, "r1").record(entry, State.DISPATCHED, evidence={"url": "/payables/new"})
    reopened = Journal(RunStore(store.path, 30), "r1").find("bc-123")
    assert reopened is not None
    assert reopened.state is State.DISPATCHED
    assert [h[0] for h in reopened.history] == ["PROPOSED", "INTENDED", "DISPATCHED"]
    assert reopened.key == {"invoice_no": "INV-2"}
    assert reopened.evidence == {"url": "/payables/new"}


def test_journal_refuses_illegal_moves(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1)
    entry = _entry()
    with pytest.raises(IllegalTransition):
        Journal(store, "r1").record(entry, State.DISPATCHED)
    assert Journal(store, "r1").entries() == []


def test_a_retry_is_a_new_attempt_on_the_same_entry(store: RunStore) -> None:
    store.create_run("r1", "t", "BC-A", pid=1)
    journal = Journal(store, "r1")
    entry = _entry()
    for state in (State.INTENDED, State.DISPATCHED, State.FAILED):
        journal.record(entry, state)
    journal.record(entry, State.INTENDED, step_no=9)
    again = journal.find("bc-123")
    assert again is not None
    assert (again.attempt_no, again.step_no, again.state) == (2, 9, State.INTENDED)
    assert len(journal.entries()) == 1
