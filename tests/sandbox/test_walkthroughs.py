"""Phase 1 exit check: a person can do family 1 and family 2 end to end, and the oracle
then reports exactly the right end state.

The script works the way a person at a browser would: it finds things through the
pages (search, links, downloads) and reads values off the PDF, never from the
scenario. The scenario is only used afterwards, as the answer key.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

import pdfplumber
from fastapi.testclient import TestClient

from sandbox.common.config import DEFAULTS
from tests.sandbox.conftest import csrf, records


def _links(html: str, prefix: str) -> list[tuple[str, str]]:
    return re.findall(rf'<a href="({re.escape(prefix)}[^"]+)"[^>]*>([^<]+)</a>', html)


def _option(html: str, select_id: str, label_starts: str) -> str:
    block = html.split(f'id="{select_id}"', 1)[1].split("</select>", 1)[0]
    for value, label in re.findall(r'<option value="([^"]*)"[^>]*>([^<]+)</option>', block):
        if label.startswith(label_starts):
            return str(value)
    raise AssertionError(f"no option {label_starts!r} in {select_id}")


def _pdf_text(client: TestClient, url: str) -> str:
    response = client.get(url)
    assert response.headers["content-type"] == "application/pdf"
    with pdfplumber.open(io.BytesIO(response.content)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _after(text: str, label: str) -> str:
    found = re.search(rf"{re.escape(label)}\s*(.+)", text)
    assert found, label
    return found.group(1).strip()


def _iso(human: str) -> str:
    return datetime.strptime(human, "%d %b %Y").date().isoformat()


def test_family_1_invoice_to_payable(
    world: dict[str, Any], mailbox: TestClient, admin: TestClient, oracle: TestClient
) -> None:
    vendor_name = world["vendors"]["main"]["name"]  # what the requester would type

    # Mailbox: search for the vendor's invoices, open the newest one, download the PDF.
    inbox = mailbox.get("/inbox", params={"q": vendor_name}).text
    newest_link = _links(inbox, "/messages/")[0][0]  # the inbox is newest first
    message = mailbox.get(newest_link).text
    [(attachment_url, _)] = _links(message, "/attachments/")
    text = _pdf_text(mailbox, attachment_url)

    invoice_no = _after(text, "Invoice No:")
    invoice_date = _iso(_after(text, "Invoice Date:"))
    due_date = _iso(_after(text, "Due Date:"))
    total = _after(text, "Total Due: INR")

    # Admin: enter the payable through the form.
    form_page = admin.get("/payables/new").text
    response = admin.post(
        "/payables",
        data={
            "csrf_token": csrf(admin, "/payables/new"),
            "vendor_id": _option(form_page, "vendor_id", vendor_name),
            "invoice_no": invoice_no,
            "invoice_date": invoice_date,
            "due_date": due_date,
            "amount": total,
            "po_number": _after(text, "PO Number:"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    saved = admin.get(response.headers["location"]).text
    ref = re.search(r"Payable (PAY-\d+) saved", saved)
    assert ref

    # Tell the requester on the team channel.
    admin.post(
        "/messages",
        data={
            "csrf_token": csrf(admin, "/messages"),
            "channel": "finance-ops",
            "body": f"Entered {invoice_no} from {vendor_name} as {ref.group(1)}. Ref: BC-TEST",
        },
    )

    # Oracle: exactly one payable with the right values, and one new message.
    expected = world["invoices"]["main_latest"]
    rows = records(oracle, "payables", vendor_name=vendor_name, invoice_no=expected["invoice_no"])
    assert len(rows) == 1
    row = rows[0]
    assert (row["amount"], row["invoice_date"], row["due_date"], row["status"]) == (
        expected["amount"],
        expected["invoice_date"],
        expected["due_date"],
        "entered",
    )
    baseline = world["baseline"]["team_messages"]
    new = records(oracle, "team_messages", channel="finance-ops", after_id=baseline)
    assert len(new) == 1 and expected["invoice_no"] in new[0]["body"]
    older = world["invoices"]["main_older"]["invoice_no"]
    assert len(records(oracle, "payables", invoice_no=older)) == 1  # untouched


def test_family_2_new_hire_onboarding(
    world: dict[str, Any], mailbox: TestClient, admin: TestClient, oracle: TestClient
) -> None:
    first_name = world["new_hires"]["a"]["first_name"]  # what the requester would type

    inbox = mailbox.get("/inbox", params={"q": f"offer letter: {first_name}"}).text
    [(message_url, _)] = _links(inbox, "/messages/")
    [(attachment_url, _)] = _links(mailbox.get(message_url).text, "/attachments/")
    letter = _pdf_text(mailbox, attachment_url)

    full_name = _after(letter, "Dear").rstrip(",")
    work_email = _after(letter, "Work email (active from your start date):")
    personal_email = _after(letter, "Personal email for correspondence before joining:")
    manager = _after(letter, "Reporting manager:")

    form_page = admin.get("/people/new").text
    response = admin.post(
        "/people",
        data={
            "csrf_token": csrf(admin, "/people/new"),
            "full_name": full_name,
            "work_email": work_email,
            "title": _after(letter, "Position:"),
            "department": _after(letter, "Department:"),
            "start_date": _iso(_after(letter, "Start date:")),
            "manager_id": _option(form_page, "manager_id", manager),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    # IT ticket through the API, sent twice with one key, as a retry would.
    headers = {
        "Authorization": f"Bearer {DEFAULTS['ADMIN_API_TOKEN']}",
        "Idempotency-Key": "walkthrough-ticket",
    }
    ticket = {
        "title": f"Laptop and accounts for {full_name}",
        "description": f"New joiner {full_name} ({work_email}). Please prepare a laptop.",
        "category": "it",
        "requester": DEFAULTS["ADMIN_USER"],
    }
    for _ in range(2):
        assert admin.post("/api/tickets", json=ticket, headers=headers).status_code == 201

    # Welcome email to the personal address.
    sent = mailbox.post(
        "/compose",
        data={
            "csrf_token": csrf(mailbox, "/compose"),
            "to": personal_email,
            "subject": "Welcome to Acme",
            "body": f"Hi {first_name}, welcome aboard! We look forward to your first day.",
        },
        follow_redirects=False,
    )
    assert sent.status_code == 303

    hire = world["new_hires"]["a"]
    [employee] = records(oracle, "employees", work_email=hire["work_email"])
    assert (
        employee["full_name"],
        employee["title"],
        employee["department"],
        employee["start_date"],
        employee["manager_name"],
        employee["status"],
    ) == (
        hire["full_name"],
        hire["title"],
        hire["department"],
        hire["start_date"],
        hire["manager_name"],
        "onboarding",
    )
    new_tickets = records(oracle, "tickets", after_id=world["baseline"]["tickets"])
    assert [t["category"] for t in new_tickets] == ["it"]
    assert hire["full_name"] in new_tickets[0]["title"]
    outbound = records(oracle, "outbound_email", to_addr=hire["personal_email"])
    assert len(outbound) == 1 and first_name in outbound[0]["body"]
