# TODOS

Deferred work, tracked with enough context that picking it up in 3 months
doesn't require re-deriving the reasoning. Created 2026-08-29 by
`/plan-eng-review` on `docs/designs/2026-08-24-external-framework-integration.md`
— this file was the design doc's own Phase 0 deliverable (§9-rev), never
built until now.

---

## 1. Orphaned-approval hole (D7)

**What:** If an agent process exits (crash, workflow run ends) while an
action is parked pending human approval, `resolve_and_execute()` still runs
correctly and the outcome is durably recorded — but nothing calls back into
a workflow that no longer exists. The calling agent never learns the
outcome.

**Why:** Design doc §8 flagged this as a genuine open hole. Mode A's
error-and-retry pattern (`ActionPending`, §12e/§13) resolves this for the
common case — a *live* agent's next turn re-invokes the guarded function and
picks up the resolved status — but only if the agent's own process/session
is still alive to have a "next turn."

**Pros of fixing:** Closes the last gap in escalation lifecycle
completeness; would let a customer build reliable long-running approval
workflows without a polling script of their own.

**Cons of fixing:** Fundamentally a workflow-resumption problem, not a
Guardian problem — the fix (a callback/webhook mechanism) is real new scope
(callback signature, delivery guarantees, retry-on-callback-failure creates
its own recursive problem) that the design doc explicitly deferred to phase
5 (§11: "per-framework adapters"). A half-built notification mechanism with
no delivery guarantee could be worse than none (false confidence).

**Context:** Guardian's own audit trail is correct in this scenario — the
Decision is durable, `decided_by="human"` is recorded, Article 14 evidence
properties hold. This is purely about *notifying* a dead caller, which is
customer/framework-specific glue. `guardian/escalation.py:pending()` already
lets a customer build their own re-poll/resume logic (e.g. a cron job that
re-drives a workflow for resolved-but-unclaimed escalations).

**Depends on:** Nothing blocks starting this; revisit when a real customer
integration needs it, or at phase 5 (per-framework adapters).

---

## 2. Multi-writer SQLite / shared-DB misconfiguration (D8)

**What:** `guardian/sdk.py:context()` takes a raw `sqlite3.Connection` with
no path validation. If two agent processes each call `db.init_db()` with a
default or different path, cumulative caps like FIN-002 silently stop
working — each process sees its own empty history and both allow.

**Why:** Design doc §7e: "if two agent processes each keep their own
history, cumulative caps silently stop working... easy to miss until it's a
production incident." This review accepted a startup-log fix (log the
resolved absolute DB path at first use, document the shared-file
requirement in the SDK module docstring) rather than a hard runtime check.

**Pros:** A grep-able startup log line turns a silent multi-day-to-discover
misconfiguration into something an operator can catch immediately.

**Cons:** Doesn't *prevent* the misconfiguration, only surfaces it — a
customer who doesn't check startup logs can still hit it. Also: once T1
(below) lands `BEGIN IMMEDIATE` reservations, multi-writer SQLite lock
contention under real concurrent load becomes a live characteristic worth
load-testing, not just a documentation note.

**Context:** SQLite itself handles the concurrency correctly once processes
share one file — this is purely about making sure they do.

**Depends on:** Land alongside or after T1 (item 3) since T1 touches the
same code path (`guardian/sdk.py:_submit()` / `context()`).

---

## 3. Package extraction (§12c)

**What:** Design doc §1's Phase 1 deliverable was "extract core into an
installable package + open registry." Only the open-registry half shipped.
There's no `pyproject.toml`/`setup.py`, and `guardian/sdk.py`,
`escalation.py`, `graph.py`, `auditor.py`, and `main.py` all still do bare
`import db` / `import schemas` assuming repo-root placement.

