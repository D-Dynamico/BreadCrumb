"""The `breadcrumb` command: run, resume, list and kill runs.

`run` works from Phase 2. The others are stubs until their phase lands; stubs exit
with an error so a script can never mistake one for a real result.
"""

from __future__ import annotations

import sys
import textwrap

import typer

from breadcrumb.config import settings
from breadcrumb.executor.state import StepRecord

app = typer.Typer(help="Breadcrumb: an AI worker that can be interrupted and still finish the job.")


def _not_yet(phase: int, what: str) -> None:
    typer.echo(f"{what} arrives in Phase {phase} (see docs/PHASES.md). Nothing was run.")
    raise typer.Exit(code=2)


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


@app.command()
def run(
    task: str = typer.Argument(..., help="The task, in plain language."),
    max_steps: int = typer.Option(0, help="Override MAX_STEPS for this run."),
    headed: bool = typer.Option(False, help="Show the browser window."),
    no_cache: bool = typer.Option(False, help="Ignore the dev cache for model calls."),
) -> None:
    """Start a run from a natural-language task."""
    from breadcrumb.executor.loop import Executor
    from breadcrumb.llm.client import make_client, requests_today

    s = settings()
    updates: dict[str, object] = {}
    if max_steps:
        updates["max_steps"] = max_steps
    if headed:
        updates["headless"] = False
    if no_cache:
        updates["dev_cache"] = False
    s = s.model_copy(update=updates)
    executor = Executor(s, make_client(s), task, printer=_print_step)
    typer.echo(f"Run {executor.state.run_id} (reference {executor.state.reference})")
    typer.echo(f"Task: {task}\n")
    state = executor.run()
    typer.echo(f"\nStatus: {state.status.upper()} after {len(state.history)} steps")
    typer.echo(textwrap.fill(f"Summary: {state.summary}", 100))
    typer.echo(
        f"Model calls: {state.model_calls} this run, {requests_today(s)} today; "
        f"tokens: {state.tokens}"
    )
    if state.violations:
        typer.echo(f"Integrity violations: {len(state.violations)}")
    typer.echo(f"Step log: {executor.run_dir / 'steps.jsonl'}")
    raise typer.Exit(code=0 if state.status == "finished" else 1)


@app.command()
def resume(run_id: str = typer.Argument(..., help="The run to resume.")) -> None:
    """Resume an interrupted run from its breadcrumbs."""
    _not_yet(3, "Resuming a run")


@app.command()
def runs() -> None:
    """List runs and their status."""
    _not_yet(3, "Listing runs")


@app.command()
def kill(run_id: str = typer.Argument(..., help="The run whose worker to terminate.")) -> None:
    """Terminate a running worker abruptly, with no cleanup (demo and harness)."""
    _not_yet(3, "Killing a worker")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app()
