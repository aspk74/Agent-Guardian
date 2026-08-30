"""guardian/sdk.py: the mode-A in-process integration (design doc
2026-08-24 s6, revised D2 in s13). Exercises the full contract end to end
against the real registry/executors/escalation/db modules (not mocks) --
same principle as tests/test_open_registry_end_to_end.py.
"""
from __future__ import annotations

import contextvars
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from pydantic import BaseModel, ValidationError

import db
import guardian.executors as executors
import guardian.registry as registry
import guardian.sdk as sdk
from schemas import DecisionStatus


def _init_db_cross_thread(path: str = ":memory:") -> sqlite3.Connection:
    """Like db.init_db(), but with check_same_thread=False -- needed only by
    the ThreadPoolExecutor tests below, which (correctly, per context()'s
    own docstring) hand this same connection to a worker thread via
    copy_context().run(). sqlite3 connections are thread-affine by default
    regardless of contextvars; db.configure()'s own docstring notes
    dashboard.py needs the same flag for the same reason. This is a test-only
    concern -- a customer wiring guardian into a real thread pool needs a
    connection opened the same way, which is exactly the kind of detail the
    documented wiring pattern in context()'s docstring should not have to
    spell out twice, so it is called out here instead."""
    return db.configure(sqlite3.connect(path, check_same_thread=False))


class RefundParams(BaseModel):
    customer_id: str
    amount_cents: int


@contextmanager
def _guarded_refund(calls: list):
    """Registers a fresh `issue_refund` action type + executor for the
    duration of one test, then tears both down -- guardian/registry.py and
    guardian/executors.py both reject a second registration on purpose
    (DuplicateRegistration / DuplicateExecutor), so tests can't just
    re-decorate at import time the way a real customer module would."""

    @sdk.guarded(sdk.ActionSpec(
        action_type="issue_refund", params_model=RefundParams, target_field="customer_id",
    ))
    def issue_refund(customer_id: str, amount_cents: int) -> str:
        calls.append((customer_id, amount_cents))
        return f"refunded {amount_cents} to {customer_id}"

    try:
        yield issue_refund
    finally:
        registry._REGISTRY.pop("issue_refund", None)
        executors.EXECUTORS.pop("issue_refund", None)


def _write_policy(tmp_path, rules_yaml: str) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(rules_yaml)
    return str(path)


_ALLOW_SMALL_ESCALATE_BIG = """
version: 1
rules:
  - id: REFUND-001
    description: Refunds over $200 need a human
    when: {action_type: issue_refund, amount_cents_gt: 20000}
    then: escalate
  - id: REFUND-002
    description: Small refunds are routine
    when: {action_type: issue_refund, amount_cents_gt: -1}
    then: allow
"""

_DENY_ALL = """
version: 1
rules:
  - id: REFUND-DENY
    description: No refunds today
    when: {action_type: issue_refund, amount_cents_gt: -1}
    then: deny
"""


def test_guarded_registers_action_type_and_executor():
    calls = []
    with _guarded_refund(calls):
        assert registry.is_registered("issue_refund")
        assert "issue_refund" in executors.EXECUTORS
    assert not registry.is_registered("issue_refund")
    assert "issue_refund" not in executors.EXECUTORS


def test_call_with_no_active_context_fails_closed():
    calls = []
    with _guarded_refund(calls) as issue_refund:
        with pytest.raises(sdk.NoActiveContext):
            issue_refund(customer_id="cust-1", amount_cents=1_000)
    assert calls == []


def test_allow_calls_through_and_records_outcome(tmp_path):
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            outcome = issue_refund(customer_id="cust-1", amount_cents=5_000)

    assert calls == [("cust-1", 5_000)]
    assert outcome.status == "success"
    assert "refunded 5000 to cust-1" in outcome.detail


def test_deny_raises_and_never_calls_through(tmp_path):
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _DENY_ALL)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with pytest.raises(sdk.ActionDenied):
                issue_refund(customer_id="cust-1", amount_cents=5_000)

    assert calls == []  # the real function must never run on DENY


def test_escalate_parks_and_raises_pending_without_calling_through(tmp_path):
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with pytest.raises(sdk.ActionPending) as excinfo:
                issue_refund(customer_id="cust-2", amount_cents=25_000)

    assert calls == []
    pending = db.get_escalation(conn, excinfo.value.action_id)
    assert pending is not None
    assert pending["status"] == "pending"


def test_retry_while_still_pending_raises_pending_again_without_new_escalation(tmp_path):
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with pytest.raises(sdk.ActionPending) as first:
                issue_refund(customer_id="cust-2", amount_cents=25_000)
            with pytest.raises(sdk.ActionPending) as second:
                issue_refund(customer_id="cust-2", amount_cents=25_000)

    # Same underlying escalation, not a second one -- the semantic-key match
    # in guardian.sdk._find_matching_escalation is what prevents a duplicate
    # park() on every retry.
    assert first.value.action_id == second.value.action_id
    assert len(db.get_escalations_for_session(conn, "s1")) == 1


