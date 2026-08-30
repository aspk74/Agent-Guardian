# Design: wiring Agent-Guardian into external agent frameworks

Date: 2026-08-24 (revised 2026-08-25 after `/plan-ceo-review`; status updated 2026-08-29 after `/plan-eng-review`)
Branch: `main`
Status: Phase 1 (mode-A SDK: `guardian/sdk.py`, `guardian/registry.py`, `@guarded`/`ActionSpec`)
is SHIPPED as of commit `d4cdeec`. This document is now primarily a historical record of the
decision process (D1-D3, the §13 spike, the §12 deep review) — see `TODOS.md` for the current
open-items list and the `## GSTACK REVIEW REPORT` section at the end of this file for the
2026-08-29 eng review's findings against the shipped code.
Supersedes: nothing. First design doc for the productization pivot.

**Decisions taken in CEO review (2026-08-25).** Read these before the body — several
sections below were written under the superseded commercial premise and are marked inline.

| # | Decision | Effect on this doc |
|---|---|---|
| D1 | **Open-core.** OSS the enforcement engine; commercial layer stays possible later, not now. | §3's "sellable to startups" premise is superseded. See §3d. |
| D2 | ~~Extract core, then MCP proxy as the first integration.~~ **DECIDED AGAINST, 2026-08-25.** First integration is now the **in-process hook/SDK (mode A)**. | §5-rev is superseded. See §13. |
| — | ~~Bespoke `Action` wire protocol CUT from phase 1.~~ Moot — no wire protocol needed for an in-process call. | — |
| D3 | Review posture: SELECTIVE EXPANSION. | Three expansions accepted, see §9-rev. |

> ⚠️ **§5-rev and §9-rev's phasing are superseded. Read §13 before acting on this document.**
> The spike (S1-S3) ran against the MCP spec directly on 2026-08-25 and returned a decisive
> answer, not an ambiguous one. Do not build the MCP proxy as phase 1. §13 has the verdict,
> the spec citations, and the revised plan.

Full rationale and the deferred list: `~/.gstack/projects/Agent-Guardian/ceo-plans/2026-08-25-open-core-mcp-proxy.md`

---

## 1. What we're actually deciding

Today Guardian only guards agents that live in this repo. The three demo workers
(`agents/finance_agent.py`, `email_agent.py`, `file_agent.py`) are safe because they
physically cannot execute anything — enforced by an AST scan across every module
(`tests/test_no_effector_imports.py:52`).

The question on the table: **how does a startup running PraisonAI / Google ADK / Agno /
LangGraph / CrewAI agents get this same protection without rewriting their agents into
our `WorkerAgent` base class?**

That is the whole problem. Everything below is in service of it.

---

## 2. Where we are (grounded)

What exists and works, verified by reading the code:

| Piece | File | Generic? |
|---|---|---|
| Quarantine split (`Action` vs `ActionEnvelope`) | `schemas.py:51-77` | **Yes** — fully generic |
| Most-restrictive-wins resolution | `guardian/policy_agent.py:83-87` | **Yes** |
| Fail-closed (`SYS-ERR` deny, `SYS-GAP` escalate) | `guardian/policy_agent.py:68-98` | **Yes** |
| `payload_hash` tamper binding | `schemas.py:62-67`, re-checked at `executors.py:96` and `escalation.py:84` | **Yes** |
| SQLite escalation + crash recovery | `guardian/escalation.py`, `db.py:53-64` | **Yes** |
| Audit trail | `guardian/auditor.py`, `db.py:16-51` | **Yes** |
| Dashboard | `dashboard.py` | Yes, minus auth |
| `ActionType` | `schemas.py:19-24` — closed 5-member enum | **No** — demo-specific |
| `Params` | `schemas.py:47` — closed 3-member union | **No** — demo-specific |
| Predicates | `guardian/predicates.py` — 6 hardcoded conditions | **No** — demo-specific |
| Executors | `guardian/executors.py:81-87` — all simulated | **No** — stubs |

So roughly 70% of the value is already domain-neutral. The work is not "rewrite Guardian."
The work is (a) opening the vocabulary, and (b) getting Actions *out of somebody else's
agent process* without lying about what that costs.

Point (b) is the hard part and most of this doc.

---

## 3. Premise challenge

**Is this the right problem?** Three things to argue with.

**3a. The category is crowded.** As of 2026 there is a real "MCP gateway / agent
governance" market: TrueFoundry, Obsidian Security, obot, Microsoft's control plane, and
the open-source Agent Governance Toolkit all sit between agents and tools and evaluate
calls against policy. Entering as "another thing that filters tool calls" is entering a
knife fight with funded security companies who have distribution we don't.

**3b. But there is a specific, documented gap.** From the landscape scan: *an approval
workflow built into Copilot Studio doesn't automatically apply to an agent built in Gemini
Enterprise — two separate human-in-the-loop implementations have to be maintained and kept
in sync.* Nobody owns **framework-agnostic approval + audit**. The gateways own *blocking*;
they are much weaker on *"a named human said yes to this exact payload, and here is the
immutable record."*

**3c. The timing is unusually good, and this reframes the product.** EU AI Act Chapter III
high-risk obligations applied **2026-08-02 — three weeks ago**. Article 14 requires human
oversight that is meaningful rather than symbolic: the overseer must be able to interpret
the output, override it, and halt the system — *and the organization must be able to prove
oversight was actually exercised*, with the override attributable to a verified natural
person recorded in the audit trail.

Read `guardian/escalation.py:64-109` against that sentence. `resolve()` already:
- requires a named human (`by: str`)
- re-verifies `payload_hash` so the human demonstrably approved *the exact payload proposed*,
  not a mutated one (the TOCTOU guard)
- writes `decided_by="human"` into an immutable `Decision`
- stamps `policy_version` — the sha256 of the ruleset in force at that moment

That is an Article 14 evidence artifact. We built it as a safety feature; the regulatory
deadline just turned it into a compliance feature.

