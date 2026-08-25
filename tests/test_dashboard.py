"""Tests for dashboard.py (PLAN.md s7 step 12): the FastAPI routes are thin
wrappers over guardian/escalation.py's park/pending/resolve, same as
main.py's CLI. These tests exercise the HTTP layer against a real sqlite
file (via monkeypatched DB_PATH/POLICY_PATH), not a mock of escalation.py --
the point is proving the dashboard calls the SAME functions the CLI does,
not a reimplementation.
"""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

import db
import guardian.auditor as auditor
import guardian.escalation as esc
import guardian.executors as executors
import dashboard
from schemas import (
    Action, ActionEnvelope, ActionType, Decision, DecisionStatus, FileParams, PaymentParams,
)


def make_escalated_envelope_and_decision(action_id: str, policy_version: str = "testver"):
    action = Action(
        id=action_id,
        session_id="dash-sess",
        requesting_agent="finance",
        action_type="make_payment",
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=75000),
    )
    envelope = ActionEnvelope(action=action, reasoning="test escalation", model="test", raw_response="{}")
    decision = Decision(
        action_id=action_id,
        status=DecisionStatus.ESCALATE,
        matched_rules=["FIN-001"],
        rule_id="FIN-001",
        policy_version=policy_version,
        reasoning="single payment over $500 needs a human",
        decided_by="policy",
        payload_hash=action.payload_hash(),
    )
    return envelope, decision


def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "dashboard.db")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\nrules:\n  - id: FIN-001\n    description: d\n"
                            "    when: {action_type: make_payment, amount_cents_gt: 50000}\n"
                            "    then: escalate\n")
    # dashboard.py opens its one process-lifetime connection at import time
    # (not per-request) with check_same_thread=False, since FastAPI's sync
    # routes run in a threadpool -- TestClient does too, so the test's swapped-in
    # connection needs the same flag db.init_db() doesn't expose, or the route
    # call fails with "SQLite objects created in a thread can only be used in
    # that same thread."
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    conn.commit()
    monkeypatch.setattr(dashboard, "_conn", conn)
    monkeypatch.setattr(dashboard, "DB_PATH", db_path)
    monkeypatch.setattr(dashboard, "POLICY_PATH", str(policy_path))
    return conn, db_path, str(policy_path)


