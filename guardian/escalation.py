"""Pending-approval state lives in SQLite, not in a LangGraph checkpointer
(PLAN.md rev 3, finding A2). One row per escalated action. The CLI (Phase 2)
and the FastAPI dashboard (Phase 3) both read/write through park/pending/resolve
so there is exactly one source of truth for "what's waiting on a human."
"""
from __future__ import annotations

from datetime import datetime, timezone

import db
import guardian.auditor as auditor
import guardian.executors as executors
from schemas import ActionEnvelope, Decision, DecisionStatus, Outcome


class UnknownEscalation(Exception):
    """resolve() called with an action_id that was never parked."""


class AlreadyResolved(Exception):
    """resolve() called twice on the same action_id."""


def park(conn, envelope: ActionEnvelope, decision: Decision) -> None:
    """Record a pending approval. decision.status must be ESCALATE -- this
    function doesn't re-check policy, it just persists what evaluate() already
    decided, exactly as PLAN.md's 'Decision, once written, is immutable and
    authoritative' rule requires (section 2.2)."""
    assert decision.status is DecisionStatus.ESCALATE
    db.insert_escalation(
        conn,
        action_id=envelope.action.id,
        session_id=envelope.action.session_id,
        envelope_json=envelope.model_dump_json(),
        decision_json=decision.model_dump_json(),
        payload_hash=decision.payload_hash,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def pending(conn, session_id: str | None = None) -> list[dict]:
    """Returns dicts with 'envelope' (ActionEnvelope) and 'decision' (Decision)
    keys, reconstructed from stored JSON, for CLI/dashboard display."""
    rows = db.get_pending_escalations(conn, session_id=session_id)
    return [
        {
            "envelope": ActionEnvelope.model_validate_json(row["envelope_json"]),
            "decision": Decision.model_validate_json(row["decision_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def resolve(conn, action_id: str, *, approved: bool, by: str) -> Decision:
    """Re-verifies payload_hash against the ORIGINAL parked envelope before
    returning an executable Decision -- this is the TOCTOU guard (PLAN.md
    finding 5 / rev-2): a human approves what was actually proposed, not
    whatever the action looks like now.

    The pending-status check and the resolving write must be a single
    atomic operation, not a separate read then a separate write: two
    concurrent callers racing on the same action_id could otherwise both
    read status='pending' before either writes, both pass this check, and
    both return an executable ALLOW Decision -- each falsely believing
    their own call is what resolved it. db.resolve_escalation()'s UPDATE
    carries the status='pending' guard in its WHERE clause and reports
    whether THIS call actually won the race via its return value."""
    row = db.get_escalation(conn, action_id)
    if row is None:
        raise UnknownEscalation(action_id)

    envelope = ActionEnvelope.model_validate_json(row["envelope_json"])
    stored_decision = Decision.model_validate_json(row["decision_json"])
    current_hash = envelope.action.payload_hash()
    if current_hash != row["payload_hash"] or current_hash != stored_decision.payload_hash:
        raise ValueError(f"payload hash mismatch for {action_id}: escalation record is inconsistent")

    resolved_at = datetime.now(timezone.utc).isoformat()
    won_race = db.resolve_escalation(
        conn, action_id,
        status="approved" if approved else "rejected",
        resolved_by=by,
        resolved_at=resolved_at,
    )
    if not won_race:
        current_status = db.get_escalation(conn, action_id)["status"]
        raise AlreadyResolved(f"{action_id} already {current_status}")

    final_status = DecisionStatus.ALLOW if approved else DecisionStatus.DENY
    return Decision(
        action_id=action_id,
        status=final_status,
        matched_rules=stored_decision.matched_rules,
        rule_id=stored_decision.rule_id,
        policy_version=stored_decision.policy_version,
        reasoning=f"human {'approved' if approved else 'rejected'} by {by} (was: {stored_decision.reasoning})",
        decided_by="human",
        payload_hash=current_hash,
    )


def resolve_and_execute(conn, action_id: str, *, approved: bool, by: str) -> Outcome | None:
    """Shared by main.py's CLI and dashboard.py's HTTP routes (PLAN.md s5:
    "Phase 2 CLI and Phase 3 HTTP call the same three functions") -- this is
    the fourth: resolve() plus, if approved, running the executor. Returns
    None for a rejected/denied resolution (nothing executes)."""
    decision = resolve(conn, action_id, approved=approved, by=by)
    if decision.status is not DecisionStatus.ALLOW:
        return None
    row = db.get_escalation(conn, action_id)
    action = ActionEnvelope.model_validate_json(row["envelope_json"]).action
    return executors.run(
        action, decision,
        outcome_lookup=lambda aid: auditor.outcome_for(conn, aid),
        outcome_record=lambda o: auditor.record_outcome(conn, o),
    )
