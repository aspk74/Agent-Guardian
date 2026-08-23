"""Recorder half of PLAN.md section 6's Auditor.

Append-only writes of every ActionEnvelope, Decision, and Outcome. Write
failure aborts the action (PLAN s3.3, s8: AuditWriteError -> deny, no
execution). The Reporter half (`report --session`) is Phase 2, out of scope
here.
"""
from __future__ import annotations

import sqlite3

import db
from schemas import ActionEnvelope, Decision, Outcome


class AuditWriteError(Exception):
    """Raised when a write to the audit log fails. Callers must treat this
    as fail-closed: deny / abort, never proceed as if the write succeeded."""


def record_envelope(conn: sqlite3.Connection, envelope: ActionEnvelope) -> None:
    try:
        db.insert_action(conn, envelope)
    except sqlite3.Error as exc:
        raise AuditWriteError(f"failed to record action {envelope.action.id}") from exc


def record_decision(conn: sqlite3.Connection, decision: Decision) -> None:
    try:
        db.insert_decision(conn, decision)
    except sqlite3.Error as exc:
        raise AuditWriteError(f"failed to record decision {decision.action_id}") from exc


def record_outcome(conn: sqlite3.Connection, outcome: Outcome) -> None:
    try:
        db.insert_outcome(conn, outcome)
    except sqlite3.Error as exc:
        raise AuditWriteError(f"failed to record outcome {outcome.action_id}") from exc


def outcome_for(conn: sqlite3.Connection, action_id: str) -> Outcome | None:
    """Used by executors.py for idempotency. Must not raise on "not found"."""
    return db.get_outcome(conn, action_id)
