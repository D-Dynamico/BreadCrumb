"""The contract: validation against the API spec, and checks derived in code (D37, D38)."""

from __future__ import annotations

from typing import Any

import pytest

from breadcrumb.contract.model import ApiSpec, Contract, derive_checks, validate

SPEC = ApiSpec(
    reads={
        "listPayables": {"id", "vendor_name", "invoice_no", "amount", "due_date", "status"},
        "listVendors": {"id", "name", "bank_account", "bank_ifsc"},
    }
)


def _contract(**over: Any) -> Contract:
    data: dict[str, Any] = {
        "goal": "Enter the newest bill and tell the requester",
        "assumptions": ["Newest means latest document date"],
        "open_questions": [],
        "facts": [
            {"key": "supplier", "type": "text", "description": "who sent it"},
            {"key": "doc_no", "type": "id", "description": "its number"},
            {"key": "total", "type": "money", "description": "amount due"},
        ],
        "deliverables": [
            {
                "id": "entered",
                "kind": "create",
                "description": "the bill entered as a record",
                "lookup_operation": "listPayables",
                "key": [
                    {"field": "vendor_name", "fact": "supplier"},
                    {"field": "invoice_no", "fact": "doc_no"},
                ],
                "values": [{"field": "amount", "fact": "total"}],
            },
            {
                "id": "told",
                "kind": "send",
                "description": "tell the requester",
                "lookup_operation": "notify",
                "must_contain": ["doc_no"],
            },
        ],
        "protected": [
            {
                "lookup_operation": "listVendors",
                "match": [{"field": "name", "fact": "supplier"}],
                "fields": ["bank_account", "bank_ifsc"],
            }
        ],
        "extra_checks": [],
    }
    data.update(over)
    return Contract.model_validate(data)


def test_a_sound_contract_has_no_errors() -> None:
    assert validate(_contract(), SPEC) == []


def test_derived_checks_cover_every_deliverable_and_protected_field() -> None:
    checks = derive_checks(_contract())
    kinds = [(c.type, c.field) for c in checks]
    assert ("record_unique", None) in kinds
    assert ("field_equals", "amount") in kinds
    assert ("message_sent", None) in kinds
    assert ("field_unchanged", None) in kinds
    unique = next(c for c in checks if c.type == "record_unique")
    assert unique.match == {"vendor_name": "supplier", "invoice_no": "doc_no"}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            {
                "deliverables": [
                    {
                        "id": "x",
                        "kind": "create",
                        "description": "d",
                        "lookup_operation": "listThings",
                        "key": [{"field": "a", "fact": "doc_no"}],
                    }
                ]
            },
            "listThings",
        ),
        (
            {
                "deliverables": [
                    {
                        "id": "x",
                        "kind": "create",
                        "description": "d",
                        "lookup_operation": "listPayables",
                        "key": [{"field": "number", "fact": "doc_no"}],
                    }
                ]
            },
            "number",
        ),
        (
            {
                "deliverables": [
                    {
                        "id": "x",
                        "kind": "create",
                        "description": "d",
                        "lookup_operation": "listPayables",
                        "key": [{"field": "invoice_no", "fact": "ref"}],
                    }
                ]
            },
            "fact:ref",
        ),
        (
            {
                "deliverables": [
                    {
                        "id": "x",
                        "kind": "create",
                        "description": "d",
                        "lookup_operation": "listPayables",
                        "key": [],
                    }
                ]
            },
            "key",
        ),
        (
            {
                "deliverables": [
                    {
                        "id": "x",
                        "kind": "send",
                        "description": "d",
                        "lookup_operation": "notify",
                        "must_contain": [],
                    }
                ]
            },
            "must_contain",
        ),
        ({"extra_checks": [{"type": "judgement", "question": "is it good?"}]}, "judgement"),
        ({"extra_checks": [{"type": "record_exists"}]}, "record_exists"),
        (
            {
                "extra_checks": [
                    {
                        "type": "field_unchanged",
                        "lookup_operation": "listVendors",
                        "match": [],
                        "fields": ["bank_account"],
                    }
                ]
            },
            "needs match",
        ),
    ],
)
def test_invalid_contracts_are_rejected_with_the_reason(change: dict[str, Any], error: str) -> None:
    errors = validate(_contract(**change), SPEC)
    assert errors and any(error in e for e in errors), errors


def test_a_create_key_must_identify_one_record() -> None:
    by_name_only = _contract().deliverables[0].model_dump()
    by_name_only["key"] = [{"field": "vendor_name", "fact": "supplier"}]
    errors = validate(_contract(deliverables=[by_name_only]), SPEC)
    assert any("identifies one record" in e for e in errors)


def test_duplicate_ids_and_two_messages_are_rejected() -> None:
    told = _contract().deliverables[1].model_dump()
    errors = validate(_contract(deliverables=[told, {**told, "id": "told2"}]), SPEC)
    assert any("one message" in e for e in errors)
    errors = validate(_contract(deliverables=[told, told]), SPEC)
    assert any("twice" in e for e in errors)


def test_a_send_always_uses_the_notify_lookup() -> None:
    told = {
        "id": "t",
        "kind": "send",
        "lookup_operation": "postMessage",
        "must_contain": ["doc_no"],
    }
    contract = _contract(deliverables=[told])
    assert contract.deliverables[0].lookup_operation == "notify"
    assert validate(contract, SPEC) == []


def test_a_contract_with_no_deliverables_is_allowed() -> None:
    # A task the worker must refuse or ask about may have nothing to produce.
    assert validate(_contract(deliverables=[], protected=[]), SPEC) == []


def test_api_spec_reads_response_fields_from_openapi() -> None:
    openapi = {
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Thing"},
                                    }
                                }
                            }
                        }
                    },
                },
                "post": {"operationId": "makeThing", "responses": {}},
            },
            "/things/{id}": {
                "get": {
                    "operationId": "getThing",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Thing"}
                                }
                            }
                        }
                    },
                }
            },
        },
        "components": {"schemas": {"Thing": {"properties": {"id": {}, "label": {}}}}},
    }
    spec = ApiSpec.from_openapi(openapi)
    assert spec.reads == {"listThings": {"id", "label"}}  # getThing needs an id
    assert "notify" in spec.with_notify().reads
