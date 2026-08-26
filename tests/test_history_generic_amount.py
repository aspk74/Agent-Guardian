"""SQLiteHistoryQuery.sum_amount_cents must read `amount_cents` structurally,
not through one concrete params class.

Binding it to PaymentParams meant cumulative caps silently only ever worked for
payments: any other amount-bearing action type raised ValidationError inside
policy_agent.evaluate()'s catch and became a permanent SYS-ERR deny. That is
the failure this file pins down, plus the fail-closed behaviour that must
survive for a type genuinely carrying no amount at all.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import db
from guardian.history import AmountlessActionType, SQLiteHistoryQuery
from schemas import (
    Action,
    ActionEnvelope,
    ActionType,
    EmailParams,
    Outcome,
    PaymentParams,
)


def _record_executed(conn, action: Action) -> None:
    """Insert an action plus the outcome that marks it executed. Only the
    outcome makes it visible to HistoryQuery (PLAN.md A4)."""
    conn_envelope = ActionEnvelope(
        action=action,
        reasoning="test fixture",
        model="claude-haiku-4-5-20251001",
        raw_response="{}",
    )
    db.insert_action(conn, conn_envelope)
    db.insert_outcome(
        conn,
        Outcome(
            action_id=action.id,
            requesting_agent=action.requesting_agent,
            action_type=action.action_type,
            status="success",
            detail="executed (test)",
        ),
    )


def test_sum_reads_amount_cents_without_binding_to_paymentparams():
    """The existing payment path must keep working -- this is the regression
    guard on the structural read, not a new capability."""
    conn = db.init_db(":memory:")
    for cents in (10_000, 25_000):
        _record_executed(
            conn,
            Action(
                session_id="sess-1",
                requesting_agent="finance-agent",
                action_type=ActionType.MAKE_PAYMENT,
                target="acme-corp",
                params=PaymentParams(counterparty="acme-corp", amount_cents=cents),
            ),
        )

    history = SQLiteHistoryQuery(conn)
    total = history.sum_amount_cents(
        agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        window=timedelta(hours=24),
    )
    assert total == 35_000


def test_amountless_action_type_raises_rather_than_summing_to_zero():
    """A cumulative amount cap on an action type with no amount_cents is a
    policy-authoring error. It must raise so evaluate()'s catch turns it into a
    fail-closed SYS-ERR deny.

    Returning 0 instead would be strictly worse than raising: a cumulative cap
    that can never be reached is a cap that does not exist, and it would fail
    open in exactly the direction PLAN.md s9.3 structuring exists to prevent.
    """
    conn = db.init_db(":memory:")
    _record_executed(
        conn,
        Action(
            session_id="sess-1",
            requesting_agent="email-agent",
            action_type=ActionType.SEND_EMAIL,
            target="someone@external.example.com",
            params=EmailParams(
                recipient="someone@external.example.com",
                subject_ref="tpl-1",
                body_ref="body-1",
            ),
        ),
    )

    history = SQLiteHistoryQuery(conn)
    with pytest.raises(AmountlessActionType):
        history.sum_amount_cents(
            agent="email-agent",
            action_type=ActionType.SEND_EMAIL,
            window=timedelta(hours=24),
        )


def test_amountless_error_is_fail_closed_through_evaluate():
    """End-to-end: the raise above must surface as a DENY, never crash
    evaluate() and never pass as an allow."""
    import guardian.policy_agent as policy_agent
    from schemas import DecisionStatus

    conn = db.init_db(":memory:")
    _record_executed(
        conn,
        Action(
            session_id="sess-1",
            requesting_agent="email-agent",
            action_type=ActionType.SEND_EMAIL,
            target="someone@external.example.com",
            params=EmailParams(
                recipient="someone@external.example.com",
                subject_ref="tpl-1",
                body_ref="body-1",
            ),
        ),
    )

    candidate = Action(
        session_id="sess-1",
        requesting_agent="email-agent",
        action_type=ActionType.SEND_EMAIL,
        target="another@external.example.com",
        params=EmailParams(
            recipient="another@external.example.com",
            subject_ref="tpl-2",
            body_ref="body-2",
        ),
    )

    # A rule that asks for a cumulative amount cap on an amount-less type.
    policy = """
version: 1
rules:
  - id: BAD-001
    description: cumulative amount cap on an action type that has no amount
    when: {action_type: send_email, window_hours: 24, sum_amount_cents_gt: 1000}
    then: allow
"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.yaml"
        path.write_text(policy)
        decision = policy_agent.evaluate(
            candidate, SQLiteHistoryQuery(conn), policy_path=str(path)
        )

    assert decision.status is DecisionStatus.DENY
    assert decision.rule_id == "SYS-ERR"
    assert "amount_cents" in decision.reasoning
