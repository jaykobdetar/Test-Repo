"""Cancellation outcomes and real descriptor-fenced CPU descendant cleanup."""

from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest

from probe_core.audit import canonical_json
from probe_core.schemas import RunManifest
import probe_core.worker as worker
from probe_core.worker_contracts import ExecutionReceipt, WorkerRequestError
from test_schemas import manifest_data
from test_worker import tiny_bundle, make_request
from test_worker_process_restart import open_process_descriptor, kill_process_descriptor


@pytest.fixture
def live_attempt(tiny_bundle, make_request, tmp_path):
    ready = tmp_path / "descendant.json"
    code = """import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'])
path = pathlib.Path(sys.argv[1])
temporary = path.with_suffix('.partial')
temporary.write_text(json.dumps({'pid': child.pid}))
os.replace(temporary, path)
time.sleep(30)
"""
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(ready)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    descriptors = []
    supervisor = None
    try:
        descriptors.append(open_process_descriptor(parent.pid))
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:
            time.sleep(0.01)
        descendant = json.loads(ready.read_text())["pid"]
        descriptors.append(open_process_descriptor(descendant))
        request = make_request(key="cancel-race").model_copy(
            update={"deadline": datetime.now(timezone.utc) + timedelta(seconds=20)}
        )
        config = tiny_bundle[0].model_copy(update={"output_directory": str(tmp_path / "attempts")})
        directory = Path(config.output_directory) / request.attempt_id
        directory.mkdir(parents=True, mode=0o700)
        metadata = {
            "pid": parent.pid,
            "identity": worker._process_identity(parent.pid),
            "boot_id": worker._boot_id(),
            "deadline": request.deadline.timestamp(),
            "monotonic_deadline": time.monotonic() + 20,
            "cgroup": None,
        }
        worker._json_write(directory / "process.json", metadata)
        worker._json_write(directory / "request.json", request.model_dump(mode="json"))
        receipt = ExecutionReceipt(
            job_id=request.job_id, attempt_id=request.attempt_id, state="RUNNING", started_at=datetime.now(timezone.utc)
        )
        worker._json_write(directory / "receipt.json", receipt.model_dump(mode="json"))
        # The process is an adopted, real process-group leader, so cleanup must
        # not rely on a multiprocessing parent handle or a reusable numeric PID.
        supervisor = worker.Supervisor(config, monitor_interval=60)
        yield supervisor, request, directory, metadata, descriptors, receipt
    finally:
        try:
            for descriptor in descriptors:
                try:
                    kill_process_descriptor(descriptor)
                except ProcessLookupError:
                    pass
            for descriptor in descriptors:
                assert select.select([descriptor], [], [], 5)[0], "cancellation fixture left a process alive"
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
            parent.wait(timeout=5)
            if supervisor is not None:
                supervisor.close(terminate=False)


def stop_fixture(descriptors):
    for descriptor in descriptors:
        try:
            kill_process_descriptor(descriptor)
        except ProcessLookupError:
            pass
    for descriptor in descriptors:
        assert select.select([descriptor], [], [], 5)[0]


def success_receipt(receipt, manifest_data):
    return receipt.model_copy(
        update={
            "state": worker.WorkerState.SUCCEEDED,
            "finished_at": datetime.now(timezone.utc),
            "manifest": RunManifest.model_validate(manifest_data),
        }
    )


