"""CLI entrypoint.

  python main.py run --scenario <name> [--session-id ID] [--db PATH]
  python main.py resolve [--db PATH] [--session-id ID]
  python main.py report --session <id> [--db PATH]

Phase 2 (PLAN.md section 7, step 9): escalations block on an interactive
approve/reject prompt instead of Phase 1's auto-approve. Pending-approval
state lives in the `escalations` table (guardian/escalation.py), not in
process memory or a LangGraph checkpointer -- killing this process mid
escalation loses nothing. `run` parks an escalation and prompts immediately;
`resolve` is the standalone recovery path: it lists whatever is still
`status='pending'` in the db and prompts for each, so a run interrupted
before a human answered can be resumed by simply invoking `resolve` against
the same --db file.
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
from schemas import ActionEnvelope, Decision, DecisionStatus

AGENTS = {"finance": FinanceAgent}


def _load_agents():
    """Imports EmailAgent/FileAgent lazily so this module still runs against
    Phase 1's SCENARIOS even before those files exist / while they're being
    built on a parallel track. Only tolerates the file being absent --
    ModuleNotFoundError for that exact module name -- so a real bug inside
    an existing agent file (bad import, missing dependency) still surfaces
    as a traceback instead of a misleading "Unknown agent" message."""
    agents = dict(AGENTS)
    try:
        from agents.email_agent import EmailAgent
        agents["email"] = EmailAgent
    except ModuleNotFoundError as exc:
        if exc.name != "agents.email_agent":
            raise

    try:
        from agents.file_agent import DeleteFileAgent, ReadFileAgent, WriteFileAgent
        agents["file-read"] = ReadFileAgent
        agents["file-write"] = WriteFileAgent
        agents["file-delete"] = DeleteFileAgent
    except ModuleNotFoundError as exc:
        if exc.name != "agents.file_agent":
            raise

    return agents


def _prompt_approval(envelope: ActionEnvelope, decision: Decision) -> bool:
    """Blocking interactive prompt. Returns True for approve, False for reject.
    Keeps asking until it gets 'y' or 'n' -- an escalation is exactly the case
    where guessing the human's intent from a malformed answer is not okay."""
    print(f"\n  *** ESCALATION *** action {envelope.action.id}")
    print(f"      agent: {envelope.action.requesting_agent}")
    print(f"      type:  {envelope.action.action_type.value} -> {envelope.action.target}")
    print(f"      rule:  {decision.rule_id} -- {decision.reasoning}")
    print(f"      LLM reasoning (audit-only): {envelope.reasoning!r}")
    while True:
        answer = input("      approve? [y/n]: ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("      please answer y or n")


def _resolve_and_execute(conn, action_id: str, *, approved: bool, by: str):
    decision = esc.resolve(conn, action_id, approved=approved, by=by)
    if decision.status is not DecisionStatus.ALLOW:
        return None
    return executors.run(
        _action_for(conn, action_id), decision,
        outcome_lookup=lambda aid: auditor.outcome_for(conn, aid),
        outcome_record=lambda o: auditor.record_outcome(conn, o),
    )


def _action_for(conn, action_id: str):
    row = db.get_escalation(conn, action_id)
    return ActionEnvelope.model_validate_json(row["envelope_json"]).action


def cmd_run(name: str, session_id: str, *, db_path: str, by: str) -> None:
    if name not in SCENARIOS:
        print(f"Unknown scenario: {name}. Known: {list(SCENARIOS)}", file=sys.stderr)
        sys.exit(1)

    conn = db.init_db(db_path)
    agent_classes = _load_agents()
    workers = {}

    for step in SCENARIOS[name]:
        agent_name, task = step["agent"], step["task"]
        if agent_name not in agent_classes:
            print(f"Unknown agent '{agent_name}' in scenario '{name}'. Known: {list(agent_classes)}",
                  file=sys.stderr)
            sys.exit(1)
        if agent_name not in workers:
            workers[agent_name] = agent_classes[agent_name]()
        worker = workers[agent_name]

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
            approved = _prompt_approval(envelope, decision)
            outcome = _resolve_and_execute(conn, envelope.action.id, approved=approved, by=by)
            if outcome is None:
                print("  outcome: none (rejected by human, never executed)")
            else:
                print(f"  outcome: {outcome.status} -- {outcome.detail}")
        elif decision.status is DecisionStatus.ALLOW:
            print(f"  outcome: {result['outcome'].status} -- {result['outcome'].detail}")
        else:
            print("  outcome: none (denied, never executed)")

    actions_n = conn.execute("select count(*) from actions").fetchone()[0]
    decisions_n = conn.execute("select count(*) from decisions").fetchone()[0]
    print(f"\naudit trail: {actions_n} action(s), {decisions_n} decision(s) recorded in {db_path}")


def cmd_report(session_id: str, db_path: str) -> None:
    """PLAN.md s7 step 10. Standalone (mirrors cmd_run/cmd_resolve above) so
    the report path is independently callable/testable, not just reachable
    through the argparse dispatch below."""
    conn = db.init_db(db_path)
    print(auditor.report(conn, session_id))


def cmd_resolve(*, db_path: str, session_id: str | None, by: str) -> None:
    """Recovery path: prompts for every escalation still status='pending' in
    the db, regardless of which process (or which now-dead process) parked
    it. This is the proof that escalation state survives a kill -- see
    tests/test_escalation_resume.py."""
    conn = db.init_db(db_path)
    rows = esc.pending(conn, session_id=session_id)
    if not rows:
        print(f"no pending escalations in {db_path}"
              + (f" for session {session_id}" if session_id else ""))
        return

    print(f"{len(rows)} pending escalation(s) in {db_path}")
    for row in rows:
        envelope, decision = row["envelope"], row["decision"]
        approved = _prompt_approval(envelope, decision)
        try:
            outcome = _resolve_and_execute(conn, envelope.action.id, approved=approved, by=by)
        except esc.AlreadyResolved:
            # Rows are a snapshot from esc.pending() taken before this loop
            # started prompting; another process resolving the same row in
            # the meantime (e.g. a concurrent `resolve` invocation) must not
            # abandon every row still waiting behind it in this batch.
            print("  skipped: resolved by another process while awaiting this prompt")
            continue
        if outcome is None:
            print("  outcome: none (rejected by human, never executed)")
        else:
            print(f"  outcome: {outcome.status} -- {outcome.detail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a demo scenario")
    p_run.add_argument("--scenario", required=True, choices=list(SCENARIOS))
    p_run.add_argument("--session-id", default="demo1")
    p_run.add_argument("--db", default="guardian.db")  # persistent by design (resumability); pass a
                                                        # fresh --db or --session-id to avoid cross-run
                                                        # accumulation against cumulative policy caps (FIN-002)
    p_run.add_argument("--by", default="cli-operator", help="identity recorded as the approver")

    p_resolve = sub.add_parser("resolve", help="resolve pending escalations (resumable after a kill)")
    p_resolve.add_argument("--db", default="guardian.db")
    p_resolve.add_argument("--session-id", default=None)
    p_resolve.add_argument("--by", default="cli-operator", help="identity recorded as the approver")

    p_report = sub.add_parser("report", help="print the audit trail for a session")
    p_report.add_argument("--session", required=True)
    p_report.add_argument("--db", default="guardian.db")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args.scenario, args.session_id, db_path=args.db, by=args.by)
    elif args.command == "resolve":
        cmd_resolve(db_path=args.db, session_id=args.session_id, by=args.by)
    elif args.command == "report":
        cmd_report(args.session, args.db)
