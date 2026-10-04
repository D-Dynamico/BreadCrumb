"""The action state machine and the rules that settle an effect (DURABILITY.md).

Everything here is pure: no database, no network. The journal stores what these
rules decide; the gateway and reconcile feed them observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from breadcrumb.ledger.values import same_value


class State(StrEnum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"
    INTENDED = "INTENDED"
    DISPATCHED = "DISPATCHED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLIED = "NOT_APPLIED"
    CONFLICT = "CONFLICT"


_ALLOWED: dict[State, frozenset[State]] = {
    # A create the duplicate check finds already there goes straight to CONFIRMED.
    State.PROPOSED: frozenset({State.AWAITING_APPROVAL, State.INTENDED, State.CONFIRMED}),
    # After approval the natural key is checked again and may already be there.
    State.AWAITING_APPROVAL: frozenset({State.INTENDED, State.REJECTED, State.CONFIRMED}),
    # DISPATCHED is written before the tool fires, so an INTENDED entry found on
    # resume was never sent.
    State.INTENDED: frozenset({State.DISPATCHED, State.NOT_APPLIED}),
    State.DISPATCHED: frozenset({State.CONFIRMED, State.FAILED, State.UNKNOWN}),
    State.UNKNOWN: frozenset({State.CONFIRMED, State.NOT_APPLIED, State.CONFLICT}),
    # Retryable: a new attempt, unless the duplicate check finds it done after all.
    State.FAILED: frozenset({State.INTENDED, State.CONFIRMED}),
    State.NOT_APPLIED: frozenset({State.INTENDED, State.CONFIRMED}),
    State.CONFIRMED: frozenset(),
    State.CONFLICT: frozenset(),
    State.REJECTED: frozenset(),
}

RETRYABLE = frozenset({State.FAILED, State.NOT_APPLIED})
IN_FLIGHT = frozenset({State.INTENDED, State.DISPATCHED, State.UNKNOWN})


class IllegalTransition(Exception):
    """A state change the machine does not allow; always a bug, never a run outcome."""


class Unanswerable(Exception):
    """Looking cannot tell whether the effect happened, so a person must decide."""


def check_transition(old: State, new: State) -> None:
    if new not in _ALLOWED[old]:
        raise IllegalTransition(f"{old} -> {new} is not allowed")


def classify_outcome(statuses: list[int | None]) -> tuple[State, str]:
    """Settle a dispatch from the HTTP statuses of the writes it sent.

    `None` means a request went out and no response came back. Standard HTTP
    meaning only: 2xx and 3xx saved, 4xx refused, 5xx or silence is unknown.
    """
    if not statuses:
        return State.FAILED, "nothing was sent"
    if any(s is None or s >= 500 for s in statuses):
        return State.UNKNOWN, "sent, but no clear answer came back"
    refused = [s for s in statuses if s is not None and s >= 400]
    if refused:
        return State.FAILED, f"refused with HTTP {refused[0]}"
    return State.CONFIRMED, f"accepted with HTTP {statuses[-1]}"


@dataclass
class Verdict:
    state: State
    reason: str
    record: dict[str, Any] | None = None
    differences: dict[str, Any] = field(default_factory=dict)
    found: int = 0  # how many records match the key


def _differences(record: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    missing = [k for k in values if k not in record]
    if missing:
        raise Unanswerable(f"the records found have no field {', '.join(missing)}")
    return {k: record[k] for k, v in values.items() if not same_value(record[k], v)}


def decide(
    kind: str,
    records: list[dict[str, Any]],
    key: dict[str, Any],
    values: dict[str, Any],
    before: dict[str, Any] | None = None,
) -> Verdict:
    """Did this effect happen? Answered from the records an app returned."""
    if not key:
        raise Unanswerable("the effect has no key to look it up by")
    if records:
        missing = [k for k in key if k not in records[0]]
        if missing:
            raise Unanswerable(f"the records found have no field {', '.join(missing)}")
    matches = [r for r in records if all(same_value(r.get(k), v) for k, v in key.items())]
    if len(matches) > 1:
        return Verdict(State.CONFLICT, f"{len(matches)} records match the key", found=len(matches))
    if not matches:
        if kind == "update":
            return Verdict(State.CONFLICT, "the record to update is not there")
        return Verdict(State.NOT_APPLIED, "no record matches the key")
    record = matches[0]
    differences = _differences(record, values)
    if not differences:
        return Verdict(State.CONFIRMED, "found, with the intended values", record, found=1)
    if kind == "update" and before and not _differences(record, before):
        return Verdict(State.NOT_APPLIED, "found, still with the old values", record, found=1)
    shown = ", ".join(f"{k}={v!r}" for k, v in differences.items())
    reason = f"found, but with other values: {shown}"
    return Verdict(State.CONFLICT, reason, record, differences, found=1)
