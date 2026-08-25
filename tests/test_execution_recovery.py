"""Proves the fix for the "approved but never executed" gap: resolve_and_execute()
durably commits status='approved' (db.resolve_escalation()'s UPDATE, inside
resolve()) BEFORE the executor ever runs. Before this fix, an executor raising
at that point propagated as a bare crash and left the action stuck forever --
resolve() requires status='pending', so a second resolve attempt on the same
action_id could only ever raise AlreadyResolved, and nothing else recorded an
Outcome.

The fix: resolve_and_execute()/execute_approved() wrap an executor's failure
as guardian.escalation.ExecutionFailed (everything except executors.run()'s
own three named guard exceptions, which propagate as-is -- see
guardian/escalation.py), and guardian.escalation.execute_approved() is a new,
separately-callable retry path that main.py's `resolve` command and
dashboard.py's `/retry/{action_id}` both use.
"""
from __future__ import annotations

import guardian.escalation as esc
import guardian.executors as executors
import db
from schemas import Action, ActionEnvelope, ActionType, Decision, DecisionStatus, PaymentParams


def make_escalated_envelope_and_decision(action_id="act-exec-1"):
    action = Action(
        id=action_id,
        session_id="exec-sess",
        requesting_agent="finance",
        action_type="make_payment",
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=75000),
    )
    envelope = ActionEnvelope(
        action=action, reasoning="test escalation", model="test", raw_response="{}"
    )
    decision = Decision(
        action_id=action_id,
        status=DecisionStatus.ESCALATE,
        matched_rules=["FIN-001"],
        rule_id="FIN-001",
        policy_version="testver",
        reasoning="single payment over $500 needs a human",
        decided_by="policy",
        payload_hash=action.payload_hash(),
    )
    return envelope, decision


def _break_payment_executor(monkeypatch, error=RuntimeError("simulated Stripe outage")):
    def _raise(_action):
        raise error
    monkeypatch.setitem(executors.EXECUTORS, ActionType.MAKE_PAYMENT, _raise)


def test_execution_failure_after_approval_is_not_lost(tmp_path, monkeypatch):
    db_path = str(tmp_path / "exec.db")
    conn = db.init_db(db_path)
    envelope, decision = make_escalated_envelope_and_decision()
    esc.park(conn, envelope, decision)

    _break_payment_executor(monkeypatch)

    try:
        esc.resolve_and_execute(conn, envelope.action.id, approved=True, by="operator-1")
        assert False, "expected ExecutionFailed"
    except esc.ExecutionFailed as exc:
        assert exc.action_id == envelope.action.id
        assert "simulated Stripe outage" in str(exc)

    # Approval must already be committed -- this is the entire point of the
    # fix: the escalation is NOT reverted just because execution failed.
    row = db.get_escalation(conn, envelope.action.id)
    assert row["status"] == "approved"
    assert row["resolved_by"] == "operator-1"
    assert db.get_outcome(conn, envelope.action.id) is None

    # A second resolve attempt must not re-approve or silently succeed --
    # status is no longer 'pending'.
    try:
        esc.resolve(conn, envelope.action.id, approved=True, by="operator-2")
        assert False, "expected AlreadyResolved"
    except esc.AlreadyResolved:
        pass

    # The recovery surface: it shows up as stuck, and only as stuck (not
    # still pending).
    assert esc.pending(conn) == []
    stuck = esc.unexecuted(conn)
    assert len(stuck) == 1
    assert stuck[0]["envelope"].action.id == envelope.action.id
    assert stuck[0]["resolved_by"] == "operator-1"


def test_execute_approved_retries_and_succeeds(tmp_path, monkeypatch):
    db_path = str(tmp_path / "exec.db")
    conn = db.init_db(db_path)
    envelope, decision = make_escalated_envelope_and_decision()
    esc.park(conn, envelope, decision)

    _break_payment_executor(monkeypatch)
    try:
        esc.resolve_and_execute(conn, envelope.action.id, approved=True, by="operator-1")
    except esc.ExecutionFailed:
        pass
    assert len(esc.unexecuted(conn)) == 1

    # "the downstream API is back" -- restore the real executor and retry.
    monkeypatch.undo()
    outcome = esc.execute_approved(conn, envelope.action.id)
    assert outcome.status == "success"
    assert "acme-corp" in outcome.detail

    assert esc.unexecuted(conn) == []
    assert db.get_outcome(conn, envelope.action.id) is not None


def test_execute_approved_is_idempotent(tmp_path, monkeypatch):
    db_path = str(tmp_path / "exec.db")
    conn = db.init_db(db_path)
    envelope, decision = make_escalated_envelope_and_decision()
    esc.park(conn, envelope, decision)
    esc.resolve(conn, envelope.action.id, approved=True, by="operator-1")

    calls = []
    real_fn = executors.EXECUTORS[ActionType.MAKE_PAYMENT]

    def _counting(action):
        calls.append(action.id)
        return real_fn(action)

    monkeypatch.setitem(executors.EXECUTORS, ActionType.MAKE_PAYMENT, _counting)

    first = esc.execute_approved(conn, envelope.action.id)
    second = esc.execute_approved(conn, envelope.action.id)

    assert first.action_id == second.action_id == envelope.action.id
    assert len(calls) == 1, "executor must not run twice for the same action_id"


def test_execute_approved_rejects_non_approved_rows(tmp_path):
    db_path = str(tmp_path / "exec.db")
    conn = db.init_db(db_path)

    pending_envelope, pending_decision = make_escalated_envelope_and_decision("act-pending")
    esc.park(conn, pending_envelope, pending_decision)
    try:
        esc.execute_approved(conn, "act-pending")
        assert False, "expected NotApproved"
    except esc.NotApproved:
        pass

    rejected_envelope, rejected_decision = make_escalated_envelope_and_decision("act-rejected")
    esc.park(conn, rejected_envelope, rejected_decision)
    esc.resolve(conn, "act-rejected", approved=False, by="operator-1")
    try:
        esc.execute_approved(conn, "act-rejected")
        assert False, "expected NotApproved"
    except esc.NotApproved:
        pass

    try:
        esc.execute_approved(conn, "act-never-existed")
        assert False, "expected UnknownEscalation"
    except esc.UnknownEscalation:
        pass


def test_resolve_and_execute_does_not_wrap_named_executor_guards(tmp_path, monkeypatch):
    """NotAuthorized/PayloadMismatchError/ExecutorMissing are executors.run()'s
    own pre-flight guards, already named and already handled per PLAN.md's
    error table as "abort" -- they must propagate as-is, not get relabeled
    into the new (retryable) ExecutionFailed."""
    db_path = str(tmp_path / "exec.db")
    conn = db.init_db(db_path)
    envelope, decision = make_escalated_envelope_and_decision()
    esc.park(conn, envelope, decision)

    monkeypatch.delitem(executors.EXECUTORS, ActionType.MAKE_PAYMENT)
    try:
        esc.resolve_and_execute(conn, envelope.action.id, approved=True, by="operator-1")
        assert False, "expected ExecutorMissing"
    except executors.ExecutorMissing:
        pass
    except esc.ExecutionFailed:
        assert False, "ExecutorMissing must not be wrapped as ExecutionFailed"
