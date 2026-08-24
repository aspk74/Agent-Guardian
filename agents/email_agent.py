"""Email worker agent (PLAN.md section 7 step 6)."""
from __future__ import annotations

import openai

from agents.base import WorkerAgent
from schemas import ActionEnvelope, ActionType, EmailParams


class EmailAgent(WorkerAgent):
    action_type = ActionType.SEND_EMAIL
    params_model = EmailParams
    target_field = "recipient"

    def __init__(self, client: openai.OpenAI | None = None, model: str = "gpt-4o-mini"):
        super().__init__(agent_name="email", client=client, model=model)

    def handle(self, task: str, session_id: str) -> ActionEnvelope:
        return self.propose(task, session_id)
