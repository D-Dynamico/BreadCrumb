"""The heartbeat that keeps a worker's lease on its run (DURABILITY.md).

A background thread renews the lease every few seconds. A worker that dies stops
renewing, and the next `breadcrumb runs` or `resume` marks the run INTERRUPTED. No
shutdown hook is needed, which is what makes a hard kill detectable.
"""

from __future__ import annotations

import sqlite3
import threading

from breadcrumb.runs.store import RunStore


class Heartbeat:
    def __init__(self, store: RunStore, run_id: str, pid: int, every: float) -> None:
        self.store = store
        self.run_id = run_id
        self.pid = pid
        self.every = every
        self.lost = False  # another worker took the run over
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name="heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.every):
            try:
                if not self.store.heartbeat(self.run_id, self.pid):
                    self.lost = True
                    return
            except sqlite3.Error:
                continue  # a busy database is retried on the next beat

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.every + 1)
