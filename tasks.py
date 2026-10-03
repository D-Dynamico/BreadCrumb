"""Cross-platform task runner, used instead of make: `uv run tasks <name>`.

Commands that belong to a later phase exist already so the interface is stable,
but they exit with an error until they are built. They never pretend to succeed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _run(*cmd: str) -> int:
    print("$", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def _python(*args: str) -> int:
    return _run(sys.executable, *args)


def setup(_: argparse.Namespace) -> int:
    uv = os.environ.get("UV", "uv")
    return _run(uv, "sync") or _python("-m", "playwright", "install", "chromium")


def lint(_: argparse.Namespace) -> int:
    steps = (
        ("-m", "ruff", "check", "."),
        ("-m", "ruff", "format", "--check", "."),
        ("-m", "mypy"),
        ("-m", "scripts.check_generality"),
    )
    failed = [" ".join(step[1:3]) for step in steps if _python(*step) != 0]
    if failed:
        print(f"lint failed: {', '.join(failed)}")
        return 1
    print("lint: all checks passed")
    return 0


def test(args: argparse.Namespace) -> int:
    return _python("-m", "pytest", *args.pytest_args)


def seed(args: argparse.Namespace) -> int:
    extra = ["--today", args.today] if args.today else []
    return _python("-m", "sandbox.seed", "--seed", str(args.seed), *extra)


def sandbox(args: argparse.Namespace) -> int:
    return _python("-m", "sandbox.launcher", "--faults", args.faults)


def _not_yet(phase: int, what: str) -> Callable[[argparse.Namespace], int]:
    def stub(_: argparse.Namespace) -> int:
        print(f"{what} arrives in Phase {phase} (see docs/PHASES.md). Nothing was run.")
        return 2

    return stub


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tasks", description="Breadcrumb project tasks")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="install dependencies and Playwright Chromium").set_defaults(
        func=setup
    )
    sub.add_parser("lint", help="ruff, mypy and the generality check").set_defaults(func=lint)
    p = sub.add_parser("test", help="run the test suite (extra args go to pytest)")
    p.add_argument("pytest_args", nargs=argparse.REMAINDER)
    p.set_defaults(func=test)

    p = sub.add_parser("seed", help="rebuild the sandbox database from a seed")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--today", help="pin the sandbox date, YYYY-MM-DD (default: today)")
    p.set_defaults(func=seed)

    p = sub.add_parser("sandbox", help="start the sandbox apps with a fault profile")
    p.add_argument(
        "--faults", default="none", choices=["none", "flaky", "session", "drift", "chaos"]
    )
    p.set_defaults(func=sandbox)

    sub.add_parser("ui", help="start the Breadcrumb web UI").set_defaults(
        func=_not_yet(5, "The web UI")
    )

    p = sub.add_parser("eval", help="run the harness on a task suite")
    p.add_argument("--suite", default="smoke", choices=["smoke", "dev", "heldout", "ablation"])
    p.set_defaults(func=_not_yet(6, "The evaluation harness"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
