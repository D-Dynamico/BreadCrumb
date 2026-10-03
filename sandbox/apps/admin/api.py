"""Acme Admin partial REST API, mounted at /api (spec at /api/openapi.json).

Covers reading vendors, payables and tickets, creating tickets, and the team
message channels. It deliberately does not cover creating payables or employees;
those need the web UI. Write endpoints accept an optional `Idempotency-Key`
header: a repeat of the same request with the same key returns the first
response instead of creating a second record (D19). A key reused with a
different request is refused with 409.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sandbox.apps.admin import records
from sandbox.apps.admin.records import ValidationFailed
from sandbox.common.config import setting
from sandbox.common.db import db, now_iso
from sandbox.common.money import format_inr

app = FastAPI(
    title="Acme Admin API",
    version="1.0",
    description="Partial API for Acme Admin. Payables and employees are created in the web UI.",
)


def _auth(authorization: Annotated[str, Header()] = "") -> str:
    expected = f"Bearer {setting('ADMIN_API_TOKEN')}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "Missing or invalid API token.")
    return setting("ADMIN_USER")


Actor = Annotated[str, Depends(_auth)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)]


class Vendor(BaseModel):
    id: int
    code: str
    name: str
    contact_email: str
    address: str
    gstin: str
    bank_name: str
    bank_account: str
    bank_ifsc: str


class Payable(BaseModel):
    id: int
    ref: str
    vendor_id: int
    vendor_name: str
    invoice_no: str
    invoice_date: str
    due_date: str
    amount: str = Field(description="Amount in rupees, for example '18927.20'")
    amount_display: str = Field(description="Amount with Indian grouping, for example '18,927.20'")
    po_number: str
    status: Literal["entered", "pending_approval", "approved"]
    created_at: str


class Ticket(BaseModel):
    id: int
    ref: str
    title: str
    description: str
    category: Literal["it", "facilities", "finance", "people"]
    requester: str
    assignee: str
    status: Literal["open", "closed"]
    created_at: str


class TicketCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    category: Literal["it", "facilities", "finance", "people"]
    requester: str = Field(min_length=1, max_length=200)
    assignee: str = Field(default="", max_length=200)


class Message(BaseModel):
    id: int
    channel: str
    author: str
    body: str
    posted_at: str


class MessageCreate(BaseModel):
    channel: Literal["finance-ops", "it-help", "general"]
    body: str = Field(min_length=1, max_length=4000)


def _payable(row: sqlite3.Row) -> dict[str, Any]:
    paise = int(row["amount_paise"])
    return Payable(
        id=row["id"],
        ref=row["ref"],
        vendor_id=row["vendor_id"],
        vendor_name=row["vendor_name"],
        invoice_no=row["invoice_no"],
        invoice_date=row["invoice_date"],
        due_date=row["due_date"],
        amount=f"{paise // 100}.{paise % 100:02d}",
        amount_display=format_inr(paise),
        po_number=row["po_number"],
        status=row["status"],
        created_at=row["created_at"],
    ).model_dump()


def _idempotent(
    key: str | None,
    scope: str,
    payload: dict[str, Any],
    create: Callable[[sqlite3.Connection], dict[str, Any]],
) -> JSONResponse:
    request_hash = hashlib.sha256(
        json.dumps({"scope": scope, "body": payload}, sort_keys=True).encode()
    ).hexdigest()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if key:
            seen = conn.execute("SELECT * FROM api_idempotency WHERE key = ?", (key,)).fetchone()
            if seen is not None:
                if seen["request_hash"] != request_hash:
                    raise HTTPException(
                        409, "This Idempotency-Key was already used for a different request."
                    )
                return JSONResponse(
                    json.loads(seen["response_body"]),
                    status_code=seen["status_code"],
                    headers={"Idempotent-Replayed": "true"},
                )
        try:
            body = create(conn)
        except ValidationFailed as exc:
            raise HTTPException(422, exc.errors) from exc
        if key:
            conn.execute(
                "INSERT INTO api_idempotency (key, request_hash, status_code, response_body,"
                " created_at) VALUES (?, ?, 201, ?, ?)",
                (key, request_hash, json.dumps(body), now_iso()),
            )
    return JSONResponse(body, status_code=201)


@app.get("/vendors", operation_id="listVendors", response_model=list[Vendor])
def list_vendors(actor: Actor, name: str = "") -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM vendors WHERE ? = '' OR name LIKE ? ORDER BY name",
            (name, f"%{name}%"),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/vendors/{vendor_id}", operation_id="getVendor", response_model=Vendor)
def get_vendor(actor: Actor, vendor_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such vendor.")
    return dict(row)


_PAYABLES = "SELECT p.*, v.name AS vendor_name FROM payables p JOIN vendors v ON v.id = p.vendor_id"


@app.get("/payables", operation_id="listPayables", response_model=list[Payable])
def list_payables(
    actor: Actor, vendor_id: int | None = None, invoice_no: str = "", status: str = ""
) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            f"{_PAYABLES} WHERE (? IS NULL OR p.vendor_id = ?) AND (? = '' OR p.invoice_no = ?)"
            " AND (? = '' OR p.status = ?) ORDER BY p.id",
            (vendor_id, vendor_id, invoice_no, invoice_no, status, status),
        ).fetchall()
    return [_payable(r) for r in rows]


@app.get("/payables/{payable_id}", operation_id="getPayable", response_model=Payable)
def get_payable(actor: Actor, payable_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(f"{_PAYABLES} WHERE p.id = ?", (payable_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such payable.")
    return _payable(row)


@app.get("/tickets", operation_id="listTickets", response_model=list[Ticket])
def list_tickets(actor: Actor, q: str = "", status: str = "") -> list[dict[str, Any]]:
    like = f"%{q}%"
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE (? = '' OR title LIKE ? OR description LIKE ?)"
            " AND (? = '' OR status = ?) ORDER BY id",
            (q, like, like, status, status),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/tickets/{ticket_id}", operation_id="getTicket", response_model=Ticket)
def get_ticket(actor: Actor, ticket_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such ticket.")
    return dict(row)


@app.post("/tickets", operation_id="createTicket", status_code=201, response_model=Ticket)
def create_ticket(
    actor: Actor, ticket: TicketCreate, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def create(conn: sqlite3.Connection) -> dict[str, Any]:
        ticket_id = records.create_ticket(conn, ticket.model_dump(), actor)
        return dict(conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone())

    return _idempotent(idempotency_key, "createTicket", ticket.model_dump(), create)


@app.get("/messages", operation_id="listMessages", response_model=list[Message])
def list_messages(actor: Actor, channel: str = "finance-ops") -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM team_messages WHERE channel = ? ORDER BY id", (channel,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/messages", operation_id="postMessage", status_code=201, response_model=Message)
def post_message(
    actor: Actor, message: MessageCreate, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def create(conn: sqlite3.Connection) -> dict[str, Any]:
        message_id = records.post_message(conn, message.channel, message.body, actor)
        row = conn.execute("SELECT * FROM team_messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row)

    return _idempotent(idempotency_key, "postMessage", message.model_dump(), create)
