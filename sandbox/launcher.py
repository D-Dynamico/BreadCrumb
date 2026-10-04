"""Start the three sandbox apps and the oracle, each as its own process.

Separate processes on separate ports make them behave like separate systems.
Everything binds to 127.0.0.1. Ctrl+C stops all of them.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Sequence

from sandbox import faults
from sandbox.common.config import PORTS, db_path

SERVICES = (
    ("mailbox", "sandbox.apps.mailbox.app:app"),
    ("vendor_portal", "sandbox.apps.vendor_portal.app:app"),
    ("admin", "sandbox.apps.admin.app:app"),
    ("oracle", "sandbox.oracle.app:app"),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox", description="Start the Acme Co. sandbox")
    parser.add_argument("--faults", default="none", choices=faults.PROFILES)
    args = parser.parse_args(argv)
    if not db_path().exists():
        print(f"No sandbox database at {db_path()}. Run `uv run tasks seed` first.")
        return 1

    env = {**os.environ, "FAULTS": args.faults}
    procs: list[tuple[str, subprocess.Popen[bytes]]] = []
    for name, target in SERVICES:
        cmd = [
            sys.executable, "-m", "uvicorn", target,
            "--host", "127.0.0.1", "--port", str(PORTS[name]), "--log-level", "warning",
        ]  # fmt: skip
        procs.append((name, subprocess.Popen(cmd, env=env)))
        print(f"  {name:<14} http://127.0.0.1:{PORTS[name]}")
    print(f"Sandbox running with faults={args.faults}. Press Ctrl+C to stop.")
    try:
        while True:
            for name, proc in procs:
                if proc.poll() is not None:
                    print(f"{name} exited with code {proc.returncode}; stopping the sandbox.")
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping the sandbox.")
        return 0
    finally:
        for _, proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for _, proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
