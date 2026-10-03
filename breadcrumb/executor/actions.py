"""The action space offered to the model, as function schemas.

Every action carries `why` (one sentence, for the step log) and two optional
bookkeeping fields, `remember` (facts) and `plan` (the updated plan), so keeping
notes and a plan costs no extra model call on a rate-limited free tier. Writes are separate
actions (`browser_submit`, `http_write`, `notify`) so they are always declared.
"""

from __future__ import annotations

from typing import Any

WRITE_ACTIONS = frozenset({"browser_submit", "http_write", "notify"})

_COMMON: dict[str, Any] = {
    "why": {"type": "string", "description": "One short sentence: why this action now."},
    "plan": {
        "type": "array",
        "description": "Your whole updated plan. Include it on your first action and "
        "whenever a step's status changes; leave it out otherwise.",
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "status": {"type": "string", "enum": ["todo", "doing", "done", "blocked"]},
            },
            "required": ["title", "status"],
        },
    },
    "remember": {
        "type": "array",
        "description": "Facts read in the current observation that you will need later.",
        "items": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short name, e.g. due_date"},
                "value": {"type": "string", "description": "Exact value as read"},
                "source": {"type": "string", "description": "Where it was read, with locator"},
            },
            "required": ["key", "value", "source"],
        },
    },
}


def _action(
    action_name: str, summary: str, params: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    params = params or {}
    return {
        "name": action_name,
        "description": summary,
        "parameters": {
            "type": "object",
            "properties": {**params, **_COMMON},
            "required": [*params.keys(), "why"],
        },
    }


_ELEMENT = {"type": "integer", "description": "Element number from the current observation"}
_CHANGE = {"type": "string", "description": "The change this makes, in plain words"}

ACTIONS: list[dict[str, Any]] = [
    _action("browser_open", "Open a URL in the browser.", {"url": {"type": "string"}}),
    _action(
        "browser_click",
        "Click a link or a button that only navigates, searches or opens something. "
        "Never use it for buttons that save, send or change data.",
        {"element": _ELEMENT},
    ),
    _action(
        "browser_type",
        "Replace the text in a text field.",
        {"element": _ELEMENT, "text": {"type": "string"}},
    ),
    _action(
        "browser_select",
        "Choose an option in a dropdown, by its visible label.",
        {"element": _ELEMENT, "option": {"type": "string"}},
    ),
    _action(
        "browser_submit",
        "Press a button that saves, submits, sends, approves or deletes something.",
        {"element": _ELEMENT, "description": _CHANGE},
    ),
    _action("login", "Sign in to the app currently open in the browser."),
    _action("files_list", "List downloaded files."),
    _action("files_read", "Read a downloaded file.", {"name": {"type": "string"}}),
    _action(
        "http_get",
        "Call a read operation of the API.",
        {
            "operation_id": {"type": "string"},
            "params_json": {"type": "string", "description": 'Parameters as JSON, e.g. {"id": 3}'},
        },
    ),
    _action(
        "http_write",
        "Call an operation of the API that creates or changes data.",
        {
            "operation_id": {"type": "string"},
            "params_json": {"type": "string", "description": "Path and query parameters as JSON"},
            "body_json": {"type": "string", "description": "Request body as JSON"},
            "description": _CHANGE,
        },
    ),
    _action(
        "notify",
        "Post a message to the requester (the user who gave you the task).",
        {"message": {"type": "string"}},
    ),
    _action(
        "finish",
        "End the task: what was done and the references of anything created, or exactly "
        "where you stopped and why.",
        {"summary": {"type": "string"}},
    ),
]
