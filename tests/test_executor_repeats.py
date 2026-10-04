"""The executor's side of repeat detection: refused actions never run, and progress
(a plan step newly done, or a confirmed commit) resets the count. No browser starts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from breadcrumb.config import settings
from breadcrumb.executor.loop import Executor
from breadcrumb.executor.repeats import Check, Response
from breadcrumb.executor.state import PlanStep
from breadcrumb.gateway.gateway import Committed
from breadcrumb.journal.journal import Entry
from breadcrumb.journal.machine import State
from breadcrumb.runs.store import RunStore

READ = {"name": "a.pdf", "why": "read it"}


@pytest.fixture
def executor(tmp_path: Path) -> Executor:
    s = settings().model_copy(update={"runs_dir": tmp_path})
    store = RunStore(s.runs_db, s.lease_timeout_seconds)
    return Executor.start(s, model=None, store=store, task="t")  # type: ignore[arg-type]


def _ran(executor: Executor, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def dispatch(name: str, args: dict[str, Any]) -> str:
        calls.append(name)
        return "read a.pdf"

    monkeypatch.setattr(executor, "_dispatch", dispatch)
    return calls


def test_a_refused_repeat_is_not_executed(
    executor: Executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _ran(executor, monkeypatch)
    record, escalate, _ = executor._act(4, "files_read", READ, Check(Response.REFUSE, "Refused"))
    assert calls == []
    assert not record.ok and record.outcome == "Refused" and not escalate


def test_a_third_repeat_escalates_without_executing(
    executor: Executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _ran(executor, monkeypatch)
    _, escalate, ended_by = executor._act(5, "files_read", READ, Check(Response.ESCALATE, "Stop"))
    assert calls == [] and escalate == "Stop" and ended_by == "repeats"


def test_a_first_repeat_runs_with_the_note(
    executor: Executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _ran(executor, monkeypatch)
    record, _, _ = executor._act(3, "files_read", READ, Check(Response.NOTE, "NOTE: again"))
    assert calls == ["files_read"]
    assert record.ok and record.outcome.startswith("NOTE: again")


def _strike(executor: Executor) -> None:
    executor.repeats.check(1, "files_read", READ, "fp")
    executor.repeats.check(2, "files_read", READ, "fp")
    assert executor.repeats.strikes == 1


def test_a_plan_step_newly_done_resets_the_count(
    executor: Executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ran(executor, monkeypatch)
    executor.state.plan = [PlanStep("find it", "doing")]
    _strike(executor)
    executor._act(3, "files_read", {**READ, "plan": [{"title": "find it", "status": "done"}]},
                  Check(Response.OK))  # fmt: skip
    assert executor.repeats.strikes == 0


def test_a_plan_step_already_done_is_not_progress(
    executor: Executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ran(executor, monkeypatch)
    executor.state.plan = [PlanStep("find it", "done")]
    _strike(executor)
    executor._act(3, "files_read", {**READ, "plan": [{"title": "find it", "status": "done"}]},
                  Check(Response.OK))  # fmt: skip
    assert executor.repeats.strikes == 1


@pytest.mark.parametrize(("state", "reset"), [(State.CONFIRMED, True), (State.FAILED, False)])
def test_a_confirmed_commit_resets_the_count(
    executor: Executor, monkeypatch: pytest.MonkeyPatch, state: State, reset: bool
) -> None:
    entry = Entry.new(run_id="r", step_no=3, action="notify", kind="send", description="",
                      key={}, values={}, lookup=None, idempotency_key="k")  # fmt: skip
    entry.state = state
    committed = Committed(state is State.CONFIRMED, "Done.", entry)
    monkeypatch.setattr(executor, "_commit", lambda *_: committed)
    _strike(executor)
    executor._act(3, "notify", {"message": "hi", "why": "tell"}, Check(Response.OK))
    assert (executor.repeats.strikes == 0) is reset


def test_repeat_messages_name_the_systems_not_opened_yet(executor: Executor) -> None:
    executor.state.visited = [executor.settings.apps[0].name]
    noted = executor._with_unvisited(Check(Response.NOTE, "NOTE: again."))
    others = [a.name for a in executor.settings.apps[1:]]
    assert all(name in noted.message for name in others)
    assert executor.settings.apps[0].name not in noted.message.split("yet in this run:")[1]
    assert executor._with_unvisited(Check(Response.OK)).message == ""


def test_a_failed_verification_gets_one_repair_pass_then_fails_honestly(
    executor: Executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from breadcrumb.contract.model import Contract
    from breadcrumb.executor.state import Fact

    contract = Contract.model_validate(
        {
            "goal": "enter it",
            "facts": [{"key": "doc_no", "type": "id"}, {"key": "who", "type": "text"}],
            "deliverables": [{"id": "entered", "kind": "create",
                              "lookup_operation": "listPayables",
                              "key": [{"field": "invoice_no", "fact": "doc_no"},
                                      {"field": "vendor_name", "fact": "who"}]}],
        }
    )  # fmt: skip
    executor.state.contract = contract.model_dump()
    executor.state.facts = {
        "doc_no": Fact("doc_no", "X-1", "a.pdf", 1, "id"),
        "who": Fact("who", "North Co", "mail", 1, "text"),
    }
    executor.run_dir.mkdir(parents=True)
    monkeypatch.setattr(executor, "_lookup", lambda spec: [])  # nothing was entered
    assert executor._verify("All done.") is None  # the repair pass
    assert executor.state.repair_used and "no record matches" in executor.state.note
    ended = executor._verify("All done, really.")
    assert ended is not None and (ended.status, ended.ended_by) == ("FAILED", "verification")
    receipt = (executor.run_dir / "receipt.md").read_text(encoding="utf-8")
    assert "| failed | no record matches |" in receipt
