"""FastAPI dashboard (PLAN.md s7 step 12): HTML + JSON over the same
guardian/escalation.py functions the Phase 2 CLI uses (park/pending/resolve).

Route handlers are thin wrappers, same as main.py's cmd_resolve -- no
escalation logic is reimplemented here. `main.py`'s _prompt_approval is a
blocking terminal input() and can't be reused for a request/response cycle,
so this module has its own (HTML-form) presentation layer, but the
underlying park/pending/resolve calls are the shared source of truth.

No authentication: by design, for local/demo use against a non-production
guardian.db (confirmed with the user for Phase 3's stretch scope). Anyone who
can reach this process's port can approve or reject pending escalations, the
same as anyone with terminal access to `python main.py resolve` could. Do not
expose this to an untrusted network without adding auth first.

Run: uvicorn dashboard:app --reload --port 8001  (or python dashboard.py)
"""
from __future__ import annotations

import html
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()

import sqlite3

import yaml

import db
import guardian.escalation as esc
import guardian.graph as graph
import guardian.policy_agent as policy_agent
import guardian.registry as registry

DB_PATH = os.environ.get("GUARDIAN_DB", "guardian.db")
POLICY_PATH = os.environ.get("GUARDIAN_POLICY", "policy.yaml")

app = FastAPI(title="Guardian Dashboard")

# One connection for the process lifetime, not one per request: db.configure()
# re-runs the full CREATE TABLE/INDEX IF NOT EXISTS schema script and a
# commit, which is wasted work on every single HTTP request, and a fresh
# sqlite3.Connection per request that's never closed leaks file descriptors
# under sustained traffic. FastAPI's sync `def` routes run in a threadpool,
# so this connection needs check_same_thread=False -- db.init_db() doesn't
# expose that constructor flag (main.py's CLI never needs it: one connection
# per process invocation, always on the main thread), so this module opens
# the connection itself and routes it through db.configure() -- the same
# WAL-mode-plus-schema-version setup init_db() uses -- rather than
# duplicating that setup by hand. sqlite serializes writers internally, and
# each request does one short operation, not an interleaved multi-statement
# transaction, so sharing this connection across threads is safe.
_conn = db.configure(sqlite3.connect(DB_PATH, check_same_thread=False))

# E3 (design doc, accepted 2026-08-25): same boot-time coverage warning
# main.py's cmd_run prints, so the dashboard-only path doesn't silently skip
# it. Advisory only -- an uncovered type still escalates correctly under
# SYS-GAP, this just makes the gap visible before an agent hits it.
with open(POLICY_PATH, "rb") as _f:
    _uncovered = registry.uncovered_action_types(yaml.safe_load(_f)["rules"])
if _uncovered:
    print(
        f"WARNING: no policy.yaml rule covers: "
        f"{', '.join(_uncovered)}. Proposals of these types "
        f"will escalate under SYS-GAP until a rule is added."
    )

# 7b's executor half of the same check -- see main.py's
# warn_uncovered_action_types for why this is a separate list from the
# rule-coverage one above.
_missing_executors = registry.uncovered_executors()
if _missing_executors:
    print(
        f"WARNING: no executor registered for: "
        f"{', '.join(_missing_executors)}. An ALLOWed proposal of these "
        f"types will fail with ExecutorMissing."
    )


def _get_conn():
    return _conn


def _render_row(row: dict) -> str:
    envelope, decision = row["envelope"], row["decision"]
    action = envelope.action
    action_id = html.escape(action.id)
    return f"""
    <tr>
      <td>{html.escape(action.requesting_agent)}</td>
      <td>{html.escape(action.action_type)}</td>
      <td>{html.escape(action.target)}</td>
      <td>{html.escape(decision.rule_id or "")}</td>
      <td>{html.escape(decision.reasoning)}</td>
      <td>{html.escape(envelope.reasoning)}</td>
      <td>
        <form method="post" action="/resolve/{action_id}" style="display:inline">
          <input type="hidden" name="approved" value="true">
          <input name="by" placeholder="your name" required>
          <button type="submit">Approve</button>
        </form>
        <form method="post" action="/resolve/{action_id}" style="display:inline">
          <input type="hidden" name="approved" value="false">
          <input name="by" placeholder="your name" required>
          <button type="submit">Reject</button>
        </form>
      </td>
    </tr>"""


