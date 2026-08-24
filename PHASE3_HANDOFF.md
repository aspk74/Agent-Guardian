# Phase 3 kickoff prompt — Guardian Agent System

Paste everything below as your first message in the new window.

---

I'm continuing work on the Guardian Agent System in `/Users/anushkasirpurkar/Agent-Guardian`. Phase 2 is done, tested, and pushed to `main` on GitHub (`0834e1b`). Read `PLAN.md` in full first — it's the source of truth (rev 3, post two review passes: `/plan-ceo-review` and `/plan-eng-review`). Don't re-litigate decisions already made there; they're settled.

## What exists right now

```
agents/base.py            WorkerAgent scaffold: one LLM call + one retry on bad JSON.
agents/finance_agent.py   FinanceAgent(client=None, model="gpt-4o-mini")
agents/email_agent.py     EmailAgent, target_field="recipient"
agents/file_agent.py      ReadFileAgent / WriteFileAgent / DeleteFileAgent (three classes)
schemas.py                Action, ActionEnvelope, ActionType, Decision, Outcome, params union
policy.yaml                7 rules (FIN-001..004, FILE-001/002, MAIL-001)
db.py                     SQLite schema + CRUD, plus Phase 2's session/cross-session query helpers
guardian/policy_agent.py  evaluate(action, history) -> Decision, most-restrictive-wins
guardian/predicates.py    rule condition matching, case-insensitive on all target_* comparisons
guardian/history.py       SQLiteHistoryQuery (windowed sums over EXECUTED outcomes only)
guardian/auditor.py       Recorder + Reporter (report(), cross_session_flags())
guardian/escalation.py    park/pending/resolve — SQLite owns pending-approval state
guardian/executors.py     the ONLY module allowed to import an effector library
guardian/graph.py         LangGraph orchestration (state lives in SQLite, not the graph)
main.py                   CLI: run / resolve / report subcommands, interactive y/n approval
demo_scenarios.py         phase1_demo (1 row) + demo1 (full six-row PLAN.md s12 scenario)
tests/                    70 tests, all passing
```

Run `.venv/bin/pytest tests/ -v` first to confirm you're starting from green (70 passed).

Environment: `.venv/` already has pydantic, pyyaml, langgraph, openai, pytest, hypothesis, python-dotenv installed. `fastapi` and `uvicorn` are **not** installed yet (confirmed via `.venv/bin/pip show fastapi uvicorn` at Phase 2 close) — Phase 3 is the first phase that needs them, so install them first and add them wherever this project's dependencies are declared. `.env` (gitignored) holds `OPENAI_API_KEY` for live LLM calls — never print its value, only check presence via length.

## Invariants that must not regress

