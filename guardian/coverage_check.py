"""LLM coverage check over the rule set (PLAN.md s7 step 14 / rev-2 defect #8).

Asks an LLM to look for gaps and risks in policy.yaml's rule set -- action
types with no covering rule, rules that look like they don't do what their
description claims, missing safeguards an author likely intended. This is
advisory: it never decides a real Decision and never touches evaluate()'s
code path.

Constrained by design: a finding's suggested disposition may only be
"escalate" or "deny". `allow` is reachable exclusively through a deterministic
rule match in guardian/policy_agent.py -- letting an LLM-driven check ever
recommend "allow" would reintroduce rev-2 defect #8. That constraint is
enforced here at parse time (Pydantic), not by asking the model nicely.
"""
from __future__ import annotations

import json
from typing import Literal

import openai
import yaml
from pydantic import BaseModel

import guardian.policy_agent as policy_agent
from guardian.llm_json import PARSE_ERRORS, strip_code_fence
from schemas import ActionType


class CoverageCheckError(Exception):
    """The LLM failed to produce a valid, constraint-satisfying response
    after one retry."""


class Finding(BaseModel):
    """One gap or risk the model noticed. `suggested_disposition` is the
    ONLY field constrained to escalate/deny -- see module docstring."""
    action_type: str
    issue: str
    suggested_disposition: Literal["escalate", "deny"]


class CoverageReport(BaseModel):
    policy_version: str
    findings: list[Finding]
    summary: str


def _system_prompt(policy_yaml_text: str, retry_note: str | None = None) -> str:
    action_types = ", ".join(t.value for t in ActionType)
    prompt = f"""You are a policy-coverage auditor for an agent-guardian system. You are given \
the full contents of policy.yaml below. Your job is to find GAPS and RISKS, never to approve \
anything.

The complete set of action types this system can ever see is: {action_types}.

policy.yaml:
---
{policy_yaml_text}
---

Look for:
- Any action type in the list above with no rule that matches it at all.
- Rules whose `when` conditions look too narrow to catch what their `description` promises.
- Any other gap a rule author likely didn't intend.

Respond with ONLY a single JSON object, no markdown fences, no prose outside the JSON:

{{
  "findings": [
    {{"action_type": "<one of the action types above>", "issue": "<what's missing or risky>", \
"suggested_disposition": "escalate" | "deny"}}
  ],
  "summary": "<one sentence overview>"
}}

Hard rule: "suggested_disposition" may ONLY ever be "escalate" or "deny". You have no authority \
to recommend "allow" for anything, under any circumstance -- allow is reachable only through an \
exact, deterministic rule match written by a human. If you believe something is safe to allow, \
say so in "issue" as a human decision to make, but the "suggested_disposition" field itself must \
still be "escalate" or "deny". If you output "allow" in that field the response will be rejected \
and you will be asked to redo it.
If there are no findings, return an empty "findings" list.
"""
    if retry_note:
        prompt += f"\nIMPORTANT: {retry_note}\n"
    return prompt


def _parse(raw_text: str, policy_version: str) -> CoverageReport:
    data = json.loads(strip_code_fence(raw_text))
    data["policy_version"] = policy_version
    # Pydantic's Literal["escalate", "deny"] on Finding.suggested_disposition is the actual
    # enforcement: a model that outputs "allow" here fails validation and is retried, never
    # silently coerced or accepted.
    return CoverageReport.model_validate(data)


def run_coverage_check(
    policy_path: str = "policy.yaml",
    client: openai.OpenAI | None = None,
    model: str = "gpt-4o-mini",
) -> CoverageReport:
    """Advisory only. Returns a CoverageReport the operator can read; never
    called from evaluate()'s path and never persisted as a Decision."""
    version = policy_agent.policy_version(policy_path)
    with open(policy_path, "rb") as f:
        raw = f.read()
    policy_yaml_text = raw.decode("utf-8")
    # Fail fast on malformed YAML before spending an LLM call on it.
    yaml.safe_load(raw)

    client = client or openai.OpenAI()

    retry_note = None
    for attempt in range(2):
        text = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _system_prompt(policy_yaml_text, retry_note)},
                      {"role": "user", "content": "Audit this policy for coverage gaps."}],
        ).choices[0].message.content or ""
        try:
            return _parse(text, version)
        except PARSE_ERRORS as exc:
            if attempt == 1:
                raise CoverageCheckError(
                    f"LLM failed to produce a valid coverage report after one retry: {exc}"
                ) from exc
            retry_note = (
                f"Your previous response was invalid ({exc}). Previous response was: "
                f"{text!r}. Remember: suggested_disposition must be exactly \"escalate\" or \"deny\", "
                "never \"allow\". Return ONLY the JSON object."
            )
