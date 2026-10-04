"""The `breadcrumb` command: run, resume, list, kill, approve, reject and answer.

`run` starts a run, `resume` continues an interrupted one from its checkpoint and
journal, `runs` lists them with anything waiting for a person, and `kill` hard-stops
a worker the way a dying machine would (DURABILITY.md, D21). `approve`, `reject` and
`answer` release a waiting run; they call `breadcrumb.runs.waits`, the same functions
the harness and the UI use (D35).
"""

from __future__ import annotations

import sys
import textwrap
import time
from typing import TYPE_CHECKING

import typer

from breadcrumb.config import Settings, settings
from breadcrumb.executor.state import StepRecord
from breadcrumb.runs import waits
from breadcrumb.runs.kill import hard_terminate
from breadcrumb.runs.store import LIVE, WAITING, LeaseError, RunStatus, RunStore

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


def _report(executor: Executor, s: Settings) -> None:
    from breadcrumb.llm.client import requests_today
    from breadcrumb.receipt.receipt import ENDED_BY

    state = executor.state
    typer.echo(f"\nStatus: {state.status} after {len(state.history)} steps")
    typer.echo(f"Ended by: {ENDED_BY.get(state.ended_by, state.ended_by)}")
    typer.echo(textwrap.fill(f"Summary: {state.summary}", 100))
    typer.echo(
        f"Model calls: {state.model_calls} this run, {requests_today(s)} today; "
        f"tokens: {state.tokens}"
    )
    if state.violations:
        typer.echo(f"Integrity violations: {len(state.violations)}")
    typer.echo(f"Step log: {executor.run_dir / 'steps.jsonl'}")
    typer.echo(f"Receipt: {executor.run_dir / 'receipt.md'}")
    raise typer.Exit(code=0 if state.status == RunStatus.DONE else 1)


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
        for wait in waits.pending(store, r.run_id):
            first = waits.describe(wait).splitlines()[0]
            command = "answer" if wait.kind == "question" else "approve"
            typer.echo(f"    WAITING: {textwrap.shorten(first, 80)}")
            typer.echo(f"      uv run breadcrumb {command} {r.run_id}")


@app.command()
def kill(run_id: str = typer.Argument(..., help="The run whose worker to terminate.")) -> None:
    """Terminate a running worker abruptly, with no cleanup (demo and harness)."""
    store = _store(settings())
    store.mark_stale()
    record = store.get(run_id)
    if record is None:
        typer.echo(f"There is no run {run_id}.")
        raise typer.Exit(code=1)
    if record.status not in LIVE + WAITING or record.pid is None:
        typer.echo(f"Run {run_id} is {record.status.value}; no worker to stop.")
        raise typer.Exit(code=1)
    gone = hard_terminate(record.pid)
    if record.status in LIVE:  # a durable wait keeps its status (DURABILITY.md)
        store.set_status(run_id, RunStatus.INTERRUPTED)
    store.add_event(run_id, "interrupted", {"by": "kill", "pid": record.pid})
    what = "was already gone" if gone else "terminated"
    status = store.get(run_id)
    shown = status.status.value if status else "INTERRUPTED"
    typer.echo(f"Worker {record.pid} {what}. Run {run_id} is {shown}; resume it with")
    typer.echo(f"  uv run breadcrumb resume {run_id}")


def _waiting_store(run_id: str) -> RunStore:
    store = _store(settings())
    if store.get(run_id) is None:
        typer.echo(f"There is no run {run_id}.")
        raise typer.Exit(code=1)
    return store


def _after_decision(store: RunStore, run_id: str) -> None:
    store.mark_stale()
    record = store.get(run_id)
    alive = record is not None and record.status in WAITING + LIVE
    fresh = record is not None and time.time() - record.heartbeat_at < store.lease_timeout
    if alive and fresh:
        typer.echo("The worker is waiting and will continue within seconds.")
    else:
        typer.echo(f"No worker is running. Continue with: uv run breadcrumb resume {run_id}")


@app.command()
def approve(
    run_id: str = typer.Argument(..., help="The run waiting for approval."),
    yes: bool = typer.Option(False, "--yes", help="Approve without asking (for the harness)."),
) -> None:
    """Show the pending action and its exact diff, then approve it."""
    store = _waiting_store(run_id)
    try:
        wait = waits.approval_pending(store, run_id)
    except waits.NothingPending as exc:
        typer.echo(f"{exc}.")
        raise typer.Exit(code=1) from exc
    typer.echo(waits.describe(wait))
    if not yes and not typer.confirm("Approve exactly this?", default=False):
        typer.echo("Not approved. Nothing changed.")
        raise typer.Exit(code=1)
    waits.approve(store, run_id)
    typer.echo("Approved.")
    _after_decision(store, run_id)


@app.command()
def reject(
    run_id: str = typer.Argument(..., help="The run waiting for approval."),
    reason: str = typer.Argument("", help="Why; passed back to the worker as your answer."),
) -> None:
    """Refuse the pending action. The reason reaches the worker as a user answer."""
    store = _waiting_store(run_id)
    try:
        waits.reject(store, run_id, reason)
    except waits.NothingPending as exc:
        typer.echo(f"{exc}.")
        raise typer.Exit(code=1) from exc
    typer.echo("Rejected.")
    _after_decision(store, run_id)


@app.command()
def answer(
    run_id: str = typer.Argument(..., help="The run waiting for an answer."),
    text: str = typer.Argument(..., help="Your answer."),
) -> None:
    """Answer the question a run is waiting on."""
    store = _waiting_store(run_id)
    try:
        wait = waits.answer(store, run_id, text)
    except waits.NothingPending as exc:
        typer.echo(f"{exc}.")
        raise typer.Exit(code=1) from exc
    typer.echo(f"Answered: {wait.payload.get('question', '')}")
    _after_decision(store, run_id)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app()
