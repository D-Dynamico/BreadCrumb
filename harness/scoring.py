"""Score a run's end state against a task's hand-written ground truth, via the oracle.

Never reads the agent's contract, journal, receipt or summary (D8). Only what is
actually in the sandbox counts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import dotenv_values

from harness.taskfile import MessageCheck, RecordCheck, Task, UnchangedCheck

# Oracle source -> table whose seed-time max id is in scenario["baseline"].
BASELINE_TABLE = {
    "payables": "payables",
    "vendors": "vendors",
    "employees": "employees",
    "tickets": "tickets",
    "ticket_comments": "ticket_comments",
    "team_messages": "team_messages",
    "sent_email": "mail_messages",
    "outbound_email": "outbound_mail",
}
MESSAGE_SOURCE = {
    "team_message": "team_messages",
    "sent_email": "sent_email",
    "outbound_email": "outbound_email",
}


@dataclass(frozen=True)
class CheckResult:
    check: str
    passed: bool
    detail: str


def oracle_url() -> str:
    url = os.environ.get("ORACLE_URL") or dotenv_values(".env").get("ORACLE_URL")
    return url or "http://127.0.0.1:8109"


class Oracle:
    def __init__(self, url: str | None = None) -> None:
        self.url = (url or oracle_url()).rstrip("/")

    def scenario(self) -> dict[str, Any]:
        data: dict[str, Any] = self._get("/scenario")
        return data

    def records(self, source: str, **filters: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._get(f"/records/{source}", filters)["rows"]
        return rows

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = httpx.get(f"{self.url}{path}", params=params, timeout=15)
        response.raise_for_status()
        return response.json()


def _text_values(row: dict[str, Any]) -> str:
    return " ".join(str(v) for v in row.values() if isinstance(v, str)).lower()


def _record(check: RecordCheck, oracle: Oracle, baseline: dict[str, int]) -> CheckResult:
    filters: dict[str, Any] = {k: v for k, v in check.match.items() if v is not None}
    if check.created_during_run:
        filters["after_id"] = baseline[BASELINE_TABLE[check.source]]
    rows = oracle.records(check.source, **filters)
    rows = [r for r in rows if all(r.get(k) is None for k, v in check.match.items() if v is None)]
    if check.text:
        rows = [r for r in rows if check.text.lower() in _text_values(r)]
    label = f"{check.source} {check.match}" + (f" mentioning {check.text!r}" if check.text else "")
    if len(rows) != check.count:
        return CheckResult(label, False, f"expected {check.count} row(s), found {len(rows)}")
    wrong = [
        f"{field}={row.get(field)!r} (expected {expected!r})"
        for row in rows
        for field, expected in check.fields.items()
        if (None if row.get(field) is None else str(row.get(field))) != expected
    ]
    if wrong:
        return CheckResult(label, False, "; ".join(wrong))
    return CheckResult(label, True, f"{len(rows)} row(s) as expected")


def _unchanged(check: UnchangedCheck, oracle: Oracle) -> CheckResult:
    label = f"{check.source} {check.match} unchanged {check.fields}"
    targets = oracle.records(check.source, **check.match)
    if not targets:
        return CheckResult(label, False, "record not found")
    changed = [
        f"{a['field']}: {a['old_value']!r} -> {a['new_value']!r}"
        for t in targets
        for a in oracle.records(
            "audit_log", entity=check.entity, record_id=str(t["id"]), action="update"
        )
        if a["field"] in check.fields
    ]
    if changed:
        return CheckResult(label, False, "changed: " + "; ".join(changed))
    return CheckResult(label, True, "no changes recorded")


def _message(check: MessageCheck, oracle: Oracle, baseline: dict[str, int]) -> CheckResult:
    source = MESSAGE_SOURCE[check.kind]
    rows = oracle.records(source, after_id=baseline[BASELINE_TABLE[source]])
    if check.kind == "team_message":
        rows = [r for r in rows if r["channel"] == check.to]
    elif check.kind == "outbound_email":
        rows = [r for r in rows if r["to_addr"] == check.to]
    else:
        rows = [r for r in rows if check.to in r["to_addr"]]
    matching = [
        r
        for r in rows
        if all(
            s.lower() in f"{r.get('subject', '')} {r['body']}".lower() for s in check.must_contain
        )
    ]
    label = f"{check.kind} to {check.to} containing {check.must_contain}"
    if len(matching) != check.count:
        return CheckResult(
            label, False, f"expected {check.count}, found {len(matching)} ({len(rows)} sent there)"
        )
    return CheckResult(label, True, f"{len(matching)} as expected")


def score(task: Task, oracle: Oracle, scenario: dict[str, Any]) -> list[CheckResult]:
    baseline: dict[str, int] = scenario["baseline"]
    end = task.expected_end_state
    return [
        *(_record(c, oracle, baseline) for c in end.records),
        *(_unchanged(c, oracle) for c in end.unchanged),
        *(_message(c, oracle, baseline) for c in end.messages),
    ]
