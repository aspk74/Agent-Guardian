"""Shared scaffold for worker agents (PLAN.md section 11).

A WorkerAgent turns a natural-language task into an ActionEnvelope by making
ONE LLM call (with one retry on malformed output). It never executes
anything -- it returns inert data for the Guardian to evaluate.

Security note (PLAN.md s2.1 / s9.2): the LLM's JSON may only ever supply
`reasoning` and the params-model fields. `session_id`, `requesting_agent`,
`action_type` are set by THIS code from caller-controlled values and never
read out of the model's response, even if the model's JSON contains keys
with those names.

`target` is likewise never LLM-supplied, for a subtler reason found by
running the live demo: policy rules key off `action.target` (e.g. FIN-003's
`target_not_in`), so if the LLM is free to phrase it -- "acme-corp" vs.
"vendor_invoices/acme-corp" vs. "Acme Corp" -- the same counterparty produces
a different policy outcome depending on the model's mood. `target` is
instead derived deterministically from whichever params field each subclass
names in `target_field` (e.g. FinanceAgent.target_field = "counterparty").
"""
from __future__ import annotations

import json
import re

import openai
from pydantic import BaseModel, ValidationError

from schemas import Action, ActionEnvelope, ActionType

# Exceptions that indicate the LLM's response could not be turned into a
# valid Action -- malformed JSON, missing keys, or params that fail the
# params_model's validation. This is the one place in the codebase allowed
# a broad-ish catch (PLAN.md s8: "ActionValidationError ... one retry, then
# deny"), and it is a specific tuple, not `except Exception`.
_PARSE_ERRORS = (json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError)


class ActionValidationError(Exception):
    """Raised when the LLM fails to produce a valid Action, twice in a row."""

    def __init__(self, message: str, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?(.*?)\n?```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Best-effort removal of a ```/```json wrapper, in case the model adds
    one despite being told not to. Does not affect well-formed output.

    A line-splitting approach here previously mishandled a fence collapsed
    onto a single line (```{"a": 1}``` with no embedded newlines) by
    stripping it down to an empty string -- caught by the JSONDecodeError
    retry, but silently burning the one allotted retry on an otherwise valid
    response. A regex match on the whole string, not line count, handles the
    single-line and multi-line cases uniformly."""
    stripped = text.strip()
    match = _CODE_FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped


class WorkerAgent:
    """Base for all worker agents. Subclasses set `action_type` and
    `params_model` as class attributes, e.g.:

        class FinanceAgent(WorkerAgent):
            action_type = ActionType.MAKE_PAYMENT
            params_model = PaymentParams
            target_field = "counterparty"
    """

    action_type: ActionType
    params_model: type[BaseModel]
    target_field: str  # name of the params_model field to use as Action.target

    def __init__(
        self,
        agent_name: str,
        client: openai.OpenAI | None = None,
        model: str = "gpt-4o-mini",
    ):
        self.agent_name = agent_name
        self.model = model
        # Injectable so tests can pass a fake client with no real API key.
        self.client = client or openai.OpenAI()

    # -- prompt construction -------------------------------------------------

    def _params_field_lines(self) -> str:
        lines = []
        for name, field in self.params_model.model_fields.items():
            if name == "kind":
                continue  # discriminator, has a fixed default, not for the LLM to set
            required = "required" if field.is_required() else "optional"
            type_name = getattr(field.annotation, "__name__", str(field.annotation))
            lines.append(f'    "{name}": <{type_name}>  ({required})')
        return "\n".join(lines)

    def _system_prompt(self, retry_note: str | None = None) -> str:
        prompt = f"""You are the "{self.agent_name}" worker agent inside a guarded multi-agent \
system. You propose actions; you never execute them -- a separate guardian process \
evaluates and may allow, deny, or escalate your proposal to a human.

You will be given a task in natural language. Decide on ONE concrete action of type \
"{self.action_type.value}" that accomplishes it, then respond with ONLY a single JSON \
object and nothing else: no markdown code fences, no prose before or after it.

The JSON object must have exactly this shape:

{{
  "reasoning": "<free text: why you chose this action>",
  "action": {{
{self._params_field_lines()}
  }}
}}

Rules:
- Include ONLY the fields listed above inside "action". Do not add extra keys such as \
"target", "action_type", "session_id", "requesting_agent", or "id" -- those are assigned by \
the system and any such keys you include will be ignored.
- amount_cents (if present) must be an integer number of cents, e.g. $750.00 -> 75000. \
Never use a float or a string for it.
"""
        if retry_note:
            prompt += f"\nIMPORTANT: {retry_note}\n"
        return prompt

    # -- LLM call --------------------------------------------------------

    def _invoke(self, system_prompt: str, task: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM response contained no text content")
        return content

    # -- parsing -----------------------------------------------------------

    def _build_envelope(self, raw_text: str, session_id: str) -> ActionEnvelope:
        data = json.loads(_strip_code_fence(raw_text))
        reasoning = data["reasoning"]
        action_obj = data["action"]
        if not isinstance(action_obj, dict):
            raise TypeError("'action' must be a JSON object")

        # Only the params-model fields (never target/action_type/session_id/id/etc,
        # even if the LLM's JSON contains such keys -- see module docstring).
        params_kwargs = {k: v for k, v in action_obj.items() if k != "target"}
        params = self.params_model(**params_kwargs)
        target = getattr(params, self.target_field)

        action = Action(
            session_id=session_id,
            requesting_agent=self.agent_name,
            action_type=self.action_type,
            target=target,
            params=params,
        )
        return ActionEnvelope(
            action=action,
            reasoning=reasoning,
            model=self.model,
            raw_response=raw_text,
        )

    # -- public API ----------------------------------------------------------

    def propose(self, task: str, session_id: str) -> ActionEnvelope:
        """Ask the LLM to reason about `task` and emit an Action. Retries the
        LLM call exactly once on malformed JSON / invalid params, then raises
        ActionValidationError."""
        text = self._invoke(self._system_prompt(), task)
        try:
            return self._build_envelope(text, session_id)
        except _PARSE_ERRORS as first_error:
            retry_note = (
                f"Your previous response could not be parsed ({first_error}). "
                f"Previous response was: {text!r}. "
                "Return ONLY a valid JSON object matching the required shape this time."
            )

        text = self._invoke(self._system_prompt(retry_note=retry_note), task)
        try:
            return self._build_envelope(text, session_id)
        except _PARSE_ERRORS as second_error:
            raise ActionValidationError(
                f"LLM failed to produce a valid action after one retry: {second_error}",
                raw_response=text,
            ) from second_error