def test_real_cpu_cancellation_kills_original_group_and_records_proof(live_attempt):
    supervisor, request, directory, metadata, descriptors, _ = live_attempt
    result = supervisor.cancel(request.attempt_id)
    assert (
        result.state == worker.WorkerState.CANCELLED and result.failure_kind == "cancelled" and result.process_stopped
    )
    assert all(select.select([descriptor], [], [], 0)[0] for descriptor in descriptors)
    proof_bytes = (directory / "cancellation.json").read_bytes()
    proof = json.loads(proof_bytes)
    assert proof == {
        "schema_version": 1,
        "job_id": request.job_id,
        "attempt_id": request.attempt_id,
        "worker_id": request.worker_id,
        "approval_id": request.approval_id,
        "request_sha256": "sha256:"
        + hashlib.sha256(canonical_json(request.model_dump(mode="json")).encode()).hexdigest(),
        "config_sha256": "sha256:"
        + hashlib.sha256(canonical_json(supervisor.config.model_dump(mode="json")).encode()).hexdigest(),
        **metadata,
        "signal": "SIGKILL",
        "signal_scope": "process_group",
        "signal_sent_at": proof["signal_sent_at"],
        "stopped_at": result.finished_at.isoformat(),
        "process_stopped": True,
        "job_scope_stopped": True,
        "result_present": False,
    }
    assert datetime.fromisoformat(proof["signal_sent_at"]) <= result.finished_at < request.deadline
    assert supervisor.cancel(request.attempt_id) == result
    assert (directory / "cancellation.json").read_bytes() == proof_bytes


def test_completed_between_get_and_cancel_is_preserved(live_attempt, manifest_data, monkeypatch):
    supervisor, request, directory, _, descriptors, receipt = live_attempt
    assert supervisor.status(request.attempt_id).state == worker.WorkerState.RUNNING
    success = success_receipt(receipt, manifest_data)
    worker._json_write(directory / "result.json", success.model_dump(mode="json"))
    stop_fixture(descriptors)
    monkeypatch.setattr(
        worker, "_signal_pidfd", lambda *_args, **_kwargs: pytest.fail("completed result must not probe or signal")
    )
    result = supervisor.cancel(request.attempt_id)
    assert result.state == worker.WorkerState.SUCCEEDED and result.manifest == success.manifest
    assert result.process_stopped and result.failure_kind is None
    assert not (directory / "cancellation.json").exists()


def test_result_published_during_signal_race_wins(live_attempt, manifest_data, monkeypatch):
    supervisor, request, directory, _, _, receipt = live_attempt
    original = worker._signal_pidfd

    def publish_then_signal(descriptor, signum, **kwargs):
        if signum == signal.SIGKILL:
            worker._json_write(
                directory / "result.json", success_receipt(receipt, manifest_data).model_dump(mode="json")
            )
        return original(descriptor, signum, **kwargs)

    monkeypatch.setattr(worker, "_signal_pidfd", publish_then_signal)
    result = supervisor.cancel(request.attempt_id)
    assert result.state == worker.WorkerState.SUCCEEDED and result.process_stopped
    assert not (directory / "cancellation.json").exists()


def test_unsupported_cpu_group_signal_refuses_without_main_only_fallback(live_attempt, monkeypatch):
    supervisor, request, directory, metadata, descriptors, _ = live_attempt
    calls = []

    def unavailable(_descriptor, signum, _info, flags):
        calls.append((signum, flags))
        raise OSError(errno.EINVAL, "synthetic older kernel")

    with monkeypatch.context() as patch:
        patch.setattr(worker.signal, "pidfd_send_signal", unavailable, raising=False)
        with pytest.raises(WorkerRequestError, match="Linux 6.9"):
            supervisor.cancel(request.attempt_id)
    assert calls == [(0, 4)]
    assert worker._same_process(metadata) and all(not select.select([fd], [], [], 0)[0] for fd in descriptors)
    assert not (directory / "cancellation.json").exists()


def test_expired_deadline_is_not_relabelled_cancelled(live_attempt, monkeypatch):
    supervisor, request, directory, metadata, _, _ = live_attempt
    worker._json_write(
        directory / "process.json", dict(metadata, deadline=time.time() - 1, monotonic_deadline=time.monotonic() - 1)
    )
    monkeypatch.setattr(
        worker,
        "_signal_pidfd",
        lambda *_args, **_kwargs: pytest.fail("expired attempt must not probe CPU cancellation support"),
    )
    result = supervisor.cancel(request.attempt_id)
    assert result.state == worker.WorkerState.FAILED and result.failure_kind == "timeout" and result.process_stopped
    assert not (directory / "cancellation.json").exists()


