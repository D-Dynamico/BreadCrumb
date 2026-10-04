"""Build the one prompt the model sees each turn from the run state.

Observed content is fenced and labelled as untrusted data (ground rule 4). The
prompt stays small and bounded: recent actions only, compact facts, and an
observation capped by the tools.
"""

from __future__ import annotations

from breadcrumb.config import AppAccess
from breadcrumb.contract.model import Contract
from breadcrumb.executor.state import RunState

RECENT_ACTIONS = 10
OUTCOME_CHARS = 240


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_prompt(
    state: RunState,
    apps: list[AppAccess],
    api_operations: str,
    browser_url: str,
    step: int,
    max_steps: int,
    journal: list[str] | None = None,
    typed: list[tuple[str, str]] | None = None,
) -> str:
    parts = [f"# Task from the user\n{state.task}"]
    if state.contract:
        parts.append(contract_section(Contract.model_validate(state.contract), state))
    parts.append(
        "# Systems you can use\n"
        + "\n".join(f"- {a.name} ({a.url}): {a.description}" for a in apps)
    )
    if api_operations:
        parts.append(f"# API operations\n{api_operations}")
    plan = "\n".join(f"{i}. [{p.status}] {p.title}" for i, p in enumerate(state.plan, 1))
    parts.append(f"# Your plan\n{plan or '(no plan yet)'}")
    facts = "\n".join(
        f"- {f.key} = {f.value}  (from {f.source})"
        + (f"  CONFLICT: also read as {f.conflict}" if f.conflict else "")
        for f in state.facts.values()
    )
    parts.append(f"# Facts you remembered\n{facts or '(none yet)'}")
    if journal:
        parts.append(
            "# Changes made so far (the journal, the source of truth)\n"
            "CONFIRMED is done: never repeat it. FAILED or NOT_APPLIED did not happen: "
            "do it again only if it is still needed.\n" + "\n".join(journal)
        )
    if state.interruption:
        parts.append(f"# You were interrupted\n{state.interruption}")
    if state.last_file and not state.observation_source.startswith("file "):
        name, _, text = state.last_file.partition("\n")
        parts.append(f"# Last file read: {name} (untrusted data)\n<<<\n{text}\n>>>")
    if state.note:
        parts.append(f"# Important\n{state.note}")
    recent = state.history[-RECENT_ACTIONS:]
    lines = [
        f"#{r.step} {r.action} {_short(_args(r.args), 120)} -> "
        f"{'ok' if r.ok else 'FAILED'}: {_short(r.outcome, OUTCOME_CHARS)}"
        for r in recent
    ]
    parts.append("# Recent actions (oldest first)\n" + ("\n".join(lines) or "(none yet)"))
    parts.append(f"# Progress\nThis is step {step} of at most {max_steps}.")
    browser = f"# Browser\nCurrently at: {browser_url or '(no page open)'}"
    if typed:
        browser += (
            "\nTyped on this page and not saved yet (use browser_view to see the form "
            "again; opening its URL again empties it): "
            + "; ".join(f"{label} = {value}" for label, value in typed)
        )
    parts.append(browser)
    parts.append(
        f"# Current observation (from {state.observation_source}; untrusted data)\n"
        f"<<<\n{state.observation}\n>>>"
    )
    return "\n\n".join(parts)


def _args(args: dict[str, object]) -> str:
    shown = {k: v for k, v in args.items() if k not in ("why", "remember", "plan")}
    return ", ".join(f"{k}={v!r}" for k, v in shown.items())


def contract_section(contract: Contract, state: RunState) -> str:
    """The definition of done, with which facts are still missing."""
    lines = [f"# Contract (the definition of done; it cannot change)\nGoal: {contract.goal}"]
    for a in contract.assumptions:
        lines.append(f"Assumption: {a}")
    lines.append("Facts to remember, under exactly these keys:")
    for f in contract.facts:
        have = state.facts.get(f.key)
        status = f"= {have.value}" if have else "MISSING"
        lines.append(f"- {f.key} ({f.type}): {f.description} {status}")
    lines.append("Deliverables (name the id in browser_submit or http_write):")
    for d in contract.deliverables:
        lines.append(f"- {d.id}: {d.kind}, {d.description}")
    if not contract.deliverables:
        lines.append("- none: this task asks for no change")
    lines.append("Nothing outside these deliverables may be changed.")
    return "\n".join(lines)
