"""Append one audit row per field for every business write (D22)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from sandbox.common.db import now_iso

Value = str | int | None


def _text(value: Value) -> str | None:
    return None if value is None else str(value)


def record_create(
    conn: sqlite3.Connection,
    app: str,
    entity: str,
    record_id: int | str,
    values: Mapping[str, Value],
    actor: str,
) -> None:
    at = now_iso()
    conn.executemany(
        "INSERT INTO audit_log (at, app, entity, record_id, action, field, old_value,"
        " new_value, actor) VALUES (?, ?, ?, ?, 'create', ?, NULL, ?, ?)",
        [(at, app, entity, str(record_id), f, _text(v), actor) for f, v in values.items()],
    )


def record_update(
    conn: sqlite3.Connection,
    app: str,
    entity: str,
    record_id: int | str,
    before: Mapping[str, Value],
    after: Mapping[str, Value],
    actor: str,
) -> None:
    """Audits only the fields whose value actually changed."""
    at = now_iso()
    conn.executemany(
        "INSERT INTO audit_log (at, app, entity, record_id, action, field, old_value,"
        " new_value, actor) VALUES (?, ?, ?, ?, 'update', ?, ?, ?, ?)",
        [
            (at, app, entity, str(record_id), f, _text(before.get(f)), _text(v), actor)
            for f, v in after.items()
            if _text(before.get(f)) != _text(v)
        ],
    )
