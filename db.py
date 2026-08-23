"""SQLite persistence for the Guardian Agent System.

Owns the four tables described in PLAN.md sections 2, 5, 6: actions,
decisions, outcomes, escalations. This module does raw SQL only -- no
business logic, no fail-closed semantics (that lives in guardian/auditor.py
and guardian/policy_agent.py).
"""
from __future__ import annotations

import json
import sqlite3

from schemas import Action, ActionEnvelope, ActionType, Decision, DecisionStatus, Outcome

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  requesting_agent TEXT NOT NULL,
  action_type TEXT NOT NULL,
  target TEXT NOT NULL,
  params_json TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  model TEXT NOT NULL,
  raw_response TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  action_id TEXT NOT NULL,
  status TEXT NOT NULL,
  matched_rules_json TEXT NOT NULL,
  rule_id TEXT,
  policy_version TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
  action_id TEXT PRIMARY KEY,
  requesting_agent TEXT NOT NULL,
  action_type TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT NOT NULL,
  executed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_window
  ON outcomes(requesting_agent, action_type, executed_at);

CREATE TABLE IF NOT EXISTS escalations (
  action_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  envelope_json TEXT NOT NULL,
  decision_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  resolved_by TEXT,
  resolved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_esc_pending ON escalations(status, created_at);
"""


def init_db(path: str = "guardian.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def insert_action(conn: sqlite3.Connection, envelope: ActionEnvelope) -> None:
    action = envelope.action
    conn.execute(
        """
        INSERT INTO actions (
          id, session_id, requesting_agent, action_type, target,
          params_json, reasoning, model, raw_response, payload_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action.id,
            action.session_id,
            action.requesting_agent,
            action.action_type.value,
            action.target,
            action.params.model_dump_json(),
            envelope.reasoning,
            envelope.model,
            envelope.raw_response,
            action.payload_hash(),
            action.created_at.isoformat(),
        ),
    )
    conn.commit()


def insert_decision(conn: sqlite3.Connection, decision: Decision) -> None:
    conn.execute(
        """
        INSERT INTO decisions (
          action_id, status, matched_rules_json, rule_id, policy_version,
          reasoning, decided_by, payload_hash, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision.action_id,
            decision.status.value,
            json.dumps(decision.matched_rules),
            decision.rule_id,
            decision.policy_version,
            decision.reasoning,
            decision.decided_by,
            decision.payload_hash,
            decision.decided_at.isoformat(),
        ),
    )
    conn.commit()


def insert_outcome(conn: sqlite3.Connection, outcome: Outcome) -> None:
    conn.execute(
        """
        INSERT INTO outcomes (
          action_id, requesting_agent, action_type, status, detail, executed_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            outcome.action_id,
            outcome.requesting_agent,
            outcome.action_type.value,
            outcome.status,
            outcome.detail,
            outcome.executed_at.isoformat(),
        ),
    )
    conn.commit()


def get_outcome(conn: sqlite3.Connection, action_id: str) -> Outcome | None:
    row = conn.execute(
        "SELECT * FROM outcomes WHERE action_id = ?", (action_id,)
    ).fetchone()
    if row is None:
        return None
    return Outcome(
        action_id=row["action_id"],
        requesting_agent=row["requesting_agent"],
        action_type=ActionType(row["action_type"]),
        status=row["status"],
        detail=row["detail"],
        executed_at=row["executed_at"],
    )


def get_decision(conn: sqlite3.Connection, action_id: str) -> Decision | None:
    row = conn.execute(
        "SELECT * FROM decisions WHERE action_id = ?", (action_id,)
    ).fetchone()
    if row is None:
        return None
    return Decision(
        action_id=row["action_id"],
        status=DecisionStatus(row["status"]),
        matched_rules=json.loads(row["matched_rules_json"]),
        rule_id=row["rule_id"],
        policy_version=row["policy_version"],
        reasoning=row["reasoning"],
        decided_by=row["decided_by"],
        payload_hash=row["payload_hash"],
        decided_at=row["decided_at"],
    )


def insert_escalation(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    session_id: str,
    envelope_json: str,
    decision_json: str,
    payload_hash: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO escalations (
          action_id, session_id, envelope_json, decision_json, payload_hash,
          status, resolved_by, resolved_at, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)
        """,
        (action_id, session_id, envelope_json, decision_json, payload_hash, created_at),
    )
    conn.commit()


def get_escalation(conn: sqlite3.Connection, action_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM escalations WHERE action_id = ?", (action_id,)
    ).fetchone()


def get_pending_escalations(
    conn: sqlite3.Connection, session_id: str | None = None
) -> list[sqlite3.Row]:
    if session_id is None:
        return conn.execute(
            "SELECT * FROM escalations WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM escalations
        WHERE status = 'pending' AND session_id = ?
        ORDER BY created_at
        """,
        (session_id,),
    ).fetchall()


def resolve_escalation(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    status: str,
    resolved_by: str,
    resolved_at: str,
) -> None:
    conn.execute(
        """
        UPDATE escalations
        SET status = ?, resolved_by = ?, resolved_at = ?
        WHERE action_id = ?
        """,
        (status, resolved_by, resolved_at, action_id),
    )
    conn.commit()
