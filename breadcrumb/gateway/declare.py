"""Turn a commit action into a declaration the gateway can act on (D38).

The worker only names the contract deliverable it is producing. Everything else
comes from the contract and the ledger: the natural key, the values, the lookup.
Before anything is journaled, this module refuses a commit that is outside the
contract, uses a fact that is missing or in conflict, or carries a money amount,
date or email address that no recorded fact supports (provenance). It also sets
the risk tier: 2 for a large amount or an outside recipient, 1 otherwise.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from breadcrumb.contract.model import Contract, Deliverable
from breadcrumb.executor.state import Fact
from breadcrumb.ledger.values import kind_of, number, same_value


class GatewayRefusal(Exception):
    """The commit cannot be made safely as asked; shown to the model."""


@dataclass(frozen=True)
class Policy:
    approval_threshold: float  # money above this needs the requester's approval
    internal_domain: str  # email recipients outside it need approval


@dataclass(frozen=True)
class Declaration:
    action: str
    deliverable: str
    kind: str  # create, update or send
    description: str
    key: dict[str, Any]
    values: dict[str, Any]
    lookup: dict[str, Any]
    idempotency_key: str
    tier: int = 1
    tier_reason: str = ""
    typed: tuple[tuple[str, str], ...] = ()  # what was typed or sent, for the diff
    facts_used: tuple[str, ...] = field(default=())

    @property
    def diff(self) -> dict[str, Any]:
        """The exact change an approver is shown and approves."""
        return {
            "deliverable": self.deliverable,
            "description": self.description,
            "reason": self.tier_reason,
            "key": self.key,
            "values": self.values,
            "typed": [list(t) for t in self.typed],
        }


def idempotency_key(*parts: object) -> str:
    text = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return "bc-" + hashlib.sha256(text.encode()).hexdigest()[:20]


def _resolve(d: Deliverable, facts: dict[str, Fact], contract: Contract) -> dict[str, Any]:
    """Fact values for the deliverable's key and values; refuses if any is unusable."""
    wanted = [r.fact for r in [*d.key, *d.values]] + list(d.must_contain)
    missing = [k for k in dict.fromkeys(wanted) if k not in facts]
    if missing:
        described = {f.key: f.description for f in contract.facts}
        shown = "; ".join(f"{k} ({described.get(k, '')})" for k in missing)
        raise GatewayRefusal(
            f"not done: remember these facts first, under exactly these keys, with "
            f"where you read them: {shown}"
        )
    conflicted = [facts[k] for k in dict.fromkeys(wanted) if facts[k].conflict]
    if conflicted:
        shown = "; ".join(f"{f.key} is {f.value!r} but was {f.conflict}" for f in conflicted)
        raise GatewayRefusal(
            f"not done: facts in conflict ({shown}). Check the source and remember the "
            "correct value again."
        )
    return {k: facts[k].value for k in wanted}


def check_provenance(typed: list[tuple[str, str]], facts: dict[str, Fact]) -> None:
    """Every money amount, date or email address written must equal a recorded fact."""
    unsupported = []
    for label, value in typed:
        kind = kind_of(value)
        if kind == "text":
            continue
        if not any(same_value(value, f.value) for f in facts.values()):
            unsupported.append(f"{label} = {value!r} ({kind})")
    if unsupported:
        raise GatewayRefusal(
            "not done: these values do not match any fact you recorded: "
            + "; ".join(unsupported)
            + ". Copy values exactly from remembered facts, or remember the value with "
            "its source first."
        )


def _tier(
    values: list[Any], typed: list[tuple[str, str]], kinds: dict[str, str], policy: Policy
) -> tuple[int, str]:
    for value, kind in [
        *((v, kinds.get(str(v), "")) for v in values),
        *((v, "") for _, v in typed),
    ]:
        amount = number(value)
        is_money = kind == "money" or kind_of(value) == "money"
        if is_money and amount is not None and amount > policy.approval_threshold:
            return (
                2,
                f"amount {value} is above the approval limit of {policy.approval_threshold:,.0f}",
            )
    for _, value in typed:
        if kind_of(value) == "email" and not str(value).lower().endswith(
            "@" + policy.internal_domain.lower()
        ):
            return 2, f"{value} is outside @{policy.internal_domain}"
    return 1, ""


def declare(
    action: str,
    args: dict[str, Any],
    *,
    run_id: str,
    reference: str,
    channel: str,
    contract: Contract,
    facts: dict[str, Fact],
    typed: list[tuple[str, str]],
    policy: Policy,
) -> Declaration:
    """Read what a commit action asks for, against the contract. Raises GatewayRefusal."""
    if action == "notify":
        sends = [d for d in contract.deliverables if d.kind == "send"]
        if not sends:
            raise GatewayRefusal(
                "not done: the task did not ask for a message to the requester. If you "
                "need the user, use ask_user."
            )
        d = sends[0]
        resolved = _resolve(d, facts, contract)
        message = str(args.get("message", ""))
        absent = [k for k in d.must_contain if str(resolved[k]).lower() not in message.lower()]
        if absent:
            shown = ", ".join(f"{k} ({resolved[k]})" for k in absent)
            raise GatewayRefusal(f"not done: the message must mention {shown}")
        key = {"channel": channel, "ref": reference}
        return Declaration(
            action, d.id, "send", " ".join(message.split())[:160], key, {},
            {"source": "notify"}, idempotency_key(run_id, "send", "notify", key),
        )  # fmt: skip

    deliverable_id = str(args.get("deliverable") or "").strip()
    target = contract.deliverable(deliverable_id)
    if target is None or target.kind == "send":
        names = ", ".join(x.id for x in contract.deliverables if x.kind != "send") or "none"
        raise GatewayRefusal(
            f"not done: {deliverable_id or 'no deliverable'!s} is outside the contract "
            f"(deliverables you may produce: {names}). If this change is really needed, "
            "ask the user with ask_user; do not make it."
        )
    d = target
    resolved = _resolve(d, facts, contract)
    check_provenance(typed, facts)
    key = {r.field: resolved[r.fact] for r in d.key}
    values = {r.field: resolved[r.fact] for r in d.values}
    kinds = {str(resolved[r.fact]): contract.fact_type(r.fact) or "" for r in d.values}
    tier, reason = _tier(list(values.values()), typed, kinds, policy)
    identity: tuple[object, ...] = (run_id, d.kind, d.lookup_operation, key)
    if d.kind == "update":
        identity = (*identity, values)
    return Declaration(
        action=action,
        deliverable=d.id,
        kind=d.kind,
        description=str(args.get("description") or d.description).strip(),
        key=key,
        values=values,
        lookup={"source": "api", "operation": d.lookup_operation, "params": {}},
        idempotency_key=idempotency_key(*identity),
        tier=tier,
        tier_reason=reason,
        typed=tuple(typed),
        facts_used=tuple(r.fact for r in [*d.key, *d.values]),
    )
