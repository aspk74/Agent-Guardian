"""End-to-end proof that the open registry (schemas.py + guardian/registry.py,
2026-08-25) delivers what design doc s6's worked example promised: a
customer registers their OWN action_type + Params model, with no code changes
to schemas.py/policy_agent.py/predicates.py/history.py, and it evaluates,
serializes, and stores exactly like a built-in type.

Also covers the two failure modes the open registry specifically introduces
(design doc s12c) and that tests/test_registry.py, tests/test_history_generic_amount.py,
and tests/test_predicates_generic_amount.py were written to make safe in
advance: an audit read for a now-unregistered type, and a parked escalation
surviving (or cleanly failing on) registry drift.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from pydantic import BaseModel

import db
import guardian.escalation as esc
import guardian.policy_agent as policy_agent
import guardian.registry as registry
from guardian.history import SQLiteHistoryQuery
from schemas import Action, ActionEnvelope, Decision, DecisionStatus, UnregisteredActionType


class RefundParams(BaseModel):
    """A customer's own Params model -- deliberately NOT PaymentParams, and
    deliberately using the same amount_cents field name design doc s6's
    worked example assumes, to prove the T20/T23 structural-read fixes
    generalize to a type this repo's core code has never heard of."""
    kind: str = "refund"
    customer_id: str
    amount_cents: int


@contextmanager
def _registered(action_type: str, params_model: type[BaseModel], target_field: str):
    """Registers a type for the duration of the test, then unregisters it --
    guardian/registry.py has no unregister(), by design (register() rejects
    duplicates on purpose), so this manipulates _REGISTRY directly and
    restores it, the same isolation pattern tests/test_registry.py uses."""
    registry.register(action_type, params_model, target_field)
    try:
        yield
    finally:
        registry._REGISTRY.pop(action_type, None)


def _write_policy(tmp_path) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text("""
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
""")
    return str(path)


def test_custom_action_type_evaluates_end_to_end(tmp_path):
    """The design doc s6 worked example, made real: register issue_refund,
    propose one small and one large refund, confirm policy resolution is
    correct for both -- using zero code paths specific to this type."""
    with _registered("issue_refund", RefundParams, "customer_id"):
        policy_path = _write_policy(tmp_path)
        history = SQLiteHistoryQuery(db.init_db(":memory:"))

        small = Action(
            session_id="s1", requesting_agent="refund-agent",
            action_type="issue_refund", target="cust-1",
            params=RefundParams(customer_id="cust-1", amount_cents=5_000),
        )
        big = Action(
            session_id="s1", requesting_agent="refund-agent",
            action_type="issue_refund", target="cust-2",
            params=RefundParams(customer_id="cust-2", amount_cents=25_000),
        )

        small_decision = policy_agent.evaluate(small, history, policy_path=policy_path)
        big_decision = policy_agent.evaluate(big, history, policy_path=policy_path)

        assert small_decision.status is DecisionStatus.ALLOW
        assert small_decision.rule_id == "REFUND-002"
        assert big_decision.status is DecisionStatus.ESCALATE
        assert big_decision.rule_id == "REFUND-001"


def test_custom_action_type_round_trips_through_storage(tmp_path):
    """Proposes, stores, and reconstructs a custom-type Action through the
    real db.py path (not a mock) -- this is what exercises
    schemas.Action._resolve_params_class's registry lookup for real."""
    with _registered("issue_refund", RefundParams, "customer_id"):
        conn = db.init_db(":memory:")
        action = Action(
            session_id="s1", requesting_agent="refund-agent",
            action_type="issue_refund", target="cust-1",
            params=RefundParams(customer_id="cust-1", amount_cents=5_000),
        )
        envelope = ActionEnvelope(
            action=action, reasoning="test", model="test-model", raw_response="{}"
        )
        db.insert_action(conn, envelope)

        reconstructed = db.get_action(conn, action.id)
        assert reconstructed is not None
        assert reconstructed.action_type == "issue_refund"
        assert isinstance(reconstructed.params, RefundParams)
        assert reconstructed.params.amount_cents == 5_000
        # payload_hash is a canonical hash over the full serialized model --
        # this only matches if SerializeAsAny actually serialized RefundParams'
        # own fields rather than silently collapsing to an empty base BaseModel.
        assert reconstructed.payload_hash() == action.payload_hash()