@pytest.mark.parametrize("acknowledgment", ["refresh", "terminate"])
def test_dead_main_cannot_acknowledge_populated_gpu_cgroup(live_attempt, tmp_path, acknowledgment):
    supervisor, request, directory, metadata, descriptors, _ = live_attempt
    stop_fixture(descriptors)
    # Deterministic cgroup-state fault injection, not a claim that these ordinary
    # files provide kernel containment. The real CPU group test above is separate.
    root = tmp_path / "cgroup-state-fixture"
    group = root / ("probe-" + request.attempt_id)
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    (group / "cgroup.procs").write_text("")
    (group / "cgroup.kill").write_text("")
    supervisor.config = supervisor.config.model_copy(update={"cgroup_directory": str(root)})
    worker._json_write(directory / "process.json", dict(metadata, cgroup=str(group)))
    with pytest.raises(WorkerRequestError, match="descendants remain"):
        if acknowledgment == "refresh":
            supervisor.status(request.attempt_id)
        else:
            supervisor._terminate(request.attempt_id, "timeout", "ExecutionDeadlineExceeded")
    assert not ExecutionReceipt.model_validate_json((directory / "receipt.json").read_text()).process_stopped
    assert not (directory / "cancellation.json").exists()


def test_monitor_cleans_real_cpu_descendant_after_leader_is_reaped(live_attempt):
    supervisor, request, directory, metadata, descriptors, _ = live_attempt
    supervisor.close(terminate=False)
    replacement = worker.Supervisor(supervisor.config, monitor_interval=0.05)
    try:
        assert request.attempt_id in replacement._group_descriptors
        kill_process_descriptor(descriptors[0])
        assert select.select([descriptors[0]], [], [], 5)[0]
        os.waitpid(metadata["pid"], 0)
        assert worker._process_identity(metadata["pid"]) is None
        # No status calls trigger cleanup: the replacement's background monitor
        # must stop the surviving group through its retained original pidfd.
        assert select.select([descriptors[1]], [], [], 5)[0], "orphan descendant survived background cleanup"
        result = replacement.status(request.attempt_id)
        assert result.process_stopped and result.failure_kind == "infrastructure"
        assert result.error_code == "ProcessExitedWithoutResult"
        assert request.attempt_id not in replacement._group_descriptors
        assert not (directory / "cancellation.json").exists()
    finally:
        replacement.close(terminate=False)


def test_new_cpu_supervisor_cannot_signal_orphan_using_old_numeric_group(live_attempt, monkeypatch):
    supervisor, request, directory, metadata, descriptors, _ = live_attempt
    supervisor.close(terminate=False)
    kill_process_descriptor(descriptors[0])
    assert select.select([descriptors[0]], [], [], 5)[0]
    os.waitpid(metadata["pid"], 0)
    replacement = worker.Supervisor(supervisor.config, monitor_interval=60)
    try:
        assert not replacement._group_descriptors
        monkeypatch.setattr(worker.os, "killpg", lambda *_: pytest.fail("reusable numeric group must not be signalled"))
        monkeypatch.setattr(
            worker, "_signal_pidfd", lambda *_args, **_kwargs: pytest.fail("no authenticated group descriptor exists")
        )
        with pytest.raises(WorkerRequestError, match="without a retained process-group identity"):
            replacement.cancel(request.attempt_id)
        assert not select.select([descriptors[1]], [], [], 0)[0]
        assert not ExecutionReceipt.model_validate_json((directory / "receipt.json").read_text()).process_stopped
        assert not (directory / "cancellation.json").exists()
    finally:
        replacement.close(terminate=False)


