"""Focused regression coverage for the complete offline coordination workflow."""

import hashlib
import multiprocessing
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor

import pytest

from probe_core.tasks import MAX_INSTRUCTION_BYTES, MAX_RESULT_BYTES, AgentWorkspace


def _submit_and_claim(directory):
    workspace = AgentWorkspace(directory)
    task = workspace.submit("shared", "Count words in the supplied text")
    return task["task_id"], workspace.claim(f"agent-{os.getpid()}")


def test_agents_complete_dependencies_and_reopen_durable_result(tmp_path):
    directory = tmp_path / "workspace"
    with AgentWorkspace(directory) as workspace:
        plan = workspace.submit("plan", "Choose text for analysis")
        analyze = workspace.submit("analyze", "Count the words", [plan["task_id"]])
        report = workspace.submit("report", "Summarize the count", [analyze["task_id"]])
        assert workspace.show(analyze["task_id"])["blocked_by"] == [plan["task_id"]]
        claimed = workspace.claim("planner")
        assert claimed["task_id"] == plan["task_id"]
        workspace.complete(claimed["task_id"], claimed["claim_token"], {"text": "Agents share useful results"})
    workspace = AgentWorkspace(directory)
    claimed = workspace.claim("analyst")
    assert claimed["task_id"] == analyze["task_id"]
    text = workspace.show(plan["task_id"])["result"]["text"]
    workspace.complete(claimed["task_id"], claimed["claim_token"], {"word_count": len(text.split())})
    claimed = workspace.claim("reporter")
    count = workspace.show(analyze["task_id"])["result"]["word_count"]
    workspace.complete(claimed["task_id"], claimed["claim_token"], {"report": f"The text contains {count} words."})
    reopened = AgentWorkspace(directory).show(report["task_id"])
    assert reopened["state"] == "completed"
    assert reopened["result"] == {"report": "The text contains 4 words."}
    assert reopened["blocked_by"] == []
    assert reopened["created_at"] <= reopened["claimed_at"] <= reopened["finished_at"]
    assert workspace.claim("finished") is None


def test_submission_idempotency_and_existing_dependency_requirement(tmp_path):
    workspace = AgentWorkspace(tmp_path / "workspace")
    parent = workspace.submit("parent", "Prepare inputs")
    task = workspace.submit("child", "Analyze", [parent["task_id"]])
    assert workspace.submit("child", "Analyze", (parent["task_id"],)) == task
    for instruction, dependencies in (("Changed", [parent["task_id"]]), ("Analyze", [])):
        with pytest.raises(ValueError, match="idempotency"):
            workspace.submit("child", instruction, dependencies)
    with pytest.raises(KeyError):
        workspace.submit("missing", "Analyze", ["unknown"])
    with pytest.raises(ValueError, match="unique"):
        workspace.submit("duplicates", "Analyze", [parent["task_id"], parent["task_id"]])
    assert len(workspace.list()) == 2


def test_cross_process_submission_and_claim_are_atomic(tmp_path):
    directory = str(tmp_path / "workspace")
    AgentWorkspace(directory)
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as workers:
        outcomes = list(workers.map(_submit_and_claim, [directory] * 8))
    assert len({task_id for task_id, _ in outcomes}) == 1
    claims = [claim for _, claim in outcomes if claim is not None]
    assert len(claims) == 1
    workspace = AgentWorkspace(directory)
    workspace.complete(claims[0]["task_id"], claims[0]["claim_token"], {"winner": True})
    assert len(workspace.list()) == 1


def test_ownership_persists_until_manual_release_and_stale_tokens_are_fenced(tmp_path):
    directory = tmp_path / "workspace"
    workspace = AgentWorkspace(directory)
    task = workspace.submit("task", "Do external work")
    old = workspace.claim("first-agent")
    with sqlite3.connect(workspace.database) as connection:
        connection.execute("UPDATE tasks SET claimed_at='1970-01-01T00:00:00+00:00'")
    reopened = AgentWorkspace(directory)
    assert reopened.claim("second-agent") is None
    assert reopened.show(task["task_id"])["state"] == "running"
    reset = reopened.release(task["task_id"])
    assert reset["state"] == "pending" and reset["agent_id"] is None
    fresh = reopened.claim("second-agent")
    assert fresh["attempt_count"] == 2 and fresh["claim_token"] != old["claim_token"]
    with pytest.raises(PermissionError):
        reopened.complete(task["task_id"], old["claim_token"], {"stale": True})
    with pytest.raises(PermissionError):
        reopened.fail(task["task_id"], "wrong-token", "incorrect owner")
    reopened.complete(task["task_id"], fresh["claim_token"], {"final": True})
    with pytest.raises(PermissionError):
        reopened.complete(task["task_id"], fresh["claim_token"], {"changed": True})
    for operation in (reopened.cancel, reopened.release):
        with pytest.raises(ValueError, match="immutable"):
            operation(task["task_id"])
    assert reopened.show(task["task_id"])["result"] == {"final": True}


