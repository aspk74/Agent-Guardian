"""LangGraph orchestrates the propose -> evaluate -> branch flow. It holds NO
state of its own -- state lives in SQLite (actions/decisions/outcomes/escalations
tables), per PLAN.md rev 3 finding A2. This module is a thin sequencer.
"""
from __future__ import annotations

from functools import cache
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

import db
import guardian.auditor as auditor
import guardian.executors as executors
import guardian.policy_agent as policy_agent
from guardian.history import SQLiteHistoryQuery
from schemas import ActionEnvelope, Decision, DecisionStatus, Outcome


class NoSuchAction(Exception):
    """retry_execution() called with an action_id that was never proposed."""


class NotAllowed(Exception):
    """retry_execution() called on an action_id whose recorded decision
    isn't ALLOW (denied, escalated, or somehow never decided) -- nothing to
    retry through this path. An escalated action's approval lives in
    guardian/escalation.py, not here -- see execute_approved() there."""


class GuardianState(TypedDict):
    envelope: ActionEnvelope
    decision: Decision | None
    outcome: Outcome | None


def _route(state: GuardianState) -> Literal["execute", "end"]:
    decision = state["decision"]
    assert decision is not None
    return "execute" if decision.status is DecisionStatus.ALLOW else "end"


@cache
def build_graph(conn):
    """conn: an open sqlite3 connection from db.init_db(). Cached per-connection
    (identity-hashed) so a session with many proposed actions compiles the
    graph once, not on every call -- graph construction is a one-time setup
    cost, not per-action work. Escalation parking for ESCALATE decisions
    happens in main.py after the graph returns, not as a graph node -- parking
    needs the caller's session context, and keeping it outside the graph keeps
    this module a pure propose/evaluate/execute sequencer."""
    history = SQLiteHistoryQuery(conn)

    def record_proposal(state: GuardianState) -> GuardianState:
        auditor.record_envelope(conn, state["envelope"])
        return state

    def evaluate_policy(state: GuardianState) -> GuardianState:
        decision = policy_agent.evaluate(state["envelope"].action, history)
        return {**state, "decision": decision}

    def record_decision(state: GuardianState) -> GuardianState:
        auditor.record_decision(conn, state["decision"])
        return state

    def execute(state: GuardianState) -> GuardianState:
        outcome = executors.run(
            state["envelope"].action,
            state["decision"],
            outcome_lookup=lambda action_id: auditor.outcome_for(conn, action_id),
            outcome_record=lambda o: auditor.record_outcome(conn, o),
        )
        return {**state, "outcome": outcome}

    graph = StateGraph(GuardianState)
    graph.add_node("record_proposal", record_proposal)
    graph.add_node("evaluate_policy", evaluate_policy)
    graph.add_node("record_decision", record_decision)
    graph.add_node("execute", execute)

    graph.add_edge(START, "record_proposal")
    graph.add_edge("record_proposal", "evaluate_policy")
    graph.add_edge("evaluate_policy", "record_decision")
    graph.add_conditional_edges("record_decision", _route, {"execute": "execute", "end": END})
    graph.add_edge("execute", END)

    return graph.compile()


def run_once(conn, envelope: ActionEnvelope) -> GuardianState:
    """One pass through propose -> evaluate -> (execute | stop). If the result
    decision is ESCALATE, the caller is responsible for guardian.escalation.park()
    -- this function does not park, since parking needs no graph state at all.

    If the decision is ALLOW, record_decision (above) has already committed
    it durably before this function's execute node runs -- so if the
    executor itself then raises, this call raises
    guardian.executors.ExecutionFailed rather than silently losing the
    action: the ALLOW decision is safe in the db, and retry_execution()
    below is the recovery path once the underlying problem is resolved."""
    compiled = build_graph(conn)
    result = compiled.invoke({"envelope": envelope, "decision": None, "outcome": None})
    return result


def retry_execution(conn, action_id: str) -> Outcome:
    """Retries an auto-allowed action whose execution previously raised
    executors.ExecutionFailed. The ALLOW decision is already durably
    recorded (record_decision ran before execute in the graph above), so
    this reconstructs the Action + Decision straight from db.py and
    re-invokes executors.run() directly, without going through the graph
    again -- re-running record_proposal/evaluate_policy/record_decision
    would be redundant at best and, for evaluate_policy, actively wrong: a
    live policy.yaml edit between the original run and this retry must not
    silently re-judge an already-decided action (PLAN.md s2.2: a Decision,
    once written, is immutable and authoritative). outcome_lookup's
    idempotency guard inside executors.run() makes this safe to call
    repeatedly."""
    action = db.get_action(conn, action_id)
    if action is None:
        raise NoSuchAction(action_id)
    decision = db.get_decision(conn, action_id)
    if decision is None or decision.status is not DecisionStatus.ALLOW:
        raise NotAllowed(f"{action_id} has no ALLOW decision to retry")
    return executors.run(
        action, decision,
        outcome_lookup=lambda aid: auditor.outcome_for(conn, aid),
        outcome_record=lambda o: auditor.record_outcome(conn, o),
    )


def unexecuted_allows(conn, session_id: str | None = None) -> list[dict]:
    """Auto-allowed actions with no recorded outcome -- stuck by a prior
    executors.ExecutionFailed. Dict shape ({"action", "decision"}) is
    intentionally different from guardian.escalation's pending()/
    unexecuted() ({"envelope", "decision"}, since an escalation stores the
    full ActionEnvelope including LLM reasoning): a non-escalated action was
    never parked, so there is no stored envelope/reasoning to reconstruct
    here, only the Action and its Decision."""
    rows = db.get_unexecuted_allows(conn, session_id=session_id)
    return [
        {
            "action": db.get_action(conn, row["action_id"]),
            "decision": db.get_decision(conn, row["action_id"]),
        }
        for row in rows
    ]
