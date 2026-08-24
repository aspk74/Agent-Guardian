# Guardian Agent System — Build Plan (rev 3)

rev 2: `/plan-ceo-review` — enforcement boundary, 9 defects fixed.
rev 3: `/plan-eng-review` — 15 findings applied. Escalation state moved to SQLite.

Two invariants carry the whole design:

1. **A worker agent cannot cause an effect.** Enforced by module imports.
2. **The policy engine cannot see attacker text.** Enforced by the type system.

---

## 1. Architecture

```
   task (natural language)
     |
     v
  +--------------------+
  |   WorkerAgent      |  imports NO effector library
  |   (LLM reasoning)  |  returns ActionEnvelope
  +--------------------+
     |
     | ActionEnvelope { action: Action, reasoning: str, model: str }
     |                            |              |
     |                    typed, policy sees   LLM prose,
     |                    ONLY this            auditor only
     v
  +===========================================================+
  |                        GUARDIAN                           |
  |                                                           |
  |  audit.record(envelope)          <- log BEFORE anything   |
  |  policy.evaluate(envelope.action, history: HistoryQuery)  |
  |  audit.record(decision)                                   |
  +===========================================================+
     |               |                    |
   allow           deny              escalate
     |               |                    |
     v               v                    v
  executors.run  return decision   escalations.park(row)
     |                                    |
     |                             status = 'pending'
     |                                    |
     |                          CLI (Ph2) / HTTP (Ph3)
     |                                    |
     |                             approve / reject
     |                                    |
     |                             re-verify payload_hash
     |                                    |
     +<───────────────────────────────────+
     v
  effect happens, outcome recorded
```

`guardian/executors.py` is the only module in the repo permitted to import an
effector library. Enforced by `test_no_effector_imports.py` (s10).

---

## 2. Schemas

### 2.1 The quarantine split

The policy engine receives `Action`. It is structurally incapable of reading
LLM prose, because prose is not on that model.

```python
# schemas.py
from enum import Enum
from datetime import datetime, timedelta
from typing import Literal, Protocol, Union
from pydantic import BaseModel, Field

class ActionType(str, Enum):
    SEND_EMAIL   = "send_email"
    MAKE_PAYMENT = "make_payment"
    READ_FILE    = "read_file"
    WRITE_FILE   = "write_file"
    DELETE_FILE  = "delete_file"

# --- typed params, discriminated union. no untyped dict on the boundary ---

class PaymentParams(BaseModel):
    kind: Literal["payment"] = "payment"
    counterparty: str
    amount_cents: int                # integer cents. never float, never Decimal.
    memo_ref: str | None = None      # opaque id, NOT free text

class EmailParams(BaseModel):
    kind: Literal["email"] = "email"
    recipient: str
    subject_ref: str                 # template id, not the rendered subject
    body_ref: str

class FileParams(BaseModel):
    kind: Literal["file"] = "file"
    path: str

Params = Union[PaymentParams, EmailParams, FileParams]

class Action(BaseModel):
    """What the policy engine sees. Every field is typed. No prose."""
    id: str                          # uuid4, stable for the whole lifecycle
    session_id: str
    requesting_agent: str
    action_type: ActionType
    target: str                      # recipient / path / counterparty id
    params: Params = Field(discriminator="kind")
    created_at: datetime

    def payload_hash(self) -> str:
        # canonical: pydantic json with sorted keys. amount is int, so no
        # Decimal normalization ambiguity (rev-2 defect C1).
        return sha256(self.model_dump_json(exclude={"created_at"})).hexdigest()

class ActionEnvelope(BaseModel):
    """What the auditor sees. Carries the quarantined LLM output."""
    action: Action
    reasoning: str                   # attacker-reachable. audit + display only.
    model: str
    raw_response: str                # for debugging bad JSON
```

`policy.evaluate()` takes `Action`, never `ActionEnvelope`. The injection
boundary is a function signature, not a comment.

### 2.2 Decision