def test_pending_json_reflects_escalation_pending(tmp_path, monkeypatch):
    conn, _, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_escalated_envelope_and_decision("act-dash-1")
    esc.park(conn, envelope, decision)

    client = TestClient(dashboard.app)
    resp = client.get("/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["action_id"] == "act-dash-1"
    assert body[0]["rule_id"] == "FIN-001"


def test_dashboard_page_renders_pending_row(tmp_path, monkeypatch):
    conn, _, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_escalated_envelope_and_decision("act-dash-2")
    esc.park(conn, envelope, decision)

    client = TestClient(dashboard.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "acme-corp" in resp.text
    assert "FIN-001" in resp.text


def test_resolve_via_api_approves_and_executes(tmp_path, monkeypatch):
    conn, db_path, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_escalated_envelope_and_decision("act-dash-3")
    esc.park(conn, envelope, decision)

    client = TestClient(dashboard.app)
    resp = client.post("/api/resolve/act-dash-3", params={"approved": True, "by": "dashboard-operator"})
    assert resp.status_code == 200
    outcome = resp.json()["outcome"]
    assert outcome is not None
    assert outcome["status"] == "success"

    # Same source of truth as the CLI: the row is now 'approved' in the db.
    fresh_conn = db.init_db(db_path)
    row = db.get_escalation(fresh_conn, "act-dash-3")
    assert row["status"] == "approved"
    assert row["resolved_by"] == "dashboard-operator"

    # No longer pending.
    resp2 = client.get("/pending")
    assert resp2.json() == []


def test_resolve_via_api_rejects(tmp_path, monkeypatch):
    conn, _, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_escalated_envelope_and_decision("act-dash-4")
    esc.park(conn, envelope, decision)

    client = TestClient(dashboard.app)
    resp = client.post("/api/resolve/act-dash-4", params={"approved": False, "by": "dashboard-operator"})
    assert resp.status_code == 200
    assert resp.json()["outcome"] is None


def test_resolve_via_form_redirects(tmp_path, monkeypatch):
    conn, _, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_escalated_envelope_and_decision("act-dash-5")
    esc.park(conn, envelope, decision)

    client = TestClient(dashboard.app, follow_redirects=False)
    resp = client.post("/resolve/act-dash-5", data={"approved": "true", "by": "form-operator"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_policy_version_endpoint_reflects_current_file(tmp_path, monkeypatch):
    conn, _, policy_path = _setup(tmp_path, monkeypatch)
    import guardian.policy_agent as policy_agent

    client = TestClient(dashboard.app)
    resp = client.get("/policy-version")
    assert resp.status_code == 200
    assert resp.json()["policy_version"] == policy_agent.policy_version(policy_path)


def test_resolve_via_api_twice_returns_409_not_500(tmp_path, monkeypatch):
    """Regression: a code review found that resolve_via_form/resolve_via_api
    had no exception handling around esc.resolve_and_execute -- unlike
    main.py's cmd_resolve, which catches (esc.AlreadyResolved, ValueError).
    Two callers resolving the same action_id (e.g. two browser tabs racing
    on the same pending row) must get a clean 409, not an unhandled 500."""
    conn, _, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_escalated_envelope_and_decision("act-dash-6")
    esc.park(conn, envelope, decision)

    client = TestClient(dashboard.app)
    first = client.post("/api/resolve/act-dash-6", params={"approved": True, "by": "operator-1"})
    assert first.status_code == 200

    second = client.post("/api/resolve/act-dash-6", params={"approved": True, "by": "operator-2"})
    assert second.status_code == 409
    assert "act-dash-6" in second.json()["detail"]


def test_resolve_via_api_unknown_action_returns_404_not_500(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    client = TestClient(dashboard.app)
    resp = client.post("/api/resolve/no-such-action", params={"approved": True, "by": "operator-1"})
    assert resp.status_code == 404


def make_allowed_envelope_and_decision(action_id: str):
    action = Action(
        id=action_id, session_id="dash-allow-sess", requesting_agent="file-read",
        action_type="read_file", target="workspace/report.md",
        params=FileParams(path="workspace/report.md"),
    )
    envelope = ActionEnvelope(action=action, reasoning="r", model="t", raw_response="{}")
    decision = Decision(
        action_id=action_id, status=DecisionStatus.ALLOW, matched_rules=["FILE-002"],
        rule_id="FILE-002", policy_version="v", reasoning="reads inside the workspace are routine",
        decided_by="policy", payload_hash=action.payload_hash(),
    )
    return envelope, decision


def test_resolve_via_api_wraps_execution_failure_as_502_not_500(tmp_path, monkeypatch):
    """Regression companion to test_resolve_via_api_twice_returns_409_not_500:
    an executor raising after approval must surface as a clean 502
    (guardian.escalation.ExecutionFailed), not an unhandled 500 -- the
    escalation itself is fine, a downstream dependency isn't."""
    conn, _, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_escalated_envelope_and_decision("act-dash-7")
    esc.park(conn, envelope, decision)

    def _raise(_action):
        raise RuntimeError("simulated Stripe outage")
    monkeypatch.setitem(executors.EXECUTORS, ActionType.MAKE_PAYMENT, _raise)

    client = TestClient(dashboard.app)
    resp = client.post("/api/resolve/act-dash-7", params={"approved": True, "by": "operator-1"})
    assert resp.status_code == 502
    assert "act-dash-7" in resp.json()["detail"]

    # Approval already committed -- confirmed via the stuck-approvals JSON.
    stuck = client.get("/stuck").json()
    assert len(stuck) == 1
    assert stuck[0]["action_id"] == "act-dash-7"


def test_stuck_allows_json_and_retry_via_api(tmp_path, monkeypatch):
    """Auto-allowed (never escalated) action whose execution previously
    failed: /stuck-allows lists it, /api/retry/{id} retries it through
    guardian.graph.retry_execution() -- the SAME recovery guardian/graph.py
    unit tests exercise directly, proven here at the HTTP layer."""
    conn, _, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_allowed_envelope_and_decision("act-dash-allow-1")
    auditor.record_envelope(conn, envelope)
    auditor.record_decision(conn, decision)
    # No outcome recorded -- simulates a prior ExecutionFailed without
    # needing to actually break/restore the executor for this HTTP-level test.

    client = TestClient(dashboard.app)
    stuck = client.get("/stuck-allows").json()
    assert len(stuck) == 1
    assert stuck[0]["action_id"] == "act-dash-allow-1"
    assert stuck[0]["rule_id"] == "FILE-002"

    resp = client.post("/api/retry/act-dash-allow-1")
    assert resp.status_code == 200
    assert resp.json()["outcome"]["status"] == "success"

    assert client.get("/stuck-allows").json() == []


def test_dashboard_page_renders_stuck_allow_row(tmp_path, monkeypatch):
    conn, _, _ = _setup(tmp_path, monkeypatch)
    envelope, decision = make_allowed_envelope_and_decision("act-dash-allow-2")
    auditor.record_envelope(conn, envelope)
    auditor.record_decision(conn, decision)

    client = TestClient(dashboard.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "act-dash-allow-2" in resp.text
    assert "Auto-allowed but not yet executed" in resp.text


def test_retry_via_api_unknown_action_returns_404_not_500(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    client = TestClient(dashboard.app)
    resp = client.post("/api/retry/no-such-action")
    assert resp.status_code == 404
