"""The gateway around every commit: contract scope, provenance, risk tiers and
approvals, the duplicate check, write-ahead journal, crash points, and settling
effects that were in flight when a run stopped.

The "app" here is a list of records; a dispatch adds one, as a form post would.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from breadcrumb.contract.model import Contract
from breadcrumb.executor.state import Fact
from breadcrumb.gateway.declare import Declaration, GatewayRefusal, Policy, declare
from breadcrumb.gateway.gateway import Fired, Gateway
from breadcrumb.journal.journal import Entry, Journal
from breadcrumb.journal.machine import State, Unanswerable
from breadcrumb.runs.crash import CrashPoints
from breadcrumb.runs.store import RunStore


class Crashed(Exception):
    pass


CONTRACT = Contract.model_validate(
    {
        "goal": "enter the bill and say so",
        "facts": [
            {"key": "supplier", "type": "text"},
            {"key": "doc_no", "type": "id"},
            {"key": "total", "type": "money"},
        ],
        "deliverables": [
            {
                "id": "entered",
                "kind": "create",
                "lookup_operation": "listPayables",
                "key": [
                    {"field": "vendor_name", "fact": "supplier"},
                    {"field": "invoice_no", "fact": "doc_no"},
                ],
                "values": [{"field": "amount", "fact": "total"}],
            },
            {
                "id": "told",
                "kind": "send",
                "lookup_operation": "notify",
                "must_contain": ["doc_no"],
            },
        ],
    }
)
POLICY = Policy(approval_threshold=100000, internal_domain="acme.test")


def _facts(total: str = "18927.20", doc_no: str = "INV-2") -> dict[str, Fact]:
    return {
        "supplier": Fact("supplier", "North Co", "mail 3", 1, "text"),
        "doc_no": Fact("doc_no", doc_no, "a.pdf p1 L5", 2, "id"),
        "total": Fact("total", total, "a.pdf p1 L9", 2, "money"),
        "due": Fact("due", "15 Nov 2026", "a.pdf p1 L7", 2, "date"),
    }


NEW = {"vendor_name": "North Co", "invoice_no": "INV-2", "amount": "18927.20"}
TYPED = [
    ('textbox "Invoice number"', "INV-2"),
    ('textbox "Amount"', "18927.20"),
    ('textbox "Due date"', "2026-11-15"),
]


def _decl(
    action: str = "browser_submit",
    facts: dict[str, Fact] | None = None,
    typed: list[tuple[str, str]] | None = None,
    **args: Any,
) -> Declaration:
    return declare(
        action,
        {"deliverable": "entered", "description": "save it", **args},
        run_id="r1",
        reference="BC-AAAA",
        channel="ops",
        contract=CONTRACT,
        facts=facts or _facts(),
        typed=TYPED if typed is None else typed,
        policy=POLICY,
    )


class World:
    """A fake app: records, a lookup like an API's, and a form that saves."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = [
            {"id": 1, "vendor_name": "North Co", "invoice_no": "INV-1", "amount": "100.00"}
        ]
        self.dispatches = 0

    def lookup(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        if spec.get("operation") != "listPayables":
            raise Unanswerable(f"no read operation {spec.get('operation')!r}")
        return list(self.records)

    def saver(self, record: dict[str, Any], status: int | None = 303) -> Any:
        def fire() -> Fired:
            self.dispatches += 1
            if status is None or status < 400 or status >= 500:  # saved, maybe silently
                self.records.append({"id": len(self.records) + 1, **record})
            return Fired("page after submit", [status])

        return fire


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    store = RunStore(tmp_path / "runs.db", lease_timeout=30)
    store.create_run("r1", "t", "BC-AAAA", pid=1)
    return store


def _gateway(store: RunStore, world: World, crash: str = "", approver: Any = None) -> Gateway:
    def die(point: str) -> None:
        raise Crashed(point)

    kwargs = {"approver": approver} if approver else {}
    return Gateway(Journal(store, "r1"), world.lookup, CrashPoints(crash, exit_hook=die), **kwargs)


def _states(store: RunStore) -> list[list[str]]:
    return [[h[0] for h in e.history] for e in Journal(store, "r1").entries()]


# -- declaring against the contract ----------------------------------------------------------
def test_keys_values_and_lookup_come_from_the_contract_and_facts() -> None:
    decl = _decl()
    assert decl.key == {"vendor_name": "North Co", "invoice_no": "INV-2"}
    assert decl.values == {"amount": "18927.20"}
    assert decl.lookup["operation"] == "listPayables"
    assert decl.tier == 1


def test_a_change_outside_the_contract_is_refused() -> None:
    with pytest.raises(GatewayRefusal, match="outside the contract"):
        _decl(deliverable="update_bank_account")
    with pytest.raises(GatewayRefusal, match="outside the contract"):
        _decl(deliverable="")


def test_a_missing_fact_is_refused_with_its_key() -> None:
    facts = {k: v for k, v in _facts().items() if k != "total"}
    with pytest.raises(GatewayRefusal, match="total"):
        _decl(facts=facts)


def test_a_fact_in_conflict_is_refused() -> None:
    facts = _facts()
    facts["total"].conflict = "'18,972.20' from mail 3"
    with pytest.raises(GatewayRefusal, match="conflict"):
        _decl(facts=facts)


def test_typed_values_need_a_recorded_fact() -> None:
    # The date is typed in another format than it was read: still the same fact.
    _decl(typed=[('textbox "Due"', "2026-11-15")])
    with pytest.raises(GatewayRefusal, match=r"18927.02"):
        _decl(typed=[('textbox "Amount"', "18927.02")])  # transposed digits
    with pytest.raises(GatewayRefusal, match="2026-11-16"):
        _decl(typed=[('textbox "Due"', "2026-11-16")])


def test_large_amounts_and_outside_recipients_are_tier_2() -> None:
    big = _facts(total="3,74,699.56")
    decl = _decl(facts=big, typed=[('textbox "Amount"', "374699.56")])
    assert decl.tier == 2 and "approval limit" in decl.tier_reason
    facts = {**_facts(), "to": Fact("to", "x@example.com", "mail", 1, "email")}
    decl = _decl(facts=facts, typed=[('textbox "To"', "x@example.com")])
    assert decl.tier == 2 and "outside" in decl.tier_reason


def test_messages_must_name_the_contract_facts() -> None:
    def notify(message: str) -> Declaration:
        return declare(
            "notify",
            {"message": message},
            run_id="r1",
            reference="BC-AAAA",
            channel="ops",
            contract=CONTRACT,
            facts=_facts(),
            typed=[],
            policy=POLICY,
        )

    decl = notify("Entered INV-2.")
    assert decl.kind == "send" and decl.key == {"channel": "ops", "ref": "BC-AAAA"}
    with pytest.raises(GatewayRefusal, match="mention"):
        notify("All done.")


# -- committing -------------------------------------------------------------------------------
def test_a_create_is_journaled_before_and_after_it_happens(store: RunStore) -> None:
    world = World()
    done = _gateway(store, world).commit(5, _decl(), world.saver(NEW))
    assert done.ok and done.entry is not None
    assert world.dispatches == 1
    assert _states(store) == [["PROPOSED", "INTENDED", "DISPATCHED", "CONFIRMED"]]


def test_a_record_already_there_is_not_created_again(store: RunStore) -> None:
    world = World()
    world.records.append({"id": 2, **NEW})
    done = _gateway(store, world).commit(5, _decl(), world.saver(NEW))
    assert done.ok and world.dispatches == 0 and "already" in done.outcome
    assert _states(store) == [["PROPOSED", "CONFIRMED"]]


def test_a_record_there_with_other_values_is_refused_not_created(store: RunStore) -> None:
    world = World()
    world.records.append({"id": 2, **NEW, "amount": "1.00"})
    done = _gateway(store, world).commit(5, _decl(), world.saver(NEW))
    assert not done.ok and world.dispatches == 0 and "amount" in done.outcome
    assert _states(store) == []


def test_a_confirmed_effect_is_never_redone_in_the_same_run(store: RunStore) -> None:
    world = World()
    gateway = _gateway(store, world)
    gateway.commit(5, _decl(), world.saver(NEW))
    world.records.clear()  # even if the world forgot, the journal remembers
    again = gateway.commit(8, _decl(), world.saver(NEW))
    assert again.ok and "already done" in again.outcome
    assert world.dispatches == 1


def test_a_validation_error_fails_and_a_retry_is_attempt_two(store: RunStore) -> None:
    world = World()
    gateway = _gateway(store, world)
    assert not gateway.commit(5, _decl(), world.saver(NEW, status=422)).ok
    retry = gateway.commit(6, _decl(), world.saver(NEW))
    assert retry.ok and retry.entry is not None and retry.entry.attempt_no == 2
    assert len(Journal(store, "r1").entries()) == 1


def test_a_server_error_is_settled_by_looking(store: RunStore) -> None:
    world = World()
    done = _gateway(store, world).commit(5, _decl(), world.saver(NEW, status=500))
    assert done.ok
    assert _states(store) == [["PROPOSED", "INTENDED", "DISPATCHED", "UNKNOWN", "CONFIRMED"]]


def test_a_lookup_that_cannot_see_the_fields_is_refused_before_anything_happens(
    store: RunStore,
) -> None:
    world = World()
    world.records = [{"id": 1, "invoice_no": "INV-1"}]  # no vendor_name, no amount
    done = _gateway(store, world).commit(5, _decl(), world.saver(NEW))
    assert not done.ok and "vendor_name" in done.outcome
    assert world.dispatches == 0 and _states(store) == []


# -- approvals (tier 2) ------------------------------------------------------------------------
BIG = {**NEW, "amount": "374699.56"}


def _big() -> Declaration:
    return _decl(facts=_facts(total="374699.56"), typed=[('textbox "Amount"', "374699.56")])


def test_tier_2_waits_for_approval_then_goes_through(store: RunStore) -> None:
    world = World()
    asked: list[dict[str, Any]] = []

    def approver(entry: Entry, decl: Declaration) -> tuple[bool, str]:
        asked.append(decl.diff)
        assert entry.state is State.AWAITING_APPROVAL and world.dispatches == 0
        return True, ""

    done = _gateway(store, world, approver=approver).commit(5, _big(), world.saver(BIG))
    assert done.ok and world.dispatches == 1
    assert asked[0]["values"] == {"amount": "374699.56"}
    assert _states(store) == [
        ["PROPOSED", "AWAITING_APPROVAL", "INTENDED", "DISPATCHED", "CONFIRMED"]
    ]


def test_a_rejected_change_is_never_made(store: RunStore) -> None:
    world = World()
    gateway = _gateway(store, world, approver=lambda e, d: (False, "wrong vendor"))
    done = gateway.commit(5, _big(), world.saver(BIG))
    assert not done.ok and "wrong vendor" in done.outcome and world.dispatches == 0
    again = gateway.commit(6, _big(), world.saver(BIG))
    assert not again.ok and "rejected" in again.outcome and world.dispatches == 0


def test_after_approval_the_key_is_checked_again(store: RunStore) -> None:
    world = World()

    def approver(entry: Entry, decl: Declaration) -> tuple[bool, str]:
        world.records.append({"id": 9, **BIG})  # someone entered it while we waited
        return True, ""

    done = _gateway(store, world, approver=approver).commit(5, _big(), world.saver(BIG))
    assert done.ok and world.dispatches == 0
    assert _states(store) == [["PROPOSED", "AWAITING_APPROVAL", "CONFIRMED"]]


def test_a_crash_while_waiting_keeps_the_entry_waiting(store: RunStore) -> None:
    world = World()

    def approver(entry: Entry, decl: Declaration) -> tuple[bool, str]:
        raise Crashed("during_approval")

    with pytest.raises(Crashed):
        _gateway(store, world, approver=approver).commit(5, _big(), world.saver(BIG))
    assert _states(store) == [["PROPOSED", "AWAITING_APPROVAL"]]
    # Resumed, the same deliverable and key find the same entry; once approved it runs.
    resumed = _gateway(store, world, approver=lambda e, d: (True, "approved earlier"))
    assert resumed.settle_in_flight().entries == []  # a wait is not in flight
    done = resumed.commit(9, _big(), world.saver(BIG))
    assert done.ok and world.dispatches == 1
    assert _states(store) == [
        ["PROPOSED", "AWAITING_APPROVAL", "INTENDED", "DISPATCHED", "CONFIRMED"]
    ]


# -- crash points --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("point", "states", "dispatches"),
    [
        ("before_intended", [], 0),
        ("after_intended", [["PROPOSED", "INTENDED"]], 0),
        ("after_dispatch", [["PROPOSED", "INTENDED", "DISPATCHED"]], 1),
    ],
)
def test_crash_points_stop_at_the_right_moment(
    store: RunStore, point: str, states: list[list[str]], dispatches: int
) -> None:
    world = World()
    with pytest.raises(Crashed, match=point):
        _gateway(store, world, crash=point).commit(5, _decl(), world.saver(NEW))
    assert _states(store) == states
    assert world.dispatches == dispatches


