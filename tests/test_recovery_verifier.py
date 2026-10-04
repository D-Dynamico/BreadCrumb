"""The recovery policy table and the verifier: both deterministic, both without a model."""

from __future__ import annotations

from typing import Any

import pytest

from breadcrumb.contract.model import Check
from breadcrumb.executor.state import Fact
from breadcrumb.journal.machine import Unanswerable
from breadcrumb.recovery.policy import Situation, Strategy, choose
from breadcrumb.verifier.verifier import overall, verify


# -- recovery -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("situation", "strategy"),
    [
        (Situation("http_get", True, "HTTP 503\nService unavailable"), Strategy.RETRY),
        (Situation("http_get", True, "HTTP 200\n[]"), Strategy.NONE),
        (Situation("http_get", True, "HTTP 404\nNo such thing"), Strategy.REPLAN),
        (Situation("browser_open", True, "page loaded", page_status=503), Strategy.RETRY),
        (Situation("browser_click", True, "page loaded", page_status=502), Strategy.RETRY),
        (
            Situation("browser_open", False, "error: opening x failed: net::ERR_CONNECTION_RESET"),
            Strategy.RETRY,
        ),
        (
            Situation("browser_click", False, "error: clicking [3] failed: Timeout 10000ms"),
            Strategy.REPLAN,  # not there, rather than slow
        ),
        (Situation("browser_open", True, "page loaded", on_sign_in_page=True), Strategy.RELOGIN),
        (Situation("browser_submit", True, "Done", on_sign_in_page=True), Strategy.RELOGIN),
        (Situation("login", True, "page loaded", on_sign_in_page=True), Strategy.REPLAN),
        (Situation("browser_click", False, "error: there is no element [9]"), Strategy.REPLAN),
        (Situation("browser_submit", False, "Not done (refused with HTTP 503)"), Strategy.NONE),
        (Situation("browser_type", True, "page loaded"), Strategy.NONE),
    ],
)
def test_recovery_policy(situation: Situation, strategy: Strategy) -> None:
    assert choose(situation) is strategy


# -- verifier -------------------------------------------------------------------------
REF = "BC-TEST"
FACTS = {
    "supplier": Fact("supplier", "North Co", "mail", 1, "text"),
    "doc_no": Fact("doc_no", "INV-2", "a.pdf p1 L5", 2, "id"),
    "total": Fact("total", "₹ 18,927.20", "a.pdf p1 L9", 2, "money"),
    "due": Fact("due", "15 Nov 2026", "a.pdf p1 L7", 2, "date"),
}
KEY = {"vendor_name": "supplier", "invoice_no": "doc_no"}
CHECKS = [
    Check("record_unique", "listPayables", KEY),
    Check("field_equals", "listPayables", KEY, field="amount", fact="total"),
    Check("field_equals", "listPayables", KEY, field="due_date", fact="due"),
    Check("message_sent", "notify", {}, must_contain=("doc_no",)),
    Check("field_unchanged", "listVendors", {"name": "supplier"}, fields=("bank_account",)),
]
ENTERED = {
    "vendor_name": "North Co",
    "invoice_no": "INV-2",
    "amount": "18927.20",
    "due_date": "2026-11-15",
}
OLDER = {"vendor_name": "North Co", "invoice_no": "INV-1", "amount": "5.00", "due_date": "x"}
VENDOR = {"name": "North Co", "bank_account": "111"}


def _world(
    payables: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    vendors: list[dict[str, Any]] | None = None,
) -> Any:
    def lookup(spec: dict[str, Any]) -> list[dict[str, Any]]:
        if spec.get("source") == "notify":
            return messages
        return {"listPayables": payables, "listVendors": vendors or [VENDOR]}[spec["operation"]]

    return lookup


SENT = {"ref": REF, "body": "Entered INV-2 for you.\n\nRef: BC-TEST"}
SNAPSHOTS = {"listVendors": [VENDOR]}


def test_everything_true_is_verified() -> None:
    verdicts = verify(CHECKS, FACTS, _world([OLDER, ENTERED], [SENT]), REF, SNAPSHOTS)
    assert [v.status for v in verdicts] == ["verified"] * 5
    assert overall(verdicts) == "verified"


def test_a_false_done_is_caught() -> None:
    # Phase 2, run B: the worker said done, but only the older record exists.
    verdicts = verify(CHECKS, FACTS, _world([OLDER], [SENT]), REF, SNAPSHOTS)
    assert verdicts[0].status == "failed" and "no record" in verdicts[0].detail
    assert overall(verdicts) == "failed"


def test_a_duplicate_and_a_wrong_value_are_caught() -> None:
    wrong = {**ENTERED, "amount": "18927.02"}
    verdicts = verify(CHECKS, FACTS, _world([ENTERED, wrong], [SENT]), REF, SNAPSHOTS)
    assert verdicts[0].status == "failed" and "2 records" in verdicts[0].detail
    verdicts = verify(CHECKS, FACTS, _world([wrong], [SENT]), REF, SNAPSHOTS)
    assert verdicts[1].status == "failed" and "18927.02" in verdicts[1].detail


def test_messages_must_be_this_runs_and_name_the_facts() -> None:
    other = {"ref": "BC-ELSE", "body": "INV-2 done"}
    vague = {"ref": REF, "body": "All done.\n\nRef: BC-TEST"}
    for messages in ([other], [vague], [SENT, SENT]):
        verdicts = verify(CHECKS, FACTS, _world([ENTERED], messages), REF, SNAPSHOTS)
        assert verdicts[3].status == "failed"


def test_a_changed_protected_field_is_caught() -> None:
    changed = [{**VENDOR, "bank_account": "999"}]
    verdicts = verify(CHECKS, FACTS, _world([ENTERED], [SENT], changed), REF, SNAPSHOTS)
    assert verdicts[4].status == "failed" and "999" in verdicts[4].detail


def test_a_fact_never_recorded_fails_the_deliverable() -> None:
    facts = {k: v for k, v in FACTS.items() if k != "doc_no"}
    verdicts = verify(CHECKS, facts, _world([ENTERED], [SENT]), REF, SNAPSHOTS)
    assert verdicts[0].status == "failed" and "doc_no" in verdicts[0].detail


def test_unreadable_apps_and_judgement_checks_are_unverifiable() -> None:
    def broken(spec: dict[str, Any]) -> list[dict[str, Any]]:
        raise Unanswerable("API down")

    verdicts = verify(CHECKS[:1], FACTS, broken, REF, SNAPSHOTS)
    assert verdicts[0].status == "unverifiable"
    judged = verify([Check("judgement", "", {})], FACTS, broken, REF, {})
    assert judged[0].status == "unverifiable" and "Phase 5" in judged[0].detail
    assert overall(judged) == "unverified"
