"""guardian/registry.py: the ActionType -> (Params, target_field) mapping,
and the E3 boot-time coverage check built on top of it.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
import yaml

from guardian import registry
from schemas import ActionType, EmailParams, FileParams, PaymentParams


@contextmanager
def _isolated_registry():
    """register()/get() operate on a module-level singleton by design (one
    registry per process, same reasoning as the built-ins registered at
    import time). Tests that need to observe DuplicateRegistration or
    UnregisteredActionType do so against a temporarily-emptied copy, restored
    unconditionally afterward so no other test ever sees a mutated registry."""
    saved = dict(registry._REGISTRY)
    registry._REGISTRY.clear()
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


def test_builtins_match_the_worker_agent_declarations():
    """Cross-check against agents/*.py's own target_field class attributes --
    the registry and the WorkerAgent subclasses each declare this mapping
    independently, and a mismatch between them would previously have been
    invisible until it broke a live demo run (the exact class of bug
    agents/base.py's module docstring describes for target itself)."""
    from agents.email_agent import EmailAgent
    from agents.file_agent import DeleteFileAgent, ReadFileAgent, WriteFileAgent
    from agents.finance_agent import FinanceAgent

    checks = [
        (FinanceAgent, PaymentParams),
        (EmailAgent, EmailParams),
        (ReadFileAgent, FileParams),
        (WriteFileAgent, FileParams),
        (DeleteFileAgent, FileParams),
    ]
    for agent_cls, params_cls in checks:
        reg = registry.get(agent_cls.action_type)
        assert reg.params_model is params_cls
        assert reg.target_field == agent_cls.target_field


def test_all_five_builtin_action_types_registered():
    expected = {
        ActionType.MAKE_PAYMENT, ActionType.SEND_EMAIL, ActionType.READ_FILE,
        ActionType.WRITE_FILE, ActionType.DELETE_FILE,
    }
    assert set(registry.all_registered()) == expected


def test_register_rejects_duplicate():
    with _isolated_registry():
        registry.register(ActionType.MAKE_PAYMENT, PaymentParams, "counterparty")
        with pytest.raises(registry.DuplicateRegistration):
            registry.register(ActionType.MAKE_PAYMENT, PaymentParams, "counterparty")


def test_register_rejects_target_field_not_on_model():
    with _isolated_registry():
        with pytest.raises(registry.InvalidTargetField):
            registry.register(ActionType.MAKE_PAYMENT, PaymentParams, "not_a_real_field")


def test_get_raises_for_unregistered_type():
    with _isolated_registry():
        with pytest.raises(registry.UnregisteredActionType):
            registry.get(ActionType.MAKE_PAYMENT)


def test_uncovered_action_types_empty_rules_flags_everything():
    assert set(registry.uncovered_action_types([])) == set(registry.all_registered())


def test_uncovered_action_types_full_coverage_flags_nothing():
    rules = [{"when": {"action_type": t}} for t in registry.all_registered()]
    assert registry.uncovered_action_types(rules) == []


def test_uncovered_action_types_against_real_policy_yaml():
    """Documents a real, pre-existing gap in this repo's policy.yaml: WRITE_FILE
    has a registered executor and a WorkerAgent (agents/file_agent.py's
    WriteFileAgent) but zero policy.yaml rules. Today that means a proposed
    write silently falls through to SYS-GAP/escalate the first time an agent
    tries it -- correct, but previously invisible until that moment. This
    test pins the gap down rather than papering over it; closing it is a
    policy-content decision (what SHOULD write_file's rule say?), not a code
    fix, and is intentionally left to whoever owns policy.yaml next."""
    with open("policy.yaml") as f:
        rules = yaml.safe_load(f)["rules"]
    uncovered = registry.uncovered_action_types(rules)
    assert ActionType.WRITE_FILE in uncovered
