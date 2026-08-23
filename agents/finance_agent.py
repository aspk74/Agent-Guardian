"""Finance worker agent (PLAN.md section 7 step 6)."""
from __future__ import annotations

import openai

from agents.base import WorkerAgent
from schemas import ActionEnvelope, ActionType, PaymentParams


class FinanceAgent(WorkerAgent):
    action_type = ActionType.MAKE_PAYMENT
    params_model = PaymentParams
    target_field = "counterparty"

    def __init__(self, client: openai.OpenAI | None = None, model: str = "gpt-4o-mini"):
        super().__init__(agent_name="finance", client=client, model=model)

    def handle(self, task: str, session_id: str) -> ActionEnvelope:
        return self.propose(task, session_id)
