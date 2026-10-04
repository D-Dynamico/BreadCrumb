"""The receipt: what the user gets at the end (AGENT_DESIGN.md section 9).

Built only from durable records (the run state, the journal, the waits, the events)
so it can be rebuilt for any run, including one that crashed and was resumed. It
says plainly how the run ended, including whether a budget ran out or repeat
detection stopped it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from breadcrumb.executor.state import RunState
from breadcrumb.journal.journal import Entry
from breadcrumb.runs.store import Wait

ENDED_BY = {
    "finish": "the worker called finish",
    "verified": "every contract check was verified",
    "verification": "contract checks failed after the repair pass",
    "unverified": "the worker finished, but some checks could not be verified",
    "step_budget": "budget exhausted: steps",
    "time_budget": "budget exhausted: time",
    "token_budget": "budget exhausted: tokens",
    "model_error": "the model could not be reached",
    "repeats": "repeat escalation: the same action on the same state after a note and a refusal",
    "reconcile": "resume could not tell whether an effect happened",
    "unclear_effect": "could not tell whether an effect happened",
    "contract": "no valid contract could be made from the request",
    "unanswered": "a question for the user is still open",
}


def build(
    state: RunState,
    entries: list[Entry],
    waits: list[Wait],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    contract = state.contract or {}
    used = {
        k: f
        for k, f in state.facts.items()
        if any(
            k == r["fact"]
            for d in contract.get("deliverables", [])
            for r in [*d.get("key", []), *d.get("values", [])]
        )
    }
    return {
        "run_id": state.run_id,
        "task": state.task,
        "status": state.status,
        "ended_by": state.ended_by,
        "ended_by_text": ENDED_BY.get(state.ended_by, state.ended_by),
        "summary": state.summary,
        "goal": contract.get("goal", ""),
        "assumptions": contract.get("assumptions", []),
        "checks": state.verdicts,
        "values_written": [
            {"fact": k, "value": f.value, "type": f.type, "source": f.source, "step": f.step}
            for k, f in used.items()
        ],
        "effects": [
            {
                "deliverable_or_action": e.description or e.action,
                "state": e.state.value,
                "attempts": e.attempt_no,
                "key": e.key,
                "history": [h[0] for h in e.history],
                "note": e.note,
            }
            for e in entries
        ],
        "approvals_and_questions": [
            {"kind": w.kind, "status": w.status, "answer": w.answer, "payload": w.payload}
            for w in waits
        ],
        "interruptions": [
            e for e in events if e["kind"] in ("interrupted", "resumed", "reconciled")
        ],
        "integrity_violations": state.violations,
        "steps": len(state.history),
        "model_calls": state.model_calls,
        "resumes": state.resumes,
    }


def markdown(r: dict[str, Any]) -> str:
    lines = [f"# Receipt for {r['run_id']}", "", f"**Task:** {r['task']}", ""]
    lines += [f"**Status:** {r['status']} ({r['ended_by_text']})", "", r["summary"] or "", ""]
    if r["goal"]:
        lines += [f"**Goal (contract):** {r['goal']}", ""]
    for a in r["assumptions"]:
        lines.append(f"- Assumed: {a}")
    after_finish = r["ended_by"] in ("verified", "verification", "unverified")
    heading = "## Checks" if after_finish else "## Checks (evaluated when the run stopped)"
    lines += ["", heading, ""]
    if r["checks"]:
        lines += ["| Check | Verdict | Detail |", "|---|---|---|"]
        lines += [f"| {c['check']} | {c['status']} | {c['detail']} |" for c in r["checks"]]
    else:
        lines.append("Not verified (the run did not reach verification).")
    lines += ["", "## Values for the deliverables and where they came from", ""]
    lines += [f"- {v['fact']} = {v['value']} ({v['type']}; from {v['source']})"
              for v in r["values_written"]] or ["- none"]  # fmt: skip
    lines += ["", "## Effects (journal)", ""]
    lines += [f"- {e['state']}: {e['deliverable_or_action']} (attempts {e['attempts']}; "
              f"{' > '.join(e['history'])})" for e in r["effects"]] or ["- none"]  # fmt: skip
    if r["approvals_and_questions"]:
        lines += ["", "## Approvals and questions", ""]
        for w in r["approvals_and_questions"]:
            what = w["payload"].get("question") or w["payload"].get("description", "")
            answer = f": {w['answer']}" if w["answer"] else ""
            lines.append(f"- {w['kind']} {w['status']}{answer} ({what})")
    if r["interruptions"]:
        lines += ["", "## Interruptions", ""]
        for e in r["interruptions"]:
            extra = {k: v for k, v in e.items() if k not in ("at", "kind")}
            lines.append(f"- {e['at']} {e['kind']} {json.dumps(extra, ensure_ascii=False)}")
    if r["integrity_violations"]:
        lines += ["", "## Integrity violations", ""] + [f"- {v}" for v in r["integrity_violations"]]
    lines += ["", f"Steps: {r['steps']}; model calls: {r['model_calls']}; "
              f"resumes: {r['resumes']}", ""]  # fmt: skip
    return "\n".join(lines)


def write(run_dir: Path, receipt: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    path = run_dir / "receipt.md"
    path.write_text(markdown(receipt), encoding="utf-8")
    return path
