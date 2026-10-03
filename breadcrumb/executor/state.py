"""What the executor knows about a run between turns.

This is the structured state the model sees each turn, instead of a growing chat
transcript: the task, the plan, remembered facts, recent actions and the latest
observation. Phase 3 checkpoints it after every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlanStep:
    title: str
    status: str = "todo"


@dataclass
class Fact:
    key: str
    value: str
    source: str
    step: int


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
    status: str = "running"
    summary: str = ""

    def remember(self, items: list[dict[str, Any]], step: int) -> list[str]:
        """Store facts; return notes about values that changed."""
        notes = []
        for item in items:
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            value, source = str(item.get("value", "")), str(item.get("source", ""))
            old = self.facts.get(key)
            if old is not None and old.value != value:
                notes.append(f"fact {key!r} changed from {old.value!r} to {value!r}")
            self.facts[key] = Fact(key, value, source, step)
        return notes
