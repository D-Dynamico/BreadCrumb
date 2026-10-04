"""The contract compiler: one model call turns the request into a contract.

The answer is parsed and validated in code (`breadcrumb.contract.model.validate`).
An invalid contract goes back to the model with the errors, up to twice; if it is still
invalid the run fails honestly instead of working without a definition of done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from breadcrumb.config import ROOT, AppAccess
from breadcrumb.contract.model import ApiSpec, Contract, validate
from breadcrumb.llm.client import ModelClient, Usage

PROMPT = ROOT / "prompts" / "contract.txt"
ATTEMPTS = 3  # the first try and up to two corrections

_REF: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"field": {"type": "string"}, "fact": {"type": "string"}},
        "required": ["field", "fact"],
    },
}
_STRINGS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

SUBMIT_CONTRACT: dict[str, Any] = {
    "name": "submit_contract",
    "description": "Submit the task contract.",
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "assumptions": _STRINGS,
            "open_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "blocking": {"type": "boolean"},
                    },
                    "required": ["question", "blocking"],
                },
            },
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["money", "date", "id", "email", "text"],
                        },
                        "description": {"type": "string"},
                    },
                    "required": ["key", "type", "description"],
                },
            },
            "deliverables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["create", "update", "send"]},
                        "description": {"type": "string"},
                        "lookup_operation": {"type": "string"},
                        "key": _REF,
                        "values": _REF,
                        "must_contain": _STRINGS,
                    },
                    "required": [
                        "id",
                        "kind",
                        "description",
                        "lookup_operation",
                        "key",
                        "values",
                        "must_contain",
                    ],
                },
            },
            "protected": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "lookup_operation": {"type": "string"},
                        "match": _REF,
                        "fields": _STRINGS,
                    },
                    "required": ["lookup_operation", "match", "fields"],
                },
            },
            "extra_checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "lookup_operation": {"type": "string"},
                        "match": _REF,
                        "fields": _STRINGS,
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["goal", "assumptions", "open_questions", "facts", "deliverables", "protected"],
    },
}


@dataclass
class Compiled:
    contract: Contract | None
    errors: list[str] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)


def build_prompt(task: str, apps: list[AppAccess], spec: ApiSpec) -> str:
    systems = "\n".join(f"- {a.name}: {a.description}" for a in apps)
    return (
        f"# Request from the user\n{task}\n\n# Systems\n{systems}\n\n"
        f"# API read operations and the fields their records have\n{spec.describe()}"
    )


def compile_contract(
    model: ModelClient, task: str, apps: list[AppAccess], spec: ApiSpec
) -> Compiled:
    system = PROMPT.read_text(encoding="utf-8")
    prompt = build_prompt(task, apps, spec)
    result = Compiled(None)
    for _attempt in range(ATTEMPTS):
        action, usage = model.decide(system, prompt, [SUBMIT_CONTRACT])
        result.usages.append(usage)
        try:
            contract = Contract.model_validate(action.args)
            errors = validate(contract, spec)
        except ValidationError as exc:
            contract, errors = None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                      for e in exc.errors()]  # fmt: skip
        if contract is not None and not errors:
            result.contract, result.errors = contract, []
            return result
        result.errors = errors
        prompt += (
            "\n\n# Your previous contract was rejected\nFix these problems and submit again:\n"
            + "\n".join(f"- {e}" for e in errors)
        )
    return result