```python
class DecisionStatus(str, Enum):
    ALLOW    = "allow"
    DENY     = "deny"
    ESCALATE = "escalate"

class Decision(BaseModel):
    action_id: str
    status: DecisionStatus
    matched_rules: list[str]         # ALL rules that matched, in file order
    rule_id: str | None              # the one that determined status
    policy_version: str              # sha256 of policy.yaml at decision time
    reasoning: str                   # rendered from the rule, not from the LLM
    decided_by: Literal["policy", "human", "system"]
    payload_hash: str                # must match at execute time
    decided_at: datetime
```

A `Decision`, once written, is **immutable and authoritative**. Resuming an
approved escalation replays the stored `Decision`; it never re-runs
`evaluate()`. That is what makes `policy_version` meaningful under Phase 3
hot reload.

---

## 3. Policy engine

```python
def evaluate(action: Action, history: HistoryQuery) -> Decision
```

Pure function of its two arguments. No DB handle, no network, no clock beyond
what `history` exposes.

### 3.1 History is a query protocol, not a list

```python
class HistoryQuery(Protocol):
    """Counts EXECUTED OUTCOMES ONLY. Proposed-and-denied actions never
    count toward a cumulative cap, or a rejected action would consume the
    victim's own limit."""
    def sum_amount_cents(self, *, agent: str, action_type: ActionType,
                         window: timedelta) -> int: ...
    def count(self, *, agent: str, action_type: ActionType,
              window: timedelta) -> int: ...
    def distinct_targets(self, *, agent: str, action_type: ActionType,
                         window: timedelta) -> int: ...
```

Production passes a SQLite-backed implementation. Tests pass a hand-built
fake. The policy engine never learns which it got.

Windowed by wall-clock `timedelta`, not by session — structuring across
sessions is the actual attack, and a session boundary is attacker-chosen.

### 3.2 Resolution: all rules evaluated, most restrictive wins

Not first-match-wins. Every rule is evaluated; matches are collected;
`deny > escalate > allow`. `Decision.matched_rules` records all of them so
the audit trail shows what else fired.

```
  rule set  ──> [FIN-001 escalate, FIN-003 deny]  ──> DENY (FIN-003)
                 matched_rules = [FIN-001, FIN-003]
```

Rationale: first-match-wins makes correctness depend on YAML line order.
One reorder silently downgrades a deny to an allow. Most-restrictive-wins
is order-independent.

### 3.3 Fail closed

| Condition | Result | rule_id |
|---|---|---|
| `policy.yaml` malformed / unreadable | refuse to start | — |
| any predicate raises | `deny` | `SYS-ERR` |
| zero rules match | `escalate` | `SYS-GAP` |
| params fail validation | rejected by Pydantic before policy | — |
| audit write fails | `deny`, no execution | `SYS-AUDIT` |

The zero-match case matters: `ActionType` is a closed enum, so an unknown
*string* is rejected at parse time. `SYS-GAP` fires when a **known** type has
no covering rule — which is exactly what happens the day someone adds an enum
member and forgets the YAML. That, not "unseen action types", is what the
Phase 3 coverage check audits.

### 3.4 policy.yaml

```yaml
version: 1

rules:
  - id: FIN-001
    description: Single payment over $500 needs a human
    when: {action_type: make_payment, amount_cents_gt: 50000}
    then: escalate

  - id: FIN-002
    description: Over $1000 by one agent in 24h needs a human
    when: {action_type: make_payment, window_hours: 24, sum_amount_cents_gt: 100000}
    then: escalate                     # catches 5 x $499 structuring

  - id: FIN-003
    description: Unknown counterparties are denied
    when: {action_type: make_payment, target_not_in: [acme-corp, globex, initech]}
    then: deny

  - id: FILE-001
    description: Production files are never deleted by an agent
    when: {action_type: delete_file, target_glob: "*.prod.*"}
    then: deny

  - id: MAIL-001
    description: External recipients need a human
    when: {action_type: send_email, target_domain_not_in: [internal.example.com]}
    then: escalate

  - id: FILE-002
    description: Reads inside the workspace are routine
    when: {action_type: read_file, target_glob: "workspace/**"}
    then: allow
```

