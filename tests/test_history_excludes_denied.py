"""Regression test for PLAN.md finding A4: structuring caps must never count
denied attempts. A denied action produces no outcome row, so
SQLiteHistoryQuery.sum_amount_cents must reflect only executed payments.
"""
from __future__ import annotations

from datetime import timedelta

import db
from guardian.history import SQLiteHistoryQuery
from schemas import (
    Action,
    ActionEnvelope,
    ActionType,
    Decision,
    DecisionStatus,
    Outcome,
    PaymentParams,
)


def test_denied_action_never_counted_in_sum():
    conn = db.init_db(":memory:")

    # Action 1: a large payment that gets DENIED. No outcome row -- denied
    # actions never execute.
    denied_action = Action(
        session_id="sess-1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="shadowco",
        params=PaymentParams(counterparty="shadowco", amount_cents=999_999_00),
    )
    denied_envelope = ActionEnvelope(
        action=denied_action,
        reasoning="pay shadowco",
        model="claude-haiku-4-5-20251001",
        raw_response="{}",
    )
    db.insert_action(conn, denied_envelope)
    db.insert_decision(
        conn,
        Decision(
            action_id=denied_action.id,
            status=DecisionStatus.DENY,
            matched_rules=["FIN-003"],
            rule_id="FIN-003",
            policy_version="deadbeef",
            reasoning="Unknown counterparties are denied",
            decided_by="policy",
            payload_hash=denied_action.payload_hash(),
        ),
    )
    # Deliberately no outcome insert for the denied action.

    # Action 2: a real payment that gets ALLOWed and executed.
    allowed_action = Action(
        session_id="sess-1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=49900),
    )
    allowed_envelope = ActionEnvelope(
        action=allowed_action,
        reasoning="pay acme-corp",
        model="claude-haiku-4-5-20251001",
        raw_response="{}",
    )
    db.insert_action(conn, allowed_envelope)
    db.insert_decision(
        conn,
        Decision(
            action_id=allowed_action.id,
            status=DecisionStatus.ALLOW,
            matched_rules=[],
            rule_id=None,
            policy_version="deadbeef",
            reasoning="no rule matched, default allow",
            decided_by="policy",
            payload_hash=allowed_action.payload_hash(),
        ),
    )
    db.insert_outcome(
        conn,
        Outcome(
            action_id=allowed_action.id,
            requesting_agent="finance-agent",
            action_type=ActionType.MAKE_PAYMENT,
            status="success",
            detail="simulated payment of 49900 cents to acme-corp",
        ),
    )

    history = SQLiteHistoryQuery(conn)
    total = history.sum_amount_cents(
        agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        window=timedelta(hours=24),
    )

    # Only the executed $499.00 payment counts, not the denied ~$10,000,000 one.
    assert total == 49900


def test_sum_is_zero_when_no_outcomes_exist():
    conn = db.init_db(":memory:")
    history = SQLiteHistoryQuery(conn)
    total = history.sum_amount_cents(
        agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        window=timedelta(hours=24),
    )
    assert total == 0