def test_retry_after_approval_executes_the_real_function(tmp_path):
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with pytest.raises(sdk.ActionPending) as first:
                issue_refund(customer_id="cust-2", amount_cents=25_000)

            import guardian.escalation as esc
            esc.resolve(conn, first.value.action_id, approved=True, by="alice")

            outcome = issue_refund(customer_id="cust-2", amount_cents=25_000)

    assert calls == [("cust-2", 25_000)]  # the real function ran exactly once
    assert outcome.status == "success"


def test_retry_after_approval_is_idempotent_on_repeated_calls(tmp_path):
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with pytest.raises(sdk.ActionPending) as first:
                issue_refund(customer_id="cust-2", amount_cents=25_000)

            import guardian.escalation as esc
            esc.resolve(conn, first.value.action_id, approved=True, by="alice")

            issue_refund(customer_id="cust-2", amount_cents=25_000)
            issue_refund(customer_id="cust-2", amount_cents=25_000)

    # executors.run()'s outcome_lookup guard makes a second/third call after
    # approval a no-op re-read, never a second real execution.
    assert calls == [("cust-2", 25_000)]


def test_retry_after_rejection_raises_denied(tmp_path):
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with pytest.raises(sdk.ActionPending) as first:
                issue_refund(customer_id="cust-2", amount_cents=25_000)

            import guardian.escalation as esc
            esc.resolve(conn, first.value.action_id, approved=False, by="alice")

            with pytest.raises(sdk.ActionDenied):
                issue_refund(customer_id="cust-2", amount_cents=25_000)

    assert calls == []


def test_different_payloads_produce_independent_escalations(tmp_path):
    """Two distinct large refunds must never be conflated into one parked
    approval -- approving one must not silently authorize the other."""
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with pytest.raises(sdk.ActionPending) as first:
                issue_refund(customer_id="cust-2", amount_cents=25_000)
            with pytest.raises(sdk.ActionPending) as second:
                issue_refund(customer_id="cust-3", amount_cents=30_000)

    assert first.value.action_id != second.value.action_id
    assert len(db.get_escalations_for_session(conn, "s1")) == 2


def test_uncovered_executors_flags_registry_only_registration():
    """registry.register() without guarded()/register_executor() is exactly
    the gap registry.uncovered_executors() (7b) exists to catch at startup,
    since it can no longer happen through guarded() itself."""
    registry.register("orphan_type", RefundParams, "customer_id")
    try:
        assert "orphan_type" in registry.uncovered_executors()
    finally:
        registry._REGISTRY.pop("orphan_type", None)


def test_invalid_params_raise_typed_sdk_exception_not_raw_pydantic(tmp_path):
    """Eng review finding #7: wrapper() used to let pydantic's own
    ValidationError escape directly when the caller passed a malformed or
    wrong-type argument (e.g. a non-numeric amount_cents). A customer's
    calling code should only ever need to catch guardian.sdk exceptions at
    this boundary, not know pydantic is used underneath -- so this must
    surface as sdk.InvalidActionParams, with the original ValidationError
    still reachable via __cause__ for anyone who wants the low-level
    detail."""
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with pytest.raises(sdk.InvalidActionParams) as excinfo:
                issue_refund(customer_id="cust-1", amount_cents="not-a-number")

    assert excinfo.value.action_type == "issue_refund"
    assert isinstance(excinfo.value.__cause__, ValidationError)
    assert calls == []  # the real function must never run


# --- contextvar wiring across ThreadPoolExecutor (eng review finding #6) ---
#
# ThreadPoolExecutor.submit() does NOT propagate contextvars to the worker
# thread it runs the callable on -- the callable sees each ContextVar's
# *default*, not whatever the submitting thread had bound, even though the
# submitting thread's `with context(...):` block is still open on the stack.
# This is upstream contextvars/concurrent.futures behavior (unlike
# asyncio.to_thread, which does copy the context), not something Guardian
# can fix from inside guardian/sdk.py -- see context()'s own docstring,
# which documents the correct fix: capture the context with
# contextvars.copy_context() before submit() and dispatch via ctx.run.
#
# The two tests below prove both halves of the review's requested fix: the
# naive pattern demonstrably fails today, and the documented pattern
# demonstrably fixes it -- using the exact same guarded function and pool in
# both, so the only variable is the dispatch method.

def test_threadpool_submit_naive_loses_context_and_raises(tmp_path):
    """The failure mode the review asked to demonstrate: context() is bound
    in the main thread, but executor.submit(fn, ...) hands the worker thread
    a fresh contextvars.Context where `_current` is back to its default
    (None) -- so the guarded call inside the worker thread fails exactly as
    if no context() had ever been entered, even though one plainly has,
    one frame up the (real) call stack."""
    calls = []
    conn = db.init_db(":memory:")
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    issue_refund, customer_id="cust-1", amount_cents=5_000
                )
                with pytest.raises(sdk.NoActiveContext):
                    future.result()

    assert calls == []  # the real function must never run