> **Positioning consequence:** Guardian's wedge is probably not "we block bad agent
> actions" (crowded, commoditizing). It's **"we produce the oversight evidence you now have
> to produce, across every framework you run."** Same code, much less contested ground.
>
> Caveat, stated plainly: this is a positioning observation, not legal advice, and shipping
> this does not make anyone Article 14 compliant. Do not put a compliance claim in
> marketing without counsel.

**What if we do nothing?** The repo stays a portfolio-grade demo. That is a legitimate
choice — the memory note from 2026-08-24 says scope was deliberately held at local/demo.
This doc exists to make the *next* choice explicit, not to assume it.

### 3d. Post-review correction: the commercial premise did not survive

Everything above §3c argued for a product sold to startups. CEO review on 2026-08-25
rejected that framing. Five objections, in order of weight:

1. **Security infra is a distribution game and there is no distribution.** MCP gateway
   vendors win on being purchasable — SOC 2, a support contract, someone to blame at 3am —
   not on better policy semantics. A solo repo does not enter a security team's
   consideration set regardless of code quality.
2. **The market may be a feature, not a product.** Every framework is adding native
   approval hooks. A cross-framework layer then only matters to shops running several
   frameworks, which is mostly enterprises, which loops back to objection 1.
3. **§3c's compliance angle cuts both ways, and this doc undersold that.** Compliance
   buyers need auditor-recognized artifacts and vendor attestations. "A Python library on
   GitHub emits this JSON" is not what a compliance officer brings to their auditor. The
   angle makes the product more credible technically and *less* accessible commercially.
4. **Who has this pain in August 2026?** Most agent deployments are still read-heavy or
   already gated by a human. "Agent autonomously takes irreversible action at volume" is
   growing fast but small right now. Building for a market 18 months out is a real risk.
5. **§4a's insight makes adoption harder.** Credential custody is correct, and "rewire
   where your Stripe key lives" is not a ten-minute integration.

**What survives:** correctness on subtle semantics most competitors get wrong, and an
audit trail whose properties are hard to retrofit. Both earn adoption in open source and
are invisible in a sales cycle.

**Estimate correction.** Every "human ~N weeks / CC ~N days" figure in this doc measures
implementation only. CC compresses building. It does not compress distribution, trust, or
sales. §5's effort numbers were reweighted on that basis before D2 was decided.

**Resolution: D1 = open-core.** OSS the enforcement core and become the reference
implementation for doing this correctly. Distribution gets solved by adoption rather than
procurement, the differentiator is exactly what earns credibility in OSS, design partners
arrive free, and a commercial layer stays available later on the standard open-core path.

---

## 4. The invariant that breaks — read this section twice

Guardian rests on two invariants (`PLAN.md:6-9`). One survives the port. One does not.

**Invariant 2 — "the policy engine cannot see attacker text" — survives intact.** It's
enforced by the type system: `evaluate()` takes `Action`, which has no prose field. That
holds no matter who constructs the `Action`. Nothing to do.

**Invariant 1 — "a worker agent cannot cause an effect" — does not survive, and pretending
otherwise would be the single worst thing we could do.**

Today it is enforced *structurally*, by module topology:

```
  THIS REPO (today)
  ─────────────────
  agents/finance_agent.py   ── imports ─→  schemas, openai          no effector
  guardian/executors.py     ── imports ─→  stripe / smtplib / os    THE ONLY ONE
                                            ↑
                          tests/test_no_effector_imports.py AST-scans
                          every other file and fails the build
```

Now put that in a customer's process:

```
  CUSTOMER'S AGENT PROCESS (external framework)
  ────────────────────────────────────────────
  their_agent.py     ─→ framework ─→ their_tool_pay()  ─→ import stripe
                                          ↑                     ↑
                                   our before_tool          credentials
                                   hook fires here          live HERE, in
                                   (cooperative)            the same process
```

Two things are now true that were not true before:

1. **The AST scan is meaningless.** We cannot scan the customer's repo — and even if we
   did, their agent process *legitimately* imports `stripe`, because that's where their
   tool lives. There is no file topology to enforce.
2. **A `before_tool` hook is a hook, not a boundary.** The framework calls us, then calls
   the tool. That is cooperation. Any code path that reaches the underlying function
   directly — a second tool that wraps it, a retry helper, an `importlib` lookup, a
   framework upgrade that changes hook ordering — goes around us entirely.

So an in-process adapter gives real policy evaluation, real audit, real human-in-the-loop.
It does **not** give non-bypassability. Those are different products with different
security claims, and for a *security* product, blurring them is fatal.

### 4a. What actually restores the boundary: credential custody

The generalizable version of "cannot cause an effect" is not "cannot import a library."
It is:

> **The process that decides has no credentials. The process that holds credentials does not decide.**

If the agent process holds no Stripe key, it cannot pay anyone regardless of what its code
does, what the LLM was tricked into emitting, or which hook got skipped. That property:

- survives arbitrary customer code (no scanning required)
- survives prompt injection
- survives framework upgrades
- is enforceable across a process boundary, which is a thing operating systems actually enforce

This is the honest generalization of Invariant 1, and it should drive the architecture.

**The trap to avoid:** if we ship in-process-only first, customers build against that API,
and moving credentials out later becomes a breaking change we will never make. The wire
boundary has to exist from day one even if both processes start on the same box.

---

## 5. Three shapes of interposition

### APPROACH A — In-process SDK (`@guarded` decorator + framework callbacks)

```
  agent ──→ framework ──→ [guardian hook] ──→ real tool ──→ effect
                                │
                          same process,
                          same DB handle
```

**Summary:** Ship `guardian-sdk`. Customer decorates tools, or registers our
`before_tool_callback`. We build an `Action` from the call args, run `evaluate()`, and
either call through, raise, or park.

- **Effort:** S (human ~1 week / CC ~half a day)
- **Risk:** Low to build, **Medium to sell** — the security claim is weaker than what this
  repo currently demonstrates
