"""Auditor: Recorder + Reporter halves of PLAN.md section 6.

Recorder (Phase 1): append-only writes of every ActionEnvelope, Decision, and
Outcome. Write failure aborts the action (PLAN s3.3, s8: AuditWriteError ->
deny, no execution).

Reporter (Phase 2, PLAN s7 step 10): `python main.py report --session <id>`
prints the chronological trail for one session -- every action proposed,
its decision, and its outcome (or why it never got one). Pattern detection
across sessions (step 11) is informational only and appended separately; it
must never feed guardian/policy_agent.py or change a Decision (PLAN s6: "the
Auditor may surface cross-session patterns... no enforcement depends on
that"). Keeping that a one-way read here, not a write anywhere, is what
keeps the enforcement boundary intact.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import db
from schemas import ActionEnvelope, Decision, DecisionStatus, Outcome, PaymentParams


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


# --------------------------------------------------------------------------
# Reporter (PLAN s6, s7 step 10)
# --------------------------------------------------------------------------

def report(conn: sqlite3.Connection, session_id: str) -> str:
    """Chronological, human-readable audit trail for one session.

    One block per action, ordered by actions.created_at (the moment it was
    proposed -- not decided_at or executed_at, so the trail reads in the
    order things actually happened to the worker, which is what a human
    reconstructing "what did this agent try to do" wants). Each block shows:

      - what was proposed (agent, action_type, target, params)
      - its Decision: status, rule_id, matched_rules, decided_by
      - its Outcome, or why it doesn't have one:
          * denied -> "denied, never executed"
          * escalated, still pending -> "pending human approval"
          * escalated, resolved -> the resulting Decision (human/allow|deny)
            plus outcome if it then executed

    Running per-agent payment totals (PLAN s12: "running totals") accumulate
    over EXECUTED outcomes only, in the same spirit as HistoryQuery (PLAN
    A4) -- a denied or still-pending payment shouldn't inflate the total a
    reader sees, even though this function has no bearing on policy.
    """
    action_rows = db.get_actions_for_session(conn, session_id)
    decisions = {d.action_id: d for d in db.get_decisions_for_session(conn, session_id)}
    outcomes = {o.action_id: o for o in db.get_outcomes_for_session(conn, session_id)}
    escalations = {row["action_id"]: row for row in db.get_escalations_for_session(conn, session_id)}

    lines: list[str] = []
    lines.append(f"=== Audit trail: session {session_id} ===")

    if not action_rows:
        lines.append("(no actions recorded for this session)")
        return "\n".join(lines)

    running_totals_cents: dict[str, int] = {}

    for i, action_row in enumerate(action_rows, start=1):
        action_id = action_row["id"]
        agent = action_row["requesting_agent"]
        action_type = action_row["action_type"]
        target = action_row["target"]
        decision = decisions.get(action_id)
        outcome = outcomes.get(action_id)
        escalation = escalations.get(action_id)

        lines.append("")
        lines.append(f"[{i}] {agent}: {action_type} -> {target}  (action_id={action_id})")
        lines.append(f"    proposed_at: {action_row['created_at']}")
        lines.append(f"    reasoning (LLM, audit-only): {action_row['reasoning']!r}")

        if decision is None:
            lines.append("    decision: none recorded")
        else:
            lines.append(
                f"    decision: {decision.status.value}  "
                f"rule_id={decision.rule_id}  matched_rules={decision.matched_rules}  "
                f"decided_by={decision.decided_by}"
            )

        if escalation is not None and escalation["status"] in ("approved", "rejected"):
            # The original Decision row is the policy's escalate call
            # (decided_by=policy) and is never mutated (PLAN s2.2: immutable
            # and authoritative). The human's resolution is a separate fact,
            # recorded on the escalations row -- surface both so the trail
            # doesn't read as if policy alone decided the outcome.
            lines.append(
                f"    resolved_by: {escalation['resolved_by']} "
                f"({escalation['status']}) at {escalation['resolved_at']}"
            )

        if outcome is not None:
            lines.append(f"    outcome: {outcome.status} -- {outcome.detail}")
        elif decision is not None and decision.status is DecisionStatus.DENY:
            lines.append("    outcome: denied, never executed")
        elif decision is not None and decision.status is DecisionStatus.ESCALATE:
            if escalation is None:
                lines.append("    outcome: escalated (no escalation record found)")
            elif escalation["status"] == "pending":
                lines.append("    outcome: pending human approval")
            elif escalation["status"] == "rejected":
                lines.append("    outcome: denied, never executed (human rejected)")
            elif escalation["status"] == "approved":
                # Approved-but-no-outcome-row means execution hasn't happened
                # yet (or failed before producing an Outcome) -- say so rather
                # than implying success. resolved_by/at already printed above.
                lines.append("    outcome: approved, not yet executed")
            else:
                lines.append(f"    outcome: escalation status={escalation['status']!r}")
        else:
            lines.append("    outcome: none")

        if action_type == "make_payment" and outcome is not None and outcome.status == "success":
            params = PaymentParams.model_validate_json(action_row["params_json"])
            running_totals_cents[agent] = running_totals_cents.get(agent, 0) + params.amount_cents
            lines.append(
                f"    running total ({agent}, executed payments): "
                f"${running_totals_cents[agent] / 100:,.2f}"
            )

    lines.append("")
    lines.append("--- Running totals (executed payments only) ---")
    if running_totals_cents:
        for agent, cents in sorted(running_totals_cents.items()):
            lines.append(f"  {agent}: ${cents / 100:,.2f}")
    else:
        lines.append("  (none)")

    flags = cross_session_flags(conn)
    lines.append("")
    lines.append("--- Cross-session pattern flags (informational, not enforced) ---")
    if flags:
        lines.extend(f"  * {flag}" for flag in flags)
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def cross_session_flags(
    conn: sqlite3.Connection, *, window: timedelta = timedelta(hours=24)
) -> list[str]:
    """Simple, informational-only signals spanning ALL sessions -- not just
    the one being reported. This exists purely so a human reading a report
    can notice "this agent looks off" across the wider history; it is never
    consulted by guardian/policy_agent.py and never changes a Decision
    (PLAN s6). Two signals, deliberately minimal per the task brief:

      1. an agent escalated multiple times across multiple DIFFERENT
         sessions within the window (a single chatty session doesn't count --
         that's not a cross-session pattern)
      2. an agent's total executed payment volume across all sessions in the
         window is unusually high (flat threshold, not a statistical model --
         this is a human nudge, not a control)
    """
    since = (datetime.now(timezone.utc) - window).isoformat()
    flags: list[str] = []

    for row in db.get_escalation_counts_by_agent(conn, since):
        if row["session_count"] > 1:
            flags.append(
                f"{row['agent']} has {row['escalation_count']} escalation(s) across "
                f"{row['session_count']} sessions in the last {int(window.total_seconds() // 3600)}h"
            )

    volume_threshold_cents = 100_000  # $1,000 -- same order of magnitude as FIN-002's cap
    for row in db.get_payment_totals_by_agent(conn, since):
        if row["total_cents"] > volume_threshold_cents:
            flags.append(
                f"{row['agent']} has ${row['total_cents'] / 100:,.2f} in executed payments "
                f"across all sessions in the last {int(window.total_seconds() // 3600)}h"
            )

    return flags
