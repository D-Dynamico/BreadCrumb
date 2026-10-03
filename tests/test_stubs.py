"""Commands for later phases must fail loudly, never look like they worked."""

from __future__ import annotations

import pytest

from tasks import main as tasks_main


@pytest.mark.parametrize("argv", [["sandbox", "--faults", "flaky"], ["ui"], ["eval"]])
def test_task_stubs_exit_with_error(argv: list[str]) -> None:
    assert tasks_main(argv) == 2
