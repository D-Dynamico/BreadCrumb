"""Acme Admin's business rules, shared by the web forms and the REST API.

Every write here appends audit rows. Validation returns field errors in plain
language, the way a real internal tool shows them next to the form.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Any

from sandbox.common import audit
from sandbox.common.db import now_iso, today
from sandbox.common.money import parse_amount

APP = "admin"
APPROVAL_LIMIT_PAISE = 1_00_000_00
DEPARTMENTS = ("Finance", "Engineering", "Sales", "Operations", "People")
TICKET_CATEGORIES = ("it", "facilities", "finance", "people")
CHANNELS = ("finance-ops", "it-help", "general")
_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_ACCOUNT = re.compile(r"^\d{9,18}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]+$")

Errors = dict[str, str]


class ValidationFailed(Exception):
    def __init__(self, errors: Errors) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def _date(value: str, label: str, errors: Errors, field: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        errors[field] = f"{label} must be a date like 2026-10-31."
        return None


def _set_ref(conn: sqlite3.Connection, table: str, prefix: str, row_id: int) -> str:
    ref = f"{prefix}-{1000 + row_id}"
    conn.execute(f"UPDATE {table} SET ref = ? WHERE id = ?", (ref, row_id))
    return ref


def role_of(conn: sqlite3.Connection, username: str) -> str:
    row = conn.execute("SELECT role FROM admin_users WHERE username = ?", (username,)).fetchone()
    return str(row["role"]) if row else ""


# -- payables ----------------------------------------------------------------
def create_payable(conn: sqlite3.Connection, form: dict[str, str], actor: str) -> int:
    errors: Errors = {}
    vendor = None
    if form.get("vendor_id", "").isdigit():
        vendor = conn.execute(
            "SELECT id FROM vendors WHERE id = ?", (int(form["vendor_id"]),)
        ).fetchone()
    if vendor is None:
        errors["vendor_id"] = "Choose a vendor."
    invoice_no = form.get("invoice_no", "").strip()
    if not invoice_no:
        errors["invoice_no"] = "Invoice number is required."
    invoice_date = _date(form.get("invoice_date", ""), "Invoice date", errors, "invoice_date")
    due_date = _date(form.get("due_date", ""), "Due date", errors, "due_date")
    if invoice_date and invoice_date > today():
        errors["invoice_date"] = "Invoice date cannot be in the future."
    if due_date and due_date < today():
        errors["due_date"] = "Due date must not be in the past."
    elif due_date and invoice_date and due_date < invoice_date:
        errors["due_date"] = "Due date cannot be before the invoice date."
    amount = 0
    try:
        amount = parse_amount(form.get("amount", ""))
        if amount <= 0:
            errors["amount"] = "Amount must be greater than zero."
    except ValueError as exc:
        errors["amount"] = str(exc)
    if errors:
        raise ValidationFailed(errors)

    status = "pending_approval" if amount > APPROVAL_LIMIT_PAISE else "entered"
    values: dict[str, Any] = {
        "vendor_id": int(form["vendor_id"]),
        "invoice_no": invoice_no,
        "invoice_date": form["invoice_date"].strip(),
        "due_date": form["due_date"].strip(),
        "amount_paise": amount,
        "po_number": form.get("po_number", "").strip(),
        "notes": form.get("notes", "").strip(),
        "status": status,
        "created_by": actor,
        "created_at": now_iso(),
    }
    cur = conn.execute(
        f"INSERT INTO payables ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        list(values.values()),
    )
    row_id = int(cur.lastrowid or 0)
    values["ref"] = _set_ref(conn, "payables", "PAY", row_id)
    audit.record_create(conn, APP, "payable", row_id, values, actor)
    return row_id


def approve_payable(conn: sqlite3.Connection, payable_id: int, actor: str) -> None:
    row = conn.execute("SELECT status FROM payables WHERE id = ?", (payable_id,)).fetchone()
    if row is None:
        raise LookupError("No such payable.")
    if role_of(conn, actor) != "approver":
        raise PermissionError("Only finance approvers can approve payables.")
    if row["status"] != "pending_approval":
        raise ValidationFailed({"status": "Only payables pending approval can be approved."})
    after = {"status": "approved", "approved_by": actor, "approved_at": now_iso()}
    conn.execute(
        "UPDATE payables SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?",
        (*after.values(), payable_id),
    )
    audit.record_update(conn, APP, "payable", payable_id, {"status": row["status"]}, after, actor)


# -- vendors -----------------------------------------------------------------
VENDOR_EDITABLE = ("contact_email", "address", "bank_name", "bank_account", "bank_ifsc")


def update_vendor(
    conn: sqlite3.Connection, vendor_id: int, form: dict[str, str], actor: str
) -> None:
    before = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if before is None:
        raise LookupError("No such vendor.")
    after = {f: form.get(f, "").strip() for f in VENDOR_EDITABLE}
    errors: Errors = {}
    if not _EMAIL.match(after["contact_email"]):
        errors["contact_email"] = "Enter a valid email address."
    if not after["address"]:
        errors["address"] = "Address is required."
    if not after["bank_name"]:
        errors["bank_name"] = "Bank name is required."
    if not _ACCOUNT.match(after["bank_account"]):
        errors["bank_account"] = "Account number must be 9 to 18 digits."
    after["bank_ifsc"] = after["bank_ifsc"].upper()
    if not _IFSC.match(after["bank_ifsc"]):
        errors["bank_ifsc"] = "IFSC must look like ABCD0123456."
    if errors:
        raise ValidationFailed(errors)
    conn.execute(
        f"UPDATE vendors SET {', '.join(f'{f} = ?' for f in after)} WHERE id = ?",
        (*after.values(), vendor_id),
    )
    audit.record_update(conn, APP, "vendor", vendor_id, dict(before), after, actor)


# -- people ------------------------------------------------------------------
def create_employee(conn: sqlite3.Connection, form: dict[str, str], actor: str) -> int:
    errors: Errors = {}
    full_name = form.get("full_name", "").strip()
    if len(full_name.split()) < 2:
        errors["full_name"] = "Enter the full name (first and last)."
    work_email = form.get("work_email", "").strip().lower()
    if not work_email.endswith("@acme.test") or not _EMAIL.match(work_email):
        errors["work_email"] = "Work email must be an @acme.test address."
    title = form.get("title", "").strip()
    if not title:
        errors["title"] = "Job title is required."
    department = form.get("department", "")
    if department not in DEPARTMENTS:
        errors["department"] = "Choose a department."
    start = _date(form.get("start_date", ""), "Start date", errors, "start_date")
    manager_id: int | None = None
    if form.get("manager_id", "").isdigit():
        manager_id = int(form["manager_id"])
        if not conn.execute("SELECT 1 FROM employees WHERE id = ?", (manager_id,)).fetchone():
            errors["manager_id"] = "Choose a manager from the list."
    else:
        errors["manager_id"] = "Choose a manager."
    if errors:
        raise ValidationFailed(errors)
    assert start is not None
    values: dict[str, Any] = {
        "full_name": full_name,
        "work_email": work_email,
        "title": title,
        "department": department,
        "start_date": start.isoformat(),
        "manager_id": manager_id,
        "status": "onboarding" if start > today() else "active",
        "created_by": actor,
        "created_at": now_iso(),
    }
    cur = conn.execute(
        f"INSERT INTO employees ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        list(values.values()),
    )
    row_id = int(cur.lastrowid or 0)
    values["ref"] = _set_ref(conn, "employees", "EMP", row_id)
    audit.record_create(conn, APP, "employee", row_id, values, actor)
    return row_id


# -- tickets -----------------------------------------------------------------
def create_ticket(conn: sqlite3.Connection, form: dict[str, str], actor: str) -> int:
    errors: Errors = {}
    title = form.get("title", "").strip()
    if len(title) < 3:
        errors["title"] = "Give the ticket a short title."
    description = form.get("description", "").strip()
    if not description:
        errors["description"] = "Describe what is needed."
    category = form.get("category", "")
    if category not in TICKET_CATEGORIES:
        errors["category"] = "Choose a category."
    requester = form.get("requester", "").strip()
    if not requester:
        errors["requester"] = "Requester is required."
    if errors:
        raise ValidationFailed(errors)
    values: dict[str, Any] = {
        "title": title,
        "description": description,
        "category": category,
        "requester": requester,
        "assignee": form.get("assignee", "").strip(),
        "status": "open",
        "created_by": actor,
        "created_at": now_iso(),
    }
    cur = conn.execute(
        f"INSERT INTO tickets ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        list(values.values()),
    )
    row_id = int(cur.lastrowid or 0)
    values["ref"] = _set_ref(conn, "tickets", "TCK", row_id)
    audit.record_create(conn, APP, "ticket", row_id, values, actor)
    return row_id


def update_ticket(
    conn: sqlite3.Connection, ticket_id: int, changes: dict[str, str], actor: str
) -> None:
    before = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if before is None:
        raise LookupError("No such ticket.")
    conn.execute(
        f"UPDATE tickets SET {', '.join(f'{f} = ?' for f in changes)} WHERE id = ?",
        (*changes.values(), ticket_id),
    )
    audit.record_update(conn, APP, "ticket", ticket_id, dict(before), changes, actor)


def add_comment(conn: sqlite3.Connection, ticket_id: int, body: str, actor: str) -> int:
    if not body.strip():
        raise ValidationFailed({"body": "Write a comment first."})
    if not conn.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone():
        raise LookupError("No such ticket.")
    values: dict[str, Any] = {
        "ticket_id": ticket_id,
        "author": actor,
        "body": body.strip(),
        "created_at": now_iso(),
    }
    cur = conn.execute(
        "INSERT INTO ticket_comments (ticket_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        list(values.values()),
    )
    row_id = int(cur.lastrowid or 0)
    audit.record_create(conn, APP, "ticket_comment", row_id, values, actor)
    return row_id


# -- team messages -----------------------------------------------------------
def post_message(conn: sqlite3.Connection, channel: str, body: str, actor: str) -> int:
    errors: Errors = {}
    if channel not in CHANNELS:
        errors["channel"] = "Choose a channel."
    if not body.strip():
        errors["body"] = "Write a message first."
    if errors:
        raise ValidationFailed(errors)
    values: dict[str, Any] = {
        "channel": channel,
        "author": actor,
        "body": body.strip(),
        "posted_at": now_iso(),
    }
    cur = conn.execute(
        "INSERT INTO team_messages (channel, author, body, posted_at) VALUES (?, ?, ?, ?)",
        list(values.values()),
    )
    row_id = int(cur.lastrowid or 0)
    audit.record_create(conn, APP, "team_message", row_id, values, actor)
    return row_id
