"""CRUD coverage for db.py. Untested SQL is a defect."""
from __future__ import annotations

import json
import sqlite3

import pytest

import db
from schemas import (
    Action,
    ActionEnvelope,
    ActionType,
    Decision,
    DecisionStatus,
    EmailParams,
    Outcome,
    PaymentParams,
)


@pytest.fixture()
def conn():
    return db.init_db(":memory:")


def make_envelope(**overrides) -> ActionEnvelope:
    action_kwargs = dict(
        session_id="sess-1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=75000),
    )
    action_kwargs.update(overrides.pop("action", {}) if "action" in overrides else {})
    action = Action(**action_kwargs)
    envelope_kwargs = dict(
        action=action,
        reasoning="pay the vendor invoice",
        model="claude-haiku-4-5-20251001",
        raw_response='{"ok": true}',
    )
    envelope_kwargs.update(overrides)
    return ActionEnvelope(**envelope_kwargs)


def make_decision(action_id: str, **overrides) -> Decision:
    kwargs = dict(
        action_id=action_id,
        status=DecisionStatus.ESCALATE,
        matched_rules=["FIN-001"],
        rule_id="FIN-001",
        policy_version="deadbeef",
        reasoning="Single payment over $500 needs a human",
        decided_by="policy",
        payload_hash="hash-abc",
    )
    kwargs.update(overrides)
    return Decision(**kwargs)


def make_outcome(action_id: str, **overrides) -> Outcome:
    kwargs = dict(
        action_id=action_id,
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        status="success",
        detail="simulated payment of 75000 cents to acme-corp",
    )
    kwargs.update(overrides)
    return Outcome(**kwargs)


# --- init_db ---

def test_init_db_creates_all_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"actions", "decisions", "outcomes", "escalations"} <= tables


def test_init_db_row_factory_is_row(conn):
    assert conn.row_factory is sqlite3.Row


def test_init_db_is_idempotent(tmp_path):
    path = str(tmp_path / "guardian.db")
    conn1 = db.init_db(path)
    conn1.close()
    conn2 = db.init_db(path)  # must not raise on existing tables
    conn2.close()


# --- actions ---

def test_insert_and_read_action_row(conn):
    envelope = make_envelope()
    db.insert_action(conn, envelope)

    row = conn.execute(
        "SELECT * FROM actions WHERE id = ?", (envelope.action.id,)
    ).fetchone()
    assert row is not None
    assert row["session_id"] == "sess-1"
    assert row["requesting_agent"] == "finance-agent"
    assert row["action_type"] == "make_payment"
    assert row["target"] == "acme-corp"
    assert json.loads(row["params_json"]) == {
        "kind": "payment",
        "counterparty": "acme-corp",
        "amount_cents": 75000,
        "memo_ref": None,
    }
    assert row["reasoning"] == "pay the vendor invoice"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["raw_response"] == '{"ok": true}'
    assert row["payload_hash"] == envelope.action.payload_hash()
    assert row["created_at"] == envelope.action.created_at.isoformat()


def test_insert_action_commits(tmp_path):
    path = str(tmp_path / "guardian.db")
    conn = db.init_db(path)
    envelope = make_envelope()
    db.insert_action(conn, envelope)
    conn.close()

    conn2 = db.init_db(path)
    row = conn2.execute(
        "SELECT id FROM actions WHERE id = ?", (envelope.action.id,)
    ).fetchone()
    assert row is not None


def test_insert_action_with_email_params(conn):
    action = Action(
        session_id="sess-2",
        requesting_agent="email-agent",
        action_type=ActionType.SEND_EMAIL,
        target="vendor@external.com",
        params=EmailParams(
            recipient="vendor@external.com", subject_ref="tmpl-1", body_ref="body-1"
        ),
    )
    envelope = ActionEnvelope(
        action=action,
        reasoning="notify vendor",
        model="claude-haiku-4-5-20251001",
        raw_response="{}",
    )
    db.insert_action(conn, envelope)
    row = conn.execute(
        "SELECT * FROM actions WHERE id = ?", (action.id,)
    ).fetchone()
    assert json.loads(row["params_json"])["kind"] == "email"


# --- decisions ---

def test_insert_and_get_decision(conn):
    envelope = make_envelope()
    db.insert_action(conn, envelope)
    decision = make_decision(envelope.action.id)
    db.insert_decision(conn, decision)

    fetched = db.get_decision(conn, envelope.action.id)
    assert fetched is not None
    assert fetched.action_id == envelope.action.id
    assert fetched.status == DecisionStatus.ESCALATE
    assert fetched.matched_rules == ["FIN-001"]
    assert fetched.rule_id == "FIN-001"
    assert fetched.policy_version == "deadbeef"
    assert fetched.decided_by == "policy"
    assert fetched.payload_hash == "hash-abc"


def test_get_decision_missing_returns_none(conn):
    assert db.get_decision(conn, "nonexistent-id") is None


