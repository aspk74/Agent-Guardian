"""The only module in this repo permitted to import an effector library.

Workers propose Action objects but never call this module directly -- only
the graph (guardian/graph.py) does, after a policy decision. Enforced by
tests/test_no_effector_imports.py, which AST-scans every other module.
"""
from __future__ import annotations

from schemas import Action, ActionType, Decision, DecisionStatus, Outcome


class NotAuthorized(Exception):
    """run() called with a Decision that isn't ALLOW."""


class PayloadMismatchError(Exception):
    """The action's current payload_hash doesn't match what was decided on.
    Guards against a mutated action being executed under a stale approval."""


class ExecutorMissing(Exception):
    """No executor registered for this action_type. A config bug, not a
    policy outcome -- if this fires, the ActionType enum grew a member
    that EXECUTORS never learned about."""


class ExecutionFailed(Exception):
    """The executor itself raised while performing the action (e.g. a real
    Stripe/SMTP call failing) -- distinct from this module's other three
    exceptions above, which are run()'s own pre-flight guards (tamper/config
    signals that a retry can't fix, so they're raised directly, never
    wrapped here). By the time this fires, decision.status was already
    ALLOW and this action's Decision is already durably recorded by the
    caller (guardian/escalation.py's resolve(), or guardian/graph.py's
    record_decision node) -- nothing is lost. Calling run() again once the
    underlying problem is resolved is the correct retry: outcome_lookup's
    idempotency guard (below) makes that safe even if the first attempt
    partially succeeded before raising. Wraps the original exception as
    __cause__."""

    def __init__(self, action_id: str, original: BaseException):
        super().__init__(f"action {action_id} execution failed: {original}")
        self.action_id = action_id


def _simulate_payment(action: Action) -> Outcome:
    p = action.params
    detail = f"paid {p.counterparty} ${p.amount_cents / 100:.2f} (simulated)"
    return Outcome(action_id=action.id, requesting_agent=action.requesting_agent,
                   action_type=action.action_type, status="success", detail=detail)


def _simulate_email(action: Action) -> Outcome:
    p = action.params
    detail = f"sent email to {p.recipient} (subject_ref={p.subject_ref}) (simulated)"
    return Outcome(action_id=action.id, requesting_agent=action.requesting_agent,
                   action_type=action.action_type, status="success", detail=detail)


def _simulate_read(action: Action) -> Outcome:
    p = action.params
    detail = f"read {p.path} (simulated)"
    return Outcome(action_id=action.id, requesting_agent=action.requesting_agent,
                   action_type=action.action_type, status="success", detail=detail)


def _simulate_write(action: Action) -> Outcome:
    p = action.params
    detail = f"wrote {p.path} (simulated)"
    return Outcome(action_id=action.id, requesting_agent=action.requesting_agent,
                   action_type=action.action_type, status="success", detail=detail)


def _simulate_delete(action: Action) -> Outcome:
    p = action.params
    detail = f"deleted {p.path} (simulated)"
    return Outcome(action_id=action.id, requesting_agent=action.requesting_agent,
                   action_type=action.action_type, status="success", detail=detail)


EXECUTORS = {
    ActionType.MAKE_PAYMENT: _simulate_payment,
    ActionType.SEND_EMAIL: _simulate_email,
    ActionType.READ_FILE: _simulate_read,
    ActionType.WRITE_FILE: _simulate_write,
    ActionType.DELETE_FILE: _simulate_delete,
}


def run(action: Action, decision: Decision, *, outcome_lookup, outcome_record) -> Outcome:
    """outcome_lookup(action_id) -> Outcome | None and outcome_record(Outcome) -> None
    are injected so this module never imports db.py or auditor.py directly --
    keeps the effector boundary a one-file diff away from a real integration."""
    if decision.status is not DecisionStatus.ALLOW:
        raise NotAuthorized(f"action {action.id} decision status is {decision.status}, not allow")
    if decision.payload_hash != action.payload_hash():
        raise PayloadMismatchError(f"action {action.id} payload changed since it was decided on")

    prior = outcome_lookup(action.id)
    if prior is not None:
        return prior

    try:
        fn = EXECUTORS[action.action_type]
    except KeyError:
        raise ExecutorMissing(f"no executor registered for {action.action_type}") from None

    try:
        outcome = fn(action)
    except Exception as exc:
        raise ExecutionFailed(action.id, exc) from exc
    outcome_record(outcome)
    return outcome
