"""What the executor knows about a run between turns.

This is the structured state the model sees each turn, instead of a growing chat
transcript: the task, the plan, remembered facts, recent actions and the latest
observation. It is checkpointed after every step (DURABILITY.md), all but the
observation: a resumed run starts with a fresh browser anyway.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from breadcrumb.ledger.values import same_value


@dataclass
class PlanStep:
    title: str
    status: str = "todo"


SENSITIVE = frozenset({"money", "date", "id", "email"})


@dataclass
class Fact:
    """One value in the ledger, with where it was read (AGENT_DESIGN.md section 6)."""

    key: str
    value: str
    source: str
    step: int
    type: str = "text"  # money, date, id, email or text
    # Set when a sensitive fact was read with two different values; commits that use
    # it are refused until the worker states the fact again in a later step.
    conflict: str = ""


@dataclass
class StepRecord:
    step: int
    action: str
    args: dict[str, Any]
    why: str
    ok: bool
    outcome: str


@dataclass
class RunState:
    run_id: str
    task: str
    reference: str
    plan: list[PlanStep] = field(default_factory=list)
    facts: dict[str, Fact] = field(default_factory=dict)
    history: list[StepRecord] = field(default_factory=list)
    observation: str = "Nothing observed yet. The browser is empty."
    observation_source: str = "none"
    tokens: int = 0
    model_calls: int = 0
    violations: list[str] = field(default_factory=list)
    status: str = "RUNNING"
    summary: str = ""
    ended_by: str = ""  # why the run ended, for the receipt (see Executor._end)
    wall_seconds: float = 0.0  # time spent in earlier sessions of this run
    resumes: int = 0
    interruption: str = ""  # what resume settled, shown to the model after a resume
    contract: dict[str, Any] | None = None  # the accepted contract (Contract.model_dump)
    snapshots: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # for unchanged
    verdicts: list[dict[str, Any]] = field(default_factory=list)  # the verifier's, latest
    repair_used: bool = False  # the one repair pass after a failed verification
    note: str = ""  # a message for the worker shown every turn (verification failures)
    delivered: list[str] = field(default_factory=list)  # answered waits passed to the worker
    last_file: str = ""  # the most recent file read, kept visible (D45)
    visited: list[str] = field(default_factory=list)  # systems opened in the browser

    def to_checkpoint(self) -> dict[str, Any]:
        data = asdict(self)
        del data["observation"], data["observation_source"]
        return data

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any]) -> RunState:
        return cls(
            **{
                **data,
                "plan": [PlanStep(**p) for p in data.get("plan", [])],
                "facts": {k: Fact(**f) for k, f in data.get("facts", {}).items()},
                "history": [StepRecord(**r) for r in data.get("history", [])],
            }
        )

    def remember(
        self, items: list[dict[str, Any]], step: int, types: dict[str, str] | None = None
    ) -> list[str]:
        """Store facts; return notes about values that changed or conflict.

        `types` are the contract's fact types, which win over a type the worker gave.
        """
        notes = []
        for item in items:
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            value, source = str(item.get("value", "")), str(item.get("source", ""))
            kind = (types or {}).get(key) or str(item.get("type") or "text")
            old = self.facts.get(key)
            conflict = ""
            if old is not None and not same_value(old.value, value):
                notes.append(f"fact {key!r} changed from {old.value!r} to {value!r}")
                if kind in SENSITIVE and old.step == step:
                    pass  # restated within one action: the later value simply wins
                elif kind in SENSITIVE:
                    conflict = f"{old.value!r} from {old.source}"
                    notes.append(
                        f"CONFLICT: {key!r} was {old.value!r} (from {old.source}) and is now "
                        f"{value!r} (from {source}). Changes that use it are blocked until "
                        "you check the source and remember the correct value again."
                    )
            self.facts[key] = Fact(key, value, source, step, kind, conflict)
        return notes
