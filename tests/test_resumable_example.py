import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from probe_core.tasks import AgentWorkspace

spec = importlib.util.spec_from_file_location("resumable_run", Path(__file__).parents[1] / "examples/resumable_run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def plan(tmp_path, count=2, exit_status=0, outputs=None):
    jobs = []
    for index in range(count):
        counter = tmp_path / f"calls-{index}"
        code = (f"from pathlib import Path; p=Path({str(counter)!r}); "
                "p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
                f"Path('result.txt').write_text('result'); raise SystemExit({exit_status})")
        jobs.append({"id": f"job-{index}", "command": [sys.executable, "-c", code],
                     "outputs": outputs if outputs is not None else ["result.txt"]})
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(jobs))
    return path, tmp_path / "state"


def test_real_commands_resume_unknown_exit_without_rerunning_completed_jobs(tmp_path, monkeypatch):
    path, state = plan(tmp_path)
    original_wait = subprocess.Popen.wait
    calls = 0

    def lose_second_exit(process, *args, **kwargs):
        nonlocal calls
        result = original_wait(process, *args, **kwargs)
        calls += 1
        if calls == 2:
            # Simulate the wrapper losing its exit observation. Reap the actual
            # child here so this offline test leaves no external process behind.
            raise KeyboardInterrupt
        return result

    with monkeypatch.context() as patch:
        patch.setattr(subprocess.Popen, "wait", lose_second_exit)
        with pytest.raises(KeyboardInterrupt):
            runner.run(path, state)
    with AgentWorkspace(state) as board:
        first, second = board.list()
        assert [first["state"], second["state"]] == ["completed", "running"]
        first_result = board.show(first["task_id"])["result"]
    unfinished = state / second["task_id"] / "1"
    assert (unfinished / "work/result.txt").exists()  # A complete-looking file is insufficient.
    assert json.loads((unfinished / "receipt.json").read_text())["returncode"] is None
    with pytest.raises(ValueError, match="--recover-stopped"):
        runner.run(path, state)
    # Exercise the documented CLI in a fresh interpreter for recovery.
    result = subprocess.run([sys.executable, str(Path(runner.__file__)), str(path), "--state", str(state),
                             "--recover-stopped", "job-1"], capture_output=True, text=True, check=True)
    completed = json.loads(result.stdout)
    assert all(task["state"] == "completed" for task in completed)
    assert completed[0]["result"] == first_result
    assert (tmp_path / "calls-0").read_text() == "x"
    assert (tmp_path / "calls-1").read_text() == "xx"
    assert Path(completed[1]["result"]["outputs"][0]).parent.parent.name == "2"
    assert (unfinished / "work/result.txt").exists()  # Previous evidence remains intact.


def test_observed_success_survives_interrupted_board_publication(tmp_path, monkeypatch):
    path, state = plan(tmp_path, count=1)
    with monkeypatch.context() as patch:
        patch.setattr(runner, "publish", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            runner.run(path, state)
    [task] = runner.run(path, state, ["job-0"])
    receipt = json.loads(Path(task["result"]["receipt"]).read_text())
    assert task["attempt_count"] == 2  # New claim publishes the earlier observed result.
    assert receipt["phase"] == "exited" and receipt["returncode"] == 0
    assert receipt["finished_at"] and receipt["pid"]
    assert (tmp_path / "calls-0").read_text() == "x"
    assert runner.run(path, state) == [task]
    changed = json.loads(path.read_text())
    changed[0]["command"].append("changed")
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="Plan changed"):
        runner.run(path, state)


def test_failure_and_partial_artifacts_are_not_success(tmp_path):
    path, state = plan(tmp_path, count=1, exit_status=7)
    with pytest.raises(ValueError, match="exited with 7"):
        runner.run(path, state)
    with AgentWorkspace(state) as board:
        [task] = board.list()
        assert task["state"] == "failed"
    receipt = json.loads((state / task["task_id"] / "1/receipt.json").read_text())
    assert receipt["returncode"] == 7 and receipt["phase"] == "exited"
    assert (Path(receipt["cwd"]) / "result.txt").exists()
    # Runner metadata must not masquerade as a command's missing artifact.
    path, state = plan(tmp_path, count=1, outputs=["receipt.json"])
    state = tmp_path / "missing-output-state"
    with pytest.raises(ValueError, match="missing"):
        runner.run(path, state)
    with AgentWorkspace(state) as board:
        [task] = board.list()
        assert task["state"] == "failed"
        receipt = json.loads((state / task["task_id"] / "1/receipt.json").read_text())
        assert receipt["returncode"] == 0  # Exit success does not invent absent outputs.
