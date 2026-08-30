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

import pydantic

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


class InvalidActionParams(Exception):
    """The caller's arguments don't satisfy `spec.params_model` (wrong type,
    missing required field, failed a pydantic validator). Wraps pydantic's
    own ValidationError as __cause__ rather than letting it escape directly
    -- the raw pydantic exception is an implementation detail of how
    params_model happens to be built (a customer could swap in a different
    validation library behind ActionSpec in principle), and this repo's
    posture is that every boundary a customer's calling code has to catch
    exposes a typed exception from this module, not a third-party one. No
    Action has been constructed yet at this point (target isn't known until
    params validates), so unlike ActionDenied/ActionPending there is no
    action_id to attach -- there is nothing to look up in the audit trail
    because nothing was ever proposed."""

    def __init__(self, action_type: str, original: pydantic.ValidationError):
        super().__init__(
            f"invalid params for action_type={action_type!r}: {original}"
        )
        self.action_type = action_type


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
    same as any other contextvars use).

    **Wiring this into a thread-dispatched framework (read this if you're
    integrating LangGraph, CrewAI, or anything else that runs your tool
    function through `concurrent.futures.ThreadPoolExecutor.submit()`):**

    `ThreadPoolExecutor.submit()` does NOT propagate contextvars to the
    worker thread -- the submitted function sees this contextvar's default
    (`None`), not whatever `context()` bound in the calling thread, even if
    that `with context(...):` block is still open on the stack above the
    submit() call. This is a `contextvars` limitation, not a Guardian one:
    Guardian has no hook into how your framework schedules the call, so it
    cannot force propagation from inside this module (see the module
    docstring, point 2, for why identity is contextvar-bound at all rather
    than a plain function argument). Concretely:

        # BROKEN: the worker thread does not see ctx bound in the caller.
        # issue_refund() raises NoActiveContext even though context(...) is
        # active on the submitting thread's stack.
        with guardian.sdk.context(session_id=sid, requesting_agent=agent, conn=conn):
            future = executor.submit(issue_refund, customer_id="c1", amount_cents=500)

        # CORRECT: capture the current context and replay it inside the
        # worker via Context.run -- copy_context() snapshots every
        # contextvar bound in the calling thread (this one included), and
        # ctx.run(fn, *args) invokes fn with that snapshot active.
        import contextvars

        with guardian.sdk.context(session_id=sid, requesting_agent=agent, conn=conn):
            ctx = contextvars.copy_context()
            future = executor.submit(ctx.run, issue_refund, customer_id="c1", amount_cents=500)

    Do this once, at the adapter boundary where your framework hands off to
    a worker thread -- e.g. wherever your LangGraph/CrewAI tool node itself
    calls or is called via `executor.submit(...)`. `asyncio.to_thread()` is
    NOT affected by this -- it copies the current context before running
    the target in the thread pool, same as this pattern does by hand, so no
    extra wiring is needed there.
    """
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
    """T1 fix (eng review 2026-08-29): the read-history -> evaluate -> decide
    sequence below runs inside a single `BEGIN IMMEDIATE` transaction, with a
    `reservations` row (db.py) inserted BEFORE history is read.

    Why this closes the race: without it, two concurrent @guarded calls in
    the same session could both call SQLiteHistoryQuery against a cumulative
    cap (e.g. FIN-002's "total payments today < $X") before either one's
    action had produced a decision or an outcome. Both would read the same
    stale total, both evaluate the cap as not-yet-crossed, and both get
    ALLOW -- a classic TOCTOU bypass of the cap.  `BEGIN IMMEDIATE` acquires
    SQLite's write lock up front (rather than only at COMMIT, like a plain
    `BEGIN`/implicit transaction would), so a second concurrent caller
    reaching its own `BEGIN IMMEDIATE` blocks until the first caller's
    transaction commits or rolls back -- there is no window where both are
    mid-evaluation at once. Inserting the reservation before reading history
    (rather than only recording the eventual decision) is what makes the
    *second* caller's history read, once it gets its turn, see the *first*
    caller's action at all: `guardian/history.py`'s SQLiteHistoryQuery now
    folds in still-pending `reservations` rows alongside executed outcomes,
    so caller 2 sees caller 1's in-flight amount even though caller 1 hasn't
    reached a decision yet, let alone executed.

    All db.* calls in this function that must NOT end the transaction early
    (insert_reservation, release_reservation) are documented as such at their
    definitions in db.py -- every commit point below is deliberate and
    explicit, not incidental.
    """
    ctx.conn.execute("BEGIN IMMEDIATE")
    try:
        db.insert_reservation(ctx.conn, action)
        # exclude_action_id=action.id: this action's own reservation (just
        # inserted above) must be visible to any OTHER concurrent caller's
        # history read, but not to this evaluate() call's own -- see
        # db.get_pending_reservation_totals()'s docstring. Without this,
        # guardian/predicates.py's sum_amount_cents_gt check (which adds the
        # candidate's own amount on top of "history" itself, PLAN.md s9.3)
        # would double-count this action against its own cap.
        history = SQLiteHistoryQuery(ctx.conn, exclude_action_id=action.id)
        decision = policy_agent.evaluate(action, history, policy_path=ctx.policy_path)
        envelope = ActionEnvelope(
            action=action,
            reasoning="(guardian.sdk: no LLM reasoning, action built directly from call args)",
            model="guardian-sdk",
            raw_response="",
        )

        if decision.status is DecisionStatus.DENY:
            # Denied attempts never count toward a cumulative cap (PLAN A4) --
            # release this action's own reservation in the same transaction
            # that records the deny, so it stops counting the instant the
            # deny is decided rather than lingering until some later cleanup.
            db.insert_action(ctx.conn, envelope)
            db.insert_decision(ctx.conn, decision)
            db.release_reservation(ctx.conn, action.id)
            ctx.conn.commit()
            raise ActionDenied(action.id, decision.reasoning)

        if decision.status is DecisionStatus.ESCALATE:
            match = _find_matching_escalation(ctx.conn, action)
            if match is None:
                # First time this semantic action has been proposed: park it,
                # and leave ITS OWN reservation (inserted above) pending --
                # an escalated action is provisionally still "reserved"
                # against the cap while awaiting a human, exactly like an
                # ALLOW is reserved while awaiting execution. It is released
                # below, on a later call, once a human resolves it either way.
                db.insert_action(ctx.conn, envelope)
                esc.park(ctx.conn, envelope, decision)
                ctx.conn.commit()
                raise ActionPending(action.id)

            # A retry of an already-parked semantic action: THIS call's own
            # reservation (for action.id, a freshly-generated id distinct
            # from the original match["action_id"]) is redundant -- the
            # original proposal's reservation already covers this action
            # against the cap, however this retry resolves. Release it here
            # so a customer's framework retrying ActionPending in a loop
            # never accumulates one extra phantom reservation per retry.
            db.release_reservation(ctx.conn, action.id)
            if match["status"] == "pending":
                ctx.conn.commit()
                raise ActionPending(match["action_id"])
            if match["status"] == "rejected":
                # Human rejected it: release the ORIGINAL reservation too --
                # a rejected action must not permanently consume the cap it
                # was provisionally reserved against (same rule as DENY above).
                db.release_reservation(ctx.conn, match["action_id"])
                ctx.conn.commit()
                raise ActionDenied(match["action_id"], "human rejected this action")
            # "approved": already resolved since the last call. Commit now
            # (releasing the write lock) before executing -- execution can be
            # slow/re-entrant and must not hold the write lock. The ORIGINAL
            # reservation is released after execute_approved() durably
            # records the outcome, outside this transaction (see below):
            # execute_approved()'s own outcome_lookup guard (via
            # executors.run) makes this idempotent if called again after a
            # successful execution.
            ctx.conn.commit()
            outcome = esc.execute_approved(ctx.conn, match["action_id"])
            # Execution succeeded (execute_approved raises otherwise, and
            # this line is then never reached -- the reservation is left
            # pending, which is the correct fail-closed direction: still
            # over-counted against the cap rather than silently dropped).
            # No BEGIN IMMEDIATE needed here: a single DELETE is already
            # atomic, and nothing concurrent needs to be excluded from a
            # release, only from the read-evaluate step above.
            db.release_reservation(ctx.conn, match["action_id"])
            ctx.conn.commit()
            return outcome

        # ALLOW: commit the reservation + decision now, releasing the write
        # lock, then execute outside the transaction (same reasoning as the
        # approved-escalation retry above -- execution must not hold the
        # lock). This action's own reservation is released once its outcome
        # is durably recorded; until then it correctly keeps counting toward
        # the cap for any concurrent sibling call still inside its own
        # BEGIN IMMEDIATE waiting on this one.
        db.insert_action(ctx.conn, envelope)
        db.insert_decision(ctx.conn, decision)
        ctx.conn.commit()
        outcome = run_with_audit(ctx.conn, action, decision)
        # Same reasoning as the approved-escalation release above: reached
        # only on success, and a plain DELETE needs no transaction of its own.
        db.release_reservation(ctx.conn, action.id)
        ctx.conn.commit()
        return outcome
    except BaseException:
        ctx.conn.rollback()
        raise


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
            try:
                params = spec.params_model(**bound.arguments)
            except pydantic.ValidationError as e:
                raise InvalidActionParams(spec.action_type, e) from e
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
