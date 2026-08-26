"""The one place that wires guardian/executors.py to the audit trail.

guardian/executors.py takes `outcome_lookup` and `outcome_record` as injected
callables specifically so it never imports db.py or auditor.py -- that keeps
the effector boundary a one-file diff away from a real integration (see that
module's run() docstring). The cost of that injection is that every caller has
to supply the same two lambdas, and there were four such call sites doing it
identically: the graph's execute node, retry_execution(), resolve_and_execute(),
and execute_approved().

Four copies of a security-relevant wiring decision is three too many -- if the
audit lookup ever needs to change (say, to consult a pending-execution record
rather than only completed outcomes), it must change in exactly one place or
the paths silently diverge. This module is that place. executors.py still
imports nothing from db/auditor; the wiring just stopped being duplicated.
"""
from __future__ import annotations

import sqlite3

import guardian.auditor as auditor
import guardian.executors as executors
from schemas import Action, Decision, Outcome


def run_with_audit(conn: sqlite3.Connection, action: Action, decision: Decision) -> Outcome:
    """executors.run() bound to this connection's audit trail.

    Raises whatever executors.run() raises -- NotAuthorized, PayloadMismatchError
    and ExecutorMissing propagate unwrapped as pre-flight guards, ExecutionFailed
    wraps a failure inside the executor itself. Callers catch these exactly as
    they did when they built the lambdas inline; this changes no behaviour.
    """
    return executors.run(
        action,
        decision,
        outcome_lookup=lambda action_id: auditor.outcome_for(conn, action_id),
        outcome_record=lambda outcome: auditor.record_outcome(conn, outcome),
    )