- **Reuses:** everything. `executors.py` becomes "invoke the wrapped callable"
- **Pros:** genuinely one line of customer code; every framework has the hook; instant demos
- **Cons:** no non-bypassability; credentials co-resident with the agent; we'd be selling a
  weaker property than the one we can currently prove

### APPROACH B — Out-of-process broker (credential custody)

```
  CUSTOMER PROCESS                    │  GUARDIAN PROCESS
  ────────────────                    │  ────────────────
  agent ─→ framework ─→ adapter ──────┼──→ evaluate() ─→ escalation ─→ executor
           (no creds at all)   Action │                                    │
                               over   │                              holds creds
                               wire   │                                    ↓
                                      │                                 effect
```

**Summary:** Guardian runs as its own local service. The adapter serializes an `Action` and
sends it. Guardian evaluates, parks if needed, and — critically — **performs the effect
itself**, using credentials the agent process has never seen.

- **Effort:** L (human ~4-6 weeks / CC ~3-5 days)
- **Risk:** Low security-wise; Medium adoption-wise — the customer has to move credentials
  and write executors on our side, which is a much bigger ask than a decorator
- **Pros:** preserves the actual invariant; language-agnostic; the audit trail becomes
  trustworthy because the agent couldn't have acted without us
- **Cons:** real integration work; the customer's "thin adapter" is no longer thin on the
  executor side

### APPROACH C — MCP interposition

```
  agent ─→ MCP client ──→ [GUARDIAN MCP PROXY] ──→ customer's MCP servers ─→ effect
                                  │
                          typed tool schemas
                          arrive for free
```

**Summary:** Guardian is an MCP proxy. Every MCP tool call flows through it.

- **Effort:** M/L (human ~3-4 weeks / CC ~2-3 days)
- **Risk:** Medium — crowded category
- **Pros:** framework-agnostic *by construction* (everything speaks MCP now); process
  boundary for free; **the tool's JSON Schema is the `Params` vocabulary, so the typed
  contract we need is already written by the customer**
- **Cons:** only covers MCP-mediated effects (misses in-process SDK tools entirely);
  competes head-on with funded MCP gateway vendors

### Recommendation (SUPERSEDED — see §5-rev)

The reasoning below was written under the commercial premise §3d rejected. It is kept
because the security analysis in it still holds; its *ordering* is what changed.

**Build B's wire protocol as the architecture. Ship A as the on-ramp. Add C as the second
adapter.**

Concretely: define the `Action` wire format and the broker contract *first*. Then ship the
in-process adapter (A) speaking that exact protocol — so adoption costs one decorator — but
with the broker able to run out-of-process from the start. MCP (C) then becomes just
another client of the same protocol rather than a parallel implementation.

Rationale mapped to the engineering preferences in this repo:
- **Explicit over clever:** two named deployment modes with two clearly documented security
  properties beats one mode with a fuzzy claim.
- **Right-sized diff:** the protocol is the load-bearing decision. Adapters are small once
  it exists.
- **Not under-engineered:** shipping A alone would foreclose B permanently.
- **Not over-engineered:** we are not building a distributed system on day one — mode A and
  mode B can share a process initially and split later without an API change.

The one non-negotiable: **document per-mode security properties in the README, and never
let mode A borrow mode B's guarantee in any demo, doc, or pitch.**

### §5-rev. Revised recommendation (D2, 2026-08-25)

**Extract the core, then ship C (MCP proxy) as the first integration. A becomes the
fallback for non-MCP effects. B's bespoke wire protocol is cut from phase 1.**

Two things forced the reversal.

**1. The tradeoff this doc set up is false on the MCP path.** §4a and §5 present an
opposition: easy adoption (A) gives the weak cooperative guarantee, the strong guarantee
(B) demands painful credential rewiring. MCP breaks it. In an MCP topology **credentials
already live in the tool server process, not the agent process.** The agent physically
cannot reach Stripe except through Guardian, then the Stripe MCP server. So a Guardian MCP
proxy gets credential custody *from the architecture*, with zero customer rewiring, while
adoption stays a config change rather than a code change. Lowest friction and the strong
property on the same path. That combination is not available on any other route here.

**2. MCP is already the wire protocol.** This doc's headline recommendation was to define
a bespoke `Action` wire format first and have everything speak it. With MCP as the first
integration that is speculative generality: MCP's JSON-RPC tool-call format already
carries typed arguments and a JSON Schema, which is exactly what `Action`/`Params` needs.
Defining a parallel format before a second integration exists to justify it buys nothing.
If the in-process SDK later needs its own transport, define it then, informed by two real
cases instead of zero.

**Cost accepted:** the MCP proxy only guards MCP-mediated effects. An agent calling the
Stripe SDK directly inside a tool body is untouched until mode A ships. That gap is
documented, not assumed away — and per §5's non-negotiable, mode A must never be described
with mode C's guarantee.

---

## 6. What a customer actually writes

The registration contract. Three things per guarded tool:

```python
from guardian.sdk import guarded, ActionSpec

@guarded(ActionSpec(
    action_type="issue_refund",        # 1. their vocabulary, not ours
    params_model=RefundParams,         # 2. typed. pydantic or JSON Schema
    target_field="customer_id",        # 3. REQUIRED — see below
))
def issue_refund(customer_id: str, amount_cents: int) -> Receipt:
    ...
```

Plus rules in their own `policy.yaml`:

```yaml
- id: REFUND-001
  description: Refunds over $200 need a human
  when: {action_type: issue_refund, amount_cents_gt: 20000}
  then: escalate
```

**Why `target_field` is mandatory and not inferred.** `agents/base.py:13-19` records a bug
found by running the live demo: policy rules key off `action.target`, so if the *model* is
free to phrase it ("acme-corp" vs "vendor_invoices/acme-corp" vs "Acme Corp"), the same
counterparty produces different policy outcomes depending on the model's mood. `target` must
be derived deterministically from a declared field. That lesson generalizes directly — an
external tool's target must be *declared at registration*, never inferred from the call, and
never LLM-phrased. This is a hard requirement on the contract, not a nicety.