`policy_version` = sha256 of this file, stamped on every Decision.

---

## 4. Executors

```python
# guardian/executors.py — the ONLY module importing effector libraries

EXECUTORS = {
    ActionType.MAKE_PAYMENT: _simulate_payment,
    ActionType.SEND_EMAIL:   _simulate_email,
    ActionType.DELETE_FILE:  _simulate_delete,
    ActionType.READ_FILE:    _simulate_read,
    ActionType.WRITE_FILE:   _simulate_write,
}

def run(action: Action, decision: Decision) -> Outcome:
    if decision.status is not DecisionStatus.ALLOW:
        raise NotAuthorized(action.id)
    if decision.payload_hash != action.payload_hash():
        raise PayloadMismatchError(action.id)        # TOCTOU guard
    if (prior := audit.outcome_for(action.id)):
        return prior                                  # idempotency guard
    try:
        fn = EXECUTORS[action.action_type]
    except KeyError:
        raise ExecutorMissing(action.action_type)
    return fn(action)
```

Workers are simulated here, so executors record rather than call live
services. Swapping `_simulate_payment` for a real Stripe call touches this
file and nothing else.

---

## 5. Escalation — SQLite owns the state

**Decision (rev 3):** pending approvals live in an `escalations` table in our
own schema. LangGraph orchestrates the flow; it does not hold the state.

Rationale: the system already has a durable store and an append-only log.
Putting pending state in LangGraph's checkpoint blob creates a second source
of truth, makes the Phase 3 dashboard depend on LangGraph's internal schema,
and breaks `sqlite3 guardian.db "select * from escalations"`.

```sql
CREATE TABLE escalations (
  action_id     TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  envelope_json TEXT NOT NULL,     -- full ActionEnvelope
  decision_json TEXT NOT NULL,     -- the authoritative Decision
  payload_hash  TEXT NOT NULL,
  status        TEXT NOT NULL,     -- pending | approved | rejected | expired
  resolved_by   TEXT,
  resolved_at   TIMESTAMP,
  created_at    TIMESTAMP NOT NULL
);
CREATE INDEX idx_esc_pending ON escalations(status, created_at);
```

```python
# guardian/escalation.py
def park(envelope, decision) -> None      # status='pending'
def pending(session_id=None) -> list[...]  # CLI and dashboard both call this
def resolve(action_id, *, approved, by) -> Decision
```

`resolve()` re-verifies `payload_hash` against the stored envelope before
returning an executable Decision. Approval binds to the payload, not the id.

Phase 2 CLI and Phase 3 HTTP call the same three functions. Killing the
process mid-escalation loses nothing: the row is already committed.

### LangGraph usage

Orchestration only. Nodes: `propose -> record -> evaluate -> record ->
branch(allow|deny|escalate)`. No `interrupt_before`, no checkpointer
dependency for state.

> Note for implementation: if a checkpointer is added later for retries,
> `SqliteSaver` ships in the separate `langgraph-checkpoint-sqlite` package
> and `from_conn_string()` is a **context manager**, not a constructor.
> rev 2 had this wrong.

---

## 6. Auditor

Two jobs, deliberately separate:

- **Recorder** — append-only writes of every ActionEnvelope, Decision, and
  Outcome. Write failure aborts the action (s3.3).
- **Reporter** — `python main.py report --session <id>`.

Pattern detection is **not** here. It lives in the policy engine via
`HistoryQuery` (s3.1). The Auditor may surface cross-session patterns in its
report, but no enforcement depends on that.

Index for the history queries:

```sql
CREATE INDEX idx_outcomes_window
  ON outcomes(requesting_agent, action_type, executed_at);
```

---

## 7. Build order

### Phase 1 — enforced core

1. `schemas.py` — Action / ActionEnvelope split, discriminated params,
   `payload_hash()`, Decision.
