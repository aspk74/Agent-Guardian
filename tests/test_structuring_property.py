"""Property test (PLAN.md T3): across any sequence of payments, the
cumulative EXECUTED total never crosses FIN-002's cap without the crossing
payment itself having matched (been escalated, and therefore never folded
into the executed total).
"""
from __future__ import annotations

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from guardian import predicates
from schemas import Action, ActionType, PaymentParams


def _rule(rule_id: str) -> dict:
    with open("policy.yaml") as f:
        rules = yaml.safe_load(f)["rules"]
    return next(r for r in rules if r["id"] == rule_id)


FIN_002 = _rule("FIN-002")
CAP_CENTS = FIN_002["when"]["sum_amount_cents_gt"]


class AccumulatingHistory:
    """Tracks only EXECUTED totals, mirroring PLAN.md s3.1: an escalated
    (not-yet-executed) payment must never be folded into the running sum."""

    def __init__(self):
        self.executed_total = 0

    def sum_amount_cents(self, *, agent, action_type, window):
        return self.executed_total

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


@given(amounts=st.lists(st.integers(min_value=100, max_value=100_000), min_size=1, max_size=20))
@settings(max_examples=200)
def test_cumulative_executed_total_never_exceeds_cap_without_escalation(amounts):
    history = AccumulatingHistory()
    for amount_cents in amounts:
        candidate = _payment(amount_cents)
        would_escalate = predicates.rule_matches(candidate, history, FIN_002)

        if would_escalate:
            # Flagged before execution -- must not be folded into the total.
            continue

        history.executed_total += amount_cents
        assert history.executed_total <= CAP_CENTS