1. **A worker agent can never import an effector library.** `tests/test_no_effector_imports.py` AST-scans every module except `guardian/executors.py`. Any new module (including Phase 3's FastAPI app) must pass this untouched, or be added to the exemption list only if it's a legitimate reason (it shouldn't need to be — the dashboard reads through `escalation.pending()`/`resolve()`, same as the CLI).
2. **`target` is never LLM-supplied.** Derived from each agent's `target_field` class attribute.
3. **Policy target matching is case-insensitive**, deliberately, everywhere (`target_in`/`target_not_in`/`target_glob`/`target_domain_not_in`). Two real bugs in Phase 2 came from this exact class of issue — a case mismatch between what the LLM phrases and what `policy.yaml` lists — caught only by live runs, not mocked tests. If Phase 3's hot-reload or coverage-check work touches `predicates.py` or `policy.yaml` parsing, watch for this class of bug again.
4. **A `Decision`, once written, is immutable and authoritative.** Resuming an approved escalation replays the stored `Decision`; it never re-runs `evaluate()`. This is what makes `policy_version` meaningful under Phase 3's hot reload (PLAN.md s2.2) — a parked escalation keeps the `policy_version` it was decided under even after `policy.yaml` changes underneath it.

## Phase 3 scope (PLAN.md section 7, steps 12-14 — "stretch")

1. **FastAPI + HTML dashboard** over the same `escalation.pending()` / `escalation.resolve()` the CLI already uses (PLAN.md s5: "Phase 2 CLI and Phase 3 HTTP call the same three functions"). Do not reimplement escalation logic in the API layer — route handlers should be thin wrappers calling `guardian/escalation.py` exactly as `main.py`'s `cmd_resolve` does. Note from Phase 2's code review: `main.py`'s `_prompt_approval` is a blocking terminal `input()` and cannot be reused as-is for an HTTP request/response cycle — the "present escalation, collect a decision" logic will need its own presentation layer here, but the underlying `park`/`pending`/`resolve` calls are the shared source of truth, not the presentation.
2. **Hot-reload `policy.yaml`.** New `policy_version` (sha256 of the file) computed on reload. Already-parked escalations must keep the `Decision` (and `policy_version`) they were given at escalation time — PLAN.md s2.2 is explicit that a stored `Decision` is never re-evaluated. Confirm `guardian/policy_agent.py`'s current per-call file read (it re-reads and re-parses `policy.yaml` on every `evaluate()` call today) is either fine as the reload mechanism or needs an explicit reload trigger — check both behaviors before assuming.
3. **LLM coverage check over the rule set.** Constrained: **may return `escalate` or `deny` only.** `allow` must remain reachable exclusively through a deterministic rule match — this is PLAN.md rev 2 defect #8, already fixed once at the plan level; don't reintroduce it at the implementation level by letting an LLM-driven check ever produce `DecisionStatus.ALLOW`.

**Exit criterion:** Phase 3 is documented as a stretch phase in PLAN.md, not gated by a single scripted scenario the way Phase 1/2 were. Before starting, ask the user what "done" means for this phase specifically (e.g., does the dashboard need auth, does hot-reload need a test proving a parked escalation survives a policy.yaml edit, does the coverage check need to run in CI or just be callable) — PLAN.md intentionally leaves these underspecified since it's marked stretch.

## How Phase 2 actually went (for calibration, not a mandate)

- Split into parallel subagent tracks for genuinely independent work (EmailAgent+FileAgent+their tests as one track, Reporter half of auditor.py + its db.py helpers as another) while handling the CLI rewrite and escalation-resumability proof directly, since those touch shared files (`main.py`) that the parallel tracks also needed to land in. If Phase 3's dashboard and hot-reload work are similarly independent of each other, the same split applies — but the dashboard will likely want `guardian/escalation.py` and `guardian/policy_agent.py` to already be stable, so sequence accordingly.
- Ran `/karpathy-guidelines` before writing code and `/code-review` after integration. The review caught a real bug the live demo run had NOT caught: `target_glob` (used by `FILE-001`'s production-delete guard) was left case-sensitive when the other three target predicates were fixed for case-insensitivity earlier in the same session — same bug class, missed one call site over, only found by review. Run both again for Phase 3, especially since hot-reload and the coverage check both touch policy-evaluation code paths where this kind of narrow miss is easy to reintroduce.
- Two live end-to-end runs (not just mocked tests) surfaced real bugs the mocks couldn't: a case-sensitive counterparty match, and a demo-scenario row-ordering issue where FIN-002's cross-counterparty cumulative sum tripped a payment earlier than the scripted narrative expected. Don't skip actual live runs against Phase 3's new surfaces (the dashboard, a hot-reloaded policy file) just because unit tests pass.
- Commit in small, reviewable units once work is approved — Phase 2 landed as 13 commits (one per logical piece: schema/query helpers, each new agent, its tests, the CLI rewrite, the resumability tests, the demo scenario, and each code-review-driven fix as its own commit) rather than one large commit. Kept `git log` genuinely useful for understanding what changed and why.

## Open questions to raise with the user before building, not assume

- What "done" means for this stretch phase (see Exit criterion above) — there's no single PLAN.md-scripted scenario like Phase 1/2 had.
- Whether the dashboard needs any authentication/access control before it's usable against a real (non-demo) `guardian.db`, given it can approve real payments.
- Whether hot-reload should be automatic (e.g. file-watch) or an explicit reload trigger (e.g. a `/reload` endpoint or CLI command) — PLAN.md doesn't say.
