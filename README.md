# Guardian Agent System

A safety layer for AI agents that take real-world actions (paying money, sending emails, deleting files).

## In short

AI agents propose actions instead of doing them directly. A guardian checks each proposal against rules you define, then allows it, blocks it, or pauses it for a human to approve. Every decision is logged. Right now the actual sending/paying/deleting is simulated (printed, not performed), so you can safely test the guardrails before connecting any real payment, email, or file system.

## The problem this solves

If you let an AI agent send emails, move money, or delete files on its own, you have to trust it never makes a mistake — and you have no easy way to stop it or review what it did. This project puts a **guardian** in between the agent and the outside world:

```
AI agent decides what it wants to do
        |
        v
   GUARDIAN checks it against your rules
        |
   -----------------------------
   |         |                 |
 allow      deny           needs a human
   |         |                 |
  do it   blocked        parked until you
                          say yes or no
```

The agent never gets to act directly — it can only *propose* an action. Every proposal is checked against a rule file (`policy.yaml`) before anything happens, and every decision is written to a permanent log.

## Who this is for

Anyone prototyping AI agents that need to take real-world actions but wants a human-in-the-loop safety net before wiring up real money, email, or file access — a policy engine, approval queue, and audit log you can point at your own rules.

## The three agents in this demo

- **Finance agent** — proposes payments
- **Email agent** — proposes sending emails
- **File agent** — proposes reading/writing/deleting files

None of these agents can actually pay anyone, send anything, or touch a real file. They call an LLM to decide *what* they'd like to do, then hand that proposal to the guardian. (The executors that would perform the real action are stubbed to just print what they *would* have done — see "What's real vs. simulated" below.)

## The rules it enforces (`policy.yaml`)

Plain-English version of the built-in rules:

- A single payment over $500 needs a human to approve it
- More than $1,000 paid out by one agent in 24 hours needs a human, even if no single payment was large (catches an agent trying to sneak past the $500 rule with several smaller payments)
- Paying anyone not on the known-vendor list is blocked outright
- Deleting a file that looks like a production file (e.g. `config.prod.yaml`) is always blocked
- Emailing anyone outside your company's domain needs a human
- Reading files inside the project's own `workspace/` folder is fine, no review needed

You can open `policy.yaml` and change these — the system picks up your edits automatically.

In one line: the guardian is a policy engine sitting between "agent decides" and "agent acts," and `policy.yaml` is the only place those decisions are defined.

## What's real vs. simulated

- **Real:** the LLM calls, the rule checking, the approve/reject flow, the database log, the web dashboard.
- **Simulated:** the actual sending of money/email/file changes. Executors just record "I would have paid acme-corp $750" instead of calling a real payment API. This is a safe sandbox to prove the guardian logic works before ever wiring it to a real payment provider or email service.

---

## How to run it

### 1. One-time setup

```bash
cd /Users/anushkasirpurkar/Agent-Guardian
python3 -m venv .venv          # skip this if .venv already exists
.venv/bin/pip install -r requirements.txt
```

You need an OpenAI API key (the demo agents use `gpt-4o-mini` to decide what action to propose). Put it in a `.env` file in this folder:

```
OPENAI_API_KEY=sk-...
```

### 2. Confirm everything works (no API key needed for this step)

```bash
.venv/bin/pytest tests/ -v
```

You should see all tests pass. This checks the guardian logic itself (rules, approvals, audit log) using fake data — it doesn't call any real LLM.

### 3. Run the full demo scenario (uses your real OpenAI key)

This runs six actions through the guardian, back to back — some get auto-allowed, some get auto-blocked, and some stop and ask you to type y/n in the terminal:

```bash
.venv/bin/python main.py run --scenario demo1 --by "your-name"
```

What you'll see happen:
| # | Action | What the guardian does |
|---|---|---|
| 1 | Read a workspace file | Allowed automatically |
| 2 | Delete a "production" config file | Blocked automatically |
| 3–5 | Pay the same vendor $499, three times | First two go through; the third trips the "$1,000/day" rule and asks you to approve |
| 6 | Pay a known vendor $750 | Asks you to approve (over the $500 single-payment limit) |
| 7 | Email someone outside the company | Asks you to approve |
| 8 | Pay an unlisted vendor | Blocked automatically |

Type `y` or `n` when it stops and asks.

### 4. See the audit trail

Every action, decision, and outcome is logged. Print a readable report for the session you just ran:

```bash
.venv/bin/python main.py report --session demo1
```

### 5. Try the web dashboard

Instead of approving things in the terminal, you can approve/reject from a browser:

```bash
.venv/bin/uvicorn dashboard:app --port 8001
```

Then open **http://127.0.0.1:8001** in your browser. If there's anything waiting for approval (run step 3 in another terminal without answering the y/n prompt, or leave one pending), you'll see it listed there with Approve/Reject buttons.

> No login is required — this dashboard is for local/demo use only. Don't expose it on a network anyone else can reach.

### 6. Try changing a rule live

While the dashboard (or anything else) is running, open `policy.yaml`, change a number (e.g. raise the $500 limit to $5000), save the file, then refresh **http://127.0.0.1:8001/policy-version** — you'll see the version hash change immediately, with no restart needed. Anything already waiting for approval keeps the rule it was originally judged under, even if you change the rules afterward.

### 7. Ask the AI to double-check your rules

This asks an LLM to review `policy.yaml` for gaps — e.g. "you have no rule at all for writing files" — without ever letting it approve anything on its own:

```bash
.venv/bin/python main.py coverage-check
```

---

## If something goes wrong mid-run

Nothing is lost. If you close the terminal or the process crashes while something is waiting for approval, it's already saved. Resume it any time with:

```bash
.venv/bin/python main.py resolve --by "your-name"
```

## Where things live

```
agents/          the three worker agents (propose actions, can't execute them)
guardian/        the rule engine, the approval flow, the audit log, the dashboard's backend logic
policy.yaml       the rules, in plain YAML
dashboard.py      the web approval UI
main.py           the command-line tool (run / resolve / report / coverage-check)
tests/            86 automated tests
guardian.db       the log of everything that's happened (created on first run)
```
