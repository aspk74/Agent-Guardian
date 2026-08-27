"""guardian/sdk.py: the mode-A in-process integration (design doc
2026-08-24 s6, revised D2 in s13). Exercises the full contract end to end
against the real registry/executors/escalation/db modules (not mocks) --
same principle as tests/test_open_registry_end_to_end.py.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from pydantic import BaseModel

import db
import guardian.executors as executors
import guardian.registry as registry
import guardian.sdk as sdk
from schemas import DecisionStatus


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
