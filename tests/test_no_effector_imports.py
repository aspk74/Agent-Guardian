"""PLAN.md finding A6 / failure mode 9.5: a worker agent (or any module other
than guardian/executors.py) must never be able to import an effector library.
This is a drift guardrail, not a sandbox -- it catches an honest `import
stripe`, not `importlib.import_module("stripe")` or similar indirection.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EFFECTOR_MODULES = {"stripe", "smtplib", "shutil", "subprocess", "requests", "httpx"}
EFFECTOR_CALLABLES = {"remove", "unlink", "rmtree"}  # os.remove, os.unlink, shutil.rmtree
ALLOWED_MODULE = REPO_ROOT / "guardian" / "executors.py"
SCAN_DIRS = ["agents", "guardian", "."]
EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "tests"}


def _iter_python_files():
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir() and d != ".":
            continue
        pattern = "*.py" if d == "." else "**/*.py"
        for path in base.glob(pattern):
            if path == ALLOWED_MODULE:
                continue
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            yield path


def _imported_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _effector_attr_calls(tree: ast.AST) -> set[str]:
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in EFFECTOR_CALLABLES:
            hits.add(node.attr)
    return hits


def test_no_module_outside_executors_imports_an_effector_library():
    violations = []
    for path in _iter_python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        found = _imported_names(tree) & EFFECTOR_MODULES
        if found:
            violations.append(f"{path.relative_to(REPO_ROOT)} imports {found}")
    assert not violations, "effector library imported outside guardian/executors.py:\n" + "\n".join(violations)


def test_no_module_outside_executors_calls_destructive_os_functions():
    violations = []
    for path in _iter_python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        found = _effector_attr_calls(tree)
        if found:
            violations.append(f"{path.relative_to(REPO_ROOT)} calls {found}")
    assert not violations, "destructive os/shutil call outside guardian/executors.py:\n" + "\n".join(violations)


def test_executors_module_itself_is_exempt_and_exists():
    assert ALLOWED_MODULE.is_file(), "guardian/executors.py must exist as the one exempt module"
