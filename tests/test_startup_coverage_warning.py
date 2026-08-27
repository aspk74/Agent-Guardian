"""main.py's warn_uncovered_action_types (E3): the CLI-side wiring of
guardian/registry.py's uncovered_action_types check. dashboard.py runs the
same check at import time (see dashboard.py's module-level warning block) --
not covered here since it fires at import, not on a callable; the assertion
that both consume registry.uncovered_action_types identically is what makes
duplicating the test unnecessary.
"""
from __future__ import annotations

import yaml

import main


def _write_policy(tmp_path, rules: list[dict]) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "rules": rules}))
    return str(path)


def test_warns_on_stderr_for_uncovered_type(tmp_path, capsys):
    # Deliberately covers only make_payment -- every other built-in type
    # (send_email, read_file, write_file, delete_file) is left uncovered.
    policy_path = _write_policy(tmp_path, [
        {"id": "T1", "when": {"action_type": "make_payment", "amount_cents_gt": 1},
         "then": "escalate"},
    ])
    main.warn_uncovered_action_types(policy_path)
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "send_email" in captured.err
    assert captured.out == ""  # goes to stderr, not stdout


def test_silent_when_every_registered_type_is_covered(tmp_path, capsys):
    from guardian import registry
    rules = [{"id": f"T{i}", "when": {"action_type": t}, "then": "allow"}
              for i, t in enumerate(registry.all_registered())]
    policy_path = _write_policy(tmp_path, rules)
    main.warn_uncovered_action_types(policy_path)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