@pytest.mark.parametrize("acknowledgment", ["refresh", "terminate"])
def test_gpu_scope_cleanup_stops_real_orphan_without_leader(live_attempt, tmp_path, monkeypatch, acknowledgment):
    supervisor, request, directory, metadata, descriptors, _ = live_attempt
    kill_process_descriptor(descriptors[0])
    assert select.select([descriptors[0]], [], [], 5)[0]
    os.waitpid(metadata["pid"], 0)
    assert not select.select([descriptors[1]], [], [], 0)[0]
    # Real processes, with the cgroup filesystem deliberately substituted: this
    # tests the cleanup decision independently of leader liveness, not kernel
    # GPU/cgroup containment (which requires the separate delegated-host gate).
    root = tmp_path / "orphan-cgroup-fixture"
    group = root / ("probe-" + request.attempt_id)
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    (group / "cgroup.procs").write_text("")
    (group / "cgroup.kill").write_text("")
    supervisor.config = supervisor.config.model_copy(update={"cgroup_directory": str(root)})
    worker._json_write(directory / "process.json", dict(metadata, cgroup=str(group), deadline=time.time() - 1))
    original_write = os.write
    kills = []

    def kill_scope(fd, payload):
        if os.readlink("/proc/self/fd/" + str(fd)) == str(group / "cgroup.kill"):
            kills.append(payload)
            kill_process_descriptor(descriptors[1])
            assert select.select([descriptors[1]], [], [], 5)[0]
            (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
        return original_write(fd, payload)

    monkeypatch.setattr(worker.os, "write", kill_scope)
    monkeypatch.setattr(worker.os, "killpg", lambda *_: pytest.fail("dead leader numeric group must not be signalled"))
    result = (
        supervisor.status(request.attempt_id)
        if acknowledgment == "refresh"
        else supervisor._terminate(request.attempt_id, "timeout", "ExecutionDeadlineExceeded")
    )
    assert kills == [b"1"]
    assert result.process_stopped and result.failure_kind == "timeout"
    assert not (directory / "cancellation.json").exists()


def test_cancel_proof_write_failure_stays_explicitly_missing(live_attempt, monkeypatch):
    supervisor, request, directory, _, _, _ = live_attempt
    original = worker._json_write

    def fail_proof(path, value):
        if path.name == "cancellation.json":
            raise OSError("synthetic storage failure")
        return original(path, value)

    monkeypatch.setattr(worker, "_json_write", fail_proof)
    with pytest.raises(OSError, match="synthetic storage"):
        supervisor.cancel(request.attempt_id)
    assert supervisor.status(request.attempt_id).state == worker.WorkerState.CANCELLED
    assert not (directory / "cancellation.json").exists()


def test_cgroup_cleanup_error_does_not_turn_dead_main_into_stop_ack(live_attempt, tmp_path, monkeypatch):
    supervisor, request, directory, metadata, descriptors, _ = live_attempt
    root = tmp_path / "cgroup-failure-fixture"
    group = root / ("probe-" + request.attempt_id)
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    (group / "cgroup.procs").write_text(str(metadata["pid"]) + "\n")
    (group / "cgroup.kill").write_text("")
    supervisor.config = supervisor.config.model_copy(update={"cgroup_directory": str(root)})
    worker._json_write(directory / "process.json", dict(metadata, cgroup=str(group)))
    original_write = os.write

    def fail_group_kill(fd, payload):
        if os.readlink("/proc/self/fd/" + str(fd)) == str(group / "cgroup.kill"):
            raise OSError(errno.EIO, "synthetic cgroup cleanup failure")
        return original_write(fd, payload)

    with monkeypatch.context() as patch:
        patch.setattr(worker.os, "write", fail_group_kill)
        with pytest.raises(OSError, match="synthetic cgroup"):
            supervisor.cancel(request.attempt_id)
    assert select.select([descriptors[0]], [], [], 5)[0], "main child was descriptor-signalled"
    assert not select.select([descriptors[1]], [], [], 0)[0], "descendant models incomplete scope cleanup"
    with pytest.raises(WorkerRequestError, match="descendants remain"):
        supervisor.status(request.attempt_id)
    assert not ExecutionReceipt.model_validate_json((directory / "receipt.json").read_text()).process_stopped
    assert not (directory / "cancellation.json").exists()
