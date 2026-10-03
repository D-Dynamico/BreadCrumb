"""The `breadcrumb` command: run, resume, list and kill runs.

Every command is a stub until its phase lands. Stubs exit with an error so a
script can never mistake one for a real result.
"""

from __future__ import annotations

import typer

app = typer.Typer(help="Breadcrumb: an AI worker that can be interrupted and still finish the job.")


def _not_yet(phase: int, what: str) -> None:
    typer.echo(f"{what} arrives in Phase {phase} (see docs/PHASES.md). Nothing was run.")
    raise typer.Exit(code=2)


@app.command()
def run(task: str = typer.Argument(..., help="The task, in plain language.")) -> None:
    """Start a run from a natural-language task."""
    _not_yet(2, "Running a task")


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
    app()