def test_threadpool_submit_via_copy_context_sees_bound_identity(tmp_path):
    """The documented fix: contextvars.copy_context() snapshots the calling
    thread's bound context (including guardian.sdk's `_current`), and
    dispatching via `executor.submit(ctx.run, fn, ...)` instead of
    `executor.submit(fn, ...)` replays that snapshot inside the worker
    thread. Same pool, same guarded function, same policy as the naive test
    above -- only the dispatch call changes, and that alone is the
    difference between NoActiveContext and a correct ALLOW."""
    calls = []
    conn = _init_db_cross_thread()
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    with _guarded_refund(calls) as issue_refund:
        with sdk.context(session_id="s1", requesting_agent="refund-bot", conn=conn,
                          policy_path=policy_path):
            ctx = contextvars.copy_context()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    ctx.run, issue_refund, customer_id="cust-1", amount_cents=5_000
                )
                outcome = future.result()

    assert calls == [("cust-1", 5_000)]
    assert outcome.status == "success"
    assert "refunded 5000 to cust-1" in outcome.detail


def test_concurrent_threads_each_copy_context_do_not_leak_identity(tmp_path):
    """Thread-isolation, flagged adjacent to finding #6/#7: two threads each
    entering their own context() (different session_id/requesting_agent)
    and dispatching their guarded call via copy_context().run() must never
    see each other's identity, even when both contexts are open
    concurrently and both worker threads are running at the same time. If
    `_current` were a plain global instead of a contextvar (or if
    copy_context() somehow shared state across threads), one thread could
    stamp its session/agent identity onto the other's action -- exactly the
    identity-spoofing class of bug the module docstring's point 2 exists to
    prevent, just via a concurrency path instead of a framework/LLM one.

    Each thread gets its OWN connection to the same file, not one connection
    object shared across threads: since T1's fix (guardian/sdk.py:_submit()),
    each guarded call opens a real `BEGIN IMMEDIATE` transaction, and a single
    sqlite3.Connection object has exactly one transaction slot -- two threads
    issuing BEGIN on the SAME connection object at once race for that one
    slot and the loser gets "cannot start a transaction within a transaction"
    regardless of timing, which has nothing to do with the identity-isolation
    property this test exists to prove. Separate connections to one file is
    also just the correct/only supported way to get real concurrent SQLite
    writers -- same pattern tests/test_escalation_resume.py already uses."""
    db_path = str(tmp_path / "concurrent.db")
    db.init_db(db_path).close()  # create the file/schema once, up front
    policy_path = _write_policy(tmp_path, _ALLOW_SMALL_ESCALATE_BIG)
    calls = []

    barrier = threading.Barrier(2)

    def run_as(session_id: str, requesting_agent: str, customer_id: str, issue_refund):
        # check_same_thread=False: this connection is opened on the outer
        # pool's worker thread but the actual guarded call is dispatched one
        # level deeper via ctx.run() on an inner pool thread (below), same
        # cross-thread-handoff shape _init_db_cross_thread's docstring
        # explains -- it is still only ever used by one thread AT A TIME
        # (never concurrently with itself), just not always the thread that
        # opened it.
        conn = db.configure(sqlite3.connect(db_path, check_same_thread=False))
        try:
            with sdk.context(session_id=session_id, requesting_agent=requesting_agent,
                              conn=conn, policy_path=policy_path):
                ctx = contextvars.copy_context()

                def _call():
                    barrier.wait(timeout=5)  # maximize overlap between the two threads
                    return issue_refund(customer_id=customer_id, amount_cents=1_000)

                with ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(ctx.run, _call).result()
        finally:
            conn.close()

    with _guarded_refund(calls) as issue_refund:
        with ThreadPoolExecutor(max_workers=2) as outer_pool:
            fut_a = outer_pool.submit(
                run_as, "session-a", "agent-a", "cust-a", issue_refund
            )
            fut_b = outer_pool.submit(
                run_as, "session-b", "agent-b", "cust-b", issue_refund
            )
            outcome_a = fut_a.result()
            outcome_b = fut_b.result()

        assert outcome_a.status == "success"
        assert outcome_b.status == "success"
        assert outcome_a.requesting_agent == "agent-a"
        assert outcome_b.requesting_agent == "agent-b"
        assert sorted(calls) == [("cust-a", 1_000), ("cust-b", 1_000)]

        # Each session recorded only its own action, under its own identity --
        # no cross-contamination of session_id/requesting_agent between threads.
        # (Still inside _guarded_refund's `with`: db.get_action reconstructs a
        # typed Action via guardian.registry, which is only populated for the
        # duration of this block -- see _guarded_refund's own docstring. A
        # fresh read-only connection is fine here since both writers are done.)
        conn = db.init_db(db_path)
        action_a = db.get_action(conn, outcome_a.action_id)
        action_b = db.get_action(conn, outcome_b.action_id)
        assert action_a.session_id == "session-a"
        assert action_a.requesting_agent == "agent-a"
        assert action_b.session_id == "session-b"
        assert action_b.requesting_agent == "agent-b"
        conn.close()
