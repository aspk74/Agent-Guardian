"""Policy engine (PLAN.md s3): evaluate(action, history) -> Decision.

Pure function of its two arguments. No DB handle, no network, no clock
beyond what `history` exposes. Resolution is most-restrictive-wins, not
first-match-wins (s3.2): every rule is evaluated, matches are collected,
and deny > escalate > allow among the matches.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Protocol

import yaml

from guardian import predicates
from schemas import Action, ActionType, Decision, DecisionStatus


class HistoryQuery(Protocol):
    """Counts EXECUTED OUTCOMES ONLY (PLAN.md s3.1). Proposed-and-denied
    actions never count toward a cumulative cap, or a rejected action would
    consume the victim's own limit."""

    def sum_amount_cents(self, *, agent: str, action_type: ActionType, window: timedelta) -> int: ...
    def count(self, *, agent: str, action_type: ActionType, window: timedelta) -> int: ...
    def distinct_targets(self, *, agent: str, action_type: ActionType, window: timedelta) -> int: ...


_SEVERITY_RANK = {
    DecisionStatus.DENY: 0,
    DecisionStatus.ESCALATE: 1,
    DecisionStatus.ALLOW: 2,
}

_THEN_TO_STATUS = {
    "deny": DecisionStatus.DENY,
    "escalate": DecisionStatus.ESCALATE,
    "allow": DecisionStatus.ALLOW,
}


def policy_version(policy_path: str = "policy.yaml") -> str:
    """sha256 of policy.yaml's current on-disk contents. evaluate() already
    recomputes this on every call (no cache to invalidate), so this is the
    read-only counterpart for callers -- the Phase 3 dashboard/CLI -- that
    just want to confirm what version is currently live, without evaluating
    an action."""
    with open(policy_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def evaluate(action: Action, history: HistoryQuery, policy_path: str = "policy.yaml") -> Decision:
    # policy.yaml missing/malformed is a startup-time failure (PLAN.md s3.3:
    # "refuse to start"). Let the exception propagate uncaught -- this is
    # NOT a predicate error and must not be swallowed into a Decision.
    with open(policy_path, "rb") as f:
        raw = f.read()
    policy_version = hashlib.sha256(raw).hexdigest()
    rules = yaml.safe_load(raw)["rules"]

    # The ONE deliberate broad catch: a predicate is arbitrary user-authored
    # logic against a YAML condition, so any failure here is a policy-author
    # bug, not a category of exception enumerable in advance. This also
    # covers severity resolution below (e.g. a typo'd `then:` value) -- any
    # error while interpreting a matched rule is equally a policy-author bug
    # and must fail closed the same way, not crash evaluate() outright.
    try:
        matched = [rule for rule in rules if predicates.rule_matches(action, history, rule)]
        if not matched:
            return Decision(
                action_id=action.id,
                status=DecisionStatus.ESCALATE,
                matched_rules=[],
                rule_id="SYS-GAP",
                policy_version=policy_version,
                reasoning="no policy rule covers this action",
                decided_by="system",
                payload_hash=action.payload_hash(),
            )

        matched_rules = [rule["id"] for rule in matched]
        winning_severity = min(_SEVERITY_RANK[_THEN_TO_STATUS[rule["then"]]] for rule in matched)
        winning_rule = next(
            rule for rule in matched
            if _SEVERITY_RANK[_THEN_TO_STATUS[rule["then"]]] == winning_severity
        )
    except Exception as exc:
        return Decision(
            action_id=action.id,
            status=DecisionStatus.DENY,
            matched_rules=[],
            rule_id="SYS-ERR",
            policy_version=policy_version,
            reasoning=f"policy predicate error: {exc}",
            decided_by="system",
            payload_hash=action.payload_hash(),
        )

    return Decision(
        action_id=action.id,
        status=_THEN_TO_STATUS[winning_rule["then"]],
        matched_rules=matched_rules,
        rule_id=winning_rule["id"],
        policy_version=policy_version,
        reasoning=winning_rule["description"],
        decided_by="policy",
        payload_hash=action.payload_hash(),
    )