**Why:** This is invisible today because everything runs from this one
repo. It becomes a hard blocker the moment anyone tries
`pip install agent-guardian-sdk` from a customer's own project — `import
db` will either fail or silently shadow the customer's own `db.py` if they
happen to have one.

**Pros:** Unblocks real external distribution — the actual point of the
whole external-framework-integration effort.

**Cons:** Real, non-trivial refactor. Touches every core file AND
`tests/test_no_effector_imports.py` — the test that enforces Invariant 1 —
which §12c specifically warns should be isolated into its own commit rather
than bundled with a layout change, since changing the file that *proves* a
security property in the same diff as a structural change makes both harder
to review and to trust. Also needs cwd-relative default fixes
(`policy_agent.py`'s `policy_path="policy.yaml"`, `db.py`'s
`init_db(path="guardian.db")`), which resolve against cwd — arbitrary in
someone else's process.

**Context:** This was deliberately scoped OUT of the current PR (which
stays "open registry + SDK logic" — see review note below) specifically
because bundling extraction with active SDK development risks exactly the
"structural + behavioral changes simultaneously" antipattern.

**Depends on:** Nothing technical blocks starting; sequence after the
current SDK work stabilizes so the import-topology change lands against a
settled `guardian/sdk.py`, not a moving target.

---

## 4. Escalation-scan performance threshold (§12b-adjacent)

**What:** `guardian/sdk.py:_find_matching_escalation` does a Python-level
loop with full `ActionEnvelope.model_validate_json()` deserialization for
every escalation in a session, on every `@guarded` call where policy says
ESCALATE (not just retries of the same action). The function's own
docstring calls this "O(session size), fine at demo/single-tenant scale"
without a number.

**Why:** A session with hundreds of escalations (a busy multi-tool agent
running for hours) means hundreds of JSON deserializations on every new
escalate-decision call.

**Pros of optimizing:** Removes a scaling cliff before a customer hits it
in production instead of during a review.

**Cons of optimizing now:** No measured production case yet, and this same
code path is about to change shape once item 3/T1's reservation-table work
lands — optimizing ahead of that risks rework.

**Context:** Concrete trigger to act on (per this review — vague
"demo-scale" deferrals aren't good enough): benchmark at 500+ pending
escalations per session; consider indexing by a semantic-key hash in SQLite
instead of a Python-side scan if it becomes real.

**Depends on:** Revisit after T1's reservation-table schema work lands,
since that touches the same table shape.

---

## 5. Framework retry-semantics verification

**What:** `guardian/sdk.py`'s error-and-retry design (`ActionPending`,
§12e/§13) assumes the calling agent's "next turn" deliberately re-invokes
the guarded function once a human has resolved the escalation. Most agent
frameworks' default tool-error retry policies (LangGraph, CrewAI, ADK)
retry *any* exception a bounded number of times then give up — they may not
distinguish "wait for a human, retry later" from "transient failure, retry
now."

**Why:** A framework could burn through its retry budget hitting
`ActionPending` before a human ever resolves it, then permanently abandon
the tool call and hallucinate a workaround. Unlike §13's S1-S3 spike (which
verified MCP's actual protocol behavior before building the MCP-first
design, then correctly reversed course when the spike failed), no
equivalent spike verified real framework retry semantics before mode A was
built.

**Pros:** A cheap spike (per §12f's own precedent) settles this with
evidence instead of an assumption baked into the load-bearing UX mechanism.

**Cons:** Real time cost (~1 day human / ~2-3 hours CC) to actually stand
up a LangGraph tool node and observe its default retry behavior against a
raised exception.

**Context:** This review recommended running the spike **before landing
this PR**, not deferring it — recorded here in case it doesn't fully
resolve before ship, so the open question has a home. If the spike found
frameworks surface exceptions to graph state rather than blind-retrying,
this item closes with evidence. If not, `ActionPending`'s design needs
revisiting (e.g., a documented "how to configure your framework's retry
policy for this exception type" integration guide, or a different signal
than a raised exception).

**Depends on:** Should be resolved (spike run) before or alongside the
current PR, not truly deferred — flagged as still-open only if that spike
hasn't completed yet.

---

## 6. WRITE_FILE has no policy.yaml coverage

**What:** `WRITE_FILE` has a registered executor and a `WorkerAgent`
(`agents/file_agent.py`'s `WriteFileAgent`) but zero `policy.yaml` rules.
Pinned down by `tests/test_registry.py::test_uncovered_action_types_against_real_policy_yaml`.

**Why:** A proposed write silently falls through to `SYS-GAP`/escalate the
first time an agent tries it — correct behavior (fail-closed), but was
previously invisible until that moment. E3's startup warning
(`main.py:warn_uncovered_action_types`) now surfaces this loudly at boot.

**Pros:** Closing it removes one of the startup warnings a real deployment
would see on day one.

**Cons:** None real — this is pure policy-content work, not a code fix.

**Context:** This is intentionally left to whoever owns `policy.yaml` next
— the test exists specifically to pin the gap down rather than paper over
it, not to force an immediate fix. What should `write_file`'s rule actually
say (which paths are safe to auto-allow, which need escalation) is a
product/security decision, not an engineering one.

**Depends on:** Nothing; can close independently at any time by adding a
rule to `policy.yaml`.
