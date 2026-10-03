"""Task files: a request, the conditions it runs under, and hand-written ground truth.

Names, amounts and dates change with every seed, so a task file is a Jinja template
over the seed's scenario (`sandbox/seed/scenario.py`, served by the oracle at
/scenario). It is rendered first, then parsed as YAML. Rendering is strict: a
reference to anything the scenario does not have is an error, never a blank.

Ground truth is checked against the oracle's sources, never against the agent's
contract, journal or receipt (EVALUATION.md, D8). Values compare as strings;
`null` means the column must be empty.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import jinja2
import yaml
from pydantic import BaseModel, ConfigDict, Field

_ID = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)
_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordCheck(_Strict):
    """Exactly `count` rows of `source` match; each must have these field values."""

    source: str
    match: dict[str, str | None]
    count: int = Field(ge=0)
    fields: dict[str, str | None] = {}
    text: str | None = Field(
        default=None, description="Must appear (any case) in some text column of the row"
    )
    created_during_run: bool = Field(
        default=False, description="Only count rows created after seeding"
    )


class UnchangedCheck(_Strict):
    """No audit row shows these fields of the matched record being changed."""

    source: str
    entity: str
    match: dict[str, str]
    fields: list[str]


class MessageCheck(_Strict):
    """Exactly `count` messages created during the run go to `to` and contain every string."""

    kind: Literal["team_message", "sent_email", "outbound_email"]
    to: str
    count: int = Field(ge=0)
    must_contain: list[str] = []


class EndState(_Strict):
    records: list[RecordCheck] = []
    unchanged: list[UnchangedCheck] = []
    messages: list[MessageCheck] = []


class Task(_Strict):
    id: str
    family: int = Field(ge=1, le=4)
    prompt: str
    seed: int | None = Field(description="None: the harness picks seeds at eval time")
    faults: Literal["none", "flaky", "session", "drift", "chaos"] = "none"
    crash_points: list[str] = []
    expected_escalation: Literal["none", "clarify", "approval", "refuse"]
    expected_end_state: EndState
    notes: str


def task_id(path: Path) -> str:
    """The id without rendering, for tools that only need to know which task it is."""
    found = _ID.search(path.read_text(encoding="utf-8"))
    if not found:
        raise ValueError(f"{path} has no top-level id")
    return found.group(1)


def render_task(path: Path, scenario: dict[str, Any]) -> Task:
    text = _ENV.from_string(path.read_text(encoding="utf-8")).render(**scenario)
    return Task.model_validate(yaml.safe_load(text))
