# Phase 2 kickoff prompt — Guardian Agent System

Paste everything below as your first message in the new window.

---

I'm continuing work on the Guardian Agent System in `/Users/anushkasirpurkar/Agent-Guardian`. Phase 1 is done, tested, and committed (`931e61a`). Read `PLAN.md` in full first — it's the source of truth (rev 3, post two review passes: `/plan-ceo-review` and `/plan-eng-review`). Don't re-litigate decisions already made there; they're settled.

## What exists right now

```
agents/base.py            WorkerAgent scaffold: one LLM call + one retry on bad JSON.
agents/finance_agent.py   FinanceAgent(client=None, model="gpt-4o-mini")
schemas.py                Action, ActionEnvelope, ActionType, Decision, Outcome, params union
policy.yaml               7 rules (FIN-001..004, FILE-001/002, MAIL-001)
db.py                     SQLite schema + CRUD: actions, decisions, outcomes, escalations
guardian/policy_agent.py  evaluate(action, history) -> Decision, most-restrictive-wins
guardian/predicates.py    rule condition matching
guardian/history.py       SQLiteHistoryQuery (windowed sums over EXECUTED outcomes only)
guardian/auditor.py       Recorder half only (record_envelope/decision/outcome, outcome_for)
guardian/escalation.py    park/pending/resolve — SQLite owns pending-approval state
guardian/executors.py     the ONLY module allowed to import an effector library
guardian/graph.py         LangGraph orchestration (state lives in SQLite, not the graph)
main.py                   CLI: python main.py --scenario phase1_demo
demo_scenarios.py         Phase 1 has one scenario (finance only)
tests/                    45 tests, all passing
```

Run `.venv/bin/pytest tests/ -v` first to confirm you're starting from green (45 passed).

Environment: `.venv/` already has pydantic, pyyaml, langgraph, openai, pytest, hypothesis, python-dotenv installed. `.env` (gitignored) holds `OPENAI_API_KEY` for live LLM calls — never print its value, only check presence via length.

## Two invariants that must not regress

1. **A worker agent can never import an effector library.** `tests/test_no_effector_imports.py` AST-scans every module except `guardian/executors.py` for `stripe`/`smtplib`/`shutil`/`subprocess`/`requests`/`httpx` and destructive `os`/`shutil` calls. Any new worker agent must pass this untouched.
2. **`target` is never LLM-supplied.** It's derived from a `target_field` class attribute naming which `params_model` field is canonical (`FinanceAgent.target_field = "counterparty"`). This was a real bug found by running Phase 1's live demo — the LLM phrased target inconsistently ("acme-corp" vs "vendor_invoices/acme-corp"), which broke a policy rule that keys off `action.target`. Every new worker agent needs its own correct `target_field`.

## Phase 2 scope (PLAN.md section 7, steps 8-11)

1. **`agents/email_agent.py`** — `EmailAgent(WorkerAgent)`, `action_type = ActionType.SEND_EMAIL`, `params_model = EmailParams`, `target_field = "recipient"`.
2. **`agents/file_agent.py`** — `FileAgent(WorkerAgent)`. Note: `FileParams` currently only has a `path` field, but the real system needs to distinguish read/write/delete somehow — decide whether `FileAgent` is one class handling all three `ActionType`s (READ_FILE/WRITE_FILE/DELETE_FILE) based on task content, or three separate classes. PLAN.md doesn't resolve this explicitly; it's a real decision, not a given — surface it to the user rather than picking silently.
3. **Interactive CLI approval** over `guardian.escalation.resolve()` — replace Phase 1's `main.py` auto-approve with an actual blocking prompt. Must verify the resumability claim PLAN.md makes: kill the process mid-escalation (a row is sitting in `escalations` with `status='pending'`), restart `main.py`, and prove you can still approve/reject that pending row and have it execute. This is the whole reason escalation state lives in SQLite instead of a LangGraph checkpointer (rev 3, finding A2) — Phase 2 is where that design decision actually gets exercised for the first time.
4. **`main.py report --session <id>`** — a readable audit trail command. `guardian/auditor.py` currently only has the Recorder half; you're adding the Reporter half now.
5. **Cross-session pattern flags** in the report layer (PLAN.md section 6: "may surface cross-session patterns... no enforcement depends on that" — this is informational only, not a policy input, don't confuse it with the in-`policy_agent.py` structuring check that already exists and enforces).
6. **Full six-row demo scenario** (PLAN.md section 12) — now buildable since all three worker agents will exist. Extend `demo_scenarios.py` beyond the single Phase 1 entry.

**Exit criterion** (PLAN.md's own words): the six-row demo scenario passes end to end, including the kill-restart-approve resumability proof for scenario #3.

## How Phase 1 actually went (for calibration, not a mandate)

- Used 3 parallel subagents for independent tracks (storage layer, policy engine, worker agent) after writing `schemas.py` and `policy.yaml` myself first, since everything downstream reads those. Gave each agent exact function signatures up front — that's what let genuinely parallel work integrate cleanly with almost no rework. If you parallelize Phase 2, the same pattern applies: `email_agent.py`/`file_agent.py` are independent of each other and of the CLI/reporter work, so that's a natural 2-or-3-way split.
- Ran `/karpathy-guidelines` before writing code (simplicity first, surgical changes, goal-driven execution) and `/code-review` after integration, before declaring done. The review caught a real fail-open-adjacent bug (a crash instead of a fail-closed deny) that the test suite alone hadn't caught — worth repeating that sequence for Phase 2 too.
- The live end-to-end run (not just mocked tests) is what surfaced the `target` derivation bug. Don't skip an actual live run against Phase 2's new agents just because the mocked unit tests pass.

## One open question already flagged above, not resolved

`FileAgent`'s shape (one class for read/write/delete, or three) — ask the user before building it; don't assume.
