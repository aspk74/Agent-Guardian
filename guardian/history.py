"""SQLite-backed HistoryQuery implementation (PLAN.md s3.1, s6).

Counts EXECUTED OUTCOMES, plus PENDING RESERVATIONS (T1 fix, eng review
2026-08-29). A denied or escalated-and-rejected action never produces an
`outcomes` row, so "executed only" used to fall out of the join automatically
(PLAN A4) -- but "executed only" was also exactly the TOCTOU hole: two
concurrent callers could each read history before either one's action had
executed (or even been decided), so neither saw the other, and both got
allowed against a cumulative cap. Folding in still-`pending` rows from the
`reservations` table (db.py) closes that: the moment guardian/sdk.py:_submit()
proposes an action, a second concurrent caller's history read sees it here
even though nothing has executed yet. A reservation that is later denied or
rejected is released (db.release_reservation) and stops counting again, same
as a denied/rejected action never producing an outcome today -- PLAN A4's
"denied attempts never count" invariant is preserved, just extended to cover
the provisional window before a decision exists at all.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import db


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


def _amount_cents(params: dict, *, action_type: str) -> int:
    """Shared amount-extraction/validation for a single params_json blob,
    read structurally rather than through PaymentParams -- binding this to
    one concrete params class meant every cumulative cap only worked for
    payments: a customer-registered action type with its own amount-bearing
    params raised ValidationError here, inside evaluate()'s catch, and became
    a permanent SYS-ERR deny. This JSON was serialized by us from a validated
    model (either an executed outcome's action, or a pending reservation's
    action -- see SQLiteHistoryQuery below) -- it is never LLM prose, so
    nothing about the quarantine split is weakened by reading it directly."""
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
    return amount


class SQLiteHistoryQuery:
    def __init__(self, conn: sqlite3.Connection, *, exclude_action_id: str | None = None):
        """exclude_action_id: see db.get_pending_reservation_totals()'s
        docstring for why this exists -- guardian/sdk.py:_submit() passes
        the action currently being evaluated so its own just-inserted
        reservation (needed so OTHER concurrent callers can see it) isn't
        double-counted against guardian/predicates.py's own explicit
        "history + this action's amount" arithmetic. Every other caller of
        this class (guardian/graph.py, tests) leaves this at the default
        None, which reproduces the exact pre-T1 query shape for outcomes and
        simply includes all pending reservations with nothing excluded."""
        self._conn = conn
        self._exclude_action_id = exclude_action_id

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
        total = sum(
            _amount_cents(json.loads(row["params_json"]), action_type=action_type)
            for row in rows
        )
        # T1 fix: fold in still-pending reservations (db.py) on top of
        # executed outcomes -- see this module's docstring. No window filter
        # on these (get_pending_reservation_totals's own docstring explains
        # why that's the right call), so a reservation counts toward the cap
        # from the moment it's proposed until it's released or converted.
        pending_json = db.get_pending_reservation_totals(
            self._conn, agent=agent, action_type=action_type,
            exclude_action_id=self._exclude_action_id,
        )
        total += sum(
            _amount_cents(json.loads(blob), action_type=action_type)
            for blob in pending_json
        )
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
        pending = db.count_pending_reservations(
            self._conn, agent=agent, action_type=action_type,
            exclude_action_id=self._exclude_action_id,
        )
        return row["n"] + pending

    def distinct_targets(
        self, *, agent: str, action_type: str, window: timedelta
    ) -> int:
        row = self._conn.execute(
            """
            SELECT DISTINCT actions.target AS target
            FROM outcomes
            JOIN actions ON actions.id = outcomes.action_id
            WHERE outcomes.requesting_agent = ?
              AND outcomes.action_type = ?
              AND outcomes.executed_at >= ?
            """,
            (agent, action_type, self._cutoff(window)),
        ).fetchall()
        targets = {r["target"] for r in row}
        targets |= set(
            db.get_pending_reservation_targets(
                self._conn, agent=agent, action_type=action_type,
                exclude_action_id=self._exclude_action_id,
            )
        )
        return len(targets)