def _render_stuck_row(row: dict) -> str:
    """Approved but never executed -- executors.run() raised after approval
    was already committed (guardian.escalation.ExecutionFailed). Distinct
    from _render_row's pending table: no approve/reject choice left, only a
    retry."""
    action = row["envelope"].action
    action_id = html.escape(action.id)
    return f"""
    <tr>
      <td>{html.escape(action.requesting_agent)}</td>
      <td>{html.escape(action.action_type)}</td>
      <td>{html.escape(action.target)}</td>
      <td>{html.escape(row["resolved_by"] or "")}</td>
      <td>
        <form method="post" action="/retry/{action_id}" style="display:inline">
          <button type="submit">Retry execution</button>
        </form>
      </td>
    </tr>"""


def _render_stuck_allow_row(row: dict) -> str:
    """Auto-allowed (never escalated) but never executed -- same failure
    shape as _render_stuck_row's escalation case, but there's no
    'approved by' since a policy rule allowed it, not a human.

    row["action"] is None when guardian.graph.unexecuted_allows() couldn't
    reconstruct it (action_type no longer registered -- registry drift since
    it was proposed, design doc 2026-08-24 s12c). Retry is still offered:
    action_id comes from the Decision, which always reconstructs (it carries
    no params), and a re-registered type makes the retry succeed normally --
    this must stay visible and actionable, not silently vanish from the list."""
    action = row["action"]
    decision = row["decision"]
    if action is None:
        action_id = html.escape(decision.action_id)
        return f"""
    <tr>
      <td colspan="3"><em>action_type no longer registered: {html.escape(row["error"] or "")}</em></td>
      <td>{html.escape(decision.rule_id or "")}</td>
      <td>
        <form method="post" action="/retry/{action_id}" style="display:inline">
          <button type="submit">Retry execution</button>
        </form>
      </td>
    </tr>"""
    action_id = html.escape(action.id)
    return f"""
    <tr>
      <td>{html.escape(action.requesting_agent)}</td>
      <td>{html.escape(action.action_type)}</td>
      <td>{html.escape(action.target)}</td>
      <td>{html.escape(decision.rule_id or "")}</td>
      <td>
        <form method="post" action="/retry/{action_id}" style="display:inline">
          <button type="submit">Retry execution</button>
        </form>
      </td>
    </tr>"""


@app.get("/", response_class=HTMLResponse)
def dashboard_page():
    conn = _get_conn()
    rows = esc.pending(conn)
    stuck = esc.unexecuted(conn)
    stuck_allows = graph.unexecuted_allows(conn)
    version = policy_agent.policy_version(POLICY_PATH)
    body = "".join(_render_row(r) for r in rows) or "<tr><td colspan='7'>no pending escalations</td></tr>"
    stuck_body = "".join(_render_stuck_row(r) for r in stuck) or "<tr><td colspan='5'>none</td></tr>"
    stuck_allow_body = ("".join(_render_stuck_allow_row(r) for r in stuck_allows)
                         or "<tr><td colspan='5'>none</td></tr>")
    return f"""<!doctype html>
<html><head><title>Guardian Dashboard</title></head>
<body>
  <h1>Guardian Dashboard</h1>
  <p>policy_version: <code>{html.escape(version)}</code> (no auth -- local/demo use only)</p>
  <table border="1" cellpadding="6">
    <tr><th>agent</th><th>type</th><th>target</th><th>rule</th>
        <th>rule reasoning</th><th>LLM reasoning (audit-only)</th><th>action</th></tr>
    {body}
  </table>
  <h2>Approved but not yet executed (previous attempt failed)</h2>
  <table border="1" cellpadding="6">
    <tr><th>agent</th><th>type</th><th>target</th><th>approved by</th><th>action</th></tr>
    {stuck_body}
  </table>
  <h2>Auto-allowed but not yet executed (previous attempt failed)</h2>
  <table border="1" cellpadding="6">
    <tr><th>agent</th><th>type</th><th>target</th><th>rule</th><th>action</th></tr>
    {stuck_allow_body}
  </table>
</body></html>"""


@app.get("/stuck")
def list_stuck(session_id: str | None = None):
    """JSON equivalent of the stuck-approvals table, same escalation.unexecuted() call."""
    conn = _get_conn()
    rows = esc.unexecuted(conn, session_id=session_id)
    return [
        {
            "action_id": r["envelope"].action.id,
            "agent": r["envelope"].action.requesting_agent,
            "action_type": r["envelope"].action.action_type,
            "target": r["envelope"].action.target,
            "resolved_by": r["resolved_by"],
            "resolved_at": r["resolved_at"],
        }
        for r in rows
    ]


