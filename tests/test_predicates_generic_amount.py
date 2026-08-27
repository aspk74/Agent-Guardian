"""guardian.predicates.rule_matches's `amount_cents_gt` condition must read
amount_cents structurally, not assume PaymentParams.

Companion to tests/test_history_generic_amount.py: that file covers the
cumulative predicate (`sum_amount_cents_gt`, in guardian/history.py), this one
covers the single-value predicate (`amount_cents_gt`, in
guardian/predicates.py). Both raise the same AmountlessActionType so a
policy-authoring bug on either fails closed the same way.
"""
from __future__ import annotations

import pytest

from guardian import predicates
from guardian.history import AmountlessActionType
from schemas import Action, ActionType, EmailParams, PaymentParams


class FakeHistory:
    def sum_amount_cents(self, *, agent, action_type, window):
        return 0

    def count(self, *, agent, action_type, window):
        return 0

    def distinct_targets(self, *, agent, action_type, window):
        return 0


HISTORY = FakeHistory()


def test_amount_cents_gt_still_matches_paymentparams():
    """Regression guard: the existing PaymentParams path must keep working
    exactly as before -- this generalization must not change payment
    behaviour, only extend what else it accepts."""
    action = Action(
        session_id="s1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=75_000),
    )
    rule = {
        "id": "TEST-001",
        "when": {"action_type": "make_payment", "amount_cents_gt": 50_000},
        "then": "escalate",
    }
    assert predicates.rule_matches(action, HISTORY, rule) is True

    rule_under = {**rule, "when": {**rule["when"], "amount_cents_gt": 100_000}}
    assert predicates.rule_matches(action, HISTORY, rule_under) is False


def test_amount_cents_gt_raises_for_action_type_with_no_amount():
    """A rule declaring amount_cents_gt on an action type whose params carry
    no amount_cents field is a policy-authoring bug -- it must raise
    AmountlessActionType, which guardian/policy_agent.py's ONE catch-all then
    turns into a fail-closed SYS-ERR deny. It must never silently pass or
    silently fail to match."""
    action = Action(
        session_id="s1",
        requesting_agent="email-agent",
        action_type=ActionType.SEND_EMAIL,
        target="someone@external.example.com",
        params=EmailParams(
            recipient="someone@external.example.com",
            subject_ref="tpl-1",
            body_ref="body-1",
        ),
    )
    rule = {
        "id": "TEST-002",
        "when": {"action_type": "send_email", "amount_cents_gt": 1000},
        "then": "escalate",
    }
    with pytest.raises(AmountlessActionType):
        predicates.rule_matches(action, HISTORY, rule)


def test_amount_cents_gt_end_to_end_fails_closed_through_evaluate():
    """Same shape as test_history_generic_amount.py's end-to-end case, but for
    the single-value predicate: a bad rule must surface as DENY/SYS-ERR, never
    crash evaluate() and never silently allow."""
    import tempfile
    from pathlib import Path

    import guardian.policy_agent as policy_agent
    from schemas import DecisionStatus

    action = Action(
        session_id="s1",
        requesting_agent="email-agent",
        action_type=ActionType.SEND_EMAIL,
        target="someone@external.example.com",
        params=EmailParams(
            recipient="someone@external.example.com",
            subject_ref="tpl-1",
            body_ref="body-1",
        ),
    )

    policy = """
version: 1
rules:
  - id: BAD-002
    description: single-value amount cap on an action type that has no amount
    when: {action_type: send_email, amount_cents_gt: 1000}
    then: allow
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.yaml"
        path.write_text(policy)
        decision = policy_agent.evaluate(action, HISTORY, policy_path=str(path))

    assert decision.status is DecisionStatus.DENY
    assert decision.rule_id == "SYS-ERR"
    assert "amount_cents" in decision.reasoning