def test_failures_and_cancellation_stay_visible_and_block_dependents(tmp_path):
    workspace = AgentWorkspace(tmp_path / "workspace")
    task = workspace.submit("task", "Try work")
    child = workspace.submit("child", "Use result", [task["task_id"]])
    claim = workspace.claim("agent")
    failed = workspace.fail(task["task_id"], claim["claim_token"], "Input unavailable")
    assert failed["state"] == "failed" and failed["failure_reason"] == "Input unavailable"
    assert workspace.claim("other") is None
    assert workspace.show(child["task_id"])["blocked_by"] == [task["task_id"]]
    workspace.release(task["task_id"])
    claim = workspace.claim("replacement")
    cancelled = workspace.cancel(task["task_id"])
    assert workspace.cancel(task["task_id"]) == cancelled
    with pytest.raises(PermissionError):
        workspace.complete(task["task_id"], claim["claim_token"], {"late": "result"})
    assert workspace.claim("other") is None
    reset = workspace.release(task["task_id"])
    assert reset["failure_reason"] is None and reset["finished_at"] is None
    assert workspace.show(child["task_id"])["state"] == "pending"


def test_bounded_strict_json_and_text_reject_without_changing_task(tmp_path):
    workspace = AgentWorkspace(tmp_path / "workspace")
    for instruction in ("", " " * 2, "a" * (MAX_INSTRUCTION_BYTES + 1), "界" * MAX_INSTRUCTION_BYTES):
        with pytest.raises(ValueError):
            workspace.submit("invalid", instruction)
    with pytest.raises(ValueError):
        workspace.submit("invalid", "Do work", "not-a-list")
    task = workspace.submit("valid", "Do work")
    claim = workspace.claim("agent")
    cyclic = []
    cyclic.append(cyclic)
    for result in (float("nan"), float("inf"), (1, 2), {1: "key"}, cyclic, "x" * MAX_RESULT_BYTES):
        with pytest.raises((ValueError, TypeError)):
            workspace.complete(task["task_id"], claim["claim_token"], result)
    with pytest.raises(ValueError):
        workspace.fail(task["task_id"], claim["claim_token"], "x" * 4097)
    assert workspace.show(task["task_id"])["state"] == "running"
    workspace.complete(task["task_id"], claim["claim_token"], {"unicode": "分析", "nullable": None})


def test_tokens_are_hashed_and_listing_is_bounded_without_payloads(tmp_path):
    workspace = AgentWorkspace(tmp_path / "workspace")
    task = workspace.submit("task", "Instructions are opaque data")
    workspace.submit("next", "Another task")
    claim = workspace.claim("agent")
    with sqlite3.connect(workspace.database) as connection:
        stored = connection.execute("SELECT claim_hash FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()[0]
    assert stored == hashlib.sha256(claim["claim_token"].encode()).hexdigest()
    assert claim["claim_token"].encode() not in workspace.database.read_bytes()
    for response in (workspace.show(task["task_id"]), workspace.submit("task", "Instructions are opaque data")):
        assert "claim_token" not in response and "claim_hash" not in response
    summary = workspace.list(limit=1)[0]
    assert summary["task_id"] == task["task_id"]
    assert not {"instruction", "result", "failure_reason", "claim_hash", "claim_token"}.intersection(summary)
    assert len(workspace.list(offset=1, limit=1)) == 1
    assert workspace.list(offset=2) == []
    for arguments in ({"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": 1.5}):
        with pytest.raises(ValueError):
            workspace.list(**arguments)


def test_private_owned_nonsymlink_state_files_are_required(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(PermissionError):
        AgentWorkspace(public)
    workspace = AgentWorkspace(tmp_path / "workspace")
    assert workspace.root.stat().st_mode & 0o077 == 0
    assert workspace.database.stat().st_mode & 0o077 == 0
    linked = tmp_path / "linked"
    linked.symlink_to(workspace.root, target_is_directory=True)
    with pytest.raises(PermissionError):
        AgentWorkspace(linked)
    workspace.database.chmod(0o640)
    with pytest.raises(PermissionError):
        workspace.list()
    workspace.database.chmod(0o600)
    saved = workspace.database.with_name("saved.sqlite3")
    workspace.database.rename(saved)
    workspace.database.symlink_to(saved)
    with pytest.raises(PermissionError):
        AgentWorkspace(workspace.root)
    workspace.database.unlink()
    os.link(saved, workspace.database)
    with pytest.raises(PermissionError):
        AgentWorkspace(workspace.root)


def test_unknown_or_changed_database_schema_is_rejected(tmp_path):
    for name, statement in (("future", "PRAGMA user_version=999"), ("changed", "DROP TABLE dependencies")):
        workspace = AgentWorkspace(tmp_path / name)
        with sqlite3.connect(workspace.database) as connection:
            connection.execute(statement)
        with pytest.raises(ValueError, match="schema"):
            AgentWorkspace(workspace.root)
