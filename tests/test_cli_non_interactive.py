"""Regression tests for main.py's handling of a closed/non-interactive stdin
mid-escalation.

Caught by code review, then reproduced live: `_prompt_approval`'s `input()`
call had no EOFError handling, so running any scenario with an escalation
against closed stdin (e.g. `echo -n "" | main.py run ...`, or any CI/non-tty
invocation) crashed with a raw traceback instead of exiting cleanly. This
also meant `demo1` (three escalations) could never be run unattended.
"""
from __future__ import annotations

import os
import subprocess
import sys


def test_run_exits_cleanly_on_closed_stdin_during_escalation(tmp_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = str(tmp_path / "noninteractive.db")

    proc = subprocess.run(
        [sys.executable, "main.py", "run", "--scenario", "phase1_demo",
         "--session-id", "eof-test", "--db", db_path],
        cwd=repo_root,
        input="",  # closed stdin -- EOF on the very first input() call
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode != 0, "a closed stdin mid-escalation must exit non-zero, not hang or succeed"
    assert "Traceback" not in proc.stdout and "Traceback" not in proc.stderr, (
        f"must not crash with a raw traceback; got:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "EOFError" not in proc.stdout and "EOFError" not in proc.stderr, (
        "the EOFError must be caught and translated into a clean message, not leak out raw"
    )
