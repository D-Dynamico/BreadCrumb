"""The task contract: what the user should end up with, fixed before work starts.

Deliverables drive everything (D38): the write scope is the list of deliverables, a
commit names the one it produces, the natural key and the lookup come from it, and
the checks are derived from it in code. Every field and operation name is checked
against the API spec before the run starts, so a contract either makes sense for
these apps or is refused. No `judgement` checks until Phase 5 (D37).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

FactType = Literal["money", "date", "id", "email", "text"]
NOTIFY = "notify"
# Fields of a message as the notify lookup returns them (breadcrumb/tools/notify.py).
NOTIFY_FIELDS = frozenset({"id", "channel", "author", "body", "posted_at", "ref"})
EXTRA_CHECKS = ("field_unchanged", "no_message_sent")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class FactSpec(BaseModel):
    key: str
    type: FactType = "text"
    description: str = ""


class FieldRef(BaseModel):
    field: str
    fact: str


class Deliverable(BaseModel):
    id: str
    kind: Literal["create", "update", "send"]
    description: str = ""
    lookup_operation: str
    key: list[FieldRef] = Field(default_factory=list)
    values: list[FieldRef] = Field(default_factory=list)
    must_contain: list[str] = Field(default_factory=list)  # fact keys, send only

    @model_validator(mode="after")
    def _send_uses_notify(self) -> Deliverable:
        # A send is a message to the requester; the notify lookup is implied, not chosen.
        if self.kind == "send":
            self.lookup_operation = NOTIFY
        return self


class Protected(BaseModel):
    lookup_operation: str
    match: list[FieldRef] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)


class ExtraCheck(BaseModel):
    # A plain string on purpose: a check type the model invents reaches validation
    # and is refused there with a reason, instead of failing to parse.
    type: str
    lookup_operation: str = ""
    match: list[FieldRef] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    question: str = ""


class Question(BaseModel):
    question: str
    blocking: bool = False


class Contract(BaseModel):
    goal: str
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[Question] = Field(default_factory=list)
    facts: list[FactSpec] = Field(default_factory=list)
    deliverables: list[Deliverable] = Field(default_factory=list)
    protected: list[Protected] = Field(default_factory=list)
    extra_checks: list[ExtraCheck] = Field(default_factory=list)
    version: int = 1

    def deliverable(self, deliverable_id: str) -> Deliverable | None:
        return next((d for d in self.deliverables if d.id == deliverable_id), None)

    def fact_type(self, key: str) -> str | None:
        return next((f.type for f in self.facts if f.key == key), None)

    @property
    def blocking_questions(self) -> list[str]:
        return [q.question for q in self.open_questions if q.blocking]


@dataclass
class ApiSpec:
    """The list operations an API offers, with the fields their records have."""

    reads: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_openapi(cls, openapi: dict[str, Any]) -> ApiSpec:
        schemas = openapi.get("components", {}).get("schemas", {})

        def fields_of(schema: dict[str, Any]) -> set[str]:
            if "$ref" in schema:
                schema = schemas.get(schema["$ref"].rsplit("/", 1)[-1], {})
            if schema.get("type") == "array":
                return fields_of(schema.get("items", {}))
            return set(schema.get("properties", {}))

        reads: dict[str, set[str]] = {}
        for path, methods in openapi.get("paths", {}).items():
            spec = methods.get("get")
            # Only reads that list records can look one up by its fields; a read that
            # needs an id in its path cannot.
            if not spec or not spec.get("operationId") or "{" in path:
                continue
            ok = spec.get("responses", {}).get("200", {})
            schema = ok.get("content", {}).get("application/json", {}).get("schema", {})
            reads[spec["operationId"]] = fields_of(schema)
        return cls(reads)

    def with_notify(self) -> ApiSpec:
        return ApiSpec({**self.reads, NOTIFY: set(NOTIFY_FIELDS)})

    def describe(self) -> str:
        return "\n".join(
            f"- {op}: returns records with fields {', '.join(sorted(f))}"
            for op, f in sorted(self.reads.items())
        )


def validate(contract: Contract, spec: ApiSpec) -> list[str]:
    """Everything wrong with a contract, in plain words. Empty means usable."""
    spec = spec.with_notify()
    errors: list[str] = []
    facts = {f.key for f in contract.facts}
    for f in contract.facts:
        if not _KEY.match(f.key):
            errors.append(f"fact key {f.key!r} must be lower_snake_case")
    if len(facts) != len(contract.facts):
        errors.append("a fact key is declared twice")

    def check_refs(where: str, operation: str, refs: list[FieldRef]) -> None:
        returned = spec.reads.get(operation)
        if returned is None:
            errors.append(f"{where}: {operation!r} is not a read operation of the API")
            return
        for ref in refs:
            if ref.field not in returned:
                errors.append(
                    f"{where}: {operation} returns no field {ref.field!r} "
                    f"(it returns {', '.join(sorted(returned))})"
                )
            if ref.fact not in facts:
                errors.append(f"{where}: fact:{ref.fact} is not a declared fact")

    seen: set[str] = set()
    for d in contract.deliverables:
        where = f"deliverable {d.id!r}"
        if d.id in seen:
            errors.append(f"deliverable id {d.id!r} is used twice")
        seen.add(d.id)
        if d.kind == "send":
            if d.lookup_operation != NOTIFY:
                errors.append(f"{where}: a send must use lookup_operation 'notify'")
            if not d.must_contain:
                errors.append(f"{where}: a send needs must_contain (facts the message names)")
            for key in d.must_contain:
                if key not in facts:
                    errors.append(f"{where}: fact:{key} is not a declared fact")
            continue
        if d.lookup_operation == NOTIFY:
            errors.append(f"{where}: only a send may use the notify lookup")
            continue
        if not d.key:
            errors.append(f"{where}: a {d.kind} needs a key (the fields that identify it)")
        elif not any(contract.fact_type(r.fact) in ("id", "email") for r in d.key):
            errors.append(
                f"{where}: the key must include a value that identifies one record, such as "
                "a reference number (a fact of type id) or an email address, not only names"
            )
        if d.kind == "update" and not d.values:
            errors.append(f"{where}: an update needs values (what it changes)")
        check_refs(where, d.lookup_operation, [*d.key, *d.values])
    if sum(d.kind == "send" for d in contract.deliverables) > 1:
        errors.append("only one message to the requester per run (one send deliverable)")

    for i, p in enumerate(contract.protected, 1):
        where = f"protected entry {i}"
        if not p.match:
            errors.append(f"{where}: needs match (the fields that pick out one record)")
        check_refs(where, p.lookup_operation, p.match)
        returned = spec.reads.get(p.lookup_operation, set())
        errors.extend(
            f"{where}: {p.lookup_operation} returns no field {f!r}"
            for f in p.fields
            if returned and f not in returned
        )
    for c in contract.extra_checks:
        if c.type == "judgement":
            errors.append("judgement checks are not supported until Phase 5 (D37)")
        elif c.type not in EXTRA_CHECKS:
            errors.append(
                f"extra check type {c.type!r} is not allowed; only {', '.join(EXTRA_CHECKS)}"
            )
        elif c.type == "field_unchanged":
            if not c.match:
                errors.append("extra check field_unchanged: needs match (one record)")
            check_refs("extra check field_unchanged", c.lookup_operation, c.match)
    return errors


@dataclass(frozen=True)
class Check:
    """One typed check the verifier can decide without a model."""

    type: str  # record_unique, field_equals, message_sent, field_unchanged, no_message_sent
    lookup_operation: str
    match: dict[str, str]  # field -> fact key
    field: str | None = None
    fact: str | None = None  # expected value, for field_equals
    fields: tuple[str, ...] = ()
    must_contain: tuple[str, ...] = ()
    deliverable: str | None = None

    def describe(self) -> str:
        target = ", ".join(f"{k}=fact:{v}" for k, v in self.match.items())
        if self.type == "record_unique":
            return f"exactly one {self.lookup_operation} record with {target}"
        if self.type == "field_equals":
            return f"{self.field} equals fact:{self.fact} on the record with {target}"
        if self.type == "message_sent":
            return f"one message to the requester naming {', '.join(self.must_contain)}"
        if self.type == "field_unchanged":
            return f"{', '.join(self.fields)} unchanged on the record with {target}"
        return self.type.replace("_", " ")


def derive_checks(contract: Contract) -> list[Check]:
    """The checks a contract implies (D38). The model never writes these."""
    checks: list[Check] = []
    for d in contract.deliverables:
        match = {r.field: r.fact for r in d.key}
        if d.kind == "send":
            checks.append(
                Check(
                    "message_sent", NOTIFY, {}, must_contain=tuple(d.must_contain), deliverable=d.id
                )
            )
            continue
        if d.kind == "create":
            checks.append(Check("record_unique", d.lookup_operation, match, deliverable=d.id))
        checks.extend(
            Check(
                "field_equals",
                d.lookup_operation,
                match,
                field=v.field,
                fact=v.fact,
                deliverable=d.id,
            )
            for v in d.values
            if v.field not in match  # a key field is already checked by finding the record
        )
    for p in contract.protected:
        match = {r.field: r.fact for r in p.match}
        checks.append(Check("field_unchanged", p.lookup_operation, match, fields=tuple(p.fields)))
    for c in contract.extra_checks:
        match = {r.field: r.fact for r in c.match}
        if c.type == "field_unchanged":
            checks.append(Check(c.type, c.lookup_operation, match, fields=tuple(c.fields)))
        elif c.type == "no_message_sent":
            checks.append(Check(c.type, NOTIFY, {}))
        else:  # never valid; kept so the verifier can say it was not evaluated (D37)
            checks.append(Check(c.type, c.lookup_operation, match))
    return checks
