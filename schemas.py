"""Shared types for the Guardian Agent System.

The split between Action and ActionEnvelope is the security boundary:
policy.evaluate() takes Action only, so it is structurally incapable of
reading LLM-generated prose (see PLAN.md section 2.1, finding C3).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, SerializeAsAny, model_validator


class ActionType:
    """Well-known action types this repo ships with, as plain strings --
    dotted access (`ActionType.MAKE_PAYMENT`) for ergonomics and backward
    compatibility with when this was a closed `Enum`.

    This is NOT the authority on which action_type strings are usable in a
    given process -- guardian/registry.py's registry is (design doc
    2026-08-24 s2, s6: ActionType/Params became an open registry rather than
    a closed set, so a caller can register any string action_type with its
    own Params model). `Action.action_type` and `Outcome.action_type` are
    typed as plain `str` for exactly that reason: any registered string is
    valid, not only these five.
    """
    SEND_EMAIL = "send_email"
    MAKE_PAYMENT = "make_payment"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"


class UnregisteredActionType(Exception):
    """Reconstructing an Action/ActionEnvelope from stored JSON referenced an
    action_type with no registered Params model in THIS process.

    Raised from Action._resolve_params_class below, at the point of
    deserializing FROM a raw dict/JSON -- never from the normal construction
    path (a WorkerAgent/mode-A adapter passing an already-validated Params
    instance directly), since only a raw, not-yet-typed params dict triggers
    the registry lookup at all. Callers reconstructing historical or stored
    data (db.py's audit reads, guardian/escalation.py's parked-escalation
    reads) can catch this and degrade gracefully -- skip one unreconstructable
    row rather than let it crash a listing of many -- instead of a raw,
    uninformative ValueError from Python's own Enum machinery, which is what
    happened here before ActionType stopped being a closed Enum.
    """


# --- typed params. no untyped dict on the policy boundary. ---
# Params is registered per-action-type in guardian/registry.py, not enumerated
# here as a closed Union -- these three ship as the built-ins, registered by
# registry.py on import, exactly the way a customer's own Params model for a
# custom action_type would be. Nothing here is special-cased for them.

class PaymentParams(BaseModel):
    kind: Literal["payment"] = "payment"
    counterparty: str
    amount_cents: int  # integer cents. never float, never Decimal (PLAN C1).
    memo_ref: str | None = None  # opaque id, not free text


class EmailParams(BaseModel):
    kind: Literal["email"] = "email"
    recipient: str
    subject_ref: str  # template id, not the rendered subject
    body_ref: str


class FileParams(BaseModel):
    kind: Literal["file"] = "file"
    path: str


# Kept as a documentation-only alias of the built-in three -- nothing in this
# file types a field against it any more (see Action.params below), but it's
# a convenient closed-set reference for code that specifically wants "one of
# the types this repo ships with," e.g. a test enumerating the built-ins.
Params = Union[PaymentParams, EmailParams, FileParams]


class Action(BaseModel):
    """What the policy engine sees. Every field is typed. No prose."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    requesting_agent: str
    action_type: str
    target: str
    # SerializeAsAny: params holds a concrete Params subclass (PaymentParams,
    # a customer's own RefundParams, etc.), and without SerializeAsAny,
    # Pydantic v2 would serialize a field typed as the bare BaseModel using
    # ONLY BaseModel's own (zero) declared fields, silently dropping every
    # subclass field on model_dump()/payload_hash() -- SerializeAsAny tells
    # Pydantic to serialize using the value's actual runtime type instead.
    params: SerializeAsAny[BaseModel]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def _resolve_params_class(cls, data: Any) -> Any:
        """Only fires when reconstructing from a raw dict (JSON deserialize)
        with `params` still a plain dict rather than an already-validated
        Params instance -- the normal construction path (agents/base.py's
        `self.params_model(**kwargs)`, or a mode-A adapter doing the same)
        passes an already-typed instance straight through untouched, since
        `isinstance(params, dict)` is False for it.

        This is a deferred import, not a module-level one: guardian/registry.py
        imports schemas.py at ITS top level (to get ActionType/PaymentParams/
        etc.), so importing it back here at schemas.py's own top level would
        be circular. By the time any Action is actually constructed, both
        modules are already fully loaded, so the import inside this method
        body is safe -- a standard way to break an import cycle without
        merging the two modules.
        """
        if not isinstance(data, dict):
            return data
        params = data.get("params")
        if not isinstance(params, dict):
            return data
        action_type = data.get("action_type")
        if action_type is None:
            return data

        import guardian.registry as registry

        try:
            reg = registry.get(action_type)
        except registry.UnregisteredActionType as exc:
            raise UnregisteredActionType(
                f"action_type {action_type!r} has no registered params model in "
                f"this process (was it renamed, or registered by a different "
                f"process?)"
            ) from exc

        data = dict(data)
        data["params"] = reg.params_model.model_validate(params)
        return data

    def payload_hash(self) -> str:
        """Canonical sha256 over everything except created_at, so re-hashing
        an approved action at execute time detects tampering (PayloadMismatchError)."""
        payload = self.model_dump(mode="json", exclude={"created_at"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class ActionEnvelope(BaseModel):
    """What the auditor sees. Carries the quarantined LLM output.
    Never pass this to policy.evaluate() -- pass envelope.action."""

    action: Action
    reasoning: str  # attacker-reachable LLM prose. audit + display only.
    model: str
    raw_response: str  # for debugging malformed JSON from the LLM


class DecisionStatus(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


class Decision(BaseModel):
    action_id: str
    status: DecisionStatus
    matched_rules: list[str] = Field(default_factory=list)  # all rules that matched
    rule_id: str | None  # the one that determined status (None only for SYS-* paths that skip rule matching)
    policy_version: str  # sha256 of policy.yaml at decision time
    reasoning: str  # rendered from the rule, not from the LLM
    decided_by: Literal["policy", "human", "system"]
    payload_hash: str  # must equal Action.payload_hash() at execute time
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Outcome(BaseModel):
    """Result of guardian.executors.run(). Denied/escalated actions never
    produce an Outcome -- this is what makes 'sum executed outcomes only'
    (PLAN A4) automatic rather than a filter someone has to remember."""

    action_id: str
    requesting_agent: str
    action_type: str
    status: Literal["success", "failed"]
    detail: str
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
