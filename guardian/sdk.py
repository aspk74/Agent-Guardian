"""Mode-A in-process SDK (design doc 2026-08-24 s6, revised D2 in s13): the
actual phase-1 external-framework integration. A customer wraps their own
tool function -- the one their PraisonAI/ADK/Agno/LangGraph/CrewAI agent
calls -- with `@guarded(ActionSpec(...))`. Guardian builds a typed `Action`
from the call, evaluates it, and only calls the real function through on
ALLOW; on DENY it raises, on ESCALATE it parks and raises, and a later retry
with the same arguments picks the parked escalation back up.

Read this docstring before extending this module -- two things are load-
bearing and easy to break by "simplifying":

1. **This is the cooperative-hook mode, not the credential-custody boundary**
   (design doc s4). It gives real policy evaluation, real audit, real
   human-in-the-loop -- it does NOT give non-bypassability. Never document,
   demo, or advertise this module as providing the guarantee mode C/B would.
   Credentials stay wherever the customer's own tool code already puts them;
   this module never asks for them and never touches them.

2. **Identity is bound by context, never by function arguments.** `target`,
   `action_type`, `session_id`, and `requesting_agent` must never be
   something an LLM-driven framework can supply, for the same reason
   agents/base.py never lets the model set them (its module docstring). If
   `session_id`/`requesting_agent` were parameters of the guarded function
   itself, they would appear in whatever tool schema the framework shows the
   model, and the model could then set them -- exactly the bug class this
   repo is built to prevent. `context()` below uses a contextvar instead:
   the customer's own (trusted, non-LLM) adapter code sets it around the
   framework's tool-call step, and the guarded function's signature -- the
   one the framework/LLM actually sees -- never changes shape.
"""
from __future__ import annotations

import contextvars
import inspect
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

import db
import guardian.escalation as esc
import guardian.executors as executors
import guardian.policy_agent as policy_agent
import guardian.registry as registry
from guardian.execution import run_with_audit
from guardian.history import SQLiteHistoryQuery
from schemas import Action, ActionEnvelope, DecisionStatus, Outcome


@dataclass(frozen=True)
class ActionSpec:
    """The registration contract, design doc s6. `target_field` is
    mandatory, not inferred or optional -- see registry.register()'s own
    InvalidTargetField check and agents/base.py's module docstring for why:
    an LLM-phrased target makes policy rules that key on `action.target`
    (e.g. FIN-003's target_not_in) produce different outcomes for the same
    real-world counterparty depending on the model's mood."""

    action_type: str
    params_model: type
    target_field: str


class NoActiveContext(Exception):
    """A @guarded function was called with no active guardian.sdk.context()
    on the stack. Fail closed, matching this repo's posture everywhere else
    (missing policy.yaml, an unmatched rule, a predicate error): there is no
    safe default identity to guess here. The alternative -- inferring
    requesting_agent from, say, the calling module's __name__ -- would be a
    silent, spoofable substitute for the one thing (identity) this system
    refuses to let anything but trusted code set."""


class ActionDenied(Exception):
    """Policy said DENY, or a parked escalation was rejected by a human.
    action_id is always populated: callers that want the full Decision/audit
    trail can look it up (db.get_decision(conn, action_id))."""

    def __init__(self, action_id: str, reasoning: str):
        super().__init__(f"action {action_id} denied: {reasoning}")
        self.action_id = action_id
        self.reasoning = reasoning


class ActionPending(Exception):
    """Policy said ESCALATE and no human has resolved it yet. This is the
    error-and-retry pattern design doc s12e/s13 chose over suspend/resume:
    no framework durable-suspend support is required, no protocol timeout to
    fight (s7a's dead end for a synchronous tool call). The caller's next
    turn simply calls the guarded function again with the SAME arguments;
    _find_matching_escalation below recognizes the resubmission and either
    executes it (if a human has since approved) or raises this again (still
    pending) -- see that function's docstring for why this can't reuse
    Action.payload_hash() directly."""

    def __init__(self, action_id: str):
        super().__init__(
            f"action {action_id} requires human approval; call again with "
            f"the same arguments once it has been resolved"
        )
        self.action_id = action_id


@dataclass(frozen=True)
class _Context:
    session_id: str
    requesting_agent: str
    conn: sqlite3.Connection
    policy_path: str


_current: contextvars.ContextVar[_Context | None] = contextvars.ContextVar(
    "guardian_sdk_context", default=None
)


@contextmanager
def context(*, session_id: str, requesting_agent: str, conn: sqlite3.Connection,
            policy_path: str = "policy.yaml"):
    """Binds identity and infra for every @guarded call inside the `with`
    block, on THIS execution context only (contextvars, not a global -- safe
    under asyncio tasks and threads started via contextvars-aware means; a
    plain thread started without copying the context will not inherit it,
    same as any other contextvars use)."""
    token = _current.set(_Context(session_id, requesting_agent, conn, policy_path))
    try:
        yield
    finally:
        _current.reset(token)


