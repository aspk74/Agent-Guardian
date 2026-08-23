"""CLI entrypoint. Phase 1: python main.py --scenario phase1_demo

Runs each scripted task through propose -> policy -> (execute | park for
human approval). Escalations are approved automatically in this Phase 1
runner (a blocking `input()` CLI prompt is Phase 2, build order step 9) --
the point here is proving the enforcement loop end to end, not the UI.
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()  # loads .env if present; never overwrites a var already set in the real environment

import db
import guardian.auditor as auditor
import guardian.escalation as esc
import guardian.executors as executors
import guardian.graph as graph
from agents.finance_agent import FinanceAgent
from demo_scenarios import SCENARIOS
from schemas import DecisionStatus

AGENTS = {"finance": FinanceAgent}


def run_scenario(name: str, session_id: str, *, db_path: str = ":memory:") -> None:
    if name not in SCENARIOS:
        print(f"Unknown scenario: {name}. Known: {list(SCENARIOS)}", file=sys.stderr)
        sys.exit(1)

    conn = db.init_db(db_path)
    workers = {}

    for step in SCENARIOS[name]:
        agent_name, task = step["agent"], step["task"]
        worker = workers.setdefault(agent_name, AGENTS[agent_name]())

        envelope = worker.handle(task, session_id=session_id)
        result = graph.run_once(conn, envelope)
        decision = result["decision"]

        print(f"\n[{agent_name}] task: {task!r}")
        print(f"  action: {envelope.action.action_type.value} -> {envelope.action.target}")
        print(f"  reasoning (LLM, audit-only): {envelope.reasoning!r}")
        print(f"  decision: {decision.status.value} (rule_id={decision.rule_id}, "
              f"matched={decision.matched_rules})")

        if decision.status is DecisionStatus.ESCALATE:
            esc.park(conn, envelope, decision)
            print("  -> escalated. auto-approving as 'demo-operator' (Phase 1 runner; "
                  "interactive CLI approval is Phase 2)")
            human_decision = esc.resolve(conn, envelope.action.id, approved=True, by="demo-operator")
            outcome = executors.run(
                envelope.action, human_decision,
                outcome_lookup=lambda aid: auditor.outcome_for(conn, aid),
                outcome_record=lambda o: auditor.record_outcome(conn, o),
            )
            print(f"  outcome: {outcome.status} -- {outcome.detail}")
        elif decision.status is DecisionStatus.ALLOW:
            print(f"  outcome: {result['outcome'].status} -- {result['outcome'].detail}")
        else:
            print("  outcome: none (denied, never executed)")

    actions_n = conn.execute("select count(*) from actions").fetchone()[0]
    decisions_n = conn.execute("select count(*) from decisions").fetchone()[0]
    print(f"\naudit trail: {actions_n} action(s), {decisions_n} decision(s) recorded in {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, choices=list(SCENARIOS))
    parser.add_argument("--session-id", default="demo1")
    parser.add_argument("--db", default=":memory:")
    args = parser.parse_args()
    run_scenario(args.scenario, args.session_id, db_path=args.db)
