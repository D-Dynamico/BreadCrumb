"""Commands for later phases must fail loudly, never look like they worked."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from breadcrumb.cli import app
from tasks import main as tasks_main


@pytest.mark.parametrize(
    "argv", [["seed", "--seed", "3"], ["sandbox", "--faults", "flaky"], ["ui"], ["eval"]]
)
def test_task_stubs_exit_with_error(argv: list[str]) -> None:
    assert tasks_main(argv) == 2


@pytest.mark.parametrize(
    "argv", [["run", "do a thing"], ["resume", "r1"], ["runs"], ["kill", "r1"]]
)
def test_cli_stubs_exit_with_error(argv: list[str]) -> None:
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 2
    assert "arrives in Phase" in result.output
