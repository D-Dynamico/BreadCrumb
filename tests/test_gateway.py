"""The gateway around every commit: duplicate check, write-ahead journal, crash
points, and settling effects that were in flight when a run stopped.

The "app" here is a list of records; a dispatch adds one, as a form post would.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from breadcrumb.gateway.gateway import (
    Declaration,
    Fired,
    Gateway,
    GatewayRefusal,
    declaration_from,
)
from breadcrumb.journal.journal import Journal
from breadcrumb.journal.machine import State, Unanswerable
from breadcrumb.runs.crash import CrashPoints
from breadcrumb.runs.store import RunStore


class Crashed(Exception):
    pass


class World:
    """A fake app: records, a lookup that filters like an API, and a form that saves."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = [
            {"id": 1, "vendor_name": "North Co", "invoice_no": "INV-1", "amount": "100.00"}
        ]
        self.dispatches = 0

    def lookup(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        if spec.get("operation") != "listPayables":
            raise Unanswerable(f"no read operation {spec.get('operation')!r}")
        params = spec.get("params") or {}
        return [r for r in self.records if all(str(r.get(k)) == str(v) for k, v in params.items())]

    def saver(self, record: dict[str, Any], status: int | None = 303) -> Any:
        def fire() -> Fired:
            self.dispatches += 1
            if status is None or status < 400 or status >= 500:  # saved, maybe silently
                self.records.append({"id": len(self.records) + 1, **record})
            return Fired("page after submit", [status])

        return fire


NEW = {"vendor_name": "North Co", "invoice_no": "INV-2", "amount": "18927.20"}


def _decl(**over: Any) -> Declaration:
    args: dict[str, Any] = {
        "description": "enter invoice INV-2",
        "effect": "create",
        "key_json": '{"vendor_name": "North Co", "invoice_no": "INV-2"}',
        "values_json": '{"amount": "18927.20"}',
        "lookup_operation": "listPayables",
        "lookup_params_json": '{"invoice_no": "INV-2"}',
        **over,
    }
    return declaration_from("browser_submit", args, run_id="r1", reference="BC-AAAA", channel="ops")


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    store = RunStore(tmp_path / "runs.db", lease_timeout=30)
    store.create_run("r1", "t", "BC-AAAA", pid=1)
    return store


def _gateway(store: RunStore, world: World, crash: str = "") -> Gateway:
    def die(point: str) -> None:
        raise Crashed(point)

    return Gateway(Journal(store, "r1"), world.lookup, CrashPoints(crash, exit_hook=die))


def _states(store: RunStore) -> list[list[str]]:
    return [[h[0] for h in e.history] for e in Journal(store, "r1").entries()]


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
    assert done.ok
    assert world.dispatches == 0
    assert "already" in done.outcome
    assert _states(store) == [["PROPOSED", "CONFIRMED"]]


def test_a_record_there_with_other_values_is_refused_not_created(store: RunStore) -> None:
    world = World()
    world.records.append({"id": 2, **NEW, "amount": "1.00"})
    done = _gateway(store, world).commit(5, _decl(), world.saver(NEW))
    assert not done.ok
    assert world.dispatches == 0
    assert "amount" in done.outcome
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
    failed = gateway.commit(5, _decl(), world.saver(NEW, status=422))
    assert not failed.ok
    retry = gateway.commit(6, _decl(), world.saver(NEW))
    assert retry.ok and retry.entry is not None
    assert retry.entry.attempt_no == 2
    assert len(Journal(store, "r1").entries()) == 1


def test_a_server_error_is_settled_by_looking(store: RunStore) -> None:
    world = World()
    done = _gateway(store, world).commit(5, _decl(), world.saver(NEW, status=500))
    assert done.ok
    assert _states(store) == [["PROPOSED", "INTENDED", "DISPATCHED", "UNKNOWN", "CONFIRMED"]]


def test_a_create_must_say_how_to_find_it(store: RunStore) -> None:
    with pytest.raises(GatewayRefusal, match="lookup_operation"):
        _decl(lookup_operation="")
    with pytest.raises(GatewayRefusal, match="key_json"):
        _decl(key_json="{}")


def test_a_lookup_the_app_does_not_offer_is_refused_before_anything_happens(
    store: RunStore,
) -> None:
    world = World()
    done = _gateway(store, world).commit(5, _decl(lookup_operation="listThings"), world.saver(NEW))
    assert not done.ok and "listThings" in done.outcome
    assert world.dispatches == 0
    assert _states(store) == []


def test_value_fields_the_lookup_cannot_see_are_refused_before_anything_happens(
    store: RunStore,
) -> None:
    world = World()
    done = _gateway(store, world).commit(5, _decl(values_json='{"total": "1"}'), world.saver(NEW))
    assert not done.ok
    assert "total" in done.outcome and "amount" in done.outcome  # names the real fields
    assert world.dispatches == 0
    assert _states(store) == []


def test_a_narrow_lookup_that_misses_falls_back_to_the_whole_list(store: RunStore) -> None:
    world = World()
    world.records.append({"id": 2, **NEW})
    wrong = _decl(lookup_params_json='{"invoice_no": "INV-0002"}')
    done = _gateway(store, world).commit(5, wrong, world.saver(NEW))
    assert done.ok and world.dispatches == 0


def test_messages_are_keyed_by_the_run_reference() -> None:
    decl = declaration_from(
        "notify", {"message": "Done"}, run_id="r1", reference="BC-AAAA", channel="ops"
    )
    assert decl.kind == "send"
    assert decl.key == {"channel": "ops", "ref": "BC-AAAA"}
    assert decl.lookup == {"source": "notify"}


# -- crash points ----------------------------------------------------------------------
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
        gateway.commit(6, _decl(key_json='{"invoice_no": "INV-3"}'), world.saver(NEW))


# -- resume: settle what was in flight, then finish without duplicates ------------------
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

    # The worker, told what is confirmed, may still try again: no second record.
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
    settled = resumed.settle_in_flight()
    assert settled.entries[0].entry.state is State.NOT_APPLIED
    done = resumed.commit(9, _decl(), world.saver(NEW))
    assert done.ok and done.entry is not None and done.entry.attempt_no == 2
    assert [r["invoice_no"] for r in world.records].count("INV-2") == 1


def test_an_effect_that_cannot_be_looked_up_escalates(store: RunStore) -> None:
    world = World()
    other = _decl(effect="other", key_json="", lookup_operation="")
    with pytest.raises(Crashed):
        _gateway(store, world, crash="after_dispatch").commit(5, other, world.saver(NEW))
    settled = _gateway(store, world).settle_in_flight()
    assert settled.escalate
    assert Journal(store, "r1").entries()[0].state is State.UNKNOWN
