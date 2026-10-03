"""The oracle answers ground truth read-only, and every task file renders under any seed."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.taskfile import render_task, task_id
from sandbox.seed.scenario import build_scenario
from tests.sandbox.conftest import TODAY

TASKS = Path(__file__).resolve().parents[2] / "harness" / "tasks"
HELDOUT = TASKS / "heldout"


def test_oracle_filters_and_refuses_bad_queries(world: dict[str, Any], oracle: TestClient) -> None:
    vendor = world["vendors"]["main"]["name"]
    rows = oracle.get("/records/payables", params={"vendor_name": vendor}).json()
    assert rows["count"] == 1  # the older invoice, already entered
    assert oracle.get("/records/passwords").status_code == 404
    assert oracle.get("/records/payables", params={"nope": "1"}).status_code == 400
    after = world["baseline"]["team_messages"]
    assert oracle.get("/records/team_messages", params={"after_id": after}).json()["count"] == 0
    assert oracle.get("/scenario").json()["seed"] == world["seed"]


def test_oracle_has_no_write_routes(oracle: TestClient) -> None:
    assert oracle.post("/records/payables", json={}).status_code == 405
    assert oracle.delete("/records/payables").status_code == 405


def test_oracle_never_exposes_logins(oracle: TestClient) -> None:
    for source in ("admin_users", "mail_accounts", "portal_accounts", "sessions"):
        assert oracle.get(f"/records/{source}").status_code == 404


@pytest.mark.parametrize("seed", [1, 2, 3, 42])
def test_every_task_file_renders_under_any_seed(seed: int) -> None:
    scenario = build_scenario(seed, TODAY)
    files = sorted(TASKS.glob("*/*.yaml"))
    assert files, "no task files"
    for path in files:
        task = render_task(path, scenario)
        assert task.id == task_id(path)
        assert "{{" not in task.prompt


def test_backfill_task_expects_every_missing_invoice_once() -> None:
    scenario = build_scenario(5, TODAY)
    task = render_task(HELDOUT / "heldout-04-backfill-crash.yaml", scenario)
    expected = {c.match["invoice_no"]: c.count for c in task.expected_end_state.records}
    batch = scenario["backfill"]["b"]
    assert expected == {i["invoice_no"]: 1 for i in batch["missing"] + batch["in_admin"]}


def test_heldout_tasks_are_frozen() -> None:
    """Held-out tasks never change after Phase 1 (ground rule 8)."""
    recorded: dict[str, str] = {}
    for line in (HELDOUT / "FROZEN.sha256").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        recorded[name] = digest
    actual = {
        p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for p in HELDOUT.glob("*.yaml")
    }
    assert actual == recorded
