"""T1 (eng review 2026-08-29, design doc 2026-08-24 around the "T1" writeup):
reproduces the exact race the review flagged in guardian/sdk.py:_submit() and
proves it is closed.

Before the fix: _submit() read history, evaluated policy, and only then
wrote anything -- with no transaction spanning those three steps. Two
concurrent @guarded calls in the same session against a cumulative cap (a
FIN-002-style "total payments today < $X" rule) could both call
SQLiteHistoryQuery before either one's action had produced a decision or an
outcome, both see the same (stale) sum, both evaluate the cap as
not-yet-crossed, and both get ALLOW -- letting the pair of payments blow
past a cap that, evaluated correctly one-at-a-time, should have stopped the
second one.

After the fix: _submit() wraps reserve -> read-history -> evaluate in a
`BEGIN IMMEDIATE` transaction, and a new `reservations` table (db.py) makes
an in-flight action visible to a concurrent caller's history read even
before any decision exists. This test uses a `threading.Barrier` to force
maximum overlap between two callers -- both must reach the "about to read
history" point before either is allowed to proceed -- which is exactly the
interleaving that used to defeat the cap.
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from pydantic import BaseModel

import db
import guardian.executors as executors
import guardian.registry as registry
import guardian.sdk as sdk
from guardian.history import SQLiteHistoryQuery
from schemas import Action


class WireParams(BaseModel):
    counterparty: str
    amount_cents: int


_CUMULATIVE_CAP_DENIES_OVER_100_DOLLARS = """
version: 1
rules:
  - id: WIRE-CAP
    description: Total wires by one agent must never exceed $100 in 24h
    when: {action_type: wire_funds, window_hours: 24, sum_amount_cents_gt: 10000}
    then: deny
  - id: WIRE-OK
    description: Individually-small wires are routine
    when: {action_type: wire_funds, amount_cents_gt: -1}
    then: allow
"""


def _write_policy(tmp_path, rules_yaml: str) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(rules_yaml)
    return str(path)


def test_concurrent_guarded_calls_do_not_bypass_cumulative_cap(tmp_path):
    """Two threads, each with its OWN connection to the same db file (the
    real supported SQLite concurrency model -- see
    test_sdk.py::test_concurrent_threads_each_copy_context_do_not_leak_identity's
    docstring for why one connection object shared across threads is not
    the right model here), each propose a $60 wire under the SAME agent and
    session at the same moment. $60 alone is under the $100 cap either way,
    but $60 + $60 = $120 is over it -- so at most one of the two must be
    allowed through; the other must be denied.

    Before the T1 fix this assertion fails nondeterministically (both often
    succeed, since both threads' history reads race before either commits),
    which is exactly the bug the eng review flagged. barrier.wait() forces
    both threads to arrive at the guarded call at the same instant on every
    run, so this reliably reproduces the interleaving rather than depending
    on scheduler luck.
    """
    db_path = str(tmp_path / "race.db")
    db.init_db(db_path).close()
    policy_path = _write_policy(tmp_path, _CUMULATIVE_CAP_DENIES_OVER_100_DOLLARS)

    calls = []

    @sdk.guarded(sdk.ActionSpec(
        action_type="wire_funds", params_model=WireParams, target_field="counterparty",
    ))
    def wire_funds(counterparty: str, amount_cents: int) -> str:
        calls.append((counterparty, amount_cents))
        return f"wired {amount_cents} to {counterparty}"

    try:
        barrier = threading.Barrier(2)
        results: list[tuple[str, str]] = []
        results_lock = threading.Lock()

        def worker(who: str) -> None:
            conn = db.configure(sqlite3.connect(db_path, check_same_thread=False))
            try:
                with sdk.context(session_id="shared-session", requesting_agent="wire-bot",
                                  conn=conn, policy_path=policy_path):
                    barrier.wait(timeout=5)  # maximize overlap between the two callers
                    try:
                        outcome = wire_funds(counterparty="acme-corp", amount_cents=6_000)
                        with results_lock:
                            results.append((who, "allowed", outcome.status))
                    except sdk.ActionDenied as exc:
                        with results_lock:
                            results.append((who, "denied", exc.reasoning))
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(worker, "caller-a")
            fut_b = pool.submit(worker, "caller-b")
            fut_a.result(timeout=10)
            fut_b.result(timeout=10)

        allowed = [r for r in results if r[1] == "allowed"]
        denied = [r for r in results if r[1] == "denied"]
        assert len(results) == 2, f"expected exactly two outcomes, got: {results}"
        assert len(allowed) == 1, (
            f"the cumulative $100 cap was bypassed by concurrency -- expected exactly "
            f"one of the two $60 wires to be allowed (since together they cross the "
            f"cap), got: {results}"
        )
        assert len(denied) == 1, f"expected exactly one denial, got: {results}"

        # The real function only ran for the one that was actually allowed --
        # this is the actual money-moving side effect the cap exists to gate.
        assert len(calls) == 1
        assert calls[0] == ("acme-corp", 6_000)

        # No reservation left behind: the allowed call's reservation was
        # released once its outcome was recorded, and the denied call's
        # reservation was released in the same transaction as its deny.
        conn = db.init_db(db_path)
        remaining = conn.execute("SELECT COUNT(*) AS n FROM reservations").fetchone()["n"]
        assert remaining == 0, "reservations must not leak past a resolved decision"
        conn.close()
    finally:
        registry._REGISTRY.pop("wire_funds", None)
        executors.EXECUTORS.pop("wire_funds", None)


def test_reservation_is_visible_to_a_second_caller_before_any_decision_exists(tmp_path):
    """Narrower, non-threaded proof of the actual mechanism (not just the
    end-to-end outcome above): a reservation inserted by one in-flight call
    is visible to a second connection's history read even though the first
    call has not reached DENY/ALLOW/ESCALATE yet. This is what makes the
    race-closing behavior deterministic rather than a lucky timing outcome
    of BEGIN IMMEDIATE alone."""
    db_path = str(tmp_path / "visibility.db")
    conn1 = db.init_db(db_path)

    action = Action(
        session_id="s1",
        requesting_agent="wire-bot",
        action_type="wire_funds",
        target="acme-corp",
        params=WireParams(counterparty="acme-corp", amount_cents=6_000),
    )
    conn1.execute("BEGIN IMMEDIATE")
    db.insert_reservation(conn1, action)
    # Deliberately NOT committed yet -- conn1 is mid-transaction, simulating
    # _submit() paused between inserting the reservation and finishing
    # evaluate(). A second, independent connection should still see it once
    # conn1 commits (WAL readers see the last committed state; the point
    # being tested is that a *committed* reservation counts, not an
    # in-progress one -- so commit here to observe the intended visibility).
    conn1.commit()

    conn2 = db.init_db(db_path)
    history = SQLiteHistoryQuery(conn2)
    total = history.sum_amount_cents(
        agent="wire-bot", action_type="wire_funds", window=timedelta(hours=24)
    )
    assert total == 6_000, "a pending reservation must be visible to a fresh connection's history read"

    count = history.count(agent="wire-bot", action_type="wire_funds", window=timedelta(hours=24))
    assert count == 1

    targets = history.distinct_targets(
        agent="wire-bot", action_type="wire_funds", window=timedelta(hours=24)
    )
    assert targets == 1

    conn1.close()
    conn2.close()
