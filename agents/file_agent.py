"""File worker agents (PLAN.md section 7 step 6).

Three separate classes, one per file operation, rather than one class
branching on operation -- each has its own fixed action_type, so there is
no risk of a read request being mistaken for (or coerced into) a write or
delete at the point the Action is constructed.
"""
from __future__ import annotations

import openai

from agents.base import WorkerAgent
from schemas import ActionEnvelope, ActionType, FileParams


class ReadFileAgent(WorkerAgent):
    action_type = ActionType.READ_FILE
    params_model = FileParams
    target_field = "path"

    def __init__(self, client: openai.OpenAI | None = None, model: str = "gpt-4o-mini"):
        super().__init__(agent_name="file-read", client=client, model=model)

    def handle(self, task: str, session_id: str) -> ActionEnvelope:
        return self.propose(task, session_id)


class WriteFileAgent(WorkerAgent):
    action_type = ActionType.WRITE_FILE
    params_model = FileParams
    target_field = "path"

    def __init__(self, client: openai.OpenAI | None = None, model: str = "gpt-4o-mini"):
        super().__init__(agent_name="file-write", client=client, model=model)

    def handle(self, task: str, session_id: str) -> ActionEnvelope:
        return self.propose(task, session_id)


class DeleteFileAgent(WorkerAgent):
    action_type = ActionType.DELETE_FILE
    params_model = FileParams
    target_field = "path"

    def __init__(self, client: openai.OpenAI | None = None, model: str = "gpt-4o-mini"):
        super().__init__(agent_name="file-delete", client=client, model=model)

    def handle(self, task: str, session_id: str) -> ActionEnvelope:
        return self.propose(task, session_id)
