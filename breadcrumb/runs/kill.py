"""A hard, cross-platform terminate for `breadcrumb kill` (D21).

No signal the worker can catch and no cleanup: on Windows `os.kill` calls
TerminateProcess, elsewhere SIGKILL is used. The run is left exactly as a machine
dying would leave it, which is what resume must cope with.
"""

from __future__ import annotations

import os
import signal
import sys


def hard_terminate(pid: int) -> bool:
    """Stop the process. Returns True if it was already gone."""
    # SIGKILL does not exist on Windows; there SIGTERM is already a hard terminate.
    sig = signal.SIGTERM if sys.platform == "win32" else getattr(signal, "SIGKILL")  # noqa: B009
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return True
    return False
