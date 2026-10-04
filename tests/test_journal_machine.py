"""The action state machine, outcome classification and reconcile decisions.

These are the deterministic rules the durability promise rests on (DURABILITY.md).
"""

from __future__ import annotations

import pytest

from breadcrumb.journal.machine import (
    IllegalTransition,
    State,
    Unanswerable,
    check_transition,
    classify_outcome,
    decide,
)
from breadcrumb.ledger.values import same_value

PAYABLES = [
    {"id": 1, "vendor_name": "North Co", "invoice_no": "INV-1", "amount": "100.00"},
    {"id": 2, "vendor_name": "North Co", "invoice_no": "INV-2", "amount": "18927.20"},
    {"id": 3, "vendor_name": "South Co", "invoice_no": "INV-2", "amount": "5.00"},
]
KEY = {"vendor_name": "North Co", "invoice_no": "INV-2"}


# -- transitions -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (State.PROPOSED, State.INTENDED),
        (State.PROPOSED, State.CONFIRMED),  # the duplicate check found it already there
        (State.INTENDED, State.DISPATCHED),
        (State.INTENDED, State.NOT_APPLIED),  # never dispatched, settled on resume
        (State.DISPATCHED, State.CONFIRMED),
        (State.DISPATCHED, State.FAILED),
        (State.DISPATCHED, State.UNKNOWN),
        (State.UNKNOWN, State.CONFIRMED),
        (State.UNKNOWN, State.NOT_APPLIED),
        (State.UNKNOWN, State.CONFLICT),
        (State.NOT_APPLIED, State.INTENDED),  # a new attempt
        (State.FAILED, State.INTENDED),
        (State.FAILED, State.CONFIRMED),
    ],
)
def test_allowed_transitions(old: State, new: State) -> None:
    check_transition(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (State.PROPOSED, State.DISPATCHED),  # nothing reaches the world without INTENDED
        (State.INTENDED, State.CONFIRMED),  # must be dispatched or found first
        (State.CONFIRMED, State.INTENDED),  # a confirmed effect is never redone
        (State.CONFLICT, State.NOT_APPLIED),  # only a person settles a conflict
        (State.UNKNOWN, State.INTENDED),  # never a blind retry
        (State.DISPATCHED, State.INTENDED),
    ],
)
def test_forbidden_transitions(old: State, new: State) -> None:
    with pytest.raises(IllegalTransition):
        check_transition(old, new)


# -- outcome of a dispatch ------------------------------------------------------------
@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], State.FAILED),  # nothing was sent
        ([201], State.CONFIRMED),
        ([303], State.CONFIRMED),  # a form that saved and redirected
        ([422], State.FAILED),  # a validation error shown
        ([409], State.FAILED),
        ([500], State.UNKNOWN),  # sent, the server broke: maybe committed
        ([None], State.UNKNOWN),  # sent, no answer
        ([303, None], State.UNKNOWN),
        ([303, 422], State.FAILED),
    ],
)
def test_classify_outcome(statuses: list[int | None], expected: State) -> None:
    state, reason = classify_outcome(statuses)
    assert state is expected
    assert reason


# -- reconcile decisions --------------------------------------------------------------
def test_create_found_once_with_same_values_is_confirmed() -> None:
    verdict = decide("create", PAYABLES, KEY, {"amount": "18,927.2"})
    assert verdict.state is State.CONFIRMED
    assert verdict.record == PAYABLES[1]


def test_create_absent_is_not_applied() -> None:
    verdict = decide("create", PAYABLES, {**KEY, "invoice_no": "INV-9"}, {})
    assert verdict.state is State.NOT_APPLIED


def test_create_found_with_other_values_is_a_conflict() -> None:
    verdict = decide("create", PAYABLES, KEY, {"amount": "18927.21"})
    assert verdict.state is State.CONFLICT
    assert "amount" in verdict.reason


def test_create_found_twice_is_a_conflict() -> None:
    verdict = decide("create", [*PAYABLES, PAYABLES[1]], KEY, {})
    assert verdict.state is State.CONFLICT


def test_update_reaching_intended_values_is_confirmed() -> None:
    verdict = decide("update", PAYABLES, KEY, {"amount": "18927.20"}, before={"amount": "1"})
    assert verdict.state is State.CONFIRMED


def test_update_still_at_before_values_is_not_applied() -> None:
    verdict = decide("update", PAYABLES, KEY, {"amount": "1"}, before={"amount": "18927.20"})
    assert verdict.state is State.NOT_APPLIED


def test_update_at_neither_value_is_a_conflict() -> None:
    verdict = decide("update", PAYABLES, KEY, {"amount": "1"}, before={"amount": "2"})
    assert verdict.state is State.CONFLICT


def test_update_of_a_missing_record_is_a_conflict() -> None:
    verdict = decide("update", PAYABLES, {"invoice_no": "INV-9"}, {"amount": "1"})
    assert verdict.state is State.CONFLICT


def test_a_key_field_the_records_do_not_have_cannot_be_answered() -> None:
    with pytest.raises(Unanswerable):
        decide("create", PAYABLES, {"supplier": "North Co"}, {})


def test_an_empty_key_cannot_be_answered() -> None:
    with pytest.raises(Unanswerable):
        decide("create", PAYABLES, {}, {})


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("18927.20", "18,927.2", True),
        ("₹ 1,00,000", "100000.00", True),
        ("North Co ", "north co", True),
        ("2026-09-01", "2026-09-01", True),
        ("2026-09-01", "2026-09-02", False),
        (18927.2, "18927.20", True),
        ("INV-1", "INV-01", False),
    ],
)
def test_same_value(a: object, b: object, same: bool) -> None:
    assert same_value(a, b) is same