def _make_executor(fn: Callable[..., Any]) -> Callable[[Action], Outcome]:
    """Adapts a customer's own tool function (arbitrary signature, arbitrary
    return type) to the Action -> Outcome contract guardian/executors.py's
    EXECUTORS dict requires, the same shape as the built-in _simulate_*
    functions. Called for real, with real params, on ALLOW -- and again on
    an approved retry, via esc.execute_approved()'s idempotency guard.

    The customer's return value does not survive as a typed object past this
    point -- see the module docstring's scope note. It's captured as
    Outcome.detail so it still reaches the audit trail and the dashboard,
    consistent with how every other executor in this repo reports its
    result."""

    def _execute(action: Action) -> Outcome:
        result = fn(**action.params.model_dump())
        return Outcome(
            action_id=action.id,
            requesting_agent=action.requesting_agent,
            action_type=action.action_type,
            status="success",
            detail=repr(result),
        )

    return _execute


def _semantic_key(action: Action) -> tuple:
    """The retry-matching key: two proposals with identical business content
    match, regardless of Action.id/created_at.

    This is deliberately NOT Action.payload_hash(). payload_hash() includes
    `id` (schemas.py's model_dump(exclude={"created_at"}) -- id is not
    excluded), because its job is tamper-detection across ONE record's own
    park -> resolve -> execute lifecycle (the same stored id, re-hashed to
    catch a mutated field) -- not cross-object equality between two
    independently-constructed Actions. A fresh Action() always gets a fresh
    random id (schemas.py's default_factory), so two calls with identical
    arguments never share a payload_hash. Retry matching needs its own key
    that excludes id and created_at on purpose."""
    return (
        action.action_type,
        action.session_id,
        action.requesting_agent,
        action.target,
        action.params.model_dump_json(),
    )


def _find_matching_escalation(conn: sqlite3.Connection, action: Action) -> dict | None:
    """Scans this session's escalations for one whose stored action matches
    `action` by _semantic_key. Session-scoped, not global: a retry is
    expected to happen within the same session the original proposal was
    made in, and db.get_escalations_for_session already exists for exactly
    this kind of session-scoped read. O(session size), which is fine at
    demo/single-tenant scale; a customer running enough concurrent escalations
    per session for this to matter is exactly the T1/12b concurrency
    situation the design doc flags as still open."""
    key = _semantic_key(action)
    for row in db.get_escalations_for_session(conn, action.session_id):
        envelope = ActionEnvelope.model_validate_json(row["envelope_json"])
        if _semantic_key(envelope.action) == key:
            return {"action_id": row["action_id"], "status": row["status"]}
    return None


def _submit(ctx: _Context, action: Action) -> Outcome:
    history = SQLiteHistoryQuery(ctx.conn)
    decision = policy_agent.evaluate(action, history, policy_path=ctx.policy_path)
    envelope = ActionEnvelope(
        action=action,
        reasoning="(guardian.sdk: no LLM reasoning, action built directly from call args)",
        model="guardian-sdk",
        raw_response="",
    )

    if decision.status is DecisionStatus.DENY:
        db.insert_action(ctx.conn, envelope)
        db.insert_decision(ctx.conn, decision)
        raise ActionDenied(action.id, decision.reasoning)

    if decision.status is DecisionStatus.ESCALATE:
        match = _find_matching_escalation(ctx.conn, action)
        if match is None:
            db.insert_action(ctx.conn, envelope)
            esc.park(ctx.conn, envelope, decision)
            raise ActionPending(action.id)
        if match["status"] == "pending":
            raise ActionPending(match["action_id"])
        if match["status"] == "rejected":
            raise ActionDenied(match["action_id"], "human rejected this action")
        # "approved": already resolved since the last call. execute_approved()'s
        # own outcome_lookup guard (via executors.run) makes this idempotent
        # if called again after a successful execution.
        return esc.execute_approved(ctx.conn, match["action_id"])

    # ALLOW
    db.insert_action(ctx.conn, envelope)
    db.insert_decision(ctx.conn, decision)
    return run_with_audit(ctx.conn, action, decision)


def guarded(spec: ActionSpec):
    """Decorator factory. Registers `spec.action_type` in guardian.registry
    (Params/target_field) and, once the decorated function is known, in
    guardian.executors (the real effector) -- both at decoration time, both
    exactly once, so the two can never drift apart the way s12c documents
    they can for a type registered by hand in two separate steps.

    Raises registry.DuplicateRegistration / executors.DuplicateExecutor if
    `spec.action_type` is already registered -- same as calling
    registry.register() twice by hand. A module reloaded/imported twice in
    the same process (common under test collection) will hit this; tests
    that need repeatable registration should follow the same
    isolated-registry pattern tests/test_registry.py and
    tests/test_open_registry_end_to_end.py already use.
    """
    registry.register(spec.action_type, spec.params_model, spec.target_field)

    def decorator(fn):
        executors.register_executor(spec.action_type, _make_executor(fn))
        signature = inspect.signature(fn)

        def wrapper(*args, **kwargs):
            ctx = _current.get()
            if ctx is None:
                raise NoActiveContext(
                    f"{fn.__name__} is @guarded but was called with no active "
                    f"guardian.sdk.context() -- wrap the call site: "
                    f"`with guardian.sdk.context(session_id=..., "
                    f"requesting_agent=..., conn=conn): ...`"
                )
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            params = spec.params_model(**bound.arguments)
            target = getattr(params, spec.target_field)
            action = Action(
                session_id=ctx.session_id,
                requesting_agent=ctx.requesting_agent,
                action_type=spec.action_type,
                target=target,
                params=params,
            )
            return _submit(ctx, action)

        wrapper.__name__ = getattr(fn, "__name__", "guarded")
        wrapper.__doc__ = fn.__doc__
        wrapper.__wrapped__ = fn
        return wrapper

    return decorator
