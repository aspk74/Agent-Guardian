"""Fail-closed paths (PLAN.md s3.3): SYS-GAP on zero-match, SYS-ERR on a
predicate exception, and a missing/malformed policy.yaml refusing to start
by letting its exception propagate rather than being swallowed into a
Decision.
"""
from __future__ import annotations

import yaml
import pytest

from guardian.policy_agent import evaluate
from schemas import Action, ActionType, DecisionStatus, FileParams, PaymentParams


class FakeHistory:
    def sum_amount_cents(self, *, agent, action_type, window):
        return 0

    def count(self, *, agent, action_type, window):
        return 0

    def distinct_targets(self, *, agent, action_type, window):
        return 0


def test_sys_gap_on_uncovered_action_type():
    # write_file has zero covering rules in the current policy.yaml -- verify
    # that assumption rather than hardcoding around it.
    with open("policy.yaml") as f:
        rules = yaml.safe_load(f)["rules"]
    assert not any(r["when"].get("action_type") == "write_file" for r in rules), (
        "policy.yaml now covers write_file -- this test's assumption is stale"
    )

    action = Action(
        session_id="s1",
        requesting_agent="file-agent",
        action_type=ActionType.WRITE_FILE,
        target="workspace/notes.txt",
        params=FileParams(path="workspace/notes.txt"),
    )
    decision = evaluate(action, FakeHistory())

    assert decision.status == DecisionStatus.ESCALATE
    assert decision.rule_id == "SYS-GAP"
    assert decision.matched_rules == []
    assert decision.decided_by == "system"


def test_sys_err_on_predicate_exception(tmp_path):
    # A rule using sum_amount_cents_gt without its required window_hours
    # partner raises a real KeyError inside rule_matches -- a policy-author
    # bug, not a malformed-yaml startup failure. evaluate() must catch it
    # and fail closed to deny/SYS-ERR rather than propagate or silently
    # skip the rule.
    bad_policy = tmp_path / "bad_policy.yaml"
    bad_policy.write_text(
        "version: 1\n"
        "rules:\n"
        "  - id: BAD-001\n"
        "    description: malformed rule, missing window_hours\n"
        "    when: {action_type: make_payment, sum_amount_cents_gt: 100000}\n"
        "    then: escalate\n"
    )

    action = Action(
        session_id="s1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=100),
    )
    decision = evaluate(action, FakeHistory(), policy_path=str(bad_policy))

    assert decision.status == DecisionStatus.DENY
    assert decision.rule_id == "SYS-ERR"
    assert decision.matched_rules == []
    assert decision.decided_by == "system"
    assert "policy predicate error" in decision.reasoning


def test_sys_err_on_malformed_then_value(tmp_path):
    # Regression test: a typo'd `then:` value (e.g. "DENYY" instead of "deny")
    # on a rule that DOES match used to raise an uncaught KeyError from
    # _THEN_TO_STATUS lookup during severity resolution -- that lookup lived
    # outside the try/except that only wrapped rule_matches(), so evaluate()
    # crashed the whole graph invocation instead of failing closed. Caught by
    # code review, not by the original test suite -- severity resolution must
    # be inside the same guarded block as predicate matching.
    bad_policy = tmp_path / "bad_then.yaml"
    bad_policy.write_text(
        "version: 1\n"
        "rules:\n"
        "  - id: BAD-002\n"
        "    description: typo'd then value\n"
        "    when: {action_type: read_file, target_glob: \"workspace/**\"}\n"
        "    then: DENYY\n"
    )

    action = Action(
        session_id="s1",
        requesting_agent="file-agent",
        action_type=ActionType.READ_FILE,
        target="workspace/report.md",
        params=FileParams(path="workspace/report.md"),
    )
    decision = evaluate(action, FakeHistory(), policy_path=str(bad_policy))

    assert decision.status == DecisionStatus.DENY
    assert decision.rule_id == "SYS-ERR"
    assert "policy predicate error" in decision.reasoning


def test_missing_policy_file_propagates_uncaught():
    # policy.yaml missing/unreadable is a STARTUP-time failure (PLAN.md
    # s3.3: "refuse to start"), not a Decision. The exception must escape
    # evaluate() rather than being turned into deny/SYS-ERR.
    action = Action(
        session_id="s1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target="acme-corp",
        params=PaymentParams(counterparty="acme-corp", amount_cents=100),
    )
    with pytest.raises(FileNotFoundError):
        evaluate(action, FakeHistory(), policy_path="/nonexistent/policy.yaml")