2. `db.py` — `actions`, `decisions`, `outcomes`, `escalations` + indexes.
3. `guardian/policy_agent.py` + `predicates.py` — YAML loader,
   most-restrictive resolution, fail-closed paths.
4. **Deterministic harness, no LLM.** Hand-built Actions + fake
   `HistoryQuery`. Prove allow / deny / escalate / SYS-GAP / SYS-ERR /
   cumulative window / rule-precedence.
5. `guardian/executors.py` + `escalation.py` + `graph.py`.
6. `agents/base.py` then `agents/finance_agent.py`. Model: `gpt-4o-mini`
   (switched from the originally-planned `claude-haiku-4-5-20251001` during
   implementation, per user request -- OpenAI's Chat Completions API instead
   of Anthropic's Messages API. The enforcement boundary is provider-agnostic
   by design: policy.evaluate() never sees which LLM proposed an action, only
   the typed Action it produced, so this swap has zero effect on sections 1-6
   of this plan).
7. End to end: `pay the vendor invoice for $750` -> escalate on FIN-001.

**Exit:** all three statuses reachable; every Decision carries `rule_id` and
`matched_rules`; `test_no_effector_imports.py` green.

### Phase 2 — full loop

8. `agents/email_agent.py`, `agents/file_agent.py`.
9. CLI approval over `escalation.resolve()`. Verify: kill the process
   mid-escalation, restart, approve, action completes.
10. `main.py report --session <id>`.
11. Cross-session pattern flags in the report layer.

**Exit:** demo scenario (s12) passes end to end.

### Phase 3 — stretch (done)

12. FastAPI + HTML over the same `escalation.pending()` the CLI uses.
    `dashboard.py`: `/` (HTML), `/pending` (JSON), `/resolve/{id}` (HTML
    form), `/api/resolve/{id}` (JSON), `/policy-version`. No auth --
    confirmed with user, local/demo use only, documented in the module
    docstring as a known gap.
13. Hot-reload `policy.yaml`. New `policy_version` on reload; parked
    escalations keep their stored Decision (s2.2). `policy_agent.evaluate()`
    already re-read policy.yaml on every call before Phase 3 (no in-process
    cache to invalidate), so hot-reload for new actions was automatic;
    `policy_agent.policy_version()` added as the read-only counterpart for
    an operator to confirm the current on-disk hash without proposing an
    action. Explicit CLI (`main.py policy-version`) and HTTP
    (`GET /policy-version`) surfaces, not a file-watcher -- confirmed with
    user. `tests/test_hot_reload.py` proves a parked escalation's stored
    Decision/policy_version survive a live policy.yaml edit; verified again
    live (uvicorn running, editing policy.yaml on disk, no restart).
14. LLM coverage check over the rule set (`guardian/coverage_check.py`).
    **Constrained: may return `escalate` or `deny` only** -- enforced by a
    Pydantic `Literal["escalate", "deny"]` on `Finding.suggested_disposition`,
    not by prompt wording alone; an `"allow"` response is rejected and
    retried once, same one-retry pattern as `agents/base.py`. Callable only
    (`main.py coverage-check`), no automatic triggering -- confirmed with
    user.

**Exit:** no auth on the dashboard (demo/local use), explicit reload trigger
(not file-watch), hot-reload proven by test + live run. All confirmed with
user as this phase's stretch-scope "done" criteria.

---

## 8. Error and rescue map

No catch-all `except Exception`.

| Exception | Raised when | Caught by | Result | Operator sees |
|---|---|---|---|---|
| `PolicyLoadError` | yaml missing/malformed | startup | refuse to start | CRITICAL, non-zero exit |
| `PolicyEvalError` | predicate raises | `evaluate()` wrapper | `deny` / `SYS-ERR` | ERROR + predicate name |
| `NoMatchingRule` | zero rules match | `evaluate()` | `escalate` / `SYS-GAP` | WARN "uncovered action type" |
| `AuditWriteError` | SQLite write fails | record node | abort, no execution | CRITICAL, non-zero exit |
| `PayloadMismatchError` | hash differs at execute | `executors.run` | abort + audit entry | CRITICAL, likely tampering |
| `NotAuthorized` | run() called on non-allow | `executors.run` | abort | CRITICAL, caller bug |
| `ExecutorMissing` | enum member has no executor | `executors.run` | abort | ERROR, config bug |
| `ActionValidationError` | LLM emits bad JSON/params | `agents/base.py` | one retry, then `deny` | WARN + `raw_response` logged |

