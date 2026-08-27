"""Typed predicates for policy rule matching (PLAN.md s3, s3.4).

Every predicate here reads only typed `Action` fields plus a `HistoryQuery`
result -- never LLM prose. `rule_matches` is deliberately allowed to raise:
a malformed rule or a params/action_type mismatch is a policy-author bug,
and the ONE catch-all in guardian/policy_agent.py turns that into a
fail-closed SYS-ERR deny.
"""
from __future__ import annotations

import fnmatch
from datetime import timedelta
from typing import TYPE_CHECKING

from guardian.history import AmountlessActionType
from schemas import Action

if TYPE_CHECKING:
    from guardian.policy_agent import HistoryQuery


def rule_matches(action: Action, history: "HistoryQuery", rule: dict) -> bool:
    """True if `action` satisfies every condition in `rule["when"]`.

    `action_type` is checked first and short-circuits False on mismatch.
    Every other key present in `when` must independently be true (AND).
    """
    when = rule["when"]

    if when["action_type"] != action.action_type:
        return False

    if "amount_cents_gt" in when:
        # Read amount_cents structurally rather than assuming PaymentParams --
        # the same convention guardian/history.py's sum_amount_cents reads
        # for cumulative caps, so a single-payment cap and a cumulative cap
        # agree on what "has an amount" means for a customer-registered
        # action type. A type whose params carry no amount_cents (or a
        # non-integer one) is a policy-authoring bug: raise so the ONE
        # catch-all in guardian/policy_agent.py turns it into a fail-closed
        # SYS-ERR deny, rather than silently never matching this rule.
        amount = getattr(action.params, "amount_cents", None)
        if amount is None or isinstance(amount, bool) or not isinstance(amount, int):
            raise AmountlessActionType(
                f"action type '{action.action_type}' has no integer "
                f"amount_cents in its params; an amount_cents_gt rule cannot "
                f"apply to it"
            )
        if not (amount > when["amount_cents_gt"]):
            return False

    if "sum_amount_cents_gt" in when:
        # window_hours always accompanies sum_amount_cents_gt in policy.yaml.
        # Indexing (not .get) so a malformed rule missing window_hours raises
        # KeyError -- a real SYS-ERR, not a silently-skipped condition.
        window = timedelta(hours=when["window_hours"])
        historical = history.sum_amount_cents(
            agent=action.requesting_agent,
            action_type=action.action_type,
            window=window,
        )
        # The candidate action has NOT executed yet: the payment that would
        # cross the cap must be judged on historical-sum + its own amount,
        # not on historical-sum alone (off-by-one on which payment trips
        # the rule -- see tests/test_structuring.py).
        candidate_total = historical + action.params.amount_cents
        if not (candidate_total > when["sum_amount_cents_gt"]):
            return False

    if "target_not_in" in when:
        # Case-insensitive: target is an honest echo of whatever the LLM
        # phrased (e.g. "Globex" vs policy.yaml's "globex"), and counterparty
        # identity shouldn't hinge on capitalization the model didn't
        # promise to preserve consistently. Caught by the live demo run --
        # a known counterparty was denied as unknown under FIN-003.
        if action.target.casefold() in {v.casefold() for v in when["target_not_in"]}:
            return False

    if "target_in" in when:
        if action.target.casefold() not in {v.casefold() for v in when["target_in"]}:
            return False

    if "target_glob" in when:
        # fnmatch.fnmatchcase (not fnmatch.fnmatch, which is platform-
        # dependent -- case-sensitive on POSIX, case-insensitive on
        # Windows) on casefolded operands: consistent case-insensitivity
        # everywhere, matching the target_not_in/target_in/target_domain_not_in
        # fix above. A production-file deny relies on this exactly like
        # FIN-003 did -- a mixed-case path must not slip past FILE-001.
        if not fnmatch.fnmatchcase(action.target.casefold(), when["target_glob"].casefold()):
            return False

    if "target_domain_not_in" in when:
        # action.target for send_email is the recipient address. No "@"
        # means split-with-no-separator just returns the whole string via
        # [-1] -- that's fine, it'll fail the "not in" check normally.
        domain = action.target.split("@")[-1]
        if domain.casefold() in {v.casefold() for v in when["target_domain_not_in"]}:
            return False

    return True
