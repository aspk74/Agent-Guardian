"""Snapshot-style test for guardian/auditor.py's Reporter half (PLAN.md s10,
listed as `test_golden_trail.py`: "snapshot of `report --session demo1`").

Not byte-exact -- the brief only requires pinning the key facts (rule_id,
decided_by, status, running total). Session is hand-built directly through
db.py inserts, no LLM and no policy_agent involved, so this test is only
exercising guardian/auditor.report() and its db.py read helpers.
"""
from __future__ import annotations

from datetime import datetime, timezone

import db
import guardian.auditor as auditor
from schemas import (
    Action,
    ActionEnvelope,
    ActionType,
    Decision,
    DecisionStatus,
    FileParams,
    Outcome,
    PaymentParams,
)

SESSION = "demo1"


def _envelope(action: Action, reasoning: str) -> ActionEnvelope:
    return ActionEnvelope(
        action=action,
        reasoning=reasoning,
        model="gpt-4o-mini",
        raw_response="{}",
    )


def _build_session(conn):
    # --- Action 1: file read, ALLOWed and executed (FILE-002) ---
    a1 = Action(
        session_id=SESSION,
        requesting_agent="file-agent",
        action_type=ActionType.READ_FILE,
        target="workspace/report.md",
        params=FileParams(path="workspace/report.md"),
    )
    e1 = _envelope(a1, "read the quarterly report")
    db.insert_action(conn, e1)
    db.insert_decision(conn, Decision(
        action_id=a1.id, status=DecisionStatus.ALLOW,
        matched_rules=["FILE-002"], rule_id="FILE-002",
        policy_version="deadbeef", reasoning="Reads inside the workspace are routine",
        decided_by="policy", payload_hash=a1.payload_hash(),
    ))
    db.insert_outcome(conn, Outcome(
        action_id=a1.id, requesting_agent="file-agent",
        action_type=ActionType.READ_FILE, status="success",
        detail="simulated read of workspace/report.md",
    ))

    # --- Action 2: payment, DENIED (FIN-003 unknown counterparty) ---
    a2 = Action(
        session_id=SESSION,
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="shadowco",
        params=PaymentParams(counterparty="shadowco", amount_cents=10_000),
    )
    e2 = _envelope(a2, "pay shadowco for services")
    db.insert_action(conn, e2)
    db.insert_decision(conn, Decision(
        action_id=a2.id, status=DecisionStatus.DENY,
        matched_rules=["FIN-003"], rule_id="FIN-003",
        policy_version="deadbeef", reasoning="Unknown counterparties are denied",
        decided_by="policy", payload_hash=a2.payload_hash(),
    ))
    # No outcome -- denied actions never execute.

    # --- Action 3: payment, ESCALATEd (FIN-001) and still pending ---
    a3 = Action(
        session_id=SESSION,
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=75_000),
    )
    e3 = _envelope(a3, "pay acme-corp invoice #422")
    db.insert_action(conn, e3)
    d3 = Decision(
        action_id=a3.id, status=DecisionStatus.ESCALATE,
        matched_rules=["FIN-001"], rule_id="FIN-001",
        policy_version="deadbeef", reasoning="Single payment over $500 needs a human",
        decided_by="policy", payload_hash=a3.payload_hash(),
    )
    db.insert_decision(conn, d3)
    db.insert_escalation(
        conn, action_id=a3.id, session_id=SESSION,
        envelope_json=e3.model_dump_json(), decision_json=d3.model_dump_json(),
        payload_hash=d3.payload_hash,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    # Left pending -- no resolve.

    # --- Action 4: payment, ESCALATEd (FIN-001), approved by human, executed ---
    a4 = Action(
        session_id=SESSION,
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="globex",
        params=PaymentParams(counterparty="globex", amount_cents=60_000),
    )
    e4 = _envelope(a4, "pay globex invoice #99")
    db.insert_action(conn, e4)
    d4 = Decision(
        action_id=a4.id, status=DecisionStatus.ESCALATE,
        matched_rules=["FIN-001"], rule_id="FIN-001",
        policy_version="deadbeef", reasoning="Single payment over $500 needs a human",
        decided_by="policy", payload_hash=a4.payload_hash(),
    )
    db.insert_decision(conn, d4)
    db.insert_escalation(
        conn, action_id=a4.id, session_id=SESSION,
        envelope_json=e4.model_dump_json(), decision_json=d4.model_dump_json(),
        payload_hash=d4.payload_hash,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.resolve_escalation(
        conn, a4.id, status="approved", resolved_by="demo-operator",
        resolved_at=datetime.now(timezone.utc).isoformat(),
    )
    db.insert_outcome(conn, Outcome(
        action_id=a4.id, requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT, status="success",
        detail="simulated payment of 60000 cents to globex",
    ))

    return a1, a2, a3, a4


def test_report_contains_key_facts_per_action():
    conn = db.init_db(":memory:")
    _build_session(conn)

    text = auditor.report(conn, SESSION)

    # allow path
    assert "FILE-002" in text
    assert "allow" in text

    # deny path
    assert "FIN-003" in text
    assert "deny" in text
    assert "denied, never executed" in text

    # escalate, still pending
    assert "pending human approval" in text

    # escalate, resolved (approved) and executed
    assert "resolved_by: demo-operator (approved)" in text
    assert "simulated payment of 60000 cents to globex" in text

    # every recorded decision must show decided_by
    assert "decided_by=policy" in text

    # running total: only the executed payment (globex, $600.00) counts --
    # the escalated-but-pending acme-corp payment and the denied shadowco
    # payment must NOT be in the total.
    assert "$600.00" in text
    assert "$750.00" not in text
    assert "$100.00" not in text


def test_report_chronological_order():
    conn = db.init_db(":memory:")
    a1, a2, a3, a4 = _build_session(conn)

    text = auditor.report(conn, SESSION)

    # Order in the printed trail should match the order actions were created.
    positions = [text.index(a.id) for a in (a1, a2, a3, a4)]
    assert positions == sorted(positions)


def test_report_empty_session_does_not_crash():
    conn = db.init_db(":memory:")
    text = auditor.report(conn, "nonexistent-session")
    assert "no actions recorded" in text


def test_report_includes_informational_flags_label():
    conn = db.init_db(":memory:")
    _build_session(conn)
    text = auditor.report(conn, SESSION)
    assert "informational, not enforced" in text


def test_cross_session_flags_do_not_affect_report_for_single_session():
    # An agent escalated across two DIFFERENT sessions should surface a flag,
    # but this must be purely informational -- it must not alter any
    # Decision already recorded for either session.
    conn = db.init_db(":memory:")
    _build_session(conn)  # session "demo1", finance-agent escalated twice

    # A second session, same agent, another escalation.
    a5 = Action(
        session_id="demo2",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=55_000),
    )
    e5 = _envelope(a5, "pay acme-corp again")
    db.insert_action(conn, e5)
    d5 = Decision(
        action_id=a5.id, status=DecisionStatus.ESCALATE,
        matched_rules=["FIN-001"], rule_id="FIN-001",
        policy_version="deadbeef", reasoning="Single payment over $500 needs a human",
        decided_by="policy", payload_hash=a5.payload_hash(),
    )
    db.insert_decision(conn, d5)
    db.insert_escalation(
        conn, action_id=a5.id, session_id="demo2",
        envelope_json=e5.model_dump_json(), decision_json=d5.model_dump_json(),
        payload_hash=d5.payload_hash,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    flags = auditor.cross_session_flags(conn)
    assert any("finance-agent" in f and "2 sessions" in f for f in flags)

    # The original session's decisions are untouched by having computed flags.
    original_decision = db.get_decision(conn, a5.id)
    assert original_decision.status is DecisionStatus.ESCALATE
    assert original_decision.decided_by == "policy"
