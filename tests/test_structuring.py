"""Structuring / off-by-one on the crossing payment (PLAN.md s9.3).

The candidate action has NOT executed yet, so FIN-002 must check
history-sum + candidate-amount, not history-sum alone. Checking the sum
alone misjudges which payment in a sequence actually trips the rule.
"""
from __future__ import annotations

from datetime import timedelta

import yaml

from guardian import predicates
from schemas import Action, ActionType, PaymentParams


def _rule(rule_id: str) -> dict:
    with open("policy.yaml") as f:
        rules = yaml.safe_load(f)["rules"]
    return next(r for r in rules if r["id"] == rule_id)


FIN_002 = _rule("FIN-002")


class FakeHistory:
    def __init__(self, sum_cents: int):
        self._sum = sum_cents

    def sum_amount_cents(self, *, agent, action_type, window):
        return self._sum

    def count(self, *, agent, action_type, window):
        return 0

    def distinct_targets(self, *, agent, action_type, window):
        return 0


def _payment(amount_cents: int) -> Action:
    return Action(
        session_id="s1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="globex",
        params=PaymentParams(counterparty="globex", amount_cents=amount_cents),
    )


def test_third_structuring_payment_escalates_off_by_one():
    # Two prior $499 payments have already executed (history sum = $998).
    # A third $499 payment is the untested candidate.
    history = FakeHistory(sum_cents=49900 * 2)
    candidate = _payment(49900)

    # Sanity check documenting the bug: the naive sum-only check ($998)
    # would NOT have crossed $1000, which is exactly the off-by-one that
    # would wrongly allow the crossing payment through.
    naive_total = history.sum_amount_cents(
        agent="finance-agent", action_type=ActionType.MAKE_PAYMENT, window=timedelta(hours=24)
    )
    assert not (naive_total > 100_000)

    # Correct check adds the candidate's own amount: 99800 + 49900 = 149700.
    assert predicates.rule_matches(candidate, history, FIN_002) is True


def test_boundary_exactly_at_cap_does_not_escalate():
    # sum_amount_cents_gt is strictly greater-than: landing exactly on the
    # cap must NOT escalate.
    history = FakeHistory(sum_cents=50_000)
    candidate = _payment(50_000)  # 50000 + 50000 = 100000, exactly the cap
    assert predicates.rule_matches(candidate, history, FIN_002) is False


def test_boundary_one_cent_over_cap_escalates():
    history = FakeHistory(sum_cents=50_000)
    candidate = _payment(50_001)  # 100001, one cent over the cap
    assert predicates.rule_matches(candidate, history, FIN_002) is True
