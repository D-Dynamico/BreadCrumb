"""Fixtures: one seeded world per test session, copied fresh for every test."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sandbox.common.config import DEFAULTS
from sandbox.seed.generate import write_world
from sandbox.seed.scenario import build_scenario

SEED = 7
TODAY = date(2026, 10, 5)


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    target = tmp_path_factory.mktemp("seeded")
    scenario = build_scenario(SEED, TODAY)
    write_world(scenario, target)
    return target, scenario


@pytest.fixture
def world(seeded_template: tuple[Path, dict[str, Any]], tmp_path: Path) -> Iterator[dict[str, Any]]:
    """A private copy of the seeded world; the apps and the oracle point at it."""
    source, scenario = seeded_template
    target = tmp_path / "data"
    shutil.copytree(source, target)
    saved = {k: os.environ.get(k) for k in ("SANDBOX_DATA_DIR", "SANDBOX_TODAY")}
    os.environ["SANDBOX_DATA_DIR"] = str(target)
    os.environ["SANDBOX_TODAY"] = TODAY.isoformat()
    try:
        yield scenario
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def admin(world: dict[str, Any]) -> TestClient:
    from sandbox.apps.admin.app import app

    return login(TestClient(app), DEFAULTS["ADMIN_USER"], DEFAULTS["ADMIN_PASSWORD"])


@pytest.fixture
def mailbox(world: dict[str, Any]) -> TestClient:
    from sandbox.apps.mailbox.app import app

    return login(TestClient(app), DEFAULTS["MAILBOX_USER"], DEFAULTS["MAILBOX_PASSWORD"])


@pytest.fixture
def portal(world: dict[str, Any]) -> TestClient:
    from sandbox.apps.vendor_portal.app import app

    return login(
        TestClient(app), DEFAULTS["VENDOR_PORTAL_USER"], DEFAULTS["VENDOR_PORTAL_PASSWORD"]
    )


@pytest.fixture
def oracle(world: dict[str, Any]) -> TestClient:
    from sandbox.oracle.app import app

    return TestClient(app)


def login(client: TestClient, username: str, password: str) -> TestClient:
    response = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return client


def csrf(client: TestClient, path: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert found, f"no form on {path}"
    return found.group(1)


def records(oracle: TestClient, source: str, **filters: Any) -> list[dict[str, Any]]:
    response = oracle.get(f"/records/{source}", params=filters)
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()["rows"]
    return rows