def test_decision_with_none_rule_id(conn):
    envelope = make_envelope()
    db.insert_action(conn, envelope)
    decision = make_decision(
        envelope.action.id,
        status=DecisionStatus.DENY,
        matched_rules=[],
        rule_id=None,
        reasoning="predicate raised",
        decided_by="system",
    )
    db.insert_decision(conn, decision)

    fetched = db.get_decision(conn, envelope.action.id)
    assert fetched.rule_id is None
    assert fetched.matched_rules == []


# --- outcomes ---

def test_insert_and_get_outcome(conn):
    envelope = make_envelope()
    db.insert_action(conn, envelope)
    outcome = make_outcome(envelope.action.id)
    db.insert_outcome(conn, outcome)

    fetched = db.get_outcome(conn, envelope.action.id)
    assert fetched is not None
    assert fetched.action_id == envelope.action.id
    assert fetched.requesting_agent == "finance-agent"
    assert fetched.action_type == ActionType.MAKE_PAYMENT
    assert fetched.status == "success"
    assert fetched.detail == "simulated payment of 75000 cents to acme-corp"


def test_get_outcome_missing_returns_none(conn):
    assert db.get_outcome(conn, "nonexistent-id") is None


# --- escalations ---

def test_insert_and_get_escalation(conn):
    db.insert_escalation(
        conn,
        action_id="act-1",
        session_id="sess-1",
        envelope_json="{}",
        decision_json="{}",
        payload_hash="hash-1",
        created_at="2026-08-22T00:00:00+00:00",
    )
    row = db.get_escalation(conn, "act-1")
    assert row is not None
    assert row["status"] == "pending"
    assert row["resolved_by"] is None
    assert row["resolved_at"] is None
    assert row["session_id"] == "sess-1"


def test_get_escalation_missing_returns_none(conn):
    assert db.get_escalation(conn, "nonexistent-id") is None


def test_get_pending_escalations_filters_by_status(conn):
    db.insert_escalation(
        conn,
        action_id="act-1",
        session_id="sess-1",
        envelope_json="{}",
        decision_json="{}",
        payload_hash="hash-1",
        created_at="2026-08-22T00:00:00+00:00",
    )
    db.insert_escalation(
        conn,
        action_id="act-2",
        session_id="sess-1",
        envelope_json="{}",
        decision_json="{}",
        payload_hash="hash-2",
        created_at="2026-08-22T00:00:01+00:00",
    )
    db.resolve_escalation(
        conn,
        "act-2",
        status="approved",
        resolved_by="alice",
        resolved_at="2026-08-22T00:05:00+00:00",
    )

    pending = db.get_pending_escalations(conn)
    assert [row["action_id"] for row in pending] == ["act-1"]


def test_get_pending_escalations_ordered_by_created_at(conn):
    db.insert_escalation(
        conn,
        action_id="act-later",
        session_id="sess-1",
        envelope_json="{}",
        decision_json="{}",
        payload_hash="hash-1",
        created_at="2026-08-22T05:00:00+00:00",
    )
    db.insert_escalation(
        conn,
        action_id="act-earlier",
        session_id="sess-1",
        envelope_json="{}",
        decision_json="{}",
        payload_hash="hash-2",
        created_at="2026-08-22T01:00:00+00:00",
    )

    pending = db.get_pending_escalations(conn)
    assert [row["action_id"] for row in pending] == ["act-earlier", "act-later"]


def test_get_pending_escalations_filters_by_session(conn):
    db.insert_escalation(
        conn,
        action_id="act-1",
        session_id="sess-1",
        envelope_json="{}",
        decision_json="{}",
        payload_hash="hash-1",
        created_at="2026-08-22T00:00:00+00:00",
    )
    db.insert_escalation(
        conn,
        action_id="act-2",
        session_id="sess-2",
        envelope_json="{}",
        decision_json="{}",
        payload_hash="hash-2",
        created_at="2026-08-22T00:00:01+00:00",
    )

    pending = db.get_pending_escalations(conn, session_id="sess-2")
    assert [row["action_id"] for row in pending] == ["act-2"]


def test_resolve_escalation_updates_not_inserts(conn):
    db.insert_escalation(
        conn,
        action_id="act-1",
        session_id="sess-1",
        envelope_json="{}",
        decision_json="{}",
        payload_hash="hash-1",
        created_at="2026-08-22T00:00:00+00:00",
    )
    db.resolve_escalation(
        conn,
        "act-1",
        status="rejected",
        resolved_by="bob",
        resolved_at="2026-08-22T00:10:00+00:00",
    )

    row = db.get_escalation(conn, "act-1")
    assert row["status"] == "rejected"
    assert row["resolved_by"] == "bob"
    assert row["resolved_at"] == "2026-08-22T00:10:00+00:00"

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM escalations WHERE action_id = ?", ("act-1",)
    ).fetchone()["n"]
    assert count == 1
