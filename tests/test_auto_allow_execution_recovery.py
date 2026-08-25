"""Proves the fix for the sibling gap to test_execution_recovery.py: an
auto-allowed (never escalated) action has the identical "decision recorded,
then execute, no crash-safety between them" shape as the escalated-then-
approved case, but before this fix main.py's cmd_run didn't even catch the
exception -- graph.run_once() raising propagated as a bare, uncaught
traceback straight out of the scenario loop.

guardian/graph.py's record_decision node commits the ALLOW decision before
its execute node runs (same sequencing bug), so guardian/executors.py's
ExecutionFailed (see test_execution_recovery.py for that class itself)
propagates from run_once() the same way. graph.retry_execution() is the
auto-allow analogue of guardian.escalation.execute_approved() -- same
idempotency guarantee, different reconstruction (straight from the actions/
decisions tables, since a non-escalated action was never parked and so has
no stored ActionEnvelope to replay).
"""
from __future__ import annotations

import db
import guardian.auditor as auditor
import guardian.executors as executors
import guardian.graph as graph
import main
from schemas import (
    Action, ActionEnvelope, ActionType, Decision, DecisionStatus, FileParams, PaymentParams,
)


def make_allowed_envelope_and_decision(action_id="act-allow-1"):
    action = Action(
        id=action_id,
        session_id="allow-sess",
        requesting_agent="file-read",
        action_type="read_file",
        target="workspace/report.md",
        params=FileParams(path="workspace/report.md"),
    )
    envelope = ActionEnvelope(
        action=action, reasoning="test auto-allow", model="test", raw_response="{}"
    )
    decision = Decision(
        action_id=action_id,
        status=DecisionStatus.ALLOW,
        matched_rules=["FILE-002"],
        rule_id="FILE-002",
        policy_version="testver",
        reasoning="reads inside the workspace are routine",
        decided_by="policy",
        payload_hash=action.payload_hash(),
    )
    return envelope, decision


def _break_read_executor(monkeypatch, error=RuntimeError("simulated disk outage")):
    def _raise(_action):
        raise error
    monkeypatch.setitem(executors.EXECUTORS, ActionType.READ_FILE, _raise)


def test_run_once_raises_execution_failed_but_decision_already_recorded(tmp_path, monkeypatch):
    db_path = str(tmp_path / "allow.db")
    conn = db.init_db(db_path)
    envelope, _decision = make_allowed_envelope_and_decision()

    _break_read_executor(monkeypatch)

    try:
        graph.run_once(conn, envelope)
        assert False, "expected ExecutionFailed"
    except executors.ExecutionFailed as exc:
        assert exc.action_id == envelope.action.id
        assert "simulated disk outage" in str(exc)

    # The whole point: record_proposal/evaluate_policy/record_decision ran
    # (they're nodes BEFORE execute in the graph) and committed before
    # execute() raised -- the ALLOW decision is not lost.
    recorded = db.get_decision(conn, envelope.action.id)
    assert recorded is not None
    assert recorded.status is DecisionStatus.ALLOW
    assert db.get_outcome(conn, envelope.action.id) is None

    stuck = graph.unexecuted_allows(conn)
    assert len(stuck) == 1
    assert stuck[0]["action"].id == envelope.action.id
    assert stuck[0]["decision"].rule_id == "FILE-002"


def test_retry_execution_succeeds_after_fix(tmp_path, monkeypatch):
    db_path = str(tmp_path / "allow.db")
    conn = db.init_db(db_path)
    envelope, _decision = make_allowed_envelope_and_decision()

    _break_read_executor(monkeypatch)
    try:
        graph.run_once(conn, envelope)
    except executors.ExecutionFailed:
        pass
    assert len(graph.unexecuted_allows(conn)) == 1

    monkeypatch.undo()  # "the disk is back"
    outcome = graph.retry_execution(conn, envelope.action.id)
    assert outcome.status == "success"
    assert "workspace/report.md" in outcome.detail

    assert graph.unexecuted_allows(conn) == []
    assert db.get_outcome(conn, envelope.action.id) is not None


