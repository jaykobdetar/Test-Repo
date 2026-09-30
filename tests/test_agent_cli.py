import json

import pytest

from probe_core.agent_cli import demo, main
from probe_core.tasks import AgentWorkspace


def test_cli_demo_executes_two_agent_handoff_and_reopens_results(tmp_path, capsys):
    state = tmp_path / "demo"
    main(["demo", "--state", str(state)])
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "completed"
    assert output["result"] == {"summary": "The source contains 4 words."}
    with AgentWorkspace(state) as workspace:
        assert workspace.show(output["task_id"]) == output
        assert [task["agent_id"] for task in workspace.list()] == ["analyst", "reporter"]


def test_demo_refuses_existing_workspace_without_claiming_user_work(tmp_path):
    state = tmp_path / "existing"
    with AgentWorkspace(state) as workspace:
        task = workspace.submit("user-work", "Keep this pending")
        with pytest.raises(FileExistsError):
            demo(state)
        assert workspace.show(task["task_id"]) == task
        assert len(workspace.list()) == 1