Shadow paths traced per flow: nil input, empty input, upstream error.

---

## 9. Failure modes

**9.1 Worker bypasses the guardian.** Closed structurally. Test 10.
**9.2 Prompt injection via task text.** Lands in `ActionEnvelope.reasoning`.
`evaluate()` takes `Action` and cannot reach it. Injection that alters typed
fields produces a different `action_type`/`amount_cents`, which policy
evaluates normally.
**9.3 Structuring.** FIN-002 sums executed outcomes in a 24h window before
deciding. Payment #3 crosses and escalates while #4 and #5 are hypothetical.
**9.4 Cap poisoning.** Denied attempts never enter the sum (s3.1), so an
attacker cannot burn the victim's own limit with rejects.
**9.5 Silent capability creep.** Test 10 AST-scans every module except
`executors.py`.
**9.6 Approval replay / mutation.** `payload_hash` re-verified in `resolve()`
and again in `run()`; `outcome_for()` blocks double execution.
**9.7 Escalation lost on crash.** Committed row, not in-memory graph state.
**9.8 Rule reorder changes behavior.** Most-restrictive-wins is
order-independent (s3.2).

---

## 10. Tests

```
tests/
  test_policy_rules.py         one case per rule id, matching + non-matching
  test_policy_precedence.py    deny beats escalate beats allow; order-independent
  test_policy_failclosed.py    SYS-ERR, SYS-GAP, malformed yaml refuses start
  test_structuring.py          window boundary, exactly-at-cap, just-under
  test_structuring_property.py random sequences: cum sum never exceeds cap
                               without an escalate  (hypothesis)
  test_history_excludes_denied.py   denied actions do not enter the sum
  test_payload_binding.py      mutate action post-approval -> PayloadMismatchError
  test_idempotency.py          same action id twice -> one effect
  test_audit_integrity.py      audit write failure aborts execution
  test_escalation_resume.py    park, drop process, reload, resolve, execute
  test_golden_trail.py         snapshot of `report --session demo1`
  test_no_effector_imports.py  AST scan, ALL modules except executors.py
```

`test_no_effector_imports.py` walks every `.py` outside
`guardian/executors.py` and fails on any import of `stripe`, `smtplib`,
`shutil`, `subprocess`, `requests`, `httpx`, or `os.remove`.

Known limit: an AST scan catches honest imports, not `importlib` /
`__import__` / `getattr` indirection. It is a guardrail against drift, not a
sandbox. Documented, not silently assumed.

---

## 11. Folder structure

```
guardian-agent-system/
  agents/
    base.py               prompt scaffold + JSON parse + one retry.
                          subclasses supply only their params schema.
    finance_agent.py
    email_agent.py
    file_agent.py
  guardian/
    policy_agent.py       evaluate(action, history) -> Decision
    predicates.py         typed predicates only
    executors.py          THE ONLY EFFECTOR MODULE
    escalation.py         park / pending / resolve
    auditor.py            recorder + reporter
    history.py            SQLite HistoryQuery implementation
    graph.py              LangGraph orchestration
  tests/                  see section 10
  schemas.py
  db.py
  policy.yaml
  main.py
  demo_scenarios.py
  README.md
```

---

## 12. Demo scenario

`python main.py --scenario demo1`

| # | Agent | Task | Expected | Rule |
|---|---|---|---|---|
| 1 | File | read workspace/report.md | **allow** | FILE-002 |
| 2 | File | delete config.prod.yaml | **deny** | FILE-001 |
| 3 | Finance | pay acme-corp $750 | **escalate** -> approve | FIN-001 |
| 4 | Email | mail vendor@external.com | **escalate** -> reject | MAIL-001 |
| 5 | Finance | pay globex $499 x3 | #1 #2 allow, #3 **escalate** | FIN-002 |
| 6 | Finance | pay shadowco $100 | **deny** | FIN-003 |

