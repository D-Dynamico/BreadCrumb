"""Acme Mailbox (port 8101): the shared finance ops inbox.

Mail to an @acme.test address is delivered to that mailbox. Mail to any other
address is recorded in `outbound_mail` and goes nowhere: nothing leaves the
machine. Opening a message does not mark it read, so every GET stays a pure read.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from sandbox.common import audit
from sandbox.common.config import files_dir
from sandbox.common.db import db, now_iso
from sandbox.common.web import WebApp

APP = "mailbox"
INTERNAL_DOMAIN = "@acme.test"
_ADDRESS = re.compile(r"^[^@\s,]+@[^@\s,]+\.[a-z]+$", re.IGNORECASE)


def _verify(conn: sqlite3.Connection, username: str, password: str) -> bool:
    row = conn.execute(
        "SELECT password FROM mail_accounts WHERE email = ? AND password IS NOT NULL",
        (username.lower(),),
    ).fetchone()
    return row is not None and row["password"] == password


kit = WebApp(APP, "Acme Mail", Path(__file__).with_name("templates"), _verify)
app = kit.app
User = Annotated[str, Depends(kit.user)]


@app.get("/")
def root(user: User) -> RedirectResponse:
    return RedirectResponse("/inbox", status_code=303)


def _folder(request: Request, user: str, folder: str, q: str, just_sent: int = 0) -> HTMLResponse:
    like = f"%{q.strip()}%"
    with db() as conn:
        rows = conn.execute(
            "SELECT m.*, (SELECT COUNT(*) FROM mail_attachments a WHERE a.message_id = m.id)"
            " AS attachment_count FROM mail_messages m WHERE owner = ? AND folder = ?"
            " AND (? = '' OR subject LIKE ? OR body LIKE ? OR from_addr LIKE ? OR from_name LIKE ?"
            " OR to_addr LIKE ?) ORDER BY sent_at DESC, id DESC",
            (user, folder, q.strip(), like, like, like, like, like),
        ).fetchall()
    template = "message_rows.html" if request.headers.get("HX-Request") else "folder.html"
    return kit.render(request, template, rows=rows, folder=folder, q=q, just_sent=just_sent)


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, user: User, q: str = "") -> HTMLResponse:
    return _folder(request, user, "inbox", q)


@app.get("/sent", response_class=HTMLResponse)
def sent(request: Request, user: User, q: str = "", sent: int = 0) -> HTMLResponse:
    return _folder(request, user, "sent", q, just_sent=sent)


@app.get("/messages/{message_id}", response_class=HTMLResponse)
def message(request: Request, user: User, message_id: int) -> HTMLResponse:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM mail_messages WHERE id = ? AND owner = ?", (message_id, user)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "That message does not exist.")
        attachments = conn.execute(
            "SELECT * FROM mail_attachments WHERE message_id = ?", (message_id,)
        ).fetchall()
    return kit.render(request, "message.html", m=row, attachments=attachments)


@app.get("/attachments/{attachment_id}")
def attachment(user: User, attachment_id: int) -> FileResponse:
    with db() as conn:
        row = conn.execute(
            "SELECT a.* FROM mail_attachments a JOIN mail_messages m ON m.id = a.message_id"
            " WHERE a.id = ? AND m.owner = ?",
            (attachment_id, user),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "That attachment does not exist.")
    return FileResponse(
        files_dir() / row["file_path"], media_type="application/pdf", filename=row["filename"]
    )


@app.get("/compose", response_class=HTMLResponse)
def compose(request: Request, user: User) -> HTMLResponse:
    return kit.render(request, "compose.html", values={}, errors={})


@app.post("/compose")
async def send(request: Request, user: User) -> Response:
    data = await request.form()
    form = {k: str(v) for k, v in data.items()}
    kit.check_csrf(request, form.pop("csrf_token", ""))
    recipients = [a.strip().lower() for a in form.get("to", "").split(",") if a.strip()]
    subject = form.get("subject", "").strip()
    body = form.get("body", "").strip()
    errors: dict[str, str] = {}
    if not recipients:
        errors["to"] = "Add at least one recipient."
    elif bad := [a for a in recipients if not _ADDRESS.match(a)]:
        errors["to"] = f"Not a valid email address: {', '.join(bad)}"
    if not subject:
        errors["subject"] = "Add a subject."
    if not body:
        errors["body"] = "The message is empty."
    with db() as conn:
        internal = [a for a in recipients if a.endswith(INTERNAL_DOMAIN)]
        unknown = [
            a
            for a in internal
            if not conn.execute("SELECT 1 FROM mail_accounts WHERE email = ?", (a,)).fetchone()
        ]
        if unknown and "to" not in errors:
            errors["to"] = f"No such mailbox at Acme: {', '.join(unknown)}"
        if errors:
            return kit.render(request, "compose.html", status_code=422, values=form, errors=errors)
        sender = conn.execute(
            "SELECT display_name FROM mail_accounts WHERE email = ?", (user,)
        ).fetchone()
        now = now_iso()
        to_addr = ", ".join(recipients)
        values = {
            "owner": user,
            "folder": "sent",
            "from_addr": user,
            "from_name": sender["display_name"],
            "to_addr": to_addr,
            "subject": subject,
            "body": body,
            "sent_at": now,
        }
        cur = conn.execute(
            "INSERT INTO mail_messages (owner, folder, from_addr, from_name, to_addr, subject,"
            " body, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            list(values.values()),
        )
        sent_id = int(cur.lastrowid or 0)
        audit.record_create(conn, APP, "message", sent_id, values, user)
        for address in recipients:
            if address in internal:
                conn.execute(
                    "INSERT INTO mail_messages (owner, folder, from_addr, from_name, to_addr,"
                    " subject, body, sent_at) VALUES (?, 'inbox', ?, ?, ?, ?, ?, ?)",
                    (address, user, sender["display_name"], to_addr, subject, body, now),
                )
            else:
                conn.execute(
                    "INSERT INTO outbound_mail (message_id, to_addr, queued_at) VALUES (?, ?, ?)",
                    (sent_id, address, now),
                )
    return RedirectResponse(f"/sent?sent={sent_id}", status_code=303)
