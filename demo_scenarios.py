"""Phase 1 scope only (PLAN.md section 7, step 7): a single FinanceAgent task
run through the full guardian loop, ending in escalate -> human approval ->
execute. The six-row demo table in PLAN.md section 12 needs EmailAgent and
FileAgent, which are Phase 2 (build order step 8) -- not built yet.
"""
from __future__ import annotations

SCENARIOS = {
    "phase1_demo": [
        {"agent": "finance", "task": "pay the vendor invoice for $750 to acme-corp"},
    ],
}
