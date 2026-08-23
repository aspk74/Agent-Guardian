"""LangGraph orchestrates the propose -> evaluate -> branch flow. It holds NO
state of its own -- state lives in SQLite (actions/decisions/outcomes/escalations
tables), per PLAN.md rev 3 finding A2. This module is a thin sequencer.
"""
from __future__ import annotations

from functools import cache
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

import guardian.auditor as auditor
import guardian.executors as executors
import guardian.policy_agent as policy_agent
from guardian.history import SQLiteHistoryQuery
from schemas import ActionEnvelope, Decision, DecisionStatus, Outcome


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
    -- this function does not park, since parking needs no graph state at all."""
    compiled = build_graph(conn)
    result = compiled.invoke({"envelope": envelope, "decision": None, "outcome": None})
    return result
