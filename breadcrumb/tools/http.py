"""HTTP tool: call operations from an API's OpenAPI spec.

GET operations are reads. Anything else is a write and is only reached through a
declared action; it always carries an `Idempotency-Key` header, which APIs that
support it use to ignore repeats (D19).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

MAX_CHARS = 10_000


class HttpError(Exception):
    """Shown to the model as the outcome of the action."""


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    summary: str
    params: list[str]
    has_body: bool

    def signature(self) -> str:
        args = ", ".join(self.params + (["body"] if self.has_body else []))
        return f"{self.method} {self.operation_id}({args}): {self.summary}"


class HttpTool:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._ops: dict[str, Operation] | None = None
        self._openapi: dict[str, Any] | None = None

    def openapi(self) -> dict[str, Any]:
        """The API's OpenAPI document, fetched once."""
        if self._openapi is None:
            response = httpx.get(f"{self.base_url}/openapi.json", timeout=15)
            response.raise_for_status()
            self._openapi = response.json()
        return self._openapi

    def operations(self) -> dict[str, Operation]:
        if self._ops is None:
            ops: dict[str, Operation] = {}
            for path, methods in self.openapi().get("paths", {}).items():
                for method, spec in methods.items():
                    op_id = spec.get("operationId")
                    if not op_id:
                        continue
                    params = [
                        p["name"] + ("" if p.get("required") else "?")
                        for p in spec.get("parameters", [])
                        if p.get("in") in ("path", "query")
                    ]
                    ops[op_id] = Operation(
                        op_id,
                        method.upper(),
                        path,
                        spec.get("summary") or spec.get("description") or "",
                        params,
                        "requestBody" in spec,
                    )
            self._ops = ops
        return self._ops

    def describe(self) -> str:
        return "\n".join(op.signature() for op in self.operations().values())

    def _url(self, op: Operation, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        path, query = op.path, dict(params)
        for name in list(query):
            token = "{" + name + "}"
            if token in path:
                path = path.replace(token, str(query.pop(name)))
        if "{" in path:
            raise HttpError(f"{op.operation_id} needs path parameters: {op.path}")
        return f"{self.base_url}{path}", query

    def _format(self, response: httpx.Response) -> str:
        try:
            body = json.dumps(response.json(), indent=1, ensure_ascii=False)
        except ValueError:
            body = response.text
        if len(body) > MAX_CHARS:
            body = body[:MAX_CHARS] + "\n... (truncated)"
        return f"HTTP {response.status_code}\n{body}"

    def get(self, operation_id: str, params: dict[str, Any]) -> str:
        op = self.operations().get(operation_id)
        if op is None or op.method != "GET":
            raise HttpError(f"{operation_id!r} is not a read operation of this API")
        url, query = self._url(op, params)
        return self._format(httpx.get(url, params=query, headers=self._headers, timeout=15))

    def get_json(self, operation_id: str, params: dict[str, Any]) -> Any:
        """A read for the gateway's lookups: the parsed body, never truncated."""
        op = self.operations().get(operation_id)
        if op is None or op.method != "GET":
            raise HttpError(f"{operation_id!r} is not a read operation of this API")
        url, query = self._url(op, params)
        response = httpx.get(url, params=query, headers=self._headers, timeout=15)
        if response.status_code >= 400:
            raise HttpError(f"{operation_id} answered HTTP {response.status_code}")
        return response.json()

    def check_write(self, operation_id: str) -> None:
        op = self.operations().get(operation_id)
        if op is None or op.method == "GET":
            raise HttpError(f"{operation_id!r} is not a write operation of this API")

    def write(
        self, operation_id: str, params: dict[str, Any], body: dict[str, Any], idempotency_key: str
    ) -> tuple[int, str]:
        """Send a write. Returns the status and the formatted response."""
        self.check_write(operation_id)
        op = self.operations()[operation_id]
        url, query = self._url(op, params)
        headers = {**self._headers, "Idempotency-Key": idempotency_key}
        response = httpx.request(
            op.method, url, params=query, json=body, headers=headers, timeout=15
        )
        return response.status_code, self._format(response)