---

## 7. Hard problems that need decisions

### 7a. Escalation latency — the biggest unsolved UX problem

Today escalation blocks on a terminal `y/n` prompt. An agent framework calls a tool and
expects a return value in seconds. Policy says ESCALATE. Now what?

```
                        tool call arrives
                               │
                        evaluate() → ESCALATE
                               │
              ┌────────────────┼────────────────┐
              ↓                ↓                ↓
        SYNC BLOCK       RAISE + ABORT     SUSPEND + RESUME
      wait for human    agent gets error   park, return handle,
      (timeout N sec)   and moves on       resume run on approval
              │                │                    │
      agent thread hangs   agent may route     needs framework
      for minutes/hours    around the block    durable-suspend
      ✗ unusable for       ✗ loses the work    support
        long approvals       and may retry     ✓ correct
                             a variant         ✗ not universally
                                                 available
```

None of the three is universally right. Proposed answer: **support sync-with-timeout and
suspend/resume as declared per-tool modes**, and be explicit in docs that suspend/resume
requires framework support (LangGraph has interrupts; ADK/CrewAI/Agno need checking
individually — none verified yet, flagged as an open research task).

This is the question most likely to be underestimated. It deserves eng-review attention.

### 7b. Opening the vocabulary without losing fail-closed

`ActionType` is a closed enum today, and that closedness is what makes `ExecutorMissing`
(`executors.py:21-24`) a startup-detectable config bug rather than a runtime surprise.

Proposal: an **open registry, validated closed at startup.** Every registered action type
must have (a) at least one policy rule mentioning it and (b) a registered executor, or the
process refuses to start — mirroring the existing "malformed policy.yaml refuses to start"
rule (`policy_agent.py:54-56`). Runtime gaps still fall through to `SYS-GAP` → escalate,
which already behaves correctly for unknown actions.

### 7c. Custom predicates

Customers will need conditions we didn't write. Three options:
- **closed predicate library** — safe, limited, we become the bottleneck on every new rule
- **Python plugins** — maximally expressive, but it's arbitrary code execution inside the
  policy engine, which is a grim thing to put in a security product
- **expression DSL (CEL)** — sandboxed, proven at scale in Kubernetes and Envoy, Layer 1
  "tried and true" rather than something we invent

Leaning CEL, but it's real work and probably not MVP. MVP can ship the closed library with
the extension point *designed* but not open.

### 7d. Multi-tenancy, auth, and the dashboard

`dashboard.py` has no auth by design and says so. The moment this leaves one laptop that
becomes a P0 — an unauthenticated approve button is a remote "make the agent do it" button.
Note that Article 14 evidence requires the approver be a *verified* natural person, so
authentication is not a deployment detail here; it's load-bearing for the product's central
claim. Also: `db.py` has no tenant column anywhere.

Explicitly flagging this as **out of scope for the first integration** but **blocking for
anything hosted**.

### 7e. Where does `history` come from?

`HistoryQuery` (`policy_agent.py:20-27`) counts *executed outcomes only* — that's what makes
cap-poisoning (`PLAN.md:9.4`) structurally impossible. In a multi-process world, if two
agent processes each keep their own history, cumulative caps like FIN-002 silently stop
working: two processes each paying $600 against a $1,000/day cap both see $0 of history and
both allow. **Cumulative rules require a single shared history store.** This is a strong
additional argument for the broker (B) and against pure in-process (A), and it is easy to
miss until it's a production incident.

---

## 8. Shadow paths

Per house rules, every new flow gets nil / empty / upstream-error traced.

| Flow | nil input | empty input | upstream error |
|---|---|---|---|
| Adapter builds `Action` from call args | missing `target_field` → refuse to register **at startup**, not at call time | empty params → pydantic validation error → treat as `ActionValidationError`, deny | tool called with unexpected kwargs → deny, log raw |
| Adapter → broker over wire | broker unreachable → **fail closed: deny** (never fail open) | empty response → deny | timeout → deny + audit `SYS-UNREACHABLE` |
| Broker → executor | no executor registered → `ExecutorMissing`, refuse at startup | — | executor raises → `ExecutionFailed`, decision already durable, retry path exists (`graph.py:106`) |
| Escalation → resume | agent process died while parked | — | approval arrives after agent gone → outcome recorded, agent never learns; **needs a resolution** |

The last row is a genuine open hole: an approval that lands after the calling agent has
exited. The action executes correctly and the audit trail is right, but nothing tells the
originating workflow. Flagging rather than papering over.

---

## 9. Phasing

### 9-rev. Revised phasing (2026-08-25)

| Phase | Deliverable | Gate to next |
|---|---|---|
| 0 | Land current branch; create `TODOS.md` (does not exist yet, and the deferred list needs a home) | tests green |
| 1 | Extract core into an installable package + open `ActionType`/`Params` registry + startup validation (**E3**: refuse to boot, or warn loudly, on a registered type with no rule and no executor) | new action type registrable end-to-end with zero core edits |
| 2 | MCP proxy (mode C): tool call → `Action` → evaluate → allow/deny/escalate. Includes **E1** shadow mode as a proxy flag, default off | a real external MCP agent gets escalated and approved; shadow mode reports without enforcing |
| 3 | **E5** 60-second quickstart with a no-API-key dangerous-demo path | a stranger sees an agent blocked in under a minute |
| 4 | Public release | — |
| 5+ | In-process SDK (mode A) for non-MCP effects; then E2/E4/E6; auth+tenancy only if hosted; E7 rule library once adopters exist | — |

Phase 2 carries the question this doc still does not answer: **what does an MCP tool call
return when policy says ESCALATE?** See §7a. That is the highest-risk unknown in the plan
and the main thing `/plan-eng-review` should attack.

Note that "which framework first?" is no longer a phase-2 question. MCP is not a framework;
choosing it *is* the framework-agnostic answer. The question returns only at phase 5.

### Superseded phasing (kept for provenance)

