"""The `breadcrumb` command: run, resume, list and kill runs.

`run` starts a run, `resume` continues an interrupted one from its checkpoint and
journal, `runs` lists them, and `kill` hard-stops a worker the way a dying machine
would (DURABILITY.md, D21).
"""

from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING

import typer

from breadcrumb.config import Settings, settings
from breadcrumb.executor.state import StepRecord
from breadcrumb.runs.kill import hard_terminate
from breadcrumb.runs.store import LIVE, LeaseError, RunStatus, RunStore

if TYPE_CHECKING:
    from breadcrumb.executor.loop import Executor

app = typer.Typer(help="Breadcrumb: an AI worker that can be interrupted and still finish the job.")


def _print_step(record: StepRecord) -> None:
    shown = {k: v for k, v in record.args.items() if k not in ("why", "remember", "plan")}
    args = ", ".join(f"{k}={v!r}" for k, v in shown.items())
    if len(args) > 110:
        args = args[:107] + "..."
    mark = "ok" if record.ok else "FAILED"
    typer.echo(f"#{record.step:>2} {record.action}({args})")
    if record.why:
        typer.echo(
            textwrap.fill(
                record.why, 100, initial_indent="     why: ", subsequent_indent="          "
            )
        )
    if record.args.get("plan"):
        steps = "; ".join(f"[{s.get('status')}] {s.get('title')}" for s in record.args["plan"])
        typer.echo(textwrap.shorten(f"     plan: {steps}", 160))
    for fact in record.args.get("remember") or []:
        typer.echo(f"     remembered {fact.get('key')} = {fact.get('value')}")
    outcome = " ".join(record.outcome.split())
    typer.echo(f"     -> {mark}: {outcome[:200]}")


def _store(s: Settings) -> RunStore:
    return RunStore(s.runs_db, s.lease_timeout_seconds)


def _settings(max_steps: int, headed: bool, no_cache: bool) -> Settings:
    updates: dict[str, object] = {}
    if max_steps:
        updates["max_steps"] = max_steps
    if headed:
        updates["headless"] = False
    if no_cache:
        updates["dev_cache"] = False
    return settings().model_copy(update=updates)


_ENDED_BY = {
    "finish": "the worker called finish",
    "step_budget": "budget exhausted: steps",
    "time_budget": "budget exhausted: time",
    "token_budget": "budget exhausted: tokens",
    "model_error": "the model could not be reached",
    "repeats": "repeat escalation: same action on the same state after a note and a refusal",
    "reconcile": "resume could not tell whether an effect happened",
    "unclear_effect": "could not tell whether an effect happened",
}


def _report(executor: Executor, s: Settings) -> None:
    from breadcrumb.llm.client import requests_today

    state = executor.state
    typer.echo(f"\nStatus: {state.status} after {len(state.history)} steps")
    typer.echo(f"Ended by: {_ENDED_BY.get(state.ended_by, state.ended_by)}")
    typer.echo(textwrap.fill(f"Summary: {state.summary}", 100))
    typer.echo(
        f"Model calls: {state.model_calls} this run, {requests_today(s)} today; "
        f"tokens: {state.tokens}"
    )
    if state.violations:
        typer.echo(f"Integrity violations: {len(state.violations)}")
    typer.echo(f"Step log: {executor.run_dir / 'steps.jsonl'}")
    raise typer.Exit(code=0 if state.status == RunStatus.FINISHED else 1)


MAX_STEPS = typer.Option(0, help="Override MAX_STEPS for this run.")
HEADED = typer.Option(False, help="Show the browser window.")
NO_CACHE = typer.Option(False, help="Ignore the dev cache for model calls.")


@app.command()
def run(
    task: str = typer.Argument(..., help="The task, in plain language."),
    max_steps: int = MAX_STEPS,
    headed: bool = HEADED,
    no_cache: bool = NO_CACHE,
) -> None:
    """Start a run from a natural-language task."""
    from breadcrumb.executor.loop import Executor
    from breadcrumb.llm.client import make_client

    s = _settings(max_steps, headed, no_cache)
    executor = Executor.start(s, make_client(s), _store(s), task, _print_step, typer.echo)
    typer.echo(f"Run {executor.state.run_id} (reference {executor.state.reference})")
    if s.crash_point:
        typer.echo(f"Crash point armed: {s.crash_point}")
    typer.echo(f"Task: {task}\n")
    executor.run()
    _report(executor, s)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="The run to resume."),
    max_steps: int = MAX_STEPS,
    headed: bool = HEADED,
    no_cache: bool = NO_CACHE,
) -> None:
    """Resume an interrupted run from its breadcrumbs."""
    from breadcrumb.executor.loop import Executor
    from breadcrumb.llm.client import make_client

    s = _settings(max_steps, headed, no_cache)
    try:
        executor = Executor.resume(s, make_client(s), _store(s), run_id, _print_step, typer.echo)
    except LeaseError as exc:
        typer.echo(f"Cannot resume: {exc}.")
        raise typer.Exit(code=1) from exc
    typer.echo(f"Task: {executor.state.task}")
    typer.echo(f"Continuing after step {len(executor.state.history)}\n")
    executor.run()
    _report(executor, s)


@app.command()
def runs() -> None:
    """List runs and their status."""
    store = _store(settings())
    store.mark_stale()
    listed = store.list_runs()
    if not listed:
        typer.echo("No runs yet.")
        return
    for r in listed:
        resumes = f" resumed x{r.resumes}" if r.resumes else ""
        typer.echo(f"{r.run_id}  {r.status.value:<12} {r.updated_at}{resumes}")
        typer.echo(f"    task: {textwrap.shorten(r.task, 90)}")
        if r.summary:
            typer.echo(f"    {textwrap.shorten(r.summary, 94)}")


@app.command()
def kill(run_id: str = typer.Argument(..., help="The run whose worker to terminate.")) -> None:
    """Terminate a running worker abruptly, with no cleanup (demo and harness)."""
    store = _store(settings())
    store.mark_stale()
    record = store.get(run_id)
    if record is None:
        typer.echo(f"There is no run {run_id}.")
        raise typer.Exit(code=1)
    if record.status not in LIVE or record.pid is None:
        typer.echo(f"Run {run_id} is {record.status.value}; no worker to stop.")
        raise typer.Exit(code=1)
    gone = hard_terminate(record.pid)
    store.set_status(run_id, RunStatus.INTERRUPTED)
    store.add_event(run_id, "interrupted", {"by": "kill", "pid": record.pid})
    what = "was already gone" if gone else "terminated"
    typer.echo(f"Worker {record.pid} {what}. Run {run_id} is INTERRUPTED; resume it with")
    typer.echo(f"  uv run breadcrumb resume {run_id}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app()
