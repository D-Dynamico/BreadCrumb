"""Crash injection for the harness and the demo (DURABILITY.md, crash points).

Off unless `CRASH_POINT` is set. It ends the worker process abruptly with
`os._exit`: no cleanup, no `finally` blocks, no flushing of anything the journal
has not already committed. That is the point: it behaves like a machine dying.

`after_dispatch` fires on the first commit; `after_dispatch:2` on the second.
`during_approval` fires while the worker waits for its first approval.
Resumed runs never crash, so one injected crash means one interruption.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

POINTS = ("before_intended", "after_intended", "after_dispatch", "during_approval")
CRASH_EXIT_CODE = 75


def _hard_exit(point: str) -> None:
    print(f"\nCRASH POINT {point}: the worker stops here, abruptly.", flush=True)
    sys.stderr.flush()
    os._exit(CRASH_EXIT_CODE)


class CrashPoints:
    def __init__(self, spec: str, exit_hook: Callable[[str], None] = _hard_exit) -> None:
        point, _, nth = spec.strip().partition(":")
        if point and point not in POINTS:
            raise ValueError(f"unknown crash point {spec!r}; use one of {', '.join(POINTS)}")
        self.point = point
        self.nth = int(nth) if nth else 1
        self.commits = 0
        self.approvals = 0
        self.exit_hook = exit_hook

    def next_commit(self) -> None:
        self.commits += 1

    def reached(self, point: str) -> None:
        if self.point == point and self.commits == self.nth:
            self.exit_hook(point)

    def waiting_for_approval(self) -> None:
        """`during_approval` fires once the n-th approval request is on disk."""
        self.approvals += 1
        if self.point == "during_approval" and self.approvals == self.nth:
            self.exit_hook("during_approval")
