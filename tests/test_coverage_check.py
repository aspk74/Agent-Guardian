"""Tests for guardian/coverage_check.py (PLAN.md s7 step 14 / rev-2 defect #8).

The openai client is fully faked, same pattern as test_finance_agent.py --
no network call, no API key needed. The one thing that MUST hold under any
model output is the constraint: suggested_disposition can only ever be
"escalate" or "deny", never "allow".
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import guardian.coverage_check as coverage_check


@dataclass
class FakeMessage:
    content: str | None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeChatCompletion:
    choices: list = field(default_factory=list)


class FakeCompletionsResource:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        assert self._responses, "FakeCompletionsResource ran out of queued responses"
        text = self._responses.pop(0)
        return FakeChatCompletion(choices=[FakeChoice(message=FakeMessage(content=text))])


class FakeChat:
    def __init__(self, responses: list[str]):
        self.completions = FakeCompletionsResource(responses)


class FakeClient:
    def __init__(self, responses: list[str]):
        self.chat = FakeChat(responses)


POLICY_YAML = """version: 1
rules:
  - id: FIN-001
    description: Single payment over $500 needs a human
    when: {action_type: make_payment, amount_cents_gt: 50000}
    then: escalate
"""


def test_valid_response_parses_into_report(tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML)

    response = """{
      "findings": [
        {"action_type": "write_file", "issue": "no rule covers write_file at all",
         "suggested_disposition": "escalate"}
      ],
      "summary": "write_file has no covering rule"
    }"""
    client = FakeClient([response])

    report = coverage_check.run_coverage_check(str(policy_path), client=client)

    assert report.summary == "write_file has no covering rule"
    assert len(report.findings) == 1
    assert report.findings[0].action_type == "write_file"
    assert report.findings[0].suggested_disposition == "escalate"
    assert len(client.chat.completions.calls) == 1  # no retry needed


def test_allow_disposition_is_rejected_and_retried(tmp_path):
    """The actual defect-#8 guard: if the model ever outputs "allow" for
    suggested_disposition, that response must be rejected (Pydantic's
    Literal["escalate", "deny"] fails validation) and retried -- never
    silently accepted or coerced into something usable."""
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML)

    bad_response = """{
      "findings": [
        {"action_type": "write_file", "issue": "looks safe to just allow",
         "suggested_disposition": "allow"}
      ],
      "summary": "bad"
    }"""
    good_response = """{
      "findings": [
        {"action_type": "write_file", "issue": "no rule covers write_file",
         "suggested_disposition": "deny"}
      ],
      "summary": "fixed on retry"
    }"""
    client = FakeClient([bad_response, good_response])

    report = coverage_check.run_coverage_check(str(policy_path), client=client)

    assert len(client.chat.completions.calls) == 2, "must have retried once after the rejected 'allow'"
    assert report.summary == "fixed on retry"
    assert report.findings[0].suggested_disposition == "deny"


def test_allow_disposition_twice_raises_coverage_check_error(tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML)

    bad_response = """{
      "findings": [{"action_type": "write_file", "issue": "x", "suggested_disposition": "allow"}],
      "summary": "bad"
    }"""
    client = FakeClient([bad_response, bad_response])

    with pytest.raises(coverage_check.CoverageCheckError):
        coverage_check.run_coverage_check(str(policy_path), client=client)


def test_malformed_json_retries_then_raises(tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML)

    client = FakeClient(["not json", "still not json"])

    with pytest.raises(coverage_check.CoverageCheckError):
        coverage_check.run_coverage_check(str(policy_path), client=client)


def test_empty_findings_is_valid(tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML)

    response = '{"findings": [], "summary": "no gaps found"}'
    client = FakeClient([response])

    report = coverage_check.run_coverage_check(str(policy_path), client=client)
    assert report.findings == []
    assert report.policy_version  # sha256 of the temp policy.yaml, non-empty