| Phase | Deliverable | Gate to next |
|---|---|---|
| 0 | Land current branch; add `docs/` + open-registry spike | tests green |
| 1 | `Action` wire protocol + open ActionType registry + startup validation | new action type registrable end-to-end with zero core edits |
| 2 | In-process SDK (mode A) + **one** framework adapter, chosen by which we can actually verify hooks on | a real external agent gets escalated and approved |
| 3 | Broker split (mode B) — same protocol, separate process, credential custody | agent process provably holds no credentials |
| 4 | Auth + tenancy on dashboard | anything hosted |
| 5 | MCP adapter (mode C) | — |

---

## 10. Open decisions for review

Updated after CEO review 2026-08-25. Remaining items are what `/plan-eng-review` is for.

**Resolved:**
- ~~**D1.** A-then-B phasing vs B-only?~~ → Neither. MCP-first (§5-rev). The tradeoff was
  false.
- ~~**D2.** Compliance-evidence vs safety-tool positioning?~~ → Neither as a *commercial*
  posture. Open-core (§3d). The compliance framing stays true technically and is a poor
  first commercial wedge.
- ~~**D3.** Which framework first?~~ → Dissolved. MCP is not a framework; picking it is the
  framework-agnostic answer. Returns at phase 5.
- ~~**D6.** EXPANSION scope now, or stay parked?~~ → Active, SELECTIVE EXPANSION. E1/E3/E5
  accepted; E2/E4/E6/E7 deferred.

**Still open, and now the eng review's job:**
- **D4.** Escalation latency (§7a) — *highest-risk unknown*. What does an MCP tool call
  return when policy says ESCALATE? Sync block, error, or suspend/resume? MCP's request
  semantics constrain this in ways this doc has not verified.
- **D5.** Predicate extensibility: closed library / CEL / plugins (§7c). Not phase 1.
- **D7.** The orphaned-approval hole in §8 — approval lands after the calling agent exited.
- **D8.** (new) §7e shared history. Cumulative caps like FIN-002 break silently across
  processes without one history store. The proxy centralizes this, which strengthens the
  D2 choice, but the failure mode needs an explicit test before anyone runs two agents.

---

## 11. Explicitly NOT in scope

- Rewriting the finance/email/file demo agents — they stay as the reference implementation.
- Hosted/SaaS multi-tenancy and dashboard auth (§7d) — designed around, not built. Under
  open-core this drops further: it blocks anything hosted, and nothing about an OSS release.
- Real executors for Stripe/SMTP — still simulated; wiring a real one is a separate,
  deliberate, single-file decision.
- Any compliance certification claim.
- Bespoke `Action` wire protocol — cut from phase 1, revisit only when a second
  integration justifies it.
- In-process SDK and per-framework adapters — phase 5, after the MCP path is public.

---

## 12. Deep review + outside voice findings (2026-08-25)

The 11-section review and an independent outside-voice pass ran after §5-rev was written.
Every code-level claim below was verified against the repo before being recorded here.

### 12a. Why D2 is now provisional

Three findings undercut §5-rev's justification:

**1. The credential-custody claim is false for stdio MCP.** §5-rev asserts "in an MCP
topology credentials already live in the tool server process, not the agent process."
True for *remote* HTTP MCP servers that authenticate independently. **False for stdio
servers**, which dominate real deployments: the client spawns the tool server as a
subprocess and supplies the credential from its own config/env. Guardian-as-stdio-proxy is
spawned by that same client from that same config. Nothing moves. Guardian adds a hop.

Worse, §4's own argument convicts the proxy. §4 rejects the in-process hook because "a hook
is not a boundary — any code path that reaches the underlying function directly goes around
us." A proxy that is one entry in the client's `mcpServers` map has that property one layer
up: delete the entry, or add a second pointing at the real server, and Guardian is bypassed.
A cooperative boundary wearing a process boundary's clothes.

