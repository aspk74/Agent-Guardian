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
from typing import Literal, Union

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    SEND_EMAIL = "send_email"
    MAKE_PAYMENT = "make_payment"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"


# --- typed params, discriminated union. no untyped dict on the policy boundary ---

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


Params = Union[PaymentParams, EmailParams, FileParams]


class Action(BaseModel):
    """What the policy engine sees. Every field is typed. No prose."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    requesting_agent: str
    action_type: ActionType
    target: str
    params: Params = Field(discriminator="kind")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
    action_type: ActionType
    status: Literal["success", "failed"]
    detail: str
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
