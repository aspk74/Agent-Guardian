"""Proves PLAN.md rev 3 finding A2: escalation state lives in the SQLite
`escalations` table, not in process memory or a LangGraph checkpointer, so
killing the process mid-escalation loses nothing.

Two levels of proof:
  - test_reopening_connection_sees_pending_row: fast, in-process, exercises
    guardian/escalation.py directly against a real file (not :memory:, since
    :memory: cannot outlive a process by definition).
  - test_kill_restart_resolve_subprocess: the actual claim from the handoff --
    a real `main.py run` subprocess is killed with a pending escalation
    sitting in the db, then a fresh `main.py resolve` subprocess (simulating
    a restart) approves it and the payment executes.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time

import db
import guardian.escalation as esc
from schemas import Action, ActionEnvelope, Decision, DecisionStatus, PaymentParams


def make_escalated_envelope_and_decision(action_id="act-resume-1"):
    action = Action(
        id=action_id,
        session_id="resume-sess",
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


def test_reopening_connection_sees_pending_row(tmp_path):
    db_path = str(tmp_path / "resume.db")

    # "process 1": park an escalation, then drop the connection entirely --
    # nothing about the pending state lives past this point except the file.
    conn1 = db.init_db(db_path)
    envelope, decision = make_escalated_envelope_and_decision()
    esc.park(conn1, envelope, decision)
    conn1.close()
    del conn1

    # "process 2" (a restart): brand new connection, same file.
    conn2 = db.init_db(db_path)
    pending = esc.pending(conn2)
    assert len(pending) == 1
    assert pending[0]["envelope"].action.id == envelope.action.id
    assert pending[0]["decision"].status is DecisionStatus.ESCALATE

    resolved = esc.resolve(conn2, envelope.action.id, approved=True, by="restarted-operator")
    assert resolved.status is DecisionStatus.ALLOW
    assert resolved.payload_hash == envelope.action.payload_hash()

    # resolved once -- a second resolve attempt must not silently re-approve.
    row = db.get_escalation(conn2, envelope.action.id)
    assert row["status"] == "approved"
    assert row["resolved_by"] == "restarted-operator"


def test_kill_restart_resolve_subprocess(tmp_path):
    """The literal handoff claim: kill `main.py` mid-escalation, restart,
    prove the pending row can still be approved and executed.

    Waits for the pending row to land by polling the db file directly, not
    by scraping subprocess stdout -- Python fully block-buffers stdout when
    it's a pipe rather than a tty, so a child's `print()` calls (including
    the approval prompt) can sit unflushed indefinitely and a readline()-based
    wait hangs. The db row landing is also the actually-relevant signal: it's
    what the resumability claim is about, not what got printed.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = str(tmp_path / "kill_restart.db")

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    run_proc = subprocess.Popen(
        [sys.executable, "main.py", "run", "--scenario", "phase1_demo",
         "--session-id", "kill-test", "--db", db_path],
        cwd=repo_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    # Poll the db file itself for the parked row -- this is what proves the
    # state is durable, independent of any stdout timing/buffering.
    deadline = time.time() + 30
    parked = []
    while time.time() < deadline:
        if run_proc.poll() is not None:
            leftover = run_proc.stdout.read()
            raise AssertionError(
                f"main.py run exited early (code={run_proc.returncode}) before parking:\n{leftover}"
            )
        try:
            conn = db.init_db(db_path)
            parked = esc.pending(conn)
            conn.close()
        except sqlite3.OperationalError:
            parked = []
        if parked:
            break
        time.sleep(0.2)
    else:
        run_proc.kill()
        raise AssertionError("no escalation ever appeared as pending in the db within 30s")

    # Kill (not terminate-and-wait-for-cleanup) -- proves no graceful
    # shutdown hook is doing the persisting; the row must already be durable.
    run_proc.send_signal(signal.SIGKILL)
    run_proc.wait(timeout=10)

    # The db file must show exactly one pending escalation now.
    conn = db.init_db(db_path)
    pending_after_kill = esc.pending(conn)
    assert len(pending_after_kill) == 1
    parked_action_id = pending_after_kill[0]["envelope"].action.id
    conn.close()

    # "restart": a fresh process, same --db file, resolves the pending row.
    resolve_proc = subprocess.run(
        [sys.executable, "main.py", "resolve", "--db", db_path, "--by", "restarted-operator"],
        cwd=repo_root,
        input="y\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert resolve_proc.returncode == 0, resolve_proc.stdout + resolve_proc.stderr
    assert "outcome: success" in resolve_proc.stdout, resolve_proc.stdout

    conn = db.init_db(db_path)
    row = db.get_escalation(conn, parked_action_id)
    assert row["status"] == "approved"
    assert row["resolved_by"] == "restarted-operator"
    outcome = db.get_outcome(conn, parked_action_id)
    assert outcome is not None
    assert outcome.status == "success"