def test_audit_read_raises_clear_exception_when_type_later_unregistered(tmp_path):
    """T21: db.get_action() on a real historical row whose action_type was
    deregistered since it was stored must raise the named, catchable
    schemas.UnregisteredActionType -- not a raw ValueError from Python's Enum
    machinery (that's what happened before this action_type stopped being a
    closed Enum), and not a silent None (which would misreport a real
    historical record as "not found")."""
    conn = db.init_db(":memory:")
    with _registered("issue_refund", RefundParams, "customer_id"):
        action = Action(
            session_id="s1", requesting_agent="refund-agent",
            action_type="issue_refund", target="cust-1",
            params=RefundParams(customer_id="cust-1", amount_cents=5_000),
        )
        envelope = ActionEnvelope(
            action=action, reasoning="test", model="test-model", raw_response="{}"
        )
        db.insert_action(conn, envelope)
    # _registered's context manager has now unregistered issue_refund.

    with pytest.raises(UnregisteredActionType):
        db.get_action(conn, action.id)


def test_multi_row_listing_tolerates_one_unregistered_type_without_losing_others(tmp_path):
    """T21's actual point: guardian.graph.unexecuted_allows() must not let
    ONE row with a deregistered type crash the listing for every OTHER
    stuck action -- the degraded row comes back with action=None and an
    error, everything else reconstructs normally."""
    import guardian.graph as graph
    from schemas import PaymentParams

    conn = db.init_db(":memory:")

    # A normal, still-registered payment that gets stuck.
    good_action = Action(
        session_id="s1", requesting_agent="finance-agent",
        action_type="make_payment", target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=1_000),
    )
    good_decision = Decision(
        action_id=good_action.id, status=DecisionStatus.ALLOW, matched_rules=["X"],
        rule_id="X", policy_version="deadbeef", reasoning="ok", decided_by="policy",
        payload_hash=good_action.payload_hash(),
    )
    db.insert_action(conn, ActionEnvelope(
        action=good_action, reasoning="t", model="m", raw_response="{}"
    ))
    db.insert_decision(conn, good_decision)

    # A refund that gets stuck, whose type is unregistered by the time we read.
    with _registered("issue_refund", RefundParams, "customer_id"):
        bad_action = Action(
            session_id="s1", requesting_agent="refund-agent",
            action_type="issue_refund", target="cust-1",
            params=RefundParams(customer_id="cust-1", amount_cents=5_000),
        )
        bad_decision = Decision(
            action_id=bad_action.id, status=DecisionStatus.ALLOW, matched_rules=["Y"],
            rule_id="Y", policy_version="deadbeef", reasoning="ok", decided_by="policy",
            payload_hash=bad_action.payload_hash(),
        )
        db.insert_action(conn, ActionEnvelope(
            action=bad_action, reasoning="t", model="m", raw_response="{}"
        ))
        db.insert_decision(conn, bad_decision)
    # issue_refund is unregistered again here -- neither action ever executed.

    results = graph.unexecuted_allows(conn)
    assert len(results) == 2

    by_decision_action_id = {r["decision"].action_id: r for r in results}
    good_row = by_decision_action_id[good_action.id]
    bad_row = by_decision_action_id[bad_action.id]

    assert good_row["action"] is not None
    assert good_row["action"].id == good_action.id
    assert good_row["error"] is None

    assert bad_row["action"] is None
    assert "issue_refund" in bad_row["error"]


def test_escalation_survives_registry_drift_between_park_and_resolve(tmp_path):
    """T22: park() an escalation for a custom type, unregister the type,
    THEN resolve() -- resolve() itself only needs the stored payload_hash to
    match (schemas doesn't need to be re-parsed for that comparison logic,
    envelope.action.payload_hash() re-derives from the ALREADY-typed object
    read back via model_validate_json, which is exactly where registry drift
    would bite). Confirms resolve() either succeeds cleanly or raises the
    same named exception -- never a raw crash with no actionable message."""
    conn = db.init_db(":memory:")
    with _registered("issue_refund", RefundParams, "customer_id"):
        action = Action(
            session_id="s1", requesting_agent="refund-agent",
            action_type="issue_refund", target="cust-1",
            params=RefundParams(customer_id="cust-1", amount_cents=5_000),
        )
        envelope = ActionEnvelope(
            action=action, reasoning="test", model="test-model", raw_response="{}"
        )
        decision = Decision(
            action_id=action.id, status=DecisionStatus.ESCALATE, matched_rules=["REFUND-001"],
            rule_id="REFUND-001", policy_version="deadbeef", reasoning="needs approval",
            decided_by="policy", payload_hash=action.payload_hash(),
        )
        esc.park(conn, envelope, decision)
    # issue_refund is unregistered again here -- the escalation is still parked.

    with pytest.raises(UnregisteredActionType):
        esc.resolve(conn, action.id, approved=True, by="tester")
