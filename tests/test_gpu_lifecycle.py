"""Lifecycle action fencing, private metadata validation, and real pidfd signal."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from probe_core.audit import canonical_json
from probe_core import gpu_lifecycle as lifecycle
from probe_core.worker_contracts import ExecutionRequest, WorkerConfig
from test_ledger import job_factory, manifest_data


@pytest.fixture
def execution(tmp_path, job_factory, monkeypatch):
    tmp_path.chmod(0o700)
    config_path, token_path = tmp_path / "worker.json", tmp_path / "token"
    config = WorkerConfig(model_directory=str(tmp_path / "model"), model=job_factory().model,
                          assets=[{"path": "config.json", "sha256": "sha256:" + "1" * 64}],
                          datasets=[{"path": str(tmp_path / "dataset.json"), "sha256": "sha256:" + "2" * 64}],
                          tensor_directory=str(tmp_path / "tensors"), output_directory=str(tmp_path / "attempts"),
                          code_git_commit="a" * 40, environment_lock_path=str(tmp_path / "lock"),
                          provider_backend="local_cpu", region="test", live_price_usd_per_hour=0)
    config_path.write_text(config.model_dump_json())
    config_path.chmod(0o600)
    token_path.write_text("PRIVATE_TOKEN_MUST_NEVER_APPEAR")
    token_path.chmod(0o600)
    now = datetime.now(timezone.utc)
    request = ExecutionRequest(job_id="job-1", attempt_id="attempt-1", worker_id="worker-1", approval_id="approval-1",
                               deadline=now + timedelta(seconds=60), spec=job_factory())
    directory = Path(config.output_directory) / request.attempt_id
    directory.parent.mkdir(mode=0o700)
    directory.mkdir(mode=0o700)
    boot = lifecycle._boot_id()
    child = {"pid": 20001, "identity": "101", "boot_id": boot, "deadline": request.deadline.timestamp(),
             "monotonic_deadline": time.monotonic() + 60, "cgroup": None}
    receipt = {"job_id": request.job_id, "attempt_id": request.attempt_id, "state": "RUNNING",
               "started_at": now.isoformat(), "process_stopped": False}
    marker = {"schema_version": 1, **{key: getattr(request, key) for key in ("job_id", "attempt_id", "worker_id", "approval_id")},
              **{key: child[key] for key in ("pid", "identity", "boot_id")},
              "request_sha256": lifecycle._sha(canonical_json(request.model_dump(mode="json")).encode()),
              "config_sha256": lifecycle._sha(canonical_json(config.model_dump(mode="json")).encode()),
              "started_at": now.isoformat()}
    def write(name, value):
        (directory / name).write_text(canonical_json(value))
        (directory / name).chmod(0o644)  # Supervisor uses private directories, inherited umask.
    write("request.json", request.model_dump(mode="json"))
    write("process.json", child)
    write("receipt.json", receipt)
    write("execution-started.json", marker)
    def process(pid, identity, *, ppid=20003, uid=None, args=None):
        uid = os.geteuid() if uid is None else uid
        return {"pid": pid, "identity": identity, "boot_id": boot, "ppid": ppid,
                "uid": uid, "gid": os.getegid(), "uids": [uid] * 4, "gids": [os.getegid()] * 4,
                "groups": [], "executable": str(Path(sys.executable).resolve()), "args": args or []}
    processes = {
        child["pid"]: process(child["pid"], child["identity"]),
        20002: process(20002, "102", args=[sys.executable, "-m", "probe_core.worker", "--config", str(config_path),
                                         "--token-file", str(token_path), "--port", "8080"]),
        20003: process(20003, "103", ppid=1, args=["python", "-m", "probe_core.gpu_launch"]),
    }
    pid_file = tmp_path / "supervisor.pid"
    pid_file.write_text("20002\n")
    pid_file.chmod(0o644)
    policy = lifecycle._Policy(worker_uid=os.geteuid(), worker_gid=os.getegid(), root_uid=os.geteuid(),
                               pid_file=pid_file, intent_root=tmp_path / "intents", path_root=tmp_path,
                               process_inspector=lambda pid: processes.get(pid))
    read_fd, write_fd = os.pipe()
    signals = []
    monkeypatch.setattr(lifecycle, "_pidfd_open", lambda pid: os.dup(read_fd))
    monkeypatch.setattr(lifecycle, "_kill_pidfd", lambda fd: signals.append(fd))
    result = dict(config=config, config_path=config_path, token_path=token_path, request=request, directory=directory,
                  child=child, receipt=receipt, marker=marker, processes=processes, policy=policy, signals=signals,
                  write=write, config_sha256=lifecycle._sha(config_path.read_bytes()))
    try:
        yield result
    finally:
        os.close(read_fd)
        os.close(write_fd)


def inspect(execution):
    return lifecycle.inspect(execution["config_path"], execution["token_path"], execution["request"],
                             config_sha256=execution["config_sha256"], _policy=execution["policy"])


def restart(execution, action_id="a" * 64):
    if "expected_before" not in execution:
        execution["expected_before"] = json.loads(json.dumps(inspect(execution)["snapshot"]))
    return lifecycle.restart(execution["config_path"], execution["token_path"], execution["request"], action_id,
                             config_sha256=execution["config_sha256"], expected_before=execution["expected_before"],
                             _policy=execution["policy"])


def test_inspection_matches_metadata_without_token(execution):
    result = inspect(execution)
    snapshot = result["snapshot"]
    assert snapshot["request"] == execution["request"].model_dump(mode="json")
    assert snapshot["child"] == execution["child"] and snapshot["child_alive"]
    assert snapshot["supervisor"]["pid"] == 20002 and snapshot["bootstrap"]["pid"] == 20003
    assert datetime.fromisoformat(snapshot["observed_at"]).tzinfo is not None
    assert abs(time.monotonic() - snapshot["observed_monotonic"]) < 1
    assert snapshot["execution_started_sha256"] == lifecycle._sha((execution["directory"] / "execution-started.json").read_bytes())
    assert snapshot["loaded_config_sha256"] == execution["marker"]["config_sha256"]
    assert snapshot["cancellation"] is None and snapshot["cancellation_sha256"] is None
    assert "PRIVATE_TOKEN" not in json.dumps(result)


def cancelled(execution):
    sent = datetime.now(timezone.utc)
    stopped = datetime.now(timezone.utc)
    request = execution["request"]
    proof = {"schema_version": 1,
             **{key: getattr(request, key) for key in ("job_id", "attempt_id", "worker_id", "approval_id")},
             "request_sha256": execution["marker"]["request_sha256"],
             "config_sha256": execution["marker"]["config_sha256"], **execution["child"],
             "signal": "SIGKILL", "signal_scope": "process_group", "signal_sent_at": sent.isoformat(),
             "stopped_at": stopped.isoformat(), "process_stopped": True, "job_scope_stopped": True, "result_present": False}
    execution["write"]("receipt.json", {**execution["receipt"], "state": "CANCELLED", "failure_kind": "cancelled",
                                        "process_stopped": True, "finished_at": stopped.isoformat()})
    execution["write"]("cancellation.json", proof)
    execution["processes"].pop(execution["child"]["pid"])
    return proof


def test_real_helper_output_satisfies_runner_snapshot_and_cancellation_validators(execution):
    from probe_core.gpu_acceptance_actions import _snapshot, _cancel_proof, _stable
    before = _snapshot(inspect(execution)["snapshot"], execution["request"])
    assert not _cancel_proof(before, before, execution["request"])
    proof = cancelled(execution)
    after = _snapshot(inspect(execution)["snapshot"], execution["request"])
    assert after["cancellation"] == proof
    assert after["cancellation_sha256"] == lifecycle._sha((execution["directory"] / "cancellation.json").read_bytes())
    assert _stable(before, after) and _cancel_proof(after, before, execution["request"])
    repeated = _snapshot(inspect(execution)["snapshot"], execution["request"])
    assert repeated["cancellation"] == proof and repeated["cancellation_sha256"] == after["cancellation_sha256"]
    assert _cancel_proof(repeated, before, execution["request"])


@pytest.mark.parametrize("field,value,code", [
    ("attempt_id", "other-attempt", "CANCELLATION_PROOF_MISMATCH"),
    ("config_sha256", "sha256:" + "0" * 64, "CANCELLATION_PROOF_MISMATCH"),
    ("request_sha256", "sha256:" + "0" * 64, "CANCELLATION_PROOF_MISMATCH"),
    ("identity", "999999", "CANCELLATION_PROOF_MISMATCH"),
    ("deadline", 1, "CANCELLATION_PROOF_MISMATCH"),
    ("monotonic_deadline", 1, "CANCELLATION_PROOF_MISMATCH"),
    ("signal_scope", "process_and_cgroup", "CANCELLATION_STOP_NOT_PROVEN"),
    ("signal", "SIGTERM", "CANCELLATION_STOP_NOT_PROVEN"),
    ("job_scope_stopped", False, "CANCELLATION_STOP_NOT_PROVEN"),
    ("result_present", True, "CANCELLATION_STOP_NOT_PROVEN"),
    ("signal_sent_at", "2020-01-01T00:00:00+00:00", "INVALID_CANCELLATION_TIME"),
    ("stopped_at", "2099-01-01T00:00:00+00:00", "INVALID_CANCELLATION_TIME"),
])
def test_mismatched_or_unproven_cancellation_refused(execution, field, value, code):
    proof = cancelled(execution)
    proof[field] = value
    execution["write"]("cancellation.json", proof)
    with pytest.raises(lifecycle.LifecycleError, match=code):
        inspect(execution)


def test_result_file_and_live_child_contradict_cancellation_proof(execution):
    original_child = execution["processes"][execution["child"]["pid"]]
    cancelled(execution)
    execution["write"]("result.json", {"state": "SUCCEEDED"})
    with pytest.raises(lifecycle.LifecycleError, match="CANCELLATION_STOP_NOT_PROVEN"):
        inspect(execution)
    (execution["directory"] / "result.json").unlink()
    execution["processes"][execution["child"]["pid"]] = original_child
    with pytest.raises(lifecycle.LifecycleError, match="CANCELLATION_STOP_NOT_PROVEN"):
        inspect(execution)


def test_optional_cancellation_file_uses_same_owned_nofollow_bounds(execution):
    cancelled(execution)
    path = execution["directory"] / "cancellation.json"
    original = path.with_suffix(".original")
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(OSError):
        inspect(execution)
    path.unlink()
    path.write_bytes(b" " * 8193)
    with pytest.raises(lifecycle.LifecycleError, match="UNSAFE_FILE"):
        inspect(execution)


def test_terminal_cancelled_child_is_observable_after_pid_reuse(execution):
    execution["processes"][20001]["identity"] = "999"
    receipt = {**execution["receipt"], "state": "CANCELLED", "failure_kind": "cancelled", "process_stopped": True,
               "finished_at": datetime.now(timezone.utc).isoformat()}
    execution["write"]("receipt.json", receipt)
    assert inspect(execution)["snapshot"]["child_alive"] is False
    with pytest.raises(lifecycle.LifecycleError, match="ATTEMPT_NOT_RUNNING"):
        restart(execution)
    assert not execution["signals"]


@pytest.mark.parametrize("mutation,code", [
    (lambda e: e["processes"][20002].update(uids=[0, 123, 0, 0]), "WRONG_WORKER_IDENTITY"),
    (lambda e: e["processes"][20002].update(gids=[999] * 4), "WRONG_WORKER_IDENTITY"),
    (lambda e: e["processes"][20002].update(args=[sys.executable, "-m", "unrelated"]), "WRONG_SUPERVISOR_COMMAND"),
    (lambda e: e["processes"][20002].update(ppid=55555), "WRONG_BOOTSTRAP_IDENTITY"),
    (lambda e: e["processes"][20003].update(args=["python", "-m", "unrelated"]), "WRONG_BOOTSTRAP_IDENTITY"),
    (lambda e: e["processes"][20003].update(uids=[123] * 4), "WRONG_BOOTSTRAP_IDENTITY"),
    (lambda e: e["processes"][20001].update(uids=[123] * 4), "WRONG_WORKER_IDENTITY"),
    (lambda e: e["policy"].pid_file.write_text("99999\n"), "WRONG_WORKER_IDENTITY"),
])
def test_wrong_process_identity_never_signals(execution, mutation, code):
    mutation(execution)
    with pytest.raises(lifecycle.LifecycleError, match=code):
        restart(execution)
    assert not execution["signals"]


@pytest.mark.parametrize("name", ["request.json", "process.json", "execution-started.json", "receipt.json"])
def test_symlink_metadata_refused(execution, name):
    path = execution["directory"] / name
    preserved = path.with_suffix(".original")
    path.rename(preserved)
    path.symlink_to(preserved)
    with pytest.raises(OSError):
        inspect(execution)


def test_symlink_parent_and_hardlink_refused(execution):
    path = execution["directory"] / "request.json"
    os.link(path, path.with_suffix(".duplicate"))
    with pytest.raises(lifecycle.LifecycleError, match="UNSAFE_FILE"):
        inspect(execution)
    path.with_suffix(".duplicate").unlink()
    original = execution["directory"]
    preserved = original.with_name("preserved")
    original.rename(preserved)
    original.symlink_to(preserved, target_is_directory=True)
    with pytest.raises(OSError):
        inspect(execution)


def test_config_hash_checked_before_intent_root_created(execution):
    execution["config_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(lifecycle.LifecycleError, match="CONFIG_HASH_MISMATCH"):
        restart(execution)
    assert not execution["policy"].intent_root.exists()


def test_missing_or_mismatched_marker_cannot_certify_start(execution):
    execution["marker"]["attempt_id"] = "different-attempt"
    execution["write"]("execution-started.json", execution["marker"])
    with pytest.raises(lifecycle.LifecycleError, match="STARTED_MARKER_MISMATCH"):
        inspect(execution)
    (execution["directory"] / "execution-started.json").unlink()
    with pytest.raises(FileNotFoundError):
        inspect(execution)


def test_loaded_config_hash_must_match_marker(execution):
    execution["marker"]["config_sha256"] = "sha256:" + "0" * 64
    execution["write"]("execution-started.json", execution["marker"])
    with pytest.raises(lifecycle.LifecycleError, match="STARTED_MARKER_MISMATCH"):
        inspect(execution)


def test_bootstrap_pid_one_is_valid_but_supervisor_pid_one_is_not(execution):
    bootstrap = execution["processes"].pop(20003)
    execution["processes"][1] = {**bootstrap, "pid": 1}
    execution["processes"][20002]["ppid"] = 1
    assert inspect(execution)["snapshot"]["bootstrap"]["pid"] == 1
    execution["policy"].pid_file.write_text("1\n")
    with pytest.raises(lifecycle.LifecycleError, match="INVALID_SUPERVISOR_HINT"):
        inspect(execution)


def test_natural_replacement_since_authorized_snapshot_is_not_signalled(execution):
    execution["expected_before"] = json.loads(json.dumps(inspect(execution)["snapshot"]))
    execution["processes"][20002] = {**execution["processes"][20002], "identity": "987654"}
    with pytest.raises(lifecycle.LifecycleError, match="TARGET_NOT_EXPECTED"):
        restart(execution)
    assert not execution["signals"]
    assert not list(execution["policy"].intent_root.glob("*.intent.json"))


@pytest.mark.parametrize("deadline", ["wall", "monotonic"])
def test_expired_deadline_never_signals(execution, monkeypatch, deadline):
    if deadline == "wall":
        monkeypatch.setattr(lifecycle.time, "time", lambda: execution["child"]["deadline"] + 1)
    else:
        monkeypatch.setattr(lifecycle.time, "monotonic", lambda: execution["child"]["monotonic_deadline"] + 1)
    with pytest.raises(lifecycle.LifecycleError, match="EXECUTION_DEADLINE_EXPIRED"):
        restart(execution)
    assert not execution["signals"]


def test_exclusive_intent_fsync_precedes_signal_and_replay_is_observation_only(execution, monkeypatch):
    original_fsync = os.fsync
    synced = []
    monkeypatch.setattr(lifecycle.os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), original_fsync(fd))[1])
    def signal_once(fd):
        assert any(__import__("stat").S_ISDIR(mode) for mode in synced)
        intent = next(execution["policy"].intent_root.glob("*.intent.json"))
        assert json.loads(intent.read_text())["before"]["child"] == execution["child"]
        assert len(synced) >= 3  # root creation, intent file, containing directory.
        execution["signals"].append(fd)
    monkeypatch.setattr(lifecycle, "_kill_pidfd", signal_once)
    first = restart(execution)
    assert first["signal_sent"] is True and not first["replayed"]
    # Replay does not require a still-running original supervisor or child.
    execution["processes"].clear()
    second = restart(execution)
    assert second["replayed"] and second["signal_sent"] is True
    assert second["before"] == first["before"] and len(execution["signals"]) == 1
    with pytest.raises(lifecycle.LifecycleError, match="ACTION_INTENT_CONFLICT"):
        restart(execution, "b" * 64)


def test_uncertain_signal_is_never_retried(execution, monkeypatch):
    def uncertain(fd):
        execution["signals"].append(fd)
        raise OSError("private diagnostic text")
    monkeypatch.setattr(lifecycle, "_kill_pidfd", uncertain)
    with pytest.raises(OSError):
        restart(execution)
    replay = restart(execution)
    assert replay["replayed"] and replay["signal_sent"] is None
    assert len(execution["signals"]) == 1


def test_concurrent_action_cannot_send_another_signal(execution, monkeypatch):
    execution["expected_before"] = inspect(execution)["snapshot"]
    arrived, release = threading.Event(), threading.Event()
    def held_signal(fd):
        execution["signals"].append(fd)
        arrived.set()
        assert release.wait(3)
    monkeypatch.setattr(lifecycle, "_kill_pidfd", held_signal)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(restart, execution)
        try:
            assert arrived.wait(3)
            with pytest.raises(lifecycle.LifecycleError, match="ACTION_BUSY"):
                restart(execution)
        finally:
            release.set()
        assert first.result(timeout=3)["signal_sent"]
    assert restart(execution)["replayed"] and len(execution["signals"]) == 1


def test_replay_cannot_change_original_authorized_supervisor(execution):
    restart(execution)
    execution["expected_before"]["supervisor"]["identity"] = "999999"
    with pytest.raises(lifecycle.LifecycleError, match="ACTION_INTENT_CONFLICT"):
        restart(execution)
    assert len(execution["signals"]) == 1


def test_unbounded_monotonic_deadline_is_not_accepted(execution):
    execution["child"]["monotonic_deadline"] += 1000
    execution["write"]("process.json", execution["child"])
    with pytest.raises(lifecycle.LifecycleError, match="INVALID_EXECUTION_DEADLINE"):
        restart(execution)
    assert not execution["signals"]


def test_recycled_pid_after_pidfd_open_is_not_signalled(execution, monkeypatch):
    original_open = lifecycle._pidfd_open
    def recycled(pid):
        result = original_open(pid)
        execution["processes"][pid] = {**execution["processes"][pid], "identity": "987654"}
        return result
    monkeypatch.setattr(lifecycle, "_pidfd_open", recycled)
    with pytest.raises(lifecycle.LifecycleError, match="TARGET_CHANGED_BEFORE_SIGNAL"):
        restart(execution)
    assert not execution["signals"]
    assert not list(execution["policy"].intent_root.glob("*.intent.json"))


def test_target_revalidated_after_durable_intent(execution, monkeypatch):
    original_write = lifecycle._write_exclusive
    def changed(directory, name, value):
        original_write(directory, name, value)
        if name.endswith(".intent.json"):
            execution["processes"][20002] = {**execution["processes"][20002], "identity": "987654"}
    monkeypatch.setattr(lifecycle, "_write_exclusive", changed)
    with pytest.raises(lifecycle.LifecycleError, match="TARGET_CHANGED_BEFORE_SIGNAL"):
        restart(execution)
    assert not execution["signals"]
    assert restart(execution)["signal_sent"] is None


def test_actual_pidfd_signal_kills_only_owned_fixture_supervisor(execution, monkeypatch):
    from probe_core.sandbox_lifecycle import _pidfd_open, _kill_pidfd
    process = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], start_new_session=True)
    unrelated = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        actual = lifecycle._inspect_process(process.pid)
        assert actual["uid"] == os.geteuid() and actual["identity"].isdigit()
        previous = execution["processes"].pop(20002)
        execution["processes"][process.pid] = {**previous, "pid": process.pid, "identity": actual["identity"]}
        execution["policy"].pid_file.write_text(str(process.pid) + "\n")
        monkeypatch.setattr(lifecycle, "_pidfd_open", _pidfd_open)
        monkeypatch.setattr(lifecycle, "_kill_pidfd", _kill_pidfd)
        result = restart(execution)
        assert result["signal_sent"] and process.wait(timeout=3) == -signal.SIGKILL
        assert unrelated.poll() is None
        assert restart(execution)["replayed"] and unrelated.poll() is None
    finally:
        for owned in (process, unrelated):
            if owned.poll() is None:
                owned.kill()
            owned.wait(timeout=3)


def test_cli_is_root_only_and_never_prints_arbitrary_error_text(monkeypatch, capsys):
    monkeypatch.setattr(lifecycle.os, "geteuid", lambda: 123)
    assert lifecycle.main() == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "ROOT_REQUIRED"
    monkeypatch.setattr(lifecycle.os, "geteuid", lambda: 0)
    monkeypatch.setattr(lifecycle.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"operation":"SECRET_TOKEN_VALUE"}')))
    assert lifecycle.main() == 1
    output = capsys.readouterr().out
    assert "SECRET_TOKEN_VALUE" not in output
    assert json.loads(output)["error_code"] == "INVALID_COMMAND"


def test_json_duplicate_and_oversized_inputs_refused():
    with pytest.raises(lifecycle.LifecycleError, match="DUPLICATE_JSON_KEY"):
        lifecycle._json(b'{"x":1,"x":2}')
    with pytest.raises(lifecycle.LifecycleError, match="NONFINITE_JSON"):
        lifecycle._json(b'{"x":NaN}')


def test_fixed_remote_helper_interpreter_matches_installed_worker_image(tmp_path, monkeypatch):
    import re
    from probe_core import gpu_acceptance_actions as actions
    recipe = (Path(__file__).parents[1] / "deploy/gpu/Dockerfile.worker").read_text()
    image_environment = re.search(r"ENV UV_PROJECT_ENVIRONMENT=(\S+)", recipe).group(1)
    assert f"ENV PATH={image_environment}/bin:$PATH" in recipe
    known_hosts = tmp_path / "known-hosts"
    known_hosts.write_text("fixed-host fixture-key\n")
    def tunnel(host, **kwargs):
        return SimpleNamespace(host=host, **kwargs)
    monkeypatch.setattr(actions, "SSHTunnel", tunnel)
    client = actions.SSHActionClient({"host": "127.0.0.1", "user": "root", "ssh_port": 22,
                                     "identity_file": str(tmp_path / "identity"), "known_hosts_file": str(known_hosts),
                                     "config_path": "/workspace/probe/config/worker.json",
                                     "token_path": "/workspace/probe/config/worker-token",
                                     "config_sha256": "sha256:" + "a" * 64})
    assert client.command[-4:] == [image_environment + "/bin/python", "-I", "-m", "probe_core.gpu_lifecycle"]
    # The helper follows the invoked venv, matching gpu_launch's sys.executable.
    assert lifecycle._Policy().interpreter == sys.executable
