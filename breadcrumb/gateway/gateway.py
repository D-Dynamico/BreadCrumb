"""The gateway: the only way a commit reaches the world (AGENT_DESIGN.md section 5).

`breadcrumb.gateway.declare` turns a commit into a declaration against the contract
(scope, keys from facts, provenance, risk tier). This module then makes it happen
safely: the natural-key check before anything is sent, a durable approval for tier
2, write-ahead journaling around the dispatch, and settling by looking whenever the
outcome is unclear. The lookup used before dispatch is the one used on resume, so
"does this already exist" is asked one way before and after (D31, D38).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from breadcrumb.gateway.declare import Declaration, GatewayRefusal
from breadcrumb.journal.journal import Entry, Journal
from breadcrumb.journal.machine import (
    IN_FLIGHT,
    State,
    Unanswerable,
    Verdict,
    classify_outcome,
    decide,
)
from breadcrumb.runs.crash import CrashPoints

__all__ = ["Committed", "Declaration", "Fired", "Gateway", "GatewayRefusal"]

Lookup = Callable[[dict[str, Any]], list[dict[str, Any]]]
# Blocks until the requester decides on this entry's diff: (approved, reason).
Approver = Callable[[Entry, Declaration], tuple[bool, str]]


@dataclass
class Fired:
    """What a tool reported after trying the effect."""

    observation: str
    statuses: list[int | None]  # HTTP status of each write sent; None: no answer
    error: str = ""


@dataclass
class Committed:
    ok: bool
    outcome: str
    entry: Entry | None = None
    observation: str | None = None
    escalate: str = ""


@dataclass
class Settled:
    entry: Entry
    text: str


@dataclass
class Settlement:
    entries: list[Settled] = field(default_factory=list)
    escalate: str = ""


def _deny(_entry: Entry, _decl: Declaration) -> tuple[bool, str]:
    return False, "no one can approve here"


class Gateway:
    def __init__(
        self,
        journal: Journal,
        lookup: Lookup,
        crash: CrashPoints | None = None,
        approver: Approver = _deny,
    ) -> None:
        self.journal = journal
        self.lookup = lookup
        self.crash = crash or CrashPoints("")
        self.approver = approver

    # -- looking ------------------------------------------------------------------------
    def _look(
        self,
        kind: str,
        key: dict[str, Any],
        values: dict[str, Any],
        lookup: dict[str, Any],
        before: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> Verdict:
        def records(spec: dict[str, Any]) -> list[dict[str, Any]]:
            found = self.lookup(spec)
            if strict and found:
                # Before acting, every declared field must be one the lookup can see, or
                # a later reconcile could not compare it.
                missing = [f for f in [*key, *values] if f not in found[0]]
                if missing:
                    raise Unanswerable(
                        f"{spec.get('operation')} returns no field {', '.join(missing)}; "
                        f"its fields are {', '.join(found[0])}"
                    )
            return found

        verdict = decide(kind, records(lookup), key, values, before)
        if verdict.found == 0 and lookup.get("params"):
            # Filters are only a hint: a wrong filter must not hide an existing record.
            verdict = decide(kind, records({**lookup, "params": {}}), key, values, before)
        return verdict

    def _settle(self, entry: Entry) -> None:
        """Answer "did this happen?" for an in-flight entry, by looking."""
        if entry.state is State.INTENDED:
            self.journal.record(entry, State.NOT_APPLIED, "never sent: stopped before dispatch")
            return
        if entry.state is State.DISPATCHED:
            self.journal.record(entry, State.UNKNOWN, "stopped after sending, before the outcome")
        if entry.lookup is None:
            raise Unanswerable("there is no way to look this effect up")
        verdict = self._look(entry.kind, entry.key, entry.values, entry.lookup, entry.before)
        self.journal.record(entry, verdict.state, verdict.reason, {"found": verdict.record})

    def settle_in_flight(self) -> Settlement:
        """On resume: settle every entry that was in flight when the run stopped."""
        settlement = Settlement()
        for entry in self.journal.entries():
            if entry.state not in IN_FLIGHT:
                continue
            try:
                self._settle(entry)
                settlement.entries.append(Settled(entry, f"{entry.state.value}: {entry.note}"))
            except Unanswerable as exc:
                settlement.entries.append(Settled(entry, f"still UNKNOWN: {exc}"))
            if entry.state in (State.CONFLICT, State.UNKNOWN):
                settlement.escalate += _question(entry)
        return settlement

    # -- committing ---------------------------------------------------------------------
    def commit(self, step: int, decl: Declaration, fire: Callable[[], Fired]) -> Committed:
        entry = self.journal.find(decl.idempotency_key)
        if entry is not None and entry.state in IN_FLIGHT:
            try:
                self._settle(entry)
            except Unanswerable:
                return Committed(False, "Not done.", entry, escalate=_question(entry))
        if entry is not None and entry.state is State.CONFIRMED:
            return Committed(
                True,
                f"Skipped: already done earlier in this run ({entry.note}). Do not repeat it.",
                entry,
            )
        if entry is not None and entry.state is State.CONFLICT:
            return Committed(False, "Not done.", entry, escalate=_question(entry))
        if entry is not None and entry.state is State.REJECTED:
            return Committed(
                False, f"Not done: the user rejected this earlier ({entry.note}).", entry
            )
        if entry is None:
            entry = Entry.new(
                run_id=self.journal.run_id,
                step_no=step,
                action=decl.action,
                kind=decl.kind,
                description=decl.description,
                key=decl.key,
                values=decl.values,
                lookup=decl.lookup,
                idempotency_key=decl.idempotency_key,
            )

        refused = self._check_first(entry, decl)
        if refused is not None:
            return refused
        if decl.tier == 2:
            if entry.state is not State.AWAITING_APPROVAL:
                self.journal.record(entry, State.AWAITING_APPROVAL, decl.tier_reason, step_no=step)
            approved, reason = self.approver(entry, decl)
            if not approved:
                self.journal.record(entry, State.REJECTED, reason or "rejected")
                answer = f" The user said: {reason}" if reason else ""
                return Committed(False, f"Not done: the user rejected this.{answer}", entry)
            # The world may have changed while waiting: check the key again.
            refused = self._check_first(entry, decl)
            if refused is not None:
                return refused

        self.crash.next_commit()
        self.crash.reached("before_intended")
        self.journal.record(entry, State.INTENDED, step_no=step)
        self.crash.reached("after_intended")
        self.journal.record(entry, State.DISPATCHED)
        fired = fire()
        self.crash.reached("after_dispatch")

        state, reason = classify_outcome(fired.statuses)
        if fired.error:
            reason = f"{reason}; {fired.error}"
        evidence = {"statuses": fired.statuses}
        if state is not State.UNKNOWN:
            self.journal.record(entry, state, reason, evidence)
            if state is State.CONFIRMED:
                return Committed(True, f"Done ({reason}).", entry, fired.observation)
            return Committed(False, f"Not done ({reason}).", entry, fired.observation)

        self.journal.record(entry, State.UNKNOWN, reason, evidence)
        try:
            self._settle(entry)
        except Unanswerable:
            return Committed(False, "Not done.", entry, fired.observation, _question(entry))
        if entry.state is State.CONFIRMED:
            text = f"Done: the answer was unclear ({reason}), but looking shows it went through."
            return Committed(True, text, entry, fired.observation)
        if entry.state is State.NOT_APPLIED:
            text = f"Not done: {reason}, and looking shows it did not go through. You may retry."
            return Committed(False, text, entry, fired.observation)
        return Committed(False, "Not done.", entry, fired.observation, _question(entry))

    def _check_first(self, entry: Entry, decl: Declaration) -> Committed | None:
        """The natural-key check before dispatch. Returns a result if nothing should be sent."""
        try:
            verdict = self._look(decl.kind, decl.key, decl.values, decl.lookup, strict=True)
        except Unanswerable as exc:
            return Committed(
                False,
                f"Not done: could not check first whether it already exists ({exc}). "
                "Try again; if it keeps failing, explain in finish.",
            )
        if decl.kind == "update":
            if verdict.found != 1:
                return Committed(False, f"Not done: the record to update was not found once "
                                 f"({verdict.found} matches).")  # fmt: skip
            if verdict.state is State.CONFIRMED:
                self.journal.record(entry, State.CONFIRMED, "already had these values",
                                    {"found": verdict.record})  # fmt: skip
                return Committed(True, "Skipped: the record already has these values.", entry)
            assert verdict.record is not None
            entry.before = {k: verdict.record.get(k) for k in decl.values}
            return None
        if verdict.found == 0:
            return None
        if verdict.state is State.CONFIRMED:
            self.journal.record(entry, State.CONFIRMED, "already existed before this action",
                                {"found": verdict.record})  # fmt: skip
            return Committed(
                True,
                f"Skipped: this already exists ({_short(verdict.record)}). Not created again.",
                entry,
            )
        return Committed(
            False,
            f"Not done: a record with this key already exists ({verdict.reason}). Check "
            "whether it is the same item; if you cannot tell, stop and explain in finish.",
        )


def _short(record: dict[str, Any] | None) -> str:
    text = json.dumps(record or {}, ensure_ascii=False)
    return text if len(text) <= 200 else text[:197] + "..."


def _question(entry: Entry) -> str:
    return (
        f"Could not tell whether this happened: {entry.description or entry.action} "
        f"(journal {entry.entry_id}, {entry.state.value}: {entry.note}). "
        "Please check the app and tell me whether it went through.\n"
    )