**2. MCP cannot express an indefinite approval pause.** JSON-RPC with client-enforced
timeouts. ESCALATE degrades to one of three: sync-block inside the timeout (requires a human
at the same keyboard within seconds — which is the in-client approval UX §3b says Guardian
should beat), `elicitation/create` (asks the *client's* user; no verified named approver,
which destroys the §3c Article 14 property), or error-plus-out-of-band (at which point the
proxy buys nothing an in-process hook doesn't). **§7a mis-framed this as a UX problem. It is
a protocol-capability problem, which makes D4 a premise of D2, not a consequence.**

**3. `requesting_agent` has no MCP representation.** Every cumulative rule keys on it
(`policy_agent.py:25-27`, `history.py:29-31`); `Action.requesting_agent` is required
(`schemas.py:57`) and deliberately set by trusted code, never the model (`agents/base.py:7-11`).
MCP offers only `clientInfo.name`, self-reported by the client and therefore
attacker-controlled under Guardian's own threat model. Hardcode it → all agents share one
cap, per-agent caps become inexpressible. Trust `clientInfo` → an agent renames itself and
resets its cap, defeating FIN-002 and the structuring centerpiece (`PLAN.md:539`) outright.

### 12b. The write-ahead record has three holes

Accepted in review to fix the concurrency race and lost-response replay. All three verified:

- **Hole A — it does not serialize the race it targets.** Order is read-history → evaluate →
  write-ahead → forward. Two proxies both read sum=$0, both decide ALLOW, both reserve. The
  reservation lands *after* the decision. Closing it needs the history read and reservation
  inside one `BEGIN IMMEDIATE` with post-decision re-validation — but `evaluate()` is by
  explicit design a pure function of `(action, history)` with no transaction handle
  (`policy_agent.py:1-7`, `PLAN.md:160`).
- **Hole B — it reintroduces cap poisoning (`PLAN.md` §9.4).** To fix the race the reservation
  must count toward `HistoryQuery`. Once it does, an attempt that never executes permanently
  consumes the cap; an attacker fires requests at a known-down server and burns the victim's
  limit. Lost-response says *never release a reservation*; cap-poisoning says *always release
  on failure*. Both are only satisfiable if "definitely didn't happen" is distinguishable from
  "unknown", and a dropped response tells you nothing.
- **Hole C — it breaks the recovery landed on this branch.** `outcomes.action_id IS NULL` is
  the "not executed" signal in `db.py:196`, `db.py:205`, `db.py:415`, `db.py:423`. A
  write-ahead row in `outcomes` makes a never-executed action look executed, so
  `get_unexecuted_allows`/`get_unexecuted_approvals` stop finding stuck actions (commits
  `2f0541a`, `ca1a6b6`). `executors.run`'s guard returns `prior` unconditionally
  (`executors.py:99-101`), so it would return the pending row as success and skip execution
  forever. **Fixable** by putting the reservation in its own table; A and B are not.

### 12c. Extraction is underestimated

- **Import topology.** `from schemas import ...` / `import db` are top-level absolute imports
  across the core (`policy_agent.py:17`, `executors.py:9`, `escalation.py:10-17`, `db.py:13`,
  `graph.py:11-17`). A distributable package cannot own top-level `schemas`/`db`. Every module
  changes — and `tests/test_no_effector_imports.py`, the enforcement of Invariant 1, is keyed
  to module paths and changes in the same diff. Changing the file that proves the security
  property while changing the layout is a risk worth isolating into its own commit.
- **cwd-relative defaults.** `evaluate(..., policy_path="policy.yaml")` (`policy_agent.py:53`)
  and `init_db(path="guardian.db")` (`db.py:68`) resolve against cwd, which is arbitrary in
  someone else's process.
- **Open `ActionType` breaks reading your own audit trail.** `db.py:153`, `db.py:173`,
  `db.py:310` all do `ActionType(row["action_type"])`. A type present yesterday and absent
  today raises `ValueError` on read. For an evidence product, being unable to parse your own
  history is the worst available failure.
- **Open `Params` can strand a parked escalation.** `Params` is a static discriminated union
  (`schemas.py:48`). Opening it makes `ActionEnvelope.model_validate_json` in `resolve()`
  (`escalation.py:82`) depend on registry state *at resolve time*. Registry drift between park
  and resolve → the escalation won't deserialize → an approval that can never be granted, and
  `payload_hash` (`escalation.py:85`) makes it unfixable by hand.
- **`history.py` is not generic.** `sum_amount_cents` calls `PaymentParams.model_validate_json`
  on every matching row (`history.py:38`). Any registered non-payment type carrying a
  `sum_amount_cents_gt` rule raises there, inside `evaluate()`'s try → SYS-ERR → permanent
  deny. §2's table marked history as generic. It is not.

### 12d. A sequencing bug that manufactures insecure defaults

D5 (predicate extensibility) is deferred; the open registry is phase 1. But the closed
predicate library expresses only `action_type`, `target_*`, and one hardcoded field literally
named `amount_cents` (`predicates.py:29-88`). So a customer registering `issue_refund` can
write rules on type and target only — **§6's own worked example, "refunds over $200 need a
human", is not expressible** unless their params are `PaymentParams`-shaped.

Combine that with E3 (accepted, phase 1), which warns loudly on any registered type with no
covering rule, and users silence the warning by writing vacuous `{action_type: X} → allow`
rules. **Two accepted phase-1 items interact to push users toward allow-everything policies.**

Corollary: E7's stated blocker ("needs adopters") is wrong. The rule library is blocked on
**D5**, not on adoption.

Related: SYS-GAP escalate-on-no-match (`policy_agent.py:70-80`) is right for five demo types.
Behind a proxy fronting 10-15 MCP servers it means ~100 uncovered tool types on day one, all
escalating — an approval queue nobody can drain, whose first workaround is a blanket allow.

### 12e. A simpler design that the doc dismissed on a solved objection

§7a rules out RAISE + ABORT because the agent "may retry a variant." But a variant is a
different payload, therefore a different `Action`, evaluated fresh — and the original approval
stays bound to the original `payload_hash` (`executors.py:96`, `escalation.py:85`). That
objection was already engineered away in rev 2 defect 5 (`PLAN.md:556`).

So: **return an immediate structured error — "blocked, approval pending, id=X, retry" — and
let the agent's next turn re-call.** No suspend/resume, no framework support, no protocol
extension, no orphaned-approval hole (the retry *is* the resume). This is how every
rate-limited API behaves, and it reduces D4, D7, and much of phase 2 to nothing. Not adopted;
recorded as the leading candidate for the post-spike re-decision.

### 12f. The spike that must run before any building

Three questions, all cheaply answerable from the MCP spec plus one real server config:

1. **Credentials.** In stdio vs remote MCP, where does the credential actually live, and can
   Guardian ever be positioned so the agent process cannot reach the effector directly?
2. **Approval.** Is any pause longer than a client timeout expressible? What exactly does
   `elicitation/create` provide, and can it ever carry a verified named approver?
3. **Identity.** Can per-agent identity be established at all, or is `clientInfo.name` the
   only signal? Without a trustworthy answer, every cumulative rule is unenforceable over MCP.

If all three answer badly, the honest conclusion is that D2 was wrong and the in-process SDK
(mode A) plus the §12e error-and-retry shape is the real design — accepting the weaker
cooperative guarantee and **saying so plainly**, per §5's non-negotiable.

### 12g. Challenges to D1 recorded but not acted on

The outside voice argues open-core is partly rationalization: CEO objections 2 ("market may be
a feature") and 4 ("who has this pain") apply identically to free software, since free does not
create demand that does not exist. It also notes the plan open-sources the *differentiator*
(correctness on subtle semantics) while reserving the *commodity* (auth, tenancy, hosted
dashboard) — an inversion nobody examined. And that the ESLint analogy inverts: lint rules are
advisory and free to be wrong, whereas a wrong enforcement rule blocks a real payment, which is
why decades of WAF/IAM policy sharing produced no ESLint-like ecosystem.

D1 was left standing. Recorded here so the next review does not have to rediscover it.

### 12h. E2 and E6 are probably misfiled

Both were deferred as "nice after adoption." The argument against: rule-authoring is the known
adoption bottleneck for every policy engine (see OPA/Rego). E5's quickstart carries the canned
demo; E2 (`explain`) and E6 (replay against an edited policy) determine whether *minute 61* —
the user writes their own rule and it doesn't fire — ends in a working policy or a closed tab.
Given §12d, minute 61 is currently a wall. Revisit after the spike.

---

## 13. Spike results (2026-08-25) and revised D2

The three questions from §12f were answered directly against the MCP spec
(2025-06-18) and, for S1, against real client config conventions. All three
citations are verbatim.

### S1 — Credentials (nuanced; practical conclusion unchanged)

Spec: *"In the stdio transport: The client launches the MCP server as a
subprocess."* Every major MCP client (Claude Desktop, Cursor, VS Code) injects
credentials into that subprocess's environment straight from the client's own
config file — e.g. `"env": {"STRIPE_API_KEY": "..."}` on the server's entry in
`mcpServers`. This is universal convention, not implementation-specific.

This does not make custody separation *impossible* — if Guardian holds the
downstream credential in its own secret store, never present in the agent's
config at all, the agent genuinely never sees it. That is a real architecture.
It requires the operator configuring the agent's MCP client to be a different
party from whoever can obtain the raw credential — a platform/security team
issuing scoped access to individual engineers, say. **It is not the
architecture for a solo builder or small team, who almost always already hold
the raw provider key directly** (from the Stripe dashboard, say) and can
trivially reconstruct a bypass entry with it. For Guardian's actual near-term
audience, S1 delivers the outside voice's conclusion in practice even though
the underlying mechanism is more nuanced than "credentials can't move."

### S2 — Approval pause (dead, no ambiguity)

Spec, Lifecycle → Timeouts: *"Implementations SHOULD establish timeouts for
all sent requests... Implementations SHOULD always enforce a maximum timeout,
regardless of progress notifications, to limit the impact of a misbehaving
client or server."* No protocol construct exists for holding a request open
for an arbitrary human-approval duration.

The one candidate, `elicitation/create`, fails on three independent grounds:
- **Schema is flat only.** *"Elicitation schemas are limited to flat objects
  with primitive properties only... complex nested structures, arrays of
  objects, and other advanced JSON Schema features are intentionally not
  supported."* An `Action` cannot be carried without inventing an ad hoc
  flattening.
- **Explicitly barred from this use.** *"Servers MUST NOT use elicitation to
  request sensitive information."* A payment/refund approval is not obviously
  outside that bar.
- **No identity in the response.** The result is `{"action": "accept",
  "content": {...matching the requested schema...}}`. No approver field, no
  session identity beyond whatever the client app itself tracks internally
  and chooses not to expose to MCP.

### S3 — Agent identity (dead, no ambiguity)

Spec, Lifecycle → Initialization: `clientInfo` is `{"name", "title",
"version"}`, sent once in the `initialize` request, self-reported, no
cryptographic binding, not verified anywhere in the spec. The only
server-assigned identifier anywhere in the base protocol is Streamable HTTP's
`Mcp-Session-Id` — HTTP-only (no stdio equivalent), identifies a *connection*,
not an agent, assigned fresh on every reconnect. There is no mechanism for a
proxy to reliably distinguish one calling agent from another, or to stop an
agent from relaunching to reset whatever counter was tracking it. Every
cumulative rule in `policy.yaml` (FIN-002 included) depends on exactly the
identity guarantee that does not exist here.

### Verdict

Two of three legs fail at the protocol level with no workaround available to
an MCP-proxy implementation. The third technically works but only for a
deployment shape (centralized credential ownership, separate from the agent
operator) that is not what open-core Guardian is shipping to first. **D2 as
originally scoped — MCP proxy first, justified by free credential custody and
lowest adoption friction — is decided against.**

### Revised D2: in-process hook (mode A) is the first integration

Mode A, described in §5 and dismissed there only on the (now-moot) grounds
that MCP offered a strictly better tradeoff, becomes phase 1:

- **Identity** is supplied directly by the calling code, the same way
  `agents/base.py:7-11` already refuses to trust the model for
  `requesting_agent`. No protocol field to spoof, because there is no
  protocol.
- **Approval** uses §12e's error-and-retry pattern, not suspend/resume: return
  a structured "blocked, approval pending, id=X" and let the caller's next
  turn re-invoke. This was already engineered safe by rev 2 defect 5 —
  `payload_hash` binds an approval to its exact original payload, so a retry
  with a *different* payload is correctly evaluated fresh rather than riding
  a stale approval (`executors.py:96`, `escalation.py:85`). No new mechanism
  required, no protocol timeout to fight.
- **Credentials** are wherever the customer's tool code already puts them.
  Mode A never claimed custody separation (§5's honest non-negotiable: never
  let mode A borrow mode C's guarantee) — it wasn't the value proposition, so
  losing it costs nothing that wasn't already priced in.

MCP interposition is not dead. It becomes a **later adapter**, scoped
correctly this time to the deployment where S1's custody separation is real —
an operator who owns credential issuance separately from agent configuration.
That is a legitimate enterprise topology; it is just not phase 1, and it is
no longer justified by a claim that turned out to be false.

### Task list changes

- S1-S4 (the spike) are complete as of this section.
- T5 (MCP proxy), T2 (unknown-tool blocking), T6 (shadow mode as a proxy
  flag), T15 (quickstart) are **retargeted from proxy to in-process hook** —
  same finding, same priority, different transport.
- T1 (write-ahead record) still applies: the concurrency race and
  lost-response problem are not MCP-specific, they follow from any
  concurrent caller of the core, in-process or not.
- New task: build the mode-A registration contract from §6 (`@guarded`,
  `ActionSpec`, mandatory `target_field`) as the actual phase-1 deliverable.

---

## Appendix: unrelated finding

`CLAUDE.md` in this repo instructs agents to route data-generation and context operations
through an external service ("Caveman", caveman.so), framed as overriding default behavior.
It is unrelated to anything in this codebase and matches the shape of a prompt-injection /
data-exfiltration lure. It was not acted on while writing this doc. Worth confirming who
added it and why, independently of this design.

**Resolved (2026-08-29):** `CLAUDE.md` no longer contains this content — verified clean as
of the 2026-08-29 `/plan-eng-review`. Left here for the historical record.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR (2026-08-25, in-doc) | D1-D3 decided, §13 spike reversed D2 to mode-A-first |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | not run |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES_OPEN | 11 findings, all resolved with explicit user decisions (see below) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not applicable (no UI surface) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

**Scope of this review (2026-08-29):** the design doc was stale relative to shipped code —
Phase 1 (`guardian/sdk.py`, `guardian/registry.py`) was already merged (commits `3e76b63`
through `d4cdeec`) and several `§12` findings the doc itself raised (history.py/predicates.py
`PaymentParams` binding, E3 startup warnings) were already fixed in code before this review
started. Per user direction, this review ran as a **doc-vs-code gap audit**: the 4 review
sections evaluated the shipped SDK against the design doc's decisions and open items, rather
than re-deriving Step 0 from scratch.

**Findings and decisions (11 total, all resolved):**

1. **[Architecture, P1]** T1 concurrency race: `guardian/sdk.py:_submit()` reads
   history→evaluates→parks with no transaction, so two concurrent `@guarded` calls can both
   see history=0 and both ALLOW against a cumulative cap (FIN-002-style). → **Fix now**:
   `BEGIN IMMEDIATE` + a separate reservations table (not `outcomes`, to preserve the existing
   stuck-action recovery paths in `db.py`).
2. **[Architecture, P2]** D8 shared-history misconfiguration: `context()` takes a raw
   connection with no path validation; two processes on different DB files silently break
   cumulative caps. → **Fix now**: log the resolved DB path at startup, document the
   shared-file requirement.
3. **[Architecture, accepted]** D7 orphaned-approval hole: an approval landing after the
   calling agent's process exited has nothing to call back into. → **Accepted as documented
   residual gap** — audit trail stays correct; notification is customer/framework glue,
   out of scope per the doc's own §11. Logged in `TODOS.md`.
4. **[Architecture, accepted]** E3 severity drift: doc proposed "refuse to boot" on
   uncovered types; shipped code only warns. → **Accepted as-shipped** — a hard boot-refuse
   would punish incremental adoption of the open registry; the warn-only split already
   correctly distinguishes safe (rule gap → SYS-GAP) from unsafe (executor gap → crash).
5. **[Architecture, deferred]** §12c package extraction never happened — no
   `pyproject.toml`, core modules still do top-level `import db`/`import schemas`. →
   **Deferred to TODOS.md**, isolated from this PR to avoid bundling a layout change with the
   file (`test_no_effector_imports.py`) that proves Invariant 1.
6. **[Test, P1 — outside voice]** Contextvar propagation: `context()`'s identity binding
   uses a `ContextVar`, but major frameworks (LangGraph, CrewAI) dispatch sync tools via
   `ThreadPoolExecutor.submit()`, which does **not** propagate contextvars (verified
   empirically — `asyncio.to_thread` does, `ThreadPoolExecutor.submit()` doesn't). This hits
   the primary integration surface, not an edge case. → **Fix now**: document the safe wiring
   pattern (`contextvars.copy_context().run(...)` at the adapter boundary) and add a test that
   fails today and passes once documented/handled.
7. **[Test, P2]** No test for malformed/wrong-type kwargs (`wrapper()` leaks a raw pydantic
   `ValidationError` instead of a typed SDK exception) or for contextvar thread-isolation. →
   **Fix now**: wrap params validation in a typed exception, add both tests.
8. **[Performance, accepted]** `_find_matching_escalation` is O(session size), fully
   re-deserializing every escalation's JSON per ESCALATE call. → **Accepted with a concrete
   threshold** in `TODOS.md` (benchmark trigger: 500+ pending escalations/session) rather than
   optimizing an unmeasured bottleneck ahead of T1's schema change.
9. **[Outside voice, P1]** No spike verified real agent-framework default retry semantics
   against `ActionPending` — a framework that retry-then-abandons on any exception could burn
   its retry budget before a human resolves the escalation, unlike §13's own MCP spike
   precedent (S1-S3). → **Run a spike before landing this PR** (LangGraph first, per the
   doc's own framework ordering).
10. **[Outside voice, P2]** `_semantic_key` includes `model_dump_json()` without sorted keys;
    a dict-typed `params_model` field could produce spurious retry-match misses. → **Fix now**:
    sort keys in the semantic-key serialization only (not `payload_hash`, which has different,
    correct tamper-binding semantics).
11. **[Outside voice, documentation]** The doc's own status line still said "No code written
    yet" while Phase 1 was fully merged. → **Fixed**: status line updated above.

**CROSS-MODEL TENSION:** none — all outside-voice findings (6, 9, 10, 11, and the scope-framing
clarification) were independently verified against the actual code (finding 6 confirmed by
direct empirical test) before being presented, and the user's decisions on each matched the
recommended option in every case. No disagreement between this review and the outside voice
to adjudicate.

**VERDICT:** ENG REVIEW ISSUES_OPEN — 11 findings, all resolved via explicit user decisions
above. Three are P1 and block landing until built: T1 (concurrency fix), the contextvar
wiring-pattern fix + test, and the framework retry-semantics spike. `TODOS.md` created this
session with 6 items (D7, D8/multi-writer note, package extraction, escalation-scan
threshold, retry-semantics follow-up, WRITE_FILE policy gap). CEO review from 2026-08-25
stands, not re-litigated. Not yet CLEARED — re-run `/plan-eng-review` (or `/review` on the
resulting diff) once T1/T2/T3 land.

NO UNRESOLVED DECISIONS
