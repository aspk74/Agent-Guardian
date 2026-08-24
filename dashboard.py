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

Run: uvicorn dashboard:app --reload --port 8000  (or python dashboard.py)
"""
from __future__ import annotations

import html
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()

import sqlite3

import db
import guardian.escalation as esc
import guardian.policy_agent as policy_agent

DB_PATH = os.environ.get("GUARDIAN_DB", "guardian.db")
POLICY_PATH = os.environ.get("GUARDIAN_POLICY", "policy.yaml")

app = FastAPI(title="Guardian Dashboard")

# One connection for the process lifetime, not one per request: db.init_db()
# re-runs the full CREATE TABLE/INDEX IF NOT EXISTS schema script and a
# commit, which is wasted work on every single HTTP request, and a fresh
# sqlite3.Connection per request that's never closed leaks file descriptors
# under sustained traffic. FastAPI's sync `def` routes run in a threadpool,
# so this connection needs check_same_thread=False -- db.init_db() doesn't
# expose that constructor flag (main.py's CLI never needs it: one
# connection per process invocation, always on the main thread), so the
# schema init is repeated here rather than widening init_db()'s signature
# for its one caller that needs a different flag. sqlite serializes writers
# internally, and each request does one short operation, not an interleaved
# multi-statement transaction, so sharing this connection across threads is
# safe.
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(db._SCHEMA)
_conn.commit()


def _get_conn():
    return _conn


def _render_row(row: dict) -> str:
    envelope, decision = row["envelope"], row["decision"]
    action = envelope.action
    action_id = html.escape(action.id)
    return f"""
    <tr>
      <td>{html.escape(action.requesting_agent)}</td>
      <td>{html.escape(action.action_type.value)}</td>
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


@app.get("/", response_class=HTMLResponse)
def dashboard_page():
    conn = _get_conn()
    rows = esc.pending(conn)
    version = policy_agent.policy_version(POLICY_PATH)
    body = "".join(_render_row(r) for r in rows) or "<tr><td colspan='7'>no pending escalations</td></tr>"
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
</body></html>"""


@app.get("/pending")
def list_pending(session_id: str | None = None):
    """JSON equivalent of the dashboard page's table, same escalation.pending() call."""
    conn = _get_conn()
    rows = esc.pending(conn, session_id=session_id)
    return [
        {
            "action_id": r["envelope"].action.id,
            "agent": r["envelope"].action.requesting_agent,
            "action_type": r["envelope"].action.action_type.value,
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
    uvicorn.run(app, host="127.0.0.1", port=8000)