def test_retry_execution_is_idempotent(tmp_path, monkeypatch):
    db_path = str(tmp_path / "allow.db")
    conn = db.init_db(db_path)
    envelope, decision = make_allowed_envelope_and_decision()
    auditor.record_envelope(conn, envelope)
    auditor.record_decision(conn, decision)

    calls = []
    real_fn = executors.EXECUTORS[ActionType.READ_FILE]

    def _counting(action):
        calls.append(action.id)
        return real_fn(action)

    monkeypatch.setitem(executors.EXECUTORS, ActionType.READ_FILE, _counting)

    first = graph.retry_execution(conn, envelope.action.id)
    second = graph.retry_execution(conn, envelope.action.id)

    assert first.action_id == second.action_id == envelope.action.id
    assert len(calls) == 1, "executor must not run twice for the same action_id"


def test_retry_execution_rejects_non_allowed_and_unknown(tmp_path):
    db_path = str(tmp_path / "allow.db")
    conn = db.init_db(db_path)

    denied_action = Action(
        id="act-denied", session_id="s", requesting_agent="finance",
        action_type="make_payment", target="shadowco",
        params=PaymentParams(counterparty="shadowco", amount_cents=10000),
    )
    denied_envelope = ActionEnvelope(action=denied_action, reasoning="r", model="t", raw_response="{}")
    denied_decision = Decision(
        action_id="act-denied", status=DecisionStatus.DENY, matched_rules=["FIN-003"],
        rule_id="FIN-003", policy_version="v", reasoning="unknown counterparty",
        decided_by="policy", payload_hash=denied_action.payload_hash(),
    )
    auditor.record_envelope(conn, denied_envelope)
    auditor.record_decision(conn, denied_decision)

    try:
        graph.retry_execution(conn, "act-denied")
        assert False, "expected NotAllowed"
    except graph.NotAllowed:
        pass

    try:
        graph.retry_execution(conn, "act-never-existed")
        assert False, "expected NoSuchAction"
    except graph.NoSuchAction:
        pass


def test_cmd_run_reports_execution_failure_instead_of_crashing(tmp_path, monkeypatch, capsys):
    """Unit-level proof of the actual reported bug: before this fix,
    main.py's cmd_run had no try/except around graph.run_once() at all, so
    an ExecutionFailed there was a bare, uncaught crash. Monkeypatches the
    agent registry (no real LLM call needed -- handle() just returns a fixed
    envelope) and graph.run_once (forced to raise), so this stays a fast
    unit test rather than a subprocess/LLM integration test."""
    db_path = str(tmp_path / "cmdrun.db")
    envelope, _decision = make_allowed_envelope_and_decision("act-cmdrun-1")

    class _FakeWorker:
        def handle(self, task, session_id):
            return envelope

    # _load_agents() returns {name: zero-arg-callable-producing-a-worker};
    # cmd_run does agent_classes[name](), so the class itself doubles as
    # that callable here -- no real LLM call needed.
    monkeypatch.setattr(main, "_load_agents", lambda: {"file-read": _FakeWorker})
    monkeypatch.setitem(main.SCENARIOS, "fake", [{"agent": "file-read", "task": "read the report"}])

    def _raise_execution_failed(conn, envelope):
        raise executors.ExecutionFailed(envelope.action.id, RuntimeError("simulated outage"))

    monkeypatch.setattr(main.graph, "run_once", _raise_execution_failed)

    main.cmd_run("fake", "cmdrun-sess", db_path=db_path, by="operator-1")

    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    assert "execution failed" in captured.out or "execution failed" in captured.err
    assert "retry with `main.py resolve`" in captured.out or "retry with `main.py resolve`" in captured.err


def test_cmd_resolve_retries_stuck_auto_allowed_actions(tmp_path, capsys):
    db_path = str(tmp_path / "cmdresolve.db")
    conn = db.init_db(db_path)
    envelope, decision = make_allowed_envelope_and_decision("act-cmdresolve-1")
    auditor.record_envelope(conn, envelope)
    auditor.record_decision(conn, decision)
    # No outcome recorded -- simulates a prior execution failure without
    # needing to actually break/restore the executor for this CLI-level test.

    main.cmd_resolve(db_path=db_path, session_id=None, by="operator-1")

    captured = capsys.readouterr()
    assert "act-cmdresolve-1" in captured.out
    assert "outcome: success" in captured.out
    assert db.get_outcome(conn, "act-cmdresolve-1") is not None
