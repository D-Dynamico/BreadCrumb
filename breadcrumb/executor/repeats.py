"""Repeat detection: the first slice of loop detection (AGENT_DESIGN.md section 3, D34).

A repeat is the same action, with the same arguments, taken on the same state as an
action within the last few steps. The state is a fingerprint chosen by what is being
observed: for a page, its URL path and its interactive elements with their values;
for a file or an API answer, its source and a hash of its content.

First repeat: the action runs, with a note. Second: it is refused, not run, and the
worker is asked for a new plan. Third: the run escalates. Progress (a confirmed
commit, or a plan step newly marked done) resets the count. Each session of a run
builds a new guard, so re-observing after a resume is never a repeat.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

WINDOW = 6
_BOOKKEEPING = ("why", "remember", "plan")
_STATEFUL = re.compile(r"\[\d+\]|\[(selected|checked|pressed|expanded)\]")


class Response(Enum):
    OK = "ok"
    NOTE = "note"
    REFUSE = "refuse"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Check:
    response: Response
    message: str = ""


def fingerprint(source: str, observation: str) -> str:
    """What the worker was looking at, reduced to what matters for "same state"."""
    if source.startswith("browser"):
        lines = observation.splitlines()
        url = next((ln[5:] for ln in lines if ln.startswith("URL: ")), "")
        state = [urlsplit(url).path, *(ln.strip() for ln in lines if _STATEFUL.search(ln))]
    else:
        state = [source, observation]
    return hashlib.sha256("\n".join(state).encode()).hexdigest()[:16]


def _signature(action: str, args: dict[str, Any], state: str) -> str:
    shown = {k: v for k, v in args.items() if k not in _BOOKKEEPING}
    return json.dumps([action, shown, state], sort_keys=True, default=str)


@dataclass
class RepeatGuard:
    seen: list[tuple[int, str]] = field(default_factory=list)  # (step, signature)
    strikes: int = 0

    def progress(self) -> None:
        self.seen.clear()
        self.strikes = 0

    def check(self, step: int, action: str, args: dict[str, Any], state: str) -> Check:
        if action == "finish":
            return Check(Response.OK)
        signature = _signature(action, args, state)
        earlier = [s for s, sig in self.seen if sig == signature and step - s <= WINDOW]
        self.seen = [(s, sig) for s, sig in self.seen if step - s < WINDOW]
        self.seen.append((step, signature))
        if not earlier:
            return Check(Response.OK)
        self.strikes += 1
        steps = ", ".join(str(s) for s in earlier)
        if self.strikes == 1:
            return Check(
                Response.NOTE,
                f"NOTE: you did exactly this at step {steps}, on the same page or content, "
                "and it changed nothing. If you needed values from it, put them in remember "
                "now; otherwise change approach.",
            )
        if self.strikes == 2:
            return Check(
                Response.REFUSE,
                f"Refused, not done: this repeats steps {steps} and {step} on the same "
                "page or content. Send an updated plan with your next action and try a "
                "different approach.",
            )
        return Check(
            Response.ESCALATE,
            f"Stopped: repeated {action} on the same page or content (steps {steps} and "
            f"{step}) after a note and a refusal. I could not make progress this way and "
            "need help.",
        )
