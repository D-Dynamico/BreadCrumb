"""The oracle (port 8109, localhost only): ground truth for the harness.

Opens the sandbox database read-only and answers "what is actually there". It has
no write endpoints. Its URL lives only in harness config; the agent never knows it
exists (enforced by the generality check). The agent's own verifier works through
the apps like a person would; this service is for scoring runs.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from sandbox.common.config import scenario_path
from sandbox.common.db import db

# Public name -> table or view. Login secrets and sessions are never exposed.
SOURCES = {
    "payables": "v_payables",
    "vendors": "vendors",
    "employees": "v_employees",
    "tickets": "tickets",
    "ticket_comments": "ticket_comments",
    "team_messages": "team_messages",
    "sent_email": "v_sent_email",
    "outbound_email": "v_outbound_email",
    "portal_invoices": "portal_invoices",
    "purchase_orders": "purchase_orders",
    "audit_log": "audit_log",
}

app = FastAPI(title="Sandbox oracle (harness only)", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/scenario")
def scenario() -> dict[str, Any]:
    """The seeded world's roles (target vendor, traps, new hires, baseline row ids)."""
    path = scenario_path()
    if not path.exists():
        raise HTTPException(404, "No scenario. Run `uv run tasks seed` first.")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@app.get("/records/{name}")
def records(name: str, request: Request) -> dict[str, Any]:
    """Rows of one source, filtered by exact `column=value` query parameters.

    `after_id=N` keeps only rows created after seeding when N is the baseline id.
    """
    source = SOURCES.get(name)
    if source is None:
        raise HTTPException(404, f"Unknown source {name!r}. Known: {sorted(SOURCES)}")
    filters = dict(request.query_params)
    after_id = filters.pop("after_id", None)
    with db(readonly=True) as conn:
        columns = [d[0] for d in conn.execute(f"SELECT * FROM {source} LIMIT 0").description]
        unknown = sorted(set(filters) - set(columns))
        if unknown:
            raise HTTPException(400, f"Unknown columns for {name}: {unknown}")
        where = [f"{col} = ?" for col in filters]
        args: list[Any] = list(filters.values())
        if after_id is not None:
            where.append("id > ?")
            args.append(int(after_id))
        sql = f"SELECT * FROM {source}" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]
    return {"source": name, "count": len(rows), "rows": rows}
