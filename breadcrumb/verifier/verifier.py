"""The verifier: is every contract check true in the apps? (AGENT_DESIGN.md section 8)

It reads the apps afresh through the same lookups the gateway uses and decides each
check in code. It sees the contract's checks, the recorded facts and the apps, never
the worker's reasoning or step log, so it cannot be talked into agreeing. Verdicts:
`verified`, `failed` (with what was observed) or `unverifiable` (with why).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from breadcrumb.contract.model import NOTIFY, Check
from breadcrumb.executor.state import Fact
from breadcrumb.journal.machine import Unanswerable
from breadcrumb.ledger.values import same_value

Lookup = Callable[[dict[str, Any]], list[dict[str, Any]]]


@dataclass(frozen=True)
class CheckVerdict:
    check: str  # the check in plain words
    type: str
    status: str  # verified, failed or unverifiable
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def overall(verdicts: list[CheckVerdict]) -> str:
    """verified only if every check is; failed if any check is; unverified otherwise."""
    if any(v.status == "failed" for v in verdicts):
        return "failed"
    if all(v.status == "verified" for v in verdicts):
        return "verified"
    return "unverified"


class _Missing(Exception):
    pass


def verify(
    checks: list[Check],
    facts: dict[str, Fact],
    lookup: Lookup,
    reference: str,
    snapshots: dict[str, list[dict[str, Any]]],
) -> list[CheckVerdict]:
    cache: dict[str, list[dict[str, Any]]] = {}

    def records(operation: str) -> list[dict[str, Any]]:
        if operation not in cache:
            spec = {"source": "notify"} if operation == NOTIFY else {
                "source": "api", "operation": operation, "params": {}}  # fmt: skip
            cache[operation] = lookup(spec)
        return cache[operation]

    def value(fact: str) -> str:
        if fact not in facts:
            raise _Missing(fact)
        return facts[fact].value

    def matching(rows: list[dict[str, Any]], match: dict[str, str]) -> list[dict[str, Any]]:
        wanted = {f: value(k) for f, k in match.items()}
        return [r for r in rows if all(same_value(r.get(f), v) for f, v in wanted.items())]

    verdicts = []
    for check in checks:
        status, detail = "unverifiable", ""
        try:
            status, detail = _decide(check, records, value, matching, reference, snapshots)
        except _Missing as missing:
            if check.type == "field_unchanged":
                detail = f"fact {missing} was never recorded, so the record is unknown"
            else:
                status = "failed"
                detail = f"fact {missing} was never recorded, so this was not produced"
        except Unanswerable as exc:
            detail = f"could not read the app: {exc}"
        verdicts.append(CheckVerdict(check.describe(), check.type, status, detail))
    return verdicts


def _decide(
    check: Check,
    records: Callable[[str], list[dict[str, Any]]],
    value: Callable[[str], str],
    matching: Callable[[list[dict[str, Any]], dict[str, str]], list[dict[str, Any]]],
    reference: str,
    snapshots: dict[str, list[dict[str, Any]]],
) -> tuple[str, str]:
    if check.type == "record_unique":
        found = matching(records(check.lookup_operation), check.match)
        if len(found) == 1:
            return "verified", f"found record {found[0].get('id', '')}"
        if not found:
            return "failed", "no record matches"
        return "failed", f"{len(found)} records match (a duplicate)"
    if check.type == "field_equals":
        assert check.field is not None and check.fact is not None
        found = matching(records(check.lookup_operation), check.match)
        expected = value(check.fact)
        if len(found) != 1:
            return "failed", f"cannot check: {len(found)} records match"
        actual = found[0].get(check.field)
        if same_value(actual, expected):
            return "verified", f"{check.field} = {actual}"
        return "failed", f"{check.field} is {actual!r}, expected {expected!r}"
    if check.type in ("message_sent", "no_message_sent"):
        mine = [m for m in records(NOTIFY) if m.get("ref") == reference]
        if check.type == "no_message_sent":
            if mine:
                return "failed", f"{len(mine)} message(s) sent"
            return "verified", "no message sent"
        wanted = [value(k) for k in check.must_contain]
        good = [m for m in mine if all(w.lower() in str(m.get("body", "")).lower() for w in wanted)]
        if len(mine) > 1:
            return "failed", f"{len(mine)} messages sent (a duplicate)"
        if len(good) == 1:
            return "verified", f"message {good[0].get('id', '')} names {', '.join(wanted)}"
        if mine:
            return "failed", f"the message does not name {', '.join(wanted)}"
        return "failed", "no message from this run"
    if check.type == "field_unchanged":
        before_rows = snapshots.get(check.lookup_operation)
        if before_rows is None:
            return "unverifiable", "no snapshot was taken at the start"
        before = matching(before_rows, check.match)
        now = matching(records(check.lookup_operation), check.match)
        if len(before) != 1 or len(now) != 1:
            return "unverifiable", f"the record matched {len(before)} before, {len(now)} now"
        changed = [
            f"{f}: {before[0].get(f)!r} -> {now[0].get(f)!r}"
            for f in check.fields
            if not same_value(before[0].get(f), now[0].get(f))
        ]
        if changed:
            return "failed", "changed: " + "; ".join(changed)
        return "verified", f"{', '.join(check.fields)} unchanged"
    if check.type == "judgement":
        return "unverifiable", "LLM-judged checks are not built until Phase 5 (D37); not evaluated"
    return "unverifiable", f"unknown check type {check.type!r}; not evaluated"
