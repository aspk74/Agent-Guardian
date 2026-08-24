"""PLAN.md s7 step 13 / s2.2: policy.yaml hot-reload must never mutate an
already-parked escalation's stored Decision or policy_version.

policy_agent.evaluate() already re-reads policy.yaml on every call (no
in-process cache), so "hot reload" for NEW actions is automatic. The actual
risk this test guards is the opposite direction: a policy.yaml edit made
AFTER an action escalated must not retroactively change what that parked
escalation resolves to. guardian/escalation.py's resolve() replays the
stored Decision -- it never calls evaluate() again -- so this is really a
regression test on that invariant, exercised specifically across a real
on-disk policy.yaml edit rather than just an in-memory Decision object.
"""
from __future__ import annotations

import db
import guardian.escalation as esc
import guardian.policy_agent as policy_agent
from guardian.history import SQLiteHistoryQuery
from schemas import Action, ActionEnvelope, DecisionStatus, PaymentParams


def test_parked_escalation_keeps_its_decision_and_policy_version_after_policy_edit(tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "version: 1\n"
        "rules:\n"
        "  - id: FIN-001\n"
        "    description: Single payment over $500 needs a human\n"
        "    when: {action_type: make_payment, amount_cents_gt: 50000}\n"
        "    then: escalate\n"
        "  - id: FIN-003\n"
        "    description: Unknown counterparties are denied\n"
        "    when: {action_type: make_payment, target_not_in: [acme-corp]}\n"
        "    then: deny\n"
    )
    version_before = policy_agent.policy_version(str(policy_path))

    db_path = str(tmp_path / "hotreload.db")
    conn = db.init_db(db_path)
    history = SQLiteHistoryQuery(conn)

    action = Action(
        session_id="hotreload-sess",
        requesting_agent="finance",
        action_type="make_payment",
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=75000),
    )
    envelope = ActionEnvelope(action=action, reasoning="test", model="test", raw_response="{}")
    decision = policy_agent.evaluate(action, history, policy_path=str(policy_path))
    assert decision.status is DecisionStatus.ESCALATE
    assert decision.rule_id == "FIN-001"
    assert decision.policy_version == version_before
    esc.park(conn, envelope, decision)

    # Edit policy.yaml on disk: FIN-001's threshold is raised, so the SAME
    # action would now be routine (would-be FIN-004-style allow) if
    # re-evaluated fresh. It must NOT be re-evaluated -- resolve() replays
    # what was already decided.
    policy_path.write_text(
        "version: 1\n"
        "rules:\n"
        "  - id: FIN-001\n"
        "    description: Single payment over $5000000 needs a human\n"
        "    when: {action_type: make_payment, amount_cents_gt: 500000000}\n"
        "    then: escalate\n"
        "  - id: FIN-004\n"
        "    description: Payments to known counterparties are now routine\n"
        "    when: {action_type: make_payment, target_in: [acme-corp]}\n"
        "    then: allow\n"
    )
    version_after_edit = policy_agent.policy_version(str(policy_path))
    assert version_after_edit != version_before, "test setup bug: edit did not change the hash"

    # A NEW action proposed now picks up the edited policy immediately --
    # this is the "hot reload is automatic for new actions" half of the claim.
    new_action = Action(
        session_id="hotreload-sess",
        requesting_agent="finance",
        action_type="make_payment",
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=75000),
    )
    new_decision = policy_agent.evaluate(new_action, history, policy_path=str(policy_path))
    assert new_decision.status is DecisionStatus.ALLOW
    assert new_decision.rule_id == "FIN-004"
    assert new_decision.policy_version == version_after_edit

    # The OLD parked escalation must still resolve under FIN-001 / the
    # original policy_version -- resolve() never re-runs evaluate().
    resolved = esc.resolve(conn, action.id, approved=True, by="operator-1")
    assert resolved.status is DecisionStatus.ALLOW  # human approved the escalation
    assert resolved.rule_id == "FIN-001"
    assert resolved.policy_version == version_before
    assert resolved.policy_version != version_after_edit
