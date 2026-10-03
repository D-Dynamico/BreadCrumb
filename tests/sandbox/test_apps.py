"""App behavior a real operations worker would run into: logins, validation, approvals,
duplicates, audit, CSRF, the API's idempotency, and where email goes."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from sandbox.common.config import DEFAULTS
from tests.sandbox.conftest import csrf, login, records

GET_PAGES = {
    "admin": [
        "/", "/payables", "/payables/new", "/payables/1", "/vendors", "/vendors/1",
        "/vendors/1/edit", "/purchase-orders", "/people", "/people/new", "/people/1",
        "/tickets", "/tickets/new", "/tickets/1", "/messages", "/messages?channel=general",
    ],
    "mailbox": ["/inbox", "/inbox?q=invoice", "/sent", "/compose", "/messages/1"],
    "portal": ["/invoices", "/invoices?status=Open", "/invoices/SH-20401"],
}  # fmt: skip
API = {"Authorization": f"Bearer {DEFAULTS['ADMIN_API_TOKEN']}"}


def _payable_form(admin: TestClient, **overrides: str) -> dict[str, str]:
    form = {
        "csrf_token": csrf(admin, "/payables/new"),
        "vendor_id": "1",
        "invoice_no": "T-100",
        "invoice_date": "2026-10-01",
        "due_date": "2026-10-30",
        "amount": "12,500.00",
    }
    return {**form, **overrides}


def test_pages_need_a_login_and_reads_never_write(
    admin: TestClient, mailbox: TestClient, portal: TestClient, oracle: TestClient
) -> None:
    from sandbox.apps.admin.app import app

    anonymous = TestClient(app).get("/payables", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/login?next=/payables"
    for name, client in (("admin", admin), ("mailbox", mailbox), ("portal", portal)):
        for path in GET_PAGES[name]:
            assert client.get(path).status_code == 200, (name, path)
    assert records(oracle, "audit_log") == []


def test_wrong_password_is_refused(world: dict[str, Any]) -> None:
    from sandbox.apps.admin.app import app

    response = TestClient(app).post("/login", data={"username": "ops.worker", "password": "no"})
    assert response.status_code == 401
    assert "Wrong username or password" in response.text


def test_payable_validation_shows_plain_errors(admin: TestClient, oracle: TestClient) -> None:
    form = _payable_form(admin, invoice_no=" ", due_date="2026-09-01", amount="0")
    response = admin.post("/payables", data=form)
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    for message in (
        "Invoice number is required.",
        "Due date must not be in the past.",
        "Amount must be greater than zero.",
    ):
        assert message in response.text
    assert records(oracle, "payables", invoice_no=" ") == []


def test_admin_accepts_the_same_invoice_twice(admin: TestClient, oracle: TestClient) -> None:
    for _ in range(2):
        response = admin.post("/payables", data=_payable_form(admin), follow_redirects=False)
        assert response.status_code == 303
    assert len(records(oracle, "payables", invoice_no="T-100")) == 2  # D17


def test_large_payable_waits_and_only_an_approver_can_approve(
    admin: TestClient, oracle: TestClient
) -> None:
    response = admin.post(
        "/payables", data=_payable_form(admin, amount="1,00,000.01"), follow_redirects=False
    )
    page = admin.get(response.headers["location"])
    assert "waiting for approval" in page.text
    assert "Approve payable" not in page.text
    [row] = records(oracle, "payables", invoice_no="T-100")
    assert row["status"] == "pending_approval"

    path = f"/payables/{row['id']}/approve"
    assert admin.post(path, data={"csrf_token": csrf(admin, "/payables/new")}).status_code == 403

    from sandbox.apps.admin.app import app

    approver = login(
        TestClient(app), DEFAULTS["SANDBOX_APPROVER_USER"], DEFAULTS["SANDBOX_APPROVER_PASSWORD"]
    )
    approver.post(path, data={"csrf_token": csrf(approver, "/payables/new")})
    [row] = records(oracle, "payables", invoice_no="T-100")
    assert (row["status"], row["approved_by"]) == ("approved", DEFAULTS["SANDBOX_APPROVER_USER"])


def test_exactly_one_lakh_is_not_above_the_limit(admin: TestClient, oracle: TestClient) -> None:
    admin.post("/payables", data=_payable_form(admin, amount="100000"))
    [row] = records(oracle, "payables", invoice_no="T-100")
    assert row["status"] == "entered"


def test_writes_are_audited_field_by_field(admin: TestClient, oracle: TestClient) -> None:
    form = {
        "csrf_token": csrf(admin, "/vendors/1/edit"),
        "contact_email": "billing@example.test",
        "address": "1 New Street",
        "bank_name": "Kaveri Bank",
        "bank_account": "123456789012",
        "bank_ifsc": "kavb0123456",
    }
    before = records(oracle, "vendors", id=1)[0]
    assert admin.post("/vendors/1/edit", data=form, follow_redirects=False).status_code == 303
    changes = {
        r["field"]: (r["old_value"], r["new_value"])
        for r in records(oracle, "audit_log", entity="vendor", record_id="1")
    }
    assert changes["bank_account"] == (before["bank_account"], "123456789012")
    assert changes["bank_ifsc"] == (before["bank_ifsc"], "KAVB0123456")


def test_forms_without_the_csrf_token_are_refused(admin: TestClient) -> None:
    form = _payable_form(admin)
    form.pop("csrf_token")
    assert admin.post("/payables", data=form).status_code == 403


def test_api_needs_a_token_and_does_not_create_payables(world: dict[str, Any]) -> None:
    from sandbox.apps.admin.app import app

    client = TestClient(app)
    assert client.get("/api/vendors").status_code == 401
    assert client.get("/api/vendors", headers=API).status_code == 200
    assert client.post("/api/payables", headers=API, json={}).status_code == 405
    paths = client.get("/api/openapi.json").json()["paths"]
    assert "post" in paths["/tickets"] and "post" not in paths["/payables"]


def test_api_idempotency_key_replays_instead_of_duplicating(
    world: dict[str, Any], oracle: TestClient
) -> None:
    from sandbox.apps.admin.app import app

    client = TestClient(app)
    body = {
        "title": "Laptop setup",
        "description": "New joiner",
        "category": "it",
        "requester": "x",
    }
    headers = {**API, "Idempotency-Key": "run-1:ticket"}
    first = client.post("/api/tickets", json=body, headers=headers)
    again = client.post("/api/tickets", json=body, headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(records(oracle, "tickets", title="Laptop setup")) == 1
    clash = client.post("/api/tickets", json={**body, "title": "Other"}, headers=headers)
    assert clash.status_code == 409
    no_key = client.post("/api/tickets", json=body, headers=API)
    assert no_key.status_code == 201
    assert len(records(oracle, "tickets", title="Laptop setup")) == 2


def _send(mailbox: TestClient, to: str) -> int:
    form = {"csrf_token": csrf(mailbox, "/compose"), "to": to, "subject": "Hi", "body": "Hello"}
    response = mailbox.post("/compose", data=form, follow_redirects=False)
    return int(response.status_code)


def test_mail_outside_acme_goes_to_the_outbound_log(
    world: dict[str, Any], mailbox: TestClient, oracle: TestClient
) -> None:
    colleague = world["requester"]["work_email"]
    assert _send(mailbox, f"someone@outside.test, {colleague}") == 303
    assert [r["to_addr"] for r in records(oracle, "outbound_email")] == ["someone@outside.test"]
    [sent] = records(oracle, "sent_email")[-1:]
    assert colleague in sent["to_addr"]
    assert _send(mailbox, "nobody@acme.test") == 422
