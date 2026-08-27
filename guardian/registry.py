"""The action_type -> (Params, target_field) registry (design doc 2026-08-24
s2, s6): the open half of "ActionType/Params became an open registry rather
than a closed set."

Every action_type string a process can ever see is registered here, alongside
its Params model and the name of the field a WorkerAgent/mode-A adapter must
use to derive Action.target (agents/base.py:13-19 -- target must be declared,
never LLM-phrased or inferred). schemas.py's Action._resolve_params_class
consults this registry (via a deferred import, to avoid a cycle: this module
imports schemas.py at ITS top level) to know which concrete Params class to
validate a stored action's params dict against when reconstructing from JSON.

The five built-ins below are registered on import exactly the way a
customer's own action_type would be -- nothing about them is special-cased.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from schemas import ActionType, EmailParams, FileParams, PaymentParams

# Re-exported so callers that only import guardian.registry (not schemas)
# can still catch this -- schemas.Action._resolve_params_class raises it too,
# from inside Pydantic validation, and the two must be the SAME class for a
# single `except UnregisteredActionType` to catch both raise sites.
from schemas import UnregisteredActionType


class DuplicateRegistration(Exception):
    """register() called twice for the same action_type."""


class InvalidTargetField(Exception):
    """register() called with a target_field that isn't an actual field on
    the given params_model -- would fail at Action-construction time with a
    confusing AttributeError instead of a clear one at registration time."""


@dataclass(frozen=True)
class ActionTypeRegistration:
    action_type: str
    params_model: type[BaseModel]
    target_field: str


_REGISTRY: dict[str, ActionTypeRegistration] = {}


def register(action_type: str, params_model: type[BaseModel], target_field: str) -> None:
    if action_type in _REGISTRY:
        raise DuplicateRegistration(f"{action_type!r} is already registered")
    if target_field not in params_model.model_fields:
        raise InvalidTargetField(
            f"target_field {target_field!r} is not a field on {params_model.__name__}"
        )
    _REGISTRY[action_type] = ActionTypeRegistration(action_type, params_model, target_field)


def get(action_type: str) -> ActionTypeRegistration:
    try:
        return _REGISTRY[action_type]
    except KeyError:
        raise UnregisteredActionType(action_type) from None


def is_registered(action_type: str) -> bool:
    return action_type in _REGISTRY


def all_registered() -> list[str]:
    return list(_REGISTRY)


def uncovered_action_types(rules: list[dict]) -> list[str]:
    """E3: registered action types with zero policy.yaml rules mentioning
    them. A type with no rule at all doesn't fail closed until the FIRST time
    an agent actually proposes it (evaluate()'s SYS-GAP path) -- this makes
    the gap visible at boot instead of at whatever moment an agent happens to
    hit it, without changing evaluate()'s runtime behavior at all."""
    covered = {
        rule["when"]["action_type"]
        for rule in rules
        if "action_type" in rule.get("when", {})
    }
    return [at for at in all_registered() if at not in covered]


def uncovered_executors() -> list[str]:
    """E3's other half (design doc s7b): registered action types with no
    registered executor. Distinct failure mode from uncovered_action_types
    above -- a rule gap degrades safely (SYS-GAP -> escalate); an executor
    gap surfaces only on the first ALLOWed proposal of that type, as
    executors.ExecutorMissing, which is a raised exception, not a Decision.
    Structurally this can no longer actually happen for anything registered
    through guardian.sdk.guarded() (it registers both atomically in the same
    call) -- this exists as a boot-time backstop for the case it's designed
    to catch: a type registered directly via registry.register() without
    ever calling guarded() or executors.register_executor().

    A deferred import, for the same reason schemas.py's
    _resolve_params_class uses one: guardian/executors.py does not import
    this module, so there's no cycle to break at MODULE level -- but
    registry.py registers its five built-ins at IMPORT time (bottom of this
    file), before guardian.executors has necessarily finished its own
    import in every possible import order, so the lookup itself is deferred
    to call time instead, exactly as schemas.py's does.
    """
    import guardian.executors as executors

    return [at for at in all_registered() if at not in executors.EXECUTORS]


# Built-ins, registered on import so nothing about existing behavior changes
# just from this module existing. These mirror agents/base.py's subclasses
# exactly (FinanceAgent.target_field="counterparty", etc.) -- a mismatch
# between the two would itself be a latent bug this registry now makes
# checkable (see tests/test_registry.py).
register(ActionType.MAKE_PAYMENT, PaymentParams, "counterparty")
register(ActionType.SEND_EMAIL, EmailParams, "recipient")
register(ActionType.READ_FILE, FileParams, "path")
register(ActionType.WRITE_FILE, FileParams, "path")
register(ActionType.DELETE_FILE, FileParams, "path")
