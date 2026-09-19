"""CPU sandbox policy tests and a mandatory-on-demand REAL containment gate.

Set PROBE_SANDBOX_REQUIRED=1, PROBE_SANDBOX_IMAGE=sha256:<built image ID>,
and optionally PROBE_SANDBOX_PODMAN=/path/to/podman. Real tests never substitute
a subprocess, Docker, or disabled resource limits for rootless Podman.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import pytest

from probe_core.sandbox import (
    GPURequestBroker, PodmanSandbox, SandboxLimits, SandboxProtocolError,
    SandboxUnavailable, _ArtifactReceiver,
)
from probe_core.schemas import JobSpec


@pytest.fixture
def job():
    data = json.loads((Path(__file__).parent / "fixtures/manifest.json").read_text())
    return JobSpec(idempotency_key="sandbox-approved-1", model=data["model"], inputs=data["inputs"], operation={"kind": "capture", "modules": [{"layer": 0, "component": "residual"}], "positions": ["last"]}, limits={"max_runtime_seconds": 30, "max_output_bytes": 100000})


def test_broker_only_resolves_controller_approved_jobs_and_deduplicates(job):
    calls = []
    broker = GPURequestBroker({"approved": job}, lambda spec: calls.append(spec) or "job-1")
    request = b'{"kind":"gpu_request","request_id":"r1","job_key":"approved"}'
    assert broker.handle(request) == {"request_id": "r1", "status": "submitted", "job_id": "job-1"}
    assert broker.handle(request)["job_id"] == "job-1"
    assert calls == [job]
    assert broker.handle(request.replace(b'"approved"', b'"another"'))["reason"] == "request_id_reused"


@pytest.mark.parametrize("request_data", [b'{}', b'not json', b'{"kind":"start_gpu","request_id":"r1","job_key":"approved"}', b'{"kind":"gpu_request","request_id":"r1","job_key":"approved","code":"arbitrary"}', b'{"kind":"gpu_request","request_id":"r1","job_key":"unapproved"}'])
def test_broker_rejects_extra_capabilities_and_unapproved_requests(job, request_data):
    calls = []
    broker = GPURequestBroker({"approved": job}, lambda spec: calls.append(spec) or "job-1")
    assert broker.handle(request_data)["status"] == "denied"
    assert calls == []


def test_broker_does_not_return_controller_exception_secrets(job):
    def fail(spec):
        raise RuntimeError("RUNPOD_API_KEY=example-credential")
    response = GPURequestBroker({"approved": job}, fail).handle(b'{"kind":"gpu_request","request_id":"r1","job_key":"approved"}')
    assert "credential" not in json.dumps(response)
    assert response["reason"] == "controller_denied"


@pytest.mark.parametrize("changes", [{"wall_seconds": 0}, {"wall_seconds": True}, {"pids": 100000}, {"cpu_cores": float("nan")}, {"cpu_cores": 0}, {"memory_bytes": 100}, {"max_output_bytes": 100000000}, {"max_broker_requests": 10000}])
def test_limits_reject_unbounded_requests(changes):
    with pytest.raises(ValueError):
        SandboxLimits(**changes)


def test_output_stream_is_bounded_and_hashed(tmp_path):
    receiver = _ArtifactReceiver(tmp_path, 100)
    receiver.receive({"kind": "begin", "path": "nested/result.txt"})
    receiver.receive({"kind": "chunk", "path": "nested/result.txt", "data": base64.b64encode(b"result").decode()})
    receiver.receive({"kind": "end", "path": "nested/result.txt", "sha256": hashlib.sha256(b"result").hexdigest()})
    assert not receiver.close()
    assert receiver.finished[0].read_bytes() == b"result"


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "a/../escape", "a//b", "a\\b", "a/%2e%2e/b", "a\x00", "."])
def test_output_receiver_rejects_traversal(tmp_path, path):
    with pytest.raises(SandboxProtocolError):
        _ArtifactReceiver(tmp_path, 100).receive({"kind": "begin", "path": path})


def test_output_receiver_rejects_excess_bytes_and_duplicate_files(tmp_path):
    receiver = _ArtifactReceiver(tmp_path, 2)
    receiver.receive({"kind": "begin", "path": "result"})
    with pytest.raises(SandboxProtocolError):
        receiver.receive({"kind": "begin", "path": "result"})
    with pytest.raises(SandboxProtocolError):
        receiver.receive({"kind": "chunk", "path": "result", "data": base64.b64encode(b"too large").decode()})
    assert receiver.close()


def test_pin_and_hardening_command_are_mandatory(tmp_path):
    with pytest.raises(ValueError):
        PodmanSandbox(image="python:latest", workspace=tmp_path)
    sandbox = PodmanSandbox(image="sha256:" + "a" * 64, workspace=tmp_path)
    command = sandbox._run_command("probe-test", tmp_path / "input", SandboxLimits(wall_seconds=17))
    for flag in ("--network=none", "--read-only", "--read-only-tmpfs=false", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never", "--unsetenv-all", "--http-proxy=false", "--user=1000:1000", "--timeout=17", "--rm"):
        assert flag in command
    assert not any(arg.startswith("--device") or "docker.sock" in arg or "podman.sock" in arg for arg in command)
    assert all("ro=true" in arg for arg in command if arg.startswith("--mount"))
    assert any(arg.startswith("--tmpfs=/output:") and "size=" in arg for arg in command)


def test_packaged_and_deployment_seccomp_profiles_are_identical(tmp_path):
    sandbox = PodmanSandbox(image="sha256:" + "a" * 64, workspace=tmp_path)
    deployment = Path(__file__).resolve().parent.parent / "deploy/sandbox/seccomp.json"
    assert sandbox.seccomp_profile.read_bytes() == deployment.read_bytes()
    profile = json.loads(sandbox.seccomp_profile.read_bytes())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {name for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" for name in rule["names"]}
    assert not allowed & {"socket", "connect", "mount", "unshare", "setns", "ptrace", "bpf", "process_vm_readv", "process_vm_writev"}


def test_runtime_attestation_fails_if_any_required_enforcement_is_missing():
    limits = SandboxLimits()
    valid = {"uid": 1000, "cap_eff": "0000000000000000", "seccomp": "2", "no_new_privs": "1", "socket_denied": True, "input_readonly": True, "root_readonly": True, "memory_max": str(limits.memory_bytes), "pids_max": "32", "cpu_max": "100000 100000", "interfaces": ["lo"]}
    PodmanSandbox._validate_attestation(valid, limits)
    for field, value in {"uid": 0, "cap_eff": "0000000000000001", "seccomp": "0", "no_new_privs": "0", "socket_denied": False, "input_readonly": False, "root_readonly": False, "memory_max": "max", "pids_max": "max", "cpu_max": "max 100000", "interfaces": ["lo", "eth0"]}.items():
        with pytest.raises(SandboxUnavailable):
            PodmanSandbox._validate_attestation({**valid, field: value}, limits)


@pytest.fixture
def real_sandbox(tmp_path):
    required = os.environ.get("PROBE_SANDBOX_REQUIRED") == "1"
    image = os.environ.get("PROBE_SANDBOX_IMAGE")
    if not image:
        if required:
            pytest.fail("REAL containment gate requires PROBE_SANDBOX_IMAGE pinned to the built local image ID")
        pytest.skip("REAL rootless Podman gate not configured; set PROBE_SANDBOX_REQUIRED=1 and PROBE_SANDBOX_IMAGE")
    sandbox = PodmanSandbox(image=image, workspace=tmp_path, podman=os.environ.get("PROBE_SANDBOX_PODMAN", "podman"))
    try:
        sandbox.check_runtime()
    except SandboxUnavailable as error:
        if required:
            pytest.fail(str(error))
        pytest.skip(str(error))
    return sandbox


def test_real_container_blocks_network_credentials_sockets_and_host_writes(real_sandbox, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "host-secret-must-not-cross")
    code = '''import json, os, pathlib, socket
assert "RUNPOD_API_KEY" not in os.environ
for path in ["/run/podman/podman.sock", "/var/run/docker.sock", "/root/.ssh", "/dev/nvidia0"]:
    try: exists = pathlib.Path(path).exists()
    except PermissionError: exists = False
    assert not exists, path
for path in ["/input/forbidden", "/etc/forbidden"]:
    try: pathlib.Path(path).write_text("escape")
    except OSError: pass
    else: raise AssertionError(path)
try: socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except PermissionError: pass
else: raise AssertionError("socket creation allowed")
assert pathlib.Path("/input/sample.txt").read_text() == "approved input"
pathlib.Path("result.txt").write_text("contained")
print("containment passed")
'''
    result = real_sandbox.run(code, inputs={"sample.txt": b"approved input"})
    assert result.returncode == 0, result
    assert result.termination_reason is None
    assert result.artifacts[0].read_text() == "contained"


def test_real_container_enforces_wall_time_and_termination(real_sandbox):
    started = time.monotonic()
    result = real_sandbox.run("while True: pass", limits=SandboxLimits(wall_seconds=2))
    assert result.termination_reason == "wall_time_limit"
    assert time.monotonic() - started < 20
    assert result.artifacts == ()


def test_real_container_has_hard_output_filesystem_bound(real_sandbox):
    code = '''from pathlib import Path
with open("large.bin", "wb") as stream:
    try:
        for _ in range(100): stream.write(b"x" * 65536); stream.flush()
    except OSError: pass
assert Path("large.bin").stat().st_size <= 1048576
'''
    result = real_sandbox.run(code, limits=SandboxLimits(max_output_bytes=1048576))
    assert result.returncode == 0, result
    assert sum(path.stat().st_size for path in result.artifacts) <= 1048576


def test_real_container_enforces_memory_cgroup(real_sandbox):
    result = real_sandbox.run("x = bytearray(1024 * 1024 * 1024); print('unexpected allocation')", limits=SandboxLimits(memory_bytes=128 * 1024**2))
    assert result.returncode != 0
    assert result.termination_reason is None, result
    assert "unexpected allocation" not in result.stdout


def test_real_container_enforces_pid_cgroup(real_sandbox):
    code = '''import subprocess, sys
children = []
try:
    for i in range(100): children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"]))
except OSError:
    print("pid limit enforced", flush=True)
else:
    raise AssertionError("PID limit was ignored")
finally:
    for child in children: child.kill()
    for child in children: child.wait()
'''
    result = real_sandbox.run(code, limits=SandboxLimits(pids=16))
    assert result.returncode == 0, result
    assert "pid limit enforced" in result.stdout


def test_real_container_gpu_broker_uses_only_approved_job(real_sandbox, job):
    calls = []
    broker = GPURequestBroker({"approved": job}, lambda spec: calls.append(spec) or "queued-job-1")
    code = '''import json, sys
print('PROBE_BROKER:{"kind":"gpu_request","request_id":"r1","job_key":"approved"}', flush=True)
response = json.loads(sys.stdin.readline())
assert response["job_id"] == "queued-job-1"
print('PROBE_BROKER:{"kind":"gpu_request","request_id":"r2","job_key":"unapproved"}', flush=True)
assert json.loads(sys.stdin.readline())["status"] == "denied"
'''
    result = real_sandbox.run(code, broker=broker)
    assert result.returncode == 0, result
    assert calls == [job]


def test_real_container_cancellation_stops_execution(real_sandbox):
    event = threading.Event()
    timer = threading.Timer(1, event.set)
    timer.start()
    try:
        result = real_sandbox.run("import time; time.sleep(30)", cancel_event=event)
    finally:
        timer.cancel()
    assert result.termination_reason == "cancelled"


def test_real_scientific_libraries_support_offline_tensor_and_statistical_analysis(real_sandbox):
    code = '''import json
from pathlib import Path
import numpy as np
import scipy.stats
import torch
import transformers
import nnsight
from sklearn.linear_model import Ridge
from safetensors.torch import save_file, load_file
assert torch.version.cuda is None
assert not torch.cuda.is_available()
x = np.arange(20, dtype=float).reshape(-1, 1)
y = 2.0 * x[:, 0] + 1.0
probe = Ridge(alpha=0.001).fit(x, y)
assert abs(probe.coef_[0] - 2.0) < 0.001
correlation = scipy.stats.pearsonr(x[:, 0], y).statistic
assert correlation > 0.999
tensor = torch.tensor(x, dtype=torch.float32)
save_file({"activation": tensor}, "analysis.safetensors")
assert torch.equal(load_file("analysis.safetensors")["activation"], tensor)
Path("analysis.json").write_text(json.dumps({"slope": float(probe.coef_[0]), "correlation": float(correlation), "torch": torch.__version__, "transformers": transformers.__version__}))
print("scientific analysis passed")
'''
    result = real_sandbox.run(code, limits=SandboxLimits(wall_seconds=60))
    assert result.returncode == 0, result
    assert result.termination_reason is None
    assert "scientific analysis passed" in result.stdout
    assert {path.name for path in result.artifacts} == {"analysis.safetensors", "analysis.json"}