def test_a_crash_point_can_target_a_later_commit(store: RunStore) -> None:
    world = World()
    gateway = _gateway(store, world, crash="after_intended:2")
    gateway.commit(5, _decl(), world.saver(NEW))
    with pytest.raises(Crashed):
        gateway.commit(6, _decl(facts=_facts(doc_no="INV-3"), typed=[]), world.saver(NEW))


# -- resume: settle what was in flight, then finish without duplicates ----------------------
@pytest.mark.parametrize(
    ("point", "settled_as"),
    [("after_intended", State.NOT_APPLIED), ("after_dispatch", State.CONFIRMED)],
)
def test_resume_settles_in_flight_effects_and_never_duplicates(
    store: RunStore, point: str, settled_as: State
) -> None:
    world = World()
    with pytest.raises(Crashed):
        _gateway(store, world, crash=point).commit(5, _decl(), world.saver(NEW))
    resumed = _gateway(store, world)  # a new process: no crash point
    settled = resumed.settle_in_flight()
    assert [s.entry.state for s in settled.entries] == [settled_as]
    assert not settled.escalate
    resumed.commit(9, _decl(), world.saver(NEW))
    assert [r["invoice_no"] for r in world.records].count("INV-2") == 1


def test_resume_after_dispatch_that_did_not_land_retries_once(store: RunStore) -> None:
    world = World()

    def lost() -> Fired:  # the request went out, the app never saved it
        world.dispatches += 1
        raise Crashed("after_dispatch")

    with pytest.raises(Crashed):
        _gateway(store, world).commit(5, _decl(), lost)
    resumed = _gateway(store, world)
    assert resumed.settle_in_flight().entries[0].entry.state is State.NOT_APPLIED
    done = resumed.commit(9, _decl(), world.saver(NEW))
    assert done.ok and done.entry is not None and done.entry.attempt_no == 2
    assert [r["invoice_no"] for r in world.records].count("INV-2") == 1


def test_an_in_flight_effect_that_cannot_be_looked_up_escalates(store: RunStore) -> None:
    world = World()
    with pytest.raises(Crashed):
        _gateway(store, world, crash="after_dispatch").commit(5, _decl(), world.saver(NEW))

    def blind(spec: dict[str, Any]) -> list[dict[str, Any]]:
        raise Unanswerable("the app cannot be read")

    settled = Gateway(Journal(store, "r1"), blind).settle_in_flight()
    assert settled.escalate
    assert Journal(store, "r1").entries()[0].state is State.UNKNOWN