Scenario 5 is the centerpiece: each payment clears the $500 single-payment
cap, and the third is stopped before it executes. Closing report prints the
full trail with `rule_id`, `matched_rules`, `decided_by`, and running totals.

---

## 13. Change log

### rev 2 (`/plan-ceo-review`) — 9 defects

| # | Defect | Fix |
|---|---|---|
| 1 | Workers held execution capability | Executor registry behind Guardian |
| 2 | Injection via free-text fields | Typed-fields-only policy contract |
| 3 | Pattern detection post-hoc | Moved into policy evaluation |
| 4 | No fail-closed semantics | Error table with deny/escalate defaults |
| 5 | Approval not bound to payload | `payload_hash` verified at execute |
| 6 | Decision could not name its rule | `rule_id` + `policy_version` |
| 7 | No durable escalation | (superseded by rev 3 / A2) |
| 8 | Phase 3 meta-check could allow | Constrained to escalate/deny |
| 9 | No test scope | `tests/` + deterministic harness |

### rev 3 (`/plan-eng-review`) — 15 findings

| ID | Sev | Finding | Fix |
|---|---|---|---|
| A1 | Med | `SqliteSaver.from_conn_string` misused; separate pip package | No longer load-bearing; noted in s5 |
| A2 | High | Checkpointer + audit log = two sources of truth | `escalations` table owns state (s5) |
| A3 | High | `history: list[Action]` couples policy to data loading | `HistoryQuery` protocol (s3.1) |
| A4 | High | Cumulative sum could count denied attempts | Executed outcomes only (s3.1, s9.4) |
| A5 | Med | Closed enum made "unknown action type" unreachable | `SYS-GAP` reframed to coverage gaps (s3.3) |
| A6 | Med | AST test scanned only `agents/` | Scans all but `executors.py` (s10) |
| C1 | Med | `Decimal` hashing unstable across `750.0`/`750.00` | `amount_cents: int` (s2.1) |
| C2 | Low | `params: dict` untyped on a security boundary | Discriminated union (s2.1) |
| C3 | High | `reasoning` on the model policy receives | `Action` / `ActionEnvelope` split (s2.1) |
| C4 | Low | `agents/base.py` unspecified, 3x duplication risk | Scope named (s11) |
| T1 | Med | "first match wins" contradicted "deny beats escalate" | Most-restrictive-wins (s3.2) |
| T2 | Low | No golden test on the audit trail | `test_golden_trail.py` |
| T3 | Low | No property test on structuring | `test_structuring_property.py` |
| T4 | Low | AST scan limits undocumented | Stated in s10 |
| P1 | Low | History query would table-scan | `idx_outcomes_window` (s6) |

---

## GSTACK REVIEW REPORT

| Run | Skill | Status | Findings |
|---|---|---|---|
| 1 | `/plan-ceo-review` | COMPLETE | 9 defects, all applied in rev 2 |
| 2 | `/plan-eng-review` | COMPLETE | 15 findings (4 high, 5 med, 6 low), all applied in rev 3 |

Sections executed: Step 0 scope challenge, Architecture, Code Quality, Tests
(with coverage diagram), Performance.

Outside voice: not run. `codex` not available in this environment.

Decisions taken:
- Guardian owns the executor registry; workers emit inert typed Actions. (run 1)
- SQLite `escalations` table owns pending state; LangGraph orchestrates only. (run 2)

Known gaps carried forward, by choice:
- AST import scan is a drift guardrail, not a sandbox (s10).
- Worker agents are simulated; no live effector integration in scope.
- No design doc (`/office-hours` not run) — problem statement came from the
  original build prompt.

VERDICT: APPROVED FOR PHASE 1

NO UNRESOLVED DECISIONS
