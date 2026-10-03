"""Sandbox settings: paths, ports and credentials.

Values come from the process environment first, then `.env`, then the defaults
below (which match `.env.example`). Paths are read on every call so tests can
point the sandbox at a temporary data directory with `SANDBOX_DATA_DIR`.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "sandbox" / "static"

PORTS = {"mailbox": 8101, "vendor_portal": 8102, "admin": 8103, "oracle": 8109}

DEFAULTS = {
    "MAILBOX_USER": "ops@acme.test",
    "MAILBOX_PASSWORD": "sandbox-mailbox-pass",
    "VENDOR_PORTAL_USER": "acme-ap",
    "VENDOR_PORTAL_PASSWORD": "sandbox-portal-pass",
    "ADMIN_USER": "ops.worker",
    "ADMIN_PASSWORD": "sandbox-admin-pass",
    "ADMIN_API_TOKEN": "sandbox-admin-token",
    "SANDBOX_APPROVER_USER": "finance.approver",
    "SANDBOX_APPROVER_PASSWORD": "sandbox-approver-pass",
    "SANDBOX_SECRET": "sandbox-only-secret",
    "FAULTS": "none",
}


@cache
def _dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def setting(name: str) -> str:
    value = os.environ.get(name) or _dotenv().get(name) or DEFAULTS.get(name)
    if value is None:
        raise KeyError(f"missing sandbox setting {name}")
    return value


def data_dir() -> Path:
    return Path(os.environ.get("SANDBOX_DATA_DIR", ROOT / "sandbox" / "data"))


def db_path() -> Path:
    return data_dir() / "sandbox.db"


def files_dir() -> Path:
    return data_dir() / "files"


def scenario_path() -> Path:
    return data_dir() / "scenario.json"
