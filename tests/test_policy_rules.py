"""One matching + one non-matching case per policy.yaml rule id (PLAN.md s10).

Predicates are tested directly against the real policy.yaml rule dicts, not
through evaluate(), so each rule's condition is isolated from the others'
precedence resolution. A FakeHistory that always returns 0 keeps FIN-002
from firing incidentally during the FIN-001/003/004 cases.
"""
from __future__ import annotations

import yaml

from guardian import predicates
from schemas import Action, ActionType, EmailParams, FileParams, PaymentParams


def _rules() -> list[dict]:
    with open("policy.yaml") as f:
        return yaml.safe_load(f)["rules"]


def _rule(rule_id: str) -> dict:
    return next(r for r in _rules() if r["id"] == rule_id)


class FakeHistory:
    """Always reports zero history -- isolates the rule under test."""

    def sum_amount_cents(self, *, agent, action_type, window):
        return 0

    def count(self, *, agent, action_type, window):
        return 0

    def distinct_targets(self, *, agent, action_type, window):
        return 0


HISTORY = FakeHistory()


def _payment(amount_cents: int, target: str = "acme-corp") -> Action:
    return Action(
        session_id="s1",
        requesting_agent="finance-agent",
        action_type=ActionType.MAKE_PAYMENT,
        target=target,
        params=PaymentParams(counterparty=target, amount_cents=amount_cents),
    )


def _email(recipient: str) -> Action:
    return Action(
        session_id="s1",
        requesting_agent="email-agent",
        action_type=ActionType.SEND_EMAIL,
        target=recipient,
        params=EmailParams(recipient=recipient, subject_ref="tmpl-1", body_ref="tmpl-2"),
    )


def _file(action_type: ActionType, path: str) -> Action:
    return Action(
        session_id="s1",
        requesting_agent="file-agent",
        action_type=action_type,
        target=path,
        params=FileParams(path=path),
    )


def test_fin_001_single_payment_over_500():
    rule = _rule("FIN-001")
    assert predicates.rule_matches(_payment(50001), HISTORY, rule) is True
    assert predicates.rule_matches(_payment(50000), HISTORY, rule) is False  # boundary: not gt


def test_fin_002_cumulative_over_1000_in_24h():
    rule = _rule("FIN-002")

    class HistWithSum:
        def sum_amount_cents(self, *, agent, action_type, window):
            return 90000

        def count(self, *, agent, action_type, window):
            return 0

        def distinct_targets(self, *, agent, action_type, window):
            return 0

    history = HistWithSum()
    matching = _payment(20000)  # 90000 + 20000 = 110000 > 100000
    non_matching = _payment(5000)  # 90000 + 5000 = 95000, not > 100000
    assert predicates.rule_matches(matching, history, rule) is True
    assert predicates.rule_matches(non_matching, history, rule) is False


def test_fin_003_unknown_counterparty_denied():
    rule = _rule("FIN-003")
    assert predicates.rule_matches(_payment(100, target="shadowco"), HISTORY, rule) is True
    assert predicates.rule_matches(_payment(100, target="acme-corp"), HISTORY, rule) is False


def test_fin_004_known_counterparty_allowed():
    rule = _rule("FIN-004")
    assert predicates.rule_matches(_payment(100, target="globex"), HISTORY, rule) is True
    assert predicates.rule_matches(_payment(100, target="shadowco"), HISTORY, rule) is False


def test_file_001_prod_delete_denied():
    rule = _rule("FILE-001")
    assert predicates.rule_matches(_file(ActionType.DELETE_FILE, "config.prod.yaml"), HISTORY, rule) is True
    assert predicates.rule_matches(_file(ActionType.DELETE_FILE, "config.dev.yaml"), HISTORY, rule) is False


def test_file_002_workspace_read_allowed():
    rule = _rule("FILE-002")
    assert predicates.rule_matches(_file(ActionType.READ_FILE, "workspace/report.md"), HISTORY, rule) is True
    assert predicates.rule_matches(_file(ActionType.READ_FILE, "etc/passwd"), HISTORY, rule) is False


def test_mail_001_external_recipient_escalated():
    rule = _rule("MAIL-001")
    assert predicates.rule_matches(_email("vendor@external.com"), HISTORY, rule) is True
    assert predicates.rule_matches(_email("alice@internal.example.com"), HISTORY, rule) is False
