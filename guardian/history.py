"""SQLite-backed HistoryQuery implementation (PLAN.md s3.1, s6).

Counts EXECUTED OUTCOMES ONLY. A denied or escalated-and-rejected action
never produces an `outcomes` row, so "executed only" falls out of the join
rather than needing a status filter someone has to remember (PLAN A4).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone


class AmountlessActionType(Exception):
    """A rule declared an amount-based condition (`amount_cents_gt` in
    guardian/predicates.py, or `sum_amount_cents_gt` here) on an action type
    whose params carry no integer `amount_cents` field.

    This is a policy-authoring error, not a runtime condition, so it is raised
    rather than swallowed: guardian/policy_agent.py's one deliberate broad catch
    turns it into a fail-closed SYS-ERR deny with the message attached. Silently
    treating a missing amount as zero would be strictly worse -- a cap that reads
    as "never reached" is a cap that does not exist, and it would fail exactly
    the way PLAN.md s9.3 structuring is designed to prevent.
    """


class SQLiteHistoryQuery:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _cutoff(self, window: timedelta) -> str:
        return (datetime.now(timezone.utc) - window).isoformat()

    def sum_amount_cents(
        self, *, agent: str, action_type: str, window: timedelta
    ) -> int:
        rows = self._conn.execute(
            """
            SELECT actions.params_json AS params_json
            FROM outcomes
            JOIN actions ON actions.id = outcomes.action_id
            WHERE outcomes.requesting_agent = ?
              AND outcomes.action_type = ?
              AND outcomes.executed_at >= ?
            """,
            (agent, action_type, self._cutoff(window)),
        ).fetchall()
        # Read `amount_cents` structurally rather than through PaymentParams.
        # Binding this to one concrete params class meant every cumulative cap
        # only worked for payments: a customer-registered action type with its
        # own amount-bearing params raised ValidationError here, inside
        # evaluate()'s catch, and became a permanent SYS-ERR deny. The rows are
        # homogeneous by construction (the query filters on a single
        # action_type), and this JSON was serialized by us from a validated
        # model -- it is never LLM prose, so nothing about the quarantine split
        # is weakened by reading it directly.
        total = 0
        for row in rows:
            params = json.loads(row["params_json"])
            if "amount_cents" not in params:
                raise AmountlessActionType(
                    f"action type '{action_type}' has no amount_cents in its "
                    f"params; a sum_amount_cents_gt rule cannot apply to it"
                )
            amount = params["amount_cents"]
            # Integer cents only (PLAN.md C1). A float here means someone wrote a
            # params model that broke the invariant, and silently summing floats
            # would reintroduce the rounding drift the integer rule exists to stop.
            if not isinstance(amount, int) or isinstance(amount, bool):
                raise AmountlessActionType(
                    f"action type '{action_type}' has non-integer amount_cents "
                    f"({amount!r}); integer cents are required"
                )
            total += amount
        return total

    def count(self, *, agent: str, action_type: str, window: timedelta) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM outcomes
            WHERE requesting_agent = ?
              AND action_type = ?
              AND executed_at >= ?
            """,
            (agent, action_type, self._cutoff(window)),
        ).fetchone()
        return row["n"]

    def distinct_targets(
        self, *, agent: str, action_type: str, window: timedelta
    ) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(DISTINCT actions.target) AS n
            FROM outcomes
            JOIN actions ON actions.id = outcomes.action_id
            WHERE outcomes.requesting_agent = ?
              AND outcomes.action_type = ?
              AND outcomes.executed_at >= ?
            """,
            (agent, action_type, self._cutoff(window)),
        ).fetchone()
        return row["n"]
