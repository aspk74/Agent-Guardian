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

-- Single-row table recording which schema generation this file was written by.
-- Added before the first public release deliberately: once someone outside this
-- repo holds a guardian.db, a schema change with no version to branch on has no
-- safe migration path, and the audit trail is the one thing that must never be
-- dropped and recreated.
CREATE TABLE IF NOT EXISTS schema_version (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL
);
"""

# Bump when _SCHEMA changes shape, and add the corresponding step to _migrate().
SCHEMA_VERSION = 1


class SchemaTooNew(Exception):
    """The database was written by a newer Guardian than this one. Refuse to
    open it rather than silently reading columns we don't understand -- same
    fail-closed posture policy.yaml gets at startup (PLAN.md s3.3)."""


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to SCHEMA_VERSION, or stamp a fresh one.

    A file predating the schema_version table reads back as no row at all.
    That is indistinguishable from a brand-new database by inspection, so both
    are stamped at the current version -- safe only because every table above
    is CREATE TABLE IF NOT EXISTS and version 1 is additive over the original
    four-table layout. The first version that is NOT additive must branch here
    on the stored value instead of assuming this.
    """
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)", (SCHEMA_VERSION,)
        )
        return
    found = row["version"] if isinstance(row, sqlite3.Row) else row[0]
    if found > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"guardian.db is schema v{found}, this build understands v{SCHEMA_VERSION}"
        )
    # found < SCHEMA_VERSION: future migration steps land here, in order.


def configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply the connection settings, schema, and version stamp that EVERY
    caller needs. Shared so a second entry point can't quietly open the same
    database with different durability settings -- dashboard.py builds its own
    connection (it needs check_same_thread=False) and must route through here.
    """
    conn.row_factory = sqlite3.Row
    # WAL lets readers proceed during a write instead of blocking on the
    # database-level lock. Persists in the file once set, so it survives
    # reconnects. Required before anything concurrent touches this database:
    # the default rollback journal serializes readers against writers, which
    # turns every audit read into contention with the write path.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def init_db(path: str = "guardian.db") -> sqlite3.Connection:
    return configure(sqlite3.connect(path))


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
            action.action_type,
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
            outcome.action_type,
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
        action_type=row["action_type"],
        status=row["status"],
        detail=row["detail"],
        executed_at=row["executed_at"],
    )


def get_action(conn: sqlite3.Connection, action_id: str) -> Action | None:
    """Reconstructs the full typed Action (params included) from the actions
    table. Action's own _resolve_params_class validator (schemas.py) looks up
    the stored action_type in guardian/registry.py to know which concrete
    Params class the params JSON validates against, so this works for any
    registered action_type without a per-type branch here -- unlike
    guardian/auditor.py's report(), which only ever needs PaymentParams for
    one report line and hardcodes that.

    Raises schemas.UnregisteredActionType if action_type isn't registered in
    THIS process -- a real historical record that can no longer be fully
    reconstructed, not a "not found" (that's the row-is-None case above,
    which returns None instead)."""
    row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        return None
    return Action(
        id=row["id"],
        session_id=row["session_id"],
        requesting_agent=row["requesting_agent"],
        action_type=row["action_type"],
        target=row["target"],
        params=json.loads(row["params_json"]),
        created_at=row["created_at"],
    )


def get_unexecuted_allows(
    conn: sqlite3.Connection, session_id: str | None = None
) -> list[sqlite3.Row]:
    """Auto-allowed actions (decisions.status='allow') with no matching
    outcome -- the executor raised inside guardian/graph.py's execute node,
    for a non-escalated action. Distinct from get_unexecuted_approvals()
    above (the escalated-then-human-approved case, tracked in the
    escalations table): resolve()'s final human decision is never written
    to the decisions table (only to escalations.status/resolved_by/at), so
    an 'allow' row here can only ever come from a direct
    policy_agent.evaluate() decision -- the two queries never overlap."""
    if session_id is None:
        return conn.execute(
            """
            SELECT decisions.* FROM decisions
            LEFT JOIN outcomes ON outcomes.action_id = decisions.action_id
            WHERE decisions.status = 'allow' AND outcomes.action_id IS NULL
            ORDER BY decisions.decided_at
            """
        ).fetchall()
    return conn.execute(
        """
        SELECT decisions.* FROM decisions
        JOIN actions ON actions.id = decisions.action_id
        LEFT JOIN outcomes ON outcomes.action_id = decisions.action_id
        WHERE decisions.status = 'allow' AND outcomes.action_id IS NULL
          AND actions.session_id = ?
        ORDER BY decisions.decided_at
        """,
        (session_id,),
    ).fetchall()


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


def get_actions_for_session(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """Raw rows, chronological. Used by the Reporter (guardian/auditor.py) to
    build the audit trail; reconstruction into Action/ActionEnvelope happens
    there, not here, since the report also wants the plain row fields (e.g.
    reasoning) without re-parsing params twice."""
    return conn.execute(
        "SELECT * FROM actions WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()


def get_decisions_for_session(conn: sqlite3.Connection, session_id: str) -> list[Decision]:
    """Decisions join to actions on action_id -- decisions carry no
    session_id column of their own (see _SCHEMA above)."""
    rows = conn.execute(
        """
        SELECT decisions.*
        FROM decisions
        JOIN actions ON actions.id = decisions.action_id
        WHERE actions.session_id = ?
        ORDER BY decisions.decided_at
        """,
        (session_id,),
    ).fetchall()
    return [
        Decision(
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
        for row in rows
    ]


def get_outcomes_for_session(conn: sqlite3.Connection, session_id: str) -> list[Outcome]:
    """Outcomes join to actions on action_id, same reasoning as decisions above.

    Outcome.action_type is a plain str field (no registry lookup, no
    _resolve_params_class validator -- that's an Action/params thing), so
    unlike get_action() there is no UnregisteredActionType failure mode here
    to tolerate: a historical action_type that's no longer registered is
    still perfectly valid Outcome data."""
    rows = conn.execute(
        """
        SELECT outcomes.*
        FROM outcomes
        JOIN actions ON actions.id = outcomes.action_id
        WHERE actions.session_id = ?
        ORDER BY outcomes.executed_at
        """,
        (session_id,),
    ).fetchall()
    return [
        Outcome(
            action_id=row["action_id"],
            requesting_agent=row["requesting_agent"],
            action_type=row["action_type"],
            status=row["status"],
            detail=row["detail"],
            executed_at=row["executed_at"],
        )
        for row in rows
    ]


def get_escalations_for_session(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """Escalations DO carry session_id directly (unlike decisions/outcomes),
    so this is a plain filter, not a join."""
    return conn.execute(
        "SELECT * FROM escalations WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()


def get_escalation_counts_by_agent(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Cross-session signal for the Reporter's informational pattern flags
    (PLAN.md s6, s7 step 11) -- NOT used by policy_agent.py. Joins to actions
    for requesting_agent since escalations itself doesn't carry the agent
    name. Groups by agent across ALL sessions, which is the point: a single
    session's report can flag an agent that's been escalated repeatedly
    elsewhere."""
    return conn.execute(
        """
        SELECT actions.requesting_agent AS agent,
               COUNT(*) AS escalation_count,
               COUNT(DISTINCT escalations.session_id) AS session_count
        FROM escalations
        JOIN actions ON actions.id = escalations.action_id
        WHERE escalations.created_at >= ?
        GROUP BY actions.requesting_agent
        ORDER BY escalation_count DESC
        """,
        (since,),
    ).fetchall()


def get_payment_totals_by_agent(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Cross-session executed-payment volume per agent, for the same
    informational report flags. Executed outcomes only, matching the
    'denied attempts never count' rule elsewhere in this file (PLAN A4) --
    though here it's for display, not enforcement."""
    rows = conn.execute(
        """
        SELECT outcomes.requesting_agent AS agent,
               actions.params_json AS params_json
        FROM outcomes
        JOIN actions ON actions.id = outcomes.action_id
        WHERE outcomes.action_type = ?
          AND outcomes.status = 'success'
          AND outcomes.executed_at >= ?
        """,
        (ActionType.MAKE_PAYMENT, since),
    ).fetchall()
    totals: dict[str, int] = {}
    for row in rows:
        params = json.loads(row["params_json"])
        totals[row["agent"]] = totals.get(row["agent"], 0) + params.get("amount_cents", 0)
    # Plain dicts, not sqlite3.Row -- these are aggregated in Python, not by
    # the query, so there's no underlying cursor row to wrap.
    return [
        {"agent": agent, "total_cents": cents}
        for agent, cents in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


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


def get_unexecuted_approvals(
    conn: sqlite3.Connection, session_id: str | None = None
) -> list[sqlite3.Row]:
    """Escalations approved by a human but with no matching outcomes row --
    the executor raised after approval was already committed (see
    guardian/escalation.py's ExecutionFailed). LEFT JOIN against outcomes
    (whose action_id is a PRIMARY KEY) rather than a NOT IN subquery, so this
    stays a straightforward indexed join, not a subquery scan."""
    if session_id is None:
        return conn.execute(
            """
            SELECT escalations.* FROM escalations
            LEFT JOIN outcomes ON outcomes.action_id = escalations.action_id
            WHERE escalations.status = 'approved' AND outcomes.action_id IS NULL
            ORDER BY escalations.created_at
            """
        ).fetchall()
    return conn.execute(
        """
        SELECT escalations.* FROM escalations
        LEFT JOIN outcomes ON outcomes.action_id = escalations.action_id
        WHERE escalations.status = 'approved' AND outcomes.action_id IS NULL
          AND escalations.session_id = ?
        ORDER BY escalations.created_at
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
) -> bool:
    """Atomically transitions a pending escalation to resolved. The
    WHERE clause's status='pending' guard (not a separate read-then-write)
    is what makes this safe under concurrent resolvers: two callers racing
    on the same action_id can both pass an earlier SELECT-based pending
    check, but only one UPDATE can ever match this WHERE clause, since
    SQLite serializes writes. Returns True if this call was the one that
    resolved it, False if another resolver won the race first."""
    cursor = conn.execute(
        """
        UPDATE escalations
        SET status = ?, resolved_by = ?, resolved_at = ?
        WHERE action_id = ? AND status = 'pending'
        """,
        (status, resolved_by, resolved_at, action_id),
    )
    conn.commit()
    return cursor.rowcount == 1
