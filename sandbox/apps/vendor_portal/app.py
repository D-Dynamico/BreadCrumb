"""SupplierHub vendor portal (port 8102): an "external" billing site.

Vendors share invoices with their customers here. Acme's account sees the
invoices addressed to Acme. Read only for customers, apart from signing in.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from sandbox.common.config import files_dir
from sandbox.common.db import db
from sandbox.common.web import WebApp


def _verify(conn: sqlite3.Connection, username: str, password: str) -> bool:
    row = conn.execute(
        "SELECT password FROM portal_accounts WHERE username = ?", (username,)
    ).fetchone()
    return row is not None and row["password"] == password


kit = WebApp("vendor_portal", "SupplierHub", Path(__file__).with_name("templates"), _verify)
app = kit.app
User = Annotated[str, Depends(kit.user)]


def _customer(user: str) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT customer FROM portal_accounts WHERE username = ?", (user,)
        ).fetchone()
    return str(row["customer"])


@app.get("/")
def root(user: User) -> RedirectResponse:
    return RedirectResponse("/invoices", status_code=303)


@app.get("/invoices", response_class=HTMLResponse)
def invoices(request: Request, user: User, vendor: str = "", status: str = "") -> HTMLResponse:
    customer = _customer(user)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM portal_invoices WHERE customer = ? AND (? = '' OR vendor_name = ?)"
            " AND (? = '' OR status = ?) ORDER BY invoice_date DESC, id DESC",
            (customer, vendor, vendor, status, status),
        ).fetchall()
        vendors = [
            r["vendor_name"]
            for r in conn.execute(
                "SELECT DISTINCT vendor_name FROM portal_invoices WHERE customer = ?"
                " ORDER BY vendor_name",
                (customer,),
            )
        ]
    return kit.render(
        request,
        "invoices.html",
        rows=rows,
        vendors=vendors,
        vendor=vendor,
        status=status,
        customer=customer,
    )


def _listing(user: str, listing_ref: str) -> sqlite3.Row:
    with db() as conn:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM portal_invoices WHERE listing_ref = ? AND customer = ?",
            (listing_ref, _customer(user)),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Invoice not found.")
    return row


@app.get("/invoices/{listing_ref}", response_class=HTMLResponse)
def invoice(request: Request, user: User, listing_ref: str) -> HTMLResponse:
    return kit.render(request, "invoice.html", inv=_listing(user, listing_ref))


@app.get("/invoices/{listing_ref}/pdf")
def invoice_pdf(user: User, listing_ref: str) -> FileResponse:
    row = _listing(user, listing_ref)
    return FileResponse(
        files_dir() / row["file_path"],
        media_type="application/pdf",
        filename=Path(row["file_path"]).name,
    )
