"""SQLite-backed HistoryQuery implementation (PLAN.md s3.1, s6).

Counts EXECUTED OUTCOMES ONLY. A denied or escalated-and-rejected action
never produces an `outcomes` row, so "executed only" falls out of the join
rather than needing a status filter someone has to remember (PLAN A4).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from schemas import ActionType, PaymentParams


class SQLiteHistoryQuery:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _cutoff(self, window: timedelta) -> str:
        return (datetime.now(timezone.utc) - window).isoformat()

    def sum_amount_cents(
        self, *, agent: str, action_type: ActionType, window: timedelta
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
            (agent, action_type.value, self._cutoff(window)),
        ).fetchall()
        total = 0
        for row in rows:
            params = PaymentParams.model_validate_json(row["params_json"])
            total += params.amount_cents
        return total

    def count(self, *, agent: str, action_type: ActionType, window: timedelta) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM outcomes
            WHERE requesting_agent = ?
              AND action_type = ?
              AND executed_at >= ?
            """,
            (agent, action_type.value, self._cutoff(window)),
        ).fetchone()
        return row["n"]

    def distinct_targets(
        self, *, agent: str, action_type: ActionType, window: timedelta
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
            (agent, action_type.value, self._cutoff(window)),
        ).fetchone()
        return row["n"]
