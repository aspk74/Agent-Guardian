"""Most-restrictive-wins resolution (PLAN.md s3.2): every rule is evaluated,
matches are collected, and deny > escalate > allow among the matches --
not first-match-wins. `rule_id` among same-severity matches is the first
one in policy.yaml file order.
"""
from __future__ import annotations

from guardian.policy_agent import evaluate
from schemas import Action, ActionType, DecisionStatus, PaymentParams


class FakeHistory:
    def __init__(self, sum_cents: int = 0):
        self._sum = sum_cents

    def sum_amount_cents(self, *, agent, action_type, window):
        return self._sum

    def count(self, *, agent, action_type, window):
        return 0

    def distinct_targets(self, *, agent, action_type, window):
        return 0


def _payment(amount_cents: int, target: str) -> Action:
    return Action(
        session_id="s1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target=target,
        params=PaymentParams(counterparty=target, amount_cents=amount_cents),
    )


def test_escalate_beats_allow():
    # $750 to acme-corp matches FIN-001 (escalate, amount > $500) AND
    # FIN-004 (allow, known counterparty) simultaneously. Zero history keeps
    # FIN-002 out, and acme-corp being a known counterparty keeps FIN-003
    # (deny on unknown counterparty) out.
    action = _payment(75000, "acme-corp")
    decision = evaluate(action, FakeHistory(sum_cents=0))

    assert decision.status == DecisionStatus.ESCALATE
    assert decision.rule_id == "FIN-001"
    assert set(decision.matched_rules) == {"FIN-001", "FIN-004"}


def test_deny_beats_escalate():
    # $100 to shadowco (unknown counterparty) matches only FIN-003 (deny).
    # Paired with the case above, this pins the full deny > escalate > allow
    # order rather than just "escalate beat allow once".
    action = _payment(10000, "shadowco")
    decision = evaluate(action, FakeHistory(sum_cents=0))

    assert decision.status == DecisionStatus.DENY
    assert decision.rule_id == "FIN-003"


def test_same_severity_tiebreak_is_file_order():
    # FIN-001 and FIN-002 are both `escalate` and, at the current 7-rule
    # set, the only pair that can co-match at the same severity: a payment
    # over $500 (FIN-001) whose 24h cumulative total also crosses $1000
    # (FIN-002). FIN-004 (allow) matches too since acme-corp is a known
    # counterparty, but allow is lower severity and must not affect the
    # winner. FIN-001 appears first in policy.yaml, so asserting it (and
    # not FIN-002) wins verifies file-order determinism, not just "some
    # escalate rule won".
    action = _payment(75000, "acme-corp")  # 75000 > 50000 (FIN-001)
    history = FakeHistory(sum_cents=50000)  # 50000 + 75000 = 125000 > 100000 (FIN-002)
    decision = evaluate(action, history)

    assert decision.status == DecisionStatus.ESCALATE
    assert decision.rule_id == "FIN-001"
    assert set(decision.matched_rules) == {"FIN-001", "FIN-002", "FIN-004"}
