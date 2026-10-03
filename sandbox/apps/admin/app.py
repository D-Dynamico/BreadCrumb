"""Acme Admin web app (port 8103): payables, vendors, people, tickets, team messages.

The partial REST API is mounted at /api (see `api.py`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sandbox.apps.admin import api, records
from sandbox.apps.admin.records import ValidationFailed
from sandbox.common.db import db
from sandbox.common.web import WebApp


def _verify(conn: sqlite3.Connection, username: str, password: str) -> bool:
    row = conn.execute(
        "SELECT password FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    return row is not None and row["password"] == password


kit = WebApp("admin", "Acme Admin", Path(__file__).with_name("templates"), _verify)
app = kit.app
app.mount("/api", api.app)
User = Annotated[str, Depends(kit.user)]


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    form = {k: str(v) for k, v in data.items()}
    kit.check_csrf(request, form.pop("csrf_token", ""))
    return form


Form_ = Annotated[dict[str, str], Depends(_form)]


def _one(conn: sqlite3.Connection, sql: str, *args: Any) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(sql, args).fetchone()
    if row is None:
        raise HTTPException(404, "That record does not exist.")
    return row


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: User) -> HTMLResponse:
    with db() as conn:
        counts = {
            "pending": conn.execute(
                "SELECT COUNT(*) FROM payables WHERE status = 'pending_approval'"
            ).fetchone()[0],
            "open_tickets": conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE status = 'open'"
            ).fetchone()[0],
        }
    return kit.render(request, "home.html", counts=counts)


# -- payables ----------------------------------------------------------------
@app.get("/payables", response_class=HTMLResponse)
def payables(request: Request, user: User, q: str = "", status: str = "") -> HTMLResponse:
    sql = (
        "SELECT p.*, v.name AS vendor_name FROM payables p JOIN vendors v ON v.id = p.vendor_id"
        " WHERE (? = '' OR v.name LIKE ? OR p.invoice_no LIKE ? OR p.ref LIKE ?)"
        " AND (? = '' OR p.status = ?) ORDER BY p.id DESC"
    )
    like = f"%{q.strip()}%"
    with db() as conn:
        rows = conn.execute(sql, (q.strip(), like, like, like, status, status)).fetchall()
    template = "payables_rows.html" if request.headers.get("HX-Request") else "payables.html"
    return kit.render(request, template, rows=rows, q=q, status=status)


def _payable_form(request: Request, values: dict[str, str], errors: dict[str, str]) -> HTMLResponse:
    with db() as conn:
        vendors = conn.execute("SELECT id, name FROM vendors ORDER BY name").fetchall()
    return kit.render(
        request,
        "payable_new.html",
        status_code=422 if errors else 200,
        vendors=vendors,
        values=values,
        errors=errors,
    )


@app.get("/payables/new", response_class=HTMLResponse)
def payable_new(request: Request, user: User) -> HTMLResponse:
    return _payable_form(request, {}, {})


@app.post("/payables")
def payable_create(request: Request, user: User, form: Form_) -> Response:
    try:
        with db() as conn:
            payable_id = records.create_payable(conn, form, user)
    except ValidationFailed as exc:
        return _payable_form(request, form, exc.errors)
    return _see_other(f"/payables/{payable_id}?saved=1")


@app.get("/payables/{payable_id}", response_class=HTMLResponse)
def payable_detail(request: Request, user: User, payable_id: int, saved: int = 0) -> HTMLResponse:
    with db() as conn:
        row = _one(
            conn,
            "SELECT p.*, v.name AS vendor_name FROM payables p JOIN vendors v"
            " ON v.id = p.vendor_id WHERE p.id = ?",
            payable_id,
        )
        role = records.role_of(conn, user)
    return kit.render(
        request,
        "payable_detail.html",
        p=row,
        saved=saved,
        can_approve=role == "approver" and row["status"] == "pending_approval",
        limit=records.APPROVAL_LIMIT_PAISE,
    )


@app.post("/payables/{payable_id}/approve")
def payable_approve(request: Request, user: User, payable_id: int, form: Form_) -> Response:
    try:
        with db() as conn:
            records.approve_payable(conn, payable_id, user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValidationFailed as exc:
        raise HTTPException(409, str(exc)) from exc
    return _see_other(f"/payables/{payable_id}")


# -- vendors and purchase orders ---------------------------------------------
@app.get("/vendors", response_class=HTMLResponse)
def vendors(request: Request, user: User, q: str = "") -> HTMLResponse:
    like = f"%{q.strip()}%"
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM vendors WHERE ? = '' OR name LIKE ? OR code LIKE ? ORDER BY name",
            (q.strip(), like, like),
        ).fetchall()
    return kit.render(request, "vendors.html", rows=rows, q=q)


@app.get("/vendors/{vendor_id}", response_class=HTMLResponse)
def vendor_detail(request: Request, user: User, vendor_id: int, saved: int = 0) -> HTMLResponse:
    with db() as conn:
        vendor = _one(conn, "SELECT * FROM vendors WHERE id = ?", vendor_id)
        recent = conn.execute(
            "SELECT * FROM payables WHERE vendor_id = ? ORDER BY invoice_date DESC LIMIT 10",
            (vendor_id,),
        ).fetchall()
    return kit.render(request, "vendor_detail.html", v=vendor, payables=recent, saved=saved)


@app.get("/vendors/{vendor_id}/edit", response_class=HTMLResponse)
def vendor_edit(request: Request, user: User, vendor_id: int) -> HTMLResponse:
    with db() as conn:
        vendor = _one(conn, "SELECT * FROM vendors WHERE id = ?", vendor_id)
    return kit.render(request, "vendor_edit.html", v=vendor, values=dict(vendor), errors={})


@app.post("/vendors/{vendor_id}/edit")
def vendor_update(request: Request, user: User, vendor_id: int, form: Form_) -> Response:
    try:
        with db() as conn:
            records.update_vendor(conn, vendor_id, form, user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValidationFailed as exc:
        with db() as conn:
            vendor = _one(conn, "SELECT * FROM vendors WHERE id = ?", vendor_id)
        return kit.render(
            request, "vendor_edit.html", status_code=422, v=vendor, values=form, errors=exc.errors
        )
    return _see_other(f"/vendors/{vendor_id}?saved=1")


@app.get("/purchase-orders", response_class=HTMLResponse)
def purchase_orders(request: Request, user: User) -> HTMLResponse:
    with db() as conn:
        rows = conn.execute(
            "SELECT po.*, v.name AS vendor_name FROM purchase_orders po"
            " JOIN vendors v ON v.id = po.vendor_id ORDER BY po.created_on DESC"
        ).fetchall()
    return kit.render(request, "purchase_orders.html", rows=rows)


# -- people ------------------------------------------------------------------
@app.get("/people", response_class=HTMLResponse)
def people(request: Request, user: User, q: str = "") -> HTMLResponse:
    like = f"%{q.strip()}%"
    with db() as conn:
        rows = conn.execute(
            "SELECT e.*, m.full_name AS manager_name FROM employees e"
            " LEFT JOIN employees m ON m.id = e.manager_id"
            " WHERE ? = '' OR e.full_name LIKE ? OR e.work_email LIKE ? ORDER BY e.full_name",
            (q.strip(), like, like),
        ).fetchall()
    return kit.render(request, "people.html", rows=rows, q=q)


def _person_form(request: Request, values: dict[str, str], errors: dict[str, str]) -> HTMLResponse:
    with db() as conn:
        managers = conn.execute(
            "SELECT id, full_name, title FROM employees ORDER BY full_name"
        ).fetchall()
    return kit.render(
        request,
        "person_new.html",
        status_code=422 if errors else 200,
        managers=managers,
        departments=records.DEPARTMENTS,
        values=values,
        errors=errors,
    )


@app.get("/people/new", response_class=HTMLResponse)
def person_new(request: Request, user: User) -> HTMLResponse:
    return _person_form(request, {}, {})


@app.post("/people")
def person_create(request: Request, user: User, form: Form_) -> Response:
    try:
        with db() as conn:
            employee_id = records.create_employee(conn, form, user)
    except ValidationFailed as exc:
        return _person_form(request, form, exc.errors)
    return _see_other(f"/people/{employee_id}?saved=1")


@app.get("/people/{employee_id}", response_class=HTMLResponse)
def person_detail(request: Request, user: User, employee_id: int, saved: int = 0) -> HTMLResponse:
    with db() as conn:
        row = _one(
            conn,
            "SELECT e.*, m.full_name AS manager_name FROM employees e"
            " LEFT JOIN employees m ON m.id = e.manager_id WHERE e.id = ?",
            employee_id,
        )
    return kit.render(request, "person_detail.html", e=row, saved=saved)


# -- tickets -----------------------------------------------------------------
@app.get("/tickets", response_class=HTMLResponse)
def tickets(request: Request, user: User, q: str = "", status: str = "") -> HTMLResponse:
    like = f"%{q.strip()}%"
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE (? = '' OR title LIKE ? OR description LIKE ?"
            " OR ref LIKE ?) AND (? = '' OR status = ?) ORDER BY id DESC",
            (q.strip(), like, like, like, status, status),
        ).fetchall()
    return kit.render(request, "tickets.html", rows=rows, q=q, status=status)


def _ticket_form(
    request: Request, user: str, values: dict[str, str], errors: dict[str, str]
) -> HTMLResponse:
    return kit.render(
        request,
        "ticket_new.html",
        status_code=422 if errors else 200,
        categories=records.TICKET_CATEGORIES,
        values={"requester": user, **values},
        errors=errors,
    )


@app.get("/tickets/new", response_class=HTMLResponse)
def ticket_new(request: Request, user: User) -> HTMLResponse:
    return _ticket_form(request, user, {}, {})


@app.post("/tickets")
def ticket_create(request: Request, user: User, form: Form_) -> Response:
    try:
        with db() as conn:
            ticket_id = records.create_ticket(conn, form, user)
    except ValidationFailed as exc:
        return _ticket_form(request, user, form, exc.errors)
    return _see_other(f"/tickets/{ticket_id}?saved=1")


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(request: Request, user: User, ticket_id: int, saved: int = 0) -> HTMLResponse:
    with db() as conn:
        ticket = _one(conn, "SELECT * FROM tickets WHERE id = ?", ticket_id)
        comments = conn.execute(
            "SELECT * FROM ticket_comments WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        ).fetchall()
    return kit.render(request, "ticket_detail.html", t=ticket, comments=comments, saved=saved)


@app.post("/tickets/{ticket_id}/comments")
def ticket_comment(request: Request, user: User, ticket_id: int, form: Form_) -> Response:
    try:
        with db() as conn:
            records.add_comment(conn, ticket_id, form.get("body", ""), user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValidationFailed as exc:
        raise HTTPException(422, str(exc)) from exc
    return _see_other(f"/tickets/{ticket_id}")


@app.post("/tickets/{ticket_id}/assign")
def ticket_assign(request: Request, user: User, ticket_id: int, form: Form_) -> Response:
    try:
        with db() as conn:
            records.update_ticket(
                conn, ticket_id, {"assignee": form.get("assignee", "").strip()}, user
            )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _see_other(f"/tickets/{ticket_id}")


@app.post("/tickets/{ticket_id}/close")
def ticket_close(request: Request, user: User, ticket_id: int, form: Form_) -> Response:
    try:
        with db() as conn:
            records.update_ticket(conn, ticket_id, {"status": "closed"}, user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _see_other(f"/tickets/{ticket_id}")


# -- team messages -----------------------------------------------------------
@app.get("/messages", response_class=HTMLResponse)
def messages(
    request: Request, user: User, channel: str = "finance-ops", posted: int = 0
) -> HTMLResponse:
    if channel not in records.CHANNELS:
        raise HTTPException(404, "No such channel.")
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM team_messages WHERE channel = ? ORDER BY id", (channel,)
        ).fetchall()
    return kit.render(
        request,
        "messages.html",
        channels=records.CHANNELS,
        channel=channel,
        rows=rows,
        posted=posted,
        errors={},
    )


@app.post("/messages")
def message_post(request: Request, user: User, form: Form_) -> Response:
    channel = form.get("channel", "")
    try:
        with db() as conn:
            message_id = records.post_message(conn, channel, form.get("body", ""), user)
    except ValidationFailed as exc:
        raise HTTPException(422, str(exc)) from exc
    return _see_other(f"/messages?channel={channel}&posted={message_id}")
