"""Agent-facing task methods; recovery stays with the state-directory owner."""

from .tasks import AgentWorkspace


class TaskService:
    def __init__(self, workspace: AgentWorkspace):
        self.workspace = workspace
        self.methods = {
            "submit_task": self.submit_task,
            "claim_task": workspace.claim,
            "complete_task": workspace.complete,
            "fail_task": workspace.fail,
            "cancel_task": workspace.cancel,
            "list_tasks": workspace.list,
            "task_status": workspace.show,
        }

    def submit_task(self, idempotency_key, instruction, depends_on=()):
        return self.workspace.submit(idempotency_key, instruction, depends_on)

    def dispatch(self, method, params):
        if method not in self.methods:
            raise PermissionError("method is not exposed to agents")
        if type(params) is not dict:
            raise ValueError("parameters must be an object")
        return self.methods[method](**params)
