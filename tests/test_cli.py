"""`breadcrumb runs`, `kill` and `resume` against a temporary run store."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from breadcrumb import cli
from breadcrumb.config import Settings, settings
from breadcrumb.executor.context import build_prompt
from breadcrumb.executor.state import Fact, PlanStep, RunState, StepRecord
from breadcrumb.runs.store import RunStatus, RunStore


@pytest.fixture
def temp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    s = settings().model_copy(update={"runs_dir": tmp_path})
    monkeypatch.setattr(cli, "settings", lambda: s)
    return s


def _store(s: Settings) -> RunStore:
    return RunStore(s.runs_db, s.lease_timeout_seconds)


def test_runs_lists_runs_and_marks_silent_ones_interrupted(temp_settings: Settings) -> None:
    store = _store(temp_settings)
    store.create_run("r1", "enter the thing", "BC-AAAA", pid=1, now=time.time() - 600)
    result = CliRunner().invoke(cli.app, ["runs"])
    assert result.exit_code == 0
    assert "r1" in result.output and "INTERRUPTED" in result.output


def test_kill_terminates_the_worker_and_leaves_the_run_resumable(
    temp_settings: Settings,
) -> None:
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _store(temp_settings).create_run("r1", "t", "BC-AAAA", pid=worker.pid)
        result = CliRunner().invoke(cli.app, ["kill", "r1"])
        assert result.exit_code == 0, result.output
        assert worker.wait(timeout=10) != 0
    finally:
        if worker.poll() is None:
            worker.kill()
    run = _store(temp_settings).get("r1")
    assert run is not None and run.status is RunStatus.INTERRUPTED
    assert [e["by"] for e in _store(temp_settings).events("r1")] == ["kill"]


def test_kill_refuses_a_run_that_is_not_running(temp_settings: Settings) -> None:
    store = _store(temp_settings)
    store.create_run("r1", "t", "BC-AAAA", pid=1)
    store.set_status("r1", RunStatus.FINISHED, "done")
    assert CliRunner().invoke(cli.app, ["kill", "r1"]).exit_code == 1
    assert CliRunner().invoke(cli.app, ["kill", "nope"]).exit_code == 1


def test_resume_refuses_a_live_or_ended_run(temp_settings: Settings) -> None:
    store = _store(temp_settings)
    store.create_run("live", "t", "BC-AAAA", pid=1)
    store.create_run("ended", "t", "BC-BBBB", pid=1)
    store.set_status("ended", RunStatus.FINISHED, "done")
    for run_id, reason in [("live", "still running"), ("ended", "FINISHED"), ("x", "no run")]:
        result = CliRunner().invoke(cli.app, ["resume", run_id])
        assert result.exit_code == 1
        assert reason in result.output


def test_checkpoint_round_trip_keeps_everything_but_the_page() -> None:
    state = RunState(run_id="r1", task="t", reference="BC-AAAA")
    state.plan = [PlanStep("find it", "done")]
    state.facts = {"total": Fact("total", "10.00", "a.pdf p1 L2", 3)}
    state.history = [StepRecord(1, "browser_open", {"url": "/"}, "look", True, "ok")]
    state.observation = "a big page"
    restored = RunState.from_checkpoint(state.to_checkpoint())
    assert restored.plan == state.plan
    assert restored.facts == state.facts
    assert restored.history == state.history
    assert restored.observation != "a big page"


def test_the_prompt_shows_the_journal_and_the_interruption() -> None:
    state = RunState(run_id="r1", task="t", reference="BC-AAAA")
    state.interruption = "This run stopped unexpectedly and was resumed."
    journal = ["- CONFIRMED: create the record (attempt 1; accepted with HTTP 303)"]
    prompt = build_prompt(state, [], "", "", 4, 60, journal)
    assert "never repeat it" in prompt
    assert journal[0] in prompt
    assert "# You were interrupted" in prompt