@app.get("/stuck-allows")
def list_stuck_allows(session_id: str | None = None):
    """JSON equivalent of the auto-allow stuck-actions table, same
    graph.unexecuted_allows() call the CLI's `resolve` command retries.

    r["action"] is None for a row whose action_type is no longer registered
    (see _render_stuck_allow_row's docstring) -- still returned, with
    action_id/error in place of the fields that need a reconstructed Action,
    same principle as the HTML view: stays visible and retryable, never
    silently dropped."""
    conn = _get_conn()
    rows = graph.unexecuted_allows(conn, session_id=session_id)
    result = []
    for r in rows:
        if r["action"] is None:
            result.append({
                "action_id": r["decision"].action_id,
                "agent": None,
                "action_type": None,
                "target": None,
                "rule_id": r["decision"].rule_id,
                "error": r["error"],
            })
        else:
            result.append({
                "action_id": r["action"].id,
                "agent": r["action"].requesting_agent,
                "action_type": r["action"].action_type,
                "target": r["action"].target,
                "rule_id": r["decision"].rule_id,
                "error": None,
            })
    return result


@app.get("/pending")
def list_pending(session_id: str | None = None):
    """JSON equivalent of the dashboard page's table, same escalation.pending() call."""
    conn = _get_conn()
    rows = esc.pending(conn, session_id=session_id)
    return [
        {
            "action_id": r["envelope"].action.id,
            "agent": r["envelope"].action.requesting_agent,
            "action_type": r["envelope"].action.action_type,
            "target": r["envelope"].action.target,
            "rule_id": r["decision"].rule_id,
            "rule_reasoning": r["decision"].reasoning,
            "llm_reasoning": r["envelope"].reasoning,
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _resolve_or_http_error(action_id: str, *, approved: bool, by: str):
    """Same escalation.resolve_and_execute() the CLI uses, translated to
    HTTP status codes instead of main.py's print-and-continue/print-and-exit
    handling -- the underlying race/consistency errors are identical, only
    the presentation differs (module docstring)."""
    try:
        return esc.resolve_and_execute(_get_conn(), action_id, approved=approved, by=by)
    except esc.UnknownEscalation:
        raise HTTPException(status_code=404, detail=f"no such escalation: {action_id}")
    except esc.AlreadyResolved as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except esc.ExecutionFailed as exc:
        # Approval already committed -- 502 signals "we're fine, a downstream
        # dependency isn't", distinct from the 409s above (which mean the
        # escalation record itself is in an unexpected state). The row now
        # shows up in the dashboard's stuck-approvals table for retry.
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/resolve/{action_id}")
def resolve_via_form(action_id: str, approved: bool = Form(...), by: str = Form(...)):
    """Thin wrapper over guardian/escalation.py, same call main.py's
    cmd_resolve makes -- see module docstring."""
    _resolve_or_http_error(action_id, approved=approved, by=by)
    return RedirectResponse("/", status_code=303)


@app.post("/api/resolve/{action_id}")
def resolve_via_api(action_id: str, approved: bool, by: str):
    outcome = _resolve_or_http_error(action_id, approved=approved, by=by)
    return {"outcome": outcome.model_dump(mode="json") if outcome else None}


def _retry_or_http_error(action_id: str):
    """Same two functions the CLI's `resolve` command retries with (main.py
    cmd_resolve), translated to HTTP status codes. Tries the escalated-then-
    approved path first (esc.execute_approved()); UnknownEscalation there
    just means action_id was never escalated, not an error, so falls
    through to the auto-allow path (graph.retry_execution()) rather than
    making the caller know in advance which table an action_id lives in."""
    try:
        return esc.execute_approved(_get_conn(), action_id)
    except esc.UnknownEscalation:
        pass
    except esc.NotApproved as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except esc.ExecutionFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    try:
        return graph.retry_execution(_get_conn(), action_id)
    except graph.NoSuchAction:
        raise HTTPException(status_code=404, detail=f"no such action: {action_id}")
    except graph.NotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except esc.ExecutionFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/retry/{action_id}")
def retry_via_form(action_id: str):
    _retry_or_http_error(action_id)
    return RedirectResponse("/", status_code=303)


@app.post("/api/retry/{action_id}")
def retry_via_api(action_id: str):
    outcome = _retry_or_http_error(action_id)
    return {"outcome": outcome.model_dump(mode="json")}


@app.get("/policy-version")
def get_policy_version():
    """Read-only: current on-disk policy.yaml hash. policy_agent.evaluate()
    already re-reads policy.yaml on every call (no in-process cache to
    invalidate), so hitting this endpoint after editing policy.yaml is
    sufficient to confirm the edit was picked up -- there is no separate
    'apply the reload' step needed."""
    return {"policy_version": policy_agent.policy_version(POLICY_PATH)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
