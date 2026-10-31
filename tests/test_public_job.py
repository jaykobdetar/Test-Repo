"""Fixed public request checks and real cheap-process monitor recovery; no ML."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from probe_core.audit import canonical_json
from probe_core.gpu_acceptance import fixed_direction_plan, fixed_plan
from probe_core.model_assets import canonical_locks
from probe_core.worker import prompt_set_hash
from probe_core.worker_contracts import ExecutionRequest, WorkerState

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


job = load("public_job_test", ROOT / "deploy/gpu/public-job.py")
public = load("public_calibration_job_test", ROOT / "deploy/gpu/public-calibration.py")


@pytest.fixture
def inputs(monkeypatch):
    lock = canonical_locks()[0]
    baked = json.loads((ROOT / "deploy/gpu/public-assets.json").read_bytes())
    prefix = "models/" + lock.repo.split("/")[-1] + "/" + lock.revision + "/"
    config = public.PublicCalibrationConfig.model_validate(
        {
            "model": {
                "repo": lock.repo,
                "revision_sha": lock.revision,
                "tokenizer_revision": lock.revision,
                "local_weight_hashes": [item.sha256 for item in lock.files if item.path.endswith(".safetensors")],
                "dtype": "bfloat16",
                "quantized": False,
                "thinking_mode": None,
                "chat_template_hash": None,
            },
            "assets": [
                {"path": item["path"][len(prefix) :], "sha256": item["sha256"]}
                for item in baked["assets"]
                if item["path"].startswith(prefix)
            ],
            "datasets": [
                {"path": "/opt/probe-assets/datasets/public-calibration-prompts.json", "sha256": public.DATASET_SHA}
            ],
            "code_git_commit": "f" * 40,
            "container_image_digest": "sha256:" + "a" * 64,
            "region": "EU-RO-1",
            "live_price_usd_per_hour": 0.74,
        }
    )
    dataset = public.PromptDataset.model_validate_json(base64.b64decode(baked["assets"][-1]["inline_base64"]))
    monkeypatch.setattr(job, "calibration_module", lambda: public)
    monkeypatch.setattr(public, "validate_assets", lambda _: dataset)
    plan = fixed_plan(
        config.model, "fixed", public.DATASET_SHA, prompt_set_hash(dataset, public.PROMPTS), public.PROMPTS
    )
    now = datetime.now(timezone.utc)
    request = ExecutionRequest(
        job_id="job-actual",
        attempt_id="attempt-actual",
        approval_id="approval-actual",
        worker_id="worker-actual",
        deadline=now + timedelta(seconds=30),
        spec=plan.cases[1].spec,
    )
    body = {
        "config": config.model_dump(mode="json"),
        "request": request.model_dump(mode="json"),
        "absolute_deadline": (now + timedelta(seconds=60)).isoformat(),
    }
    return SimpleNamespace(config=config, dataset=dataset, plan=plan, request=request, body=body)


def test_every_existing_fixed_case_and_direction_is_accepted(inputs):
    cases = list(inputs.plan.cases) + list(
        fixed_direction_plan(
            inputs.config.model,
            "direction",
            public.DATASET_SHA,
            prompt_set_hash(inputs.dataset, public.PROMPTS),
            public.PROMPTS,
        ).cases
    )
    for case in cases:
        body = deepcopy(inputs.body)
        body["request"]["spec"] = case.spec.model_dump(mode="json")
        assert job.validate_start(body)[1].spec == case.spec


@pytest.mark.parametrize("mutation", ["prompt", "seed", "operation", "model", "limit", "science", "extra"])
def test_only_identity_and_deadline_fields_can_change(inputs, mutation):
    body = deepcopy(inputs.body)
    spec = body["request"]["spec"]
    if mutation == "prompt":
        spec["inputs"]["prompt_ids"] = ["private-heldout"]
    if mutation == "seed":
        spec["inputs"]["random_seed"] = 124
    if mutation == "operation":
        spec["operation"]["modules"][0]["layer"] = 15
    if mutation == "model":
        spec["model"]["revision_sha"] = "b" * 40
    if mutation == "limit":
        spec["limits"]["max_runtime_seconds"] = 121
    if mutation == "science":
        body["request"]["science"]["session_id"] = "claims-science"
    if mutation == "extra":
        body["command"] = "arbitrary.py"
    with pytest.raises(ValueError):
        job.validate_start(body)


@pytest.mark.parametrize("seconds", [0, -1, 901])
def test_invalid_absolute_deadline_refused(inputs, seconds):
    now = datetime.now(timezone.utc)
    inputs.body["absolute_deadline"] = (now + timedelta(seconds=seconds)).isoformat()
    with pytest.raises(ValueError, match="ORIGINAL_DEADLINE_INVALID"):
        job.validate_start(inputs.body, now)


# The synthetic child uses only the standard library. It passes the same release
# handshake as production and writes a typed receipt or a real retained output.
CHILD = r"""
import json,os,pathlib,subprocess,sys,time
fd,folder,mode=int(sys.argv[1]),pathlib.Path(sys.argv[2]),sys.argv[3]
assert os.read(fd,1)==b'1'
os.close(fd)
def put(name,value):
    tmp=folder/(name+'.partial')
    tmp.write_text(json.dumps(value))
    os.replace(tmp,folder/name)
put('execution-started.json',{'pid':os.getpid()})
if mode in ('descendant','orphan'):
    code="import os,pathlib,time; os.setsid(); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(folder/'grandchild.pid')
    subprocess.Popen([sys.executable,'-I','-c',code])
    end=time.monotonic()+3
    while not (folder/'grandchild.pid').exists():
        assert time.monotonic()<end
        time.sleep(.01)
    if mode=='orphan': os._exit(0)
if mode=='output':
    (folder/'job-output').mkdir()
    (folder/'job-output'/'summary.json').write_bytes(b'xx')
if mode=='complete':
    while not (folder/'finish').exists(): time.sleep(.005)
    value=json.loads((folder/'prepared-receipt.json').read_text())
    from datetime import datetime,timezone
    value['finished_at']=datetime.now(timezone.utc).isoformat()
    put('child-receipt.json',value)
time.sleep(30)
"""


@pytest.fixture
def monitor_environment(tmp_path, inputs, monkeypatch):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    monkeypatch.setattr(job, "UID", os.geteuid())
    harness = tmp_path / "monitor.py"
    harness.write_text(
        "import importlib.util,sys\n"
        f"sys.path.insert(0,{str(ROOT)!r})\n"
        f's=importlib.util.spec_from_file_location("tested_public_job",{str(ROOT / "deploy/gpu/public-job.py")!r})\n'
        "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m)\n"
        'm._environment=lambda *args: {"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"}\n'
        "from pathlib import Path\n"
        f"code={CHILD!r}\n"
        'def command(fd): return [sys.executable,"-I","-c",code,str(fd),sys.argv[2],sys.argv[3]]\n'
        "m.run_monitor(Path(sys.argv[1]),Path(sys.argv[2]),command=command,drop=False)\n"
    )
    processes = []
    mode = ["sleep"]

    def launch(_):
        with (tmp_path / "monitor.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-I", str(harness), str(state), str(workspace), mode[0]],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        processes.append(process)
        return process

    monkeypatch.setattr(job, "launch_monitor", launch)
    env = SimpleNamespace(
        state=state, workspace=workspace, mode=mode, processes=processes, log=tmp_path / "monitor.log"
    )
    yield env
    # Cleanup is descriptor-fenced; no signalling a remembered numeric PID.
    if (state / "claim.json").exists():
        job.cancel({"attempt_id": inputs.request.attempt_id}, state)
    for process in processes:
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            fd = job.pidfd_open(process.pid)
            try:
                job.pidfd_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
            process.wait(timeout=3)


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("bounded real-process observation did not arrive")


def started(env, inputs):
    assert job.start(inputs.body, env.state, env.workspace).state == WorkerState.ACCEPTED
    wait_for(lambda: (env.workspace / "execution-started.json").exists())
    return job.inspect_job({"attempt_id": inputs.request.attempt_id}, env.state, env.workspace)


def test_detached_same_attempt_survives_start_transport_exit(inputs, monitor_environment, monkeypatch):
    env = monitor_environment
    read, write = os.pipe()
    transport = os.fork()
    if transport == 0:
        os.close(read)
        try:
            receipt = job.start(inputs.body, env.state, env.workspace)
            os.write(write, receipt.model_dump_json().encode())
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(write)
    raw = os.read(read, 65536)
    os.close(read)
    assert os.waitpid(transport, 0)[1] == 0 and json.loads(raw)["state"] == "ACCEPTED"
    wait_for(lambda: (env.workspace / "execution-started.json").exists())
    before = job.inspect_job({"attempt_id": inputs.request.attempt_id}, env.state, env.workspace)
    monkeypatch.setattr(job, "launch_monitor", lambda _: pytest.fail("same request must never relaunch"))
    monkeypatch.setattr(job, "validate_start", lambda _: pytest.fail("recovery must not regenerate deadlines"))
    assert job.start(inputs.body, env.state, env.workspace).state == WorkerState.RUNNING
    after = job.inspect_job({"attempt_id": inputs.request.attempt_id}, env.state, env.workspace)
    for field in (
        "request_sha256",
        "config_sha256",
        "child_identity",
        "monitor_identity",
        "original_deadline",
        "effective_job_deadline",
        "execution_started",
    ):
        assert after[field] == before[field]
    assert job.same_process(before["child_identity"]) and job.same_process(before["monitor_identity"])
    changed = deepcopy(inputs.body)
    changed["request"]["attempt_id"] = "second-attempt"
    with pytest.raises(ValueError, match="POD_ALREADY_CLAIMED"):
        job.start(changed, env.state, env.workspace)


def test_cancellation_stops_actual_child_and_setsid_descendant(inputs, monitor_environment):
    env = monitor_environment
    env.mode[0] = "descendant"
    before = started(env, inputs)
    grandchild = int(
        wait_for(
            lambda: (
                (env.workspace / "grandchild.pid").read_text() if (env.workspace / "grandchild.pid").exists() else None
            )
        )
    )
    descendant = job.process_identity(grandchild)
    assert descendant and job.same_process(before["child_identity"])
    receipt = job.cancel({"attempt_id": inputs.request.attempt_id}, env.state)
    assert receipt.state == WorkerState.CANCELLED and receipt.process_stopped
    assert not job.same_process(before["child_identity"]) and not job.same_process(descendant)
    proof = job.inspect_job({"attempt_id": inputs.request.attempt_id}, env.state, env.workspace)["cancellation"]
    assert proof["child_identity"] == before["child_identity"]
    assert before["child_identity"] in proof["signalled"] and descendant in proof["signalled"]
    assert proof["descendants_stopped"] and not proof["result_present"]


def test_original_deadline_enforced_without_status_or_reconnect_calls(inputs, monitor_environment):
    env = monitor_environment
    inputs.body["request"]["deadline"] = (datetime.now(timezone.utc) + timedelta(seconds=1.2)).isoformat()
    before = started(env, inputs)
    env.processes[0].wait(timeout=5)  # No status calls drive termination.
    receipt = job.status({"attempt_id": inputs.request.attempt_id}, env.state)
    assert receipt.state == WorkerState.FAILED and receipt.failure_kind == "timeout"
    assert receipt.error_code == "ExecutionDeadlineExceeded" and receipt.process_stopped
    assert not job.same_process(before["child_identity"])


def test_delayed_monitor_never_releases_child_after_original_deadline(inputs, monitor_environment, monkeypatch):
    env = monitor_environment
    launch = job.launch_monitor
    monkeypatch.setattr(job, "launch_monitor", lambda _: None)
    job.start(inputs.body, env.state, env.workspace)
    claim = job.read_json(env.state / "claim.json")
    claim["monotonic_deadline"] = time.monotonic() - 1
    job.write_json(env.state / "claim.json", claim)
    process = launch(env.state)
    process.wait(timeout=5)
    assert not (env.workspace / "execution-started.json").exists()
    assert not (env.state / "child.json").exists()
    receipt = job.status({"attempt_id": inputs.request.attempt_id}, env.state)
    assert receipt.error_code == "ExecutionDeadlineExceeded" and receipt.process_stopped


def test_output_monitor_proves_specific_limit(inputs, monitor_environment):
    env = monitor_environment
    env.mode[0] = "output"
    inputs.body["request"]["spec"] = inputs.plan.cases[4].spec.model_dump(mode="json")
    started(env, inputs)
    env.processes[0].wait(timeout=5)
    receipt = job.status({"attempt_id": inputs.request.attempt_id}, env.state)
    assert receipt.failure_kind == "policy" and receipt.error_code == "OutputLimitExceeded" and receipt.process_stopped


def test_exited_leader_does_not_leave_setsid_orphan_computation(inputs, monitor_environment):
    env = monitor_environment
    env.mode[0] = "orphan"
    started(env, inputs)
    env.processes[0].wait(timeout=5)
    grandchild = int((env.workspace / "grandchild.pid").read_text())
    assert job.process_identity(grandchild) is None
    receipt = job.status({"attempt_id": inputs.request.attempt_id}, env.state)
    assert receipt.error_code == "ChildExitedWithoutReceipt" and receipt.process_stopped


def test_completed_before_cancel_preserves_success(inputs, monitor_environment):
    env = monitor_environment
    env.mode[0] = "complete"
    before = started(env, inputs)
    manifest = json.loads((ROOT / "tests/fixtures/manifest.json").read_text())
    value = {
        "job_id": inputs.request.job_id,
        "attempt_id": inputs.request.attempt_id,
        "state": "SUCCEEDED",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest,
    }
    job.write_json(env.workspace / "prepared-receipt.json", value)
    (env.workspace / "finish").touch()
    wait_for(lambda: (env.workspace / "child-receipt.json").exists())
    receipt = job.cancel({"attempt_id": inputs.request.attempt_id}, env.state)
    assert receipt.state == WorkerState.SUCCEEDED and receipt.process_stopped
    assert not (env.state / "cancellation.json").exists()
    assert not job.same_process(before["child_identity"])


def test_missing_monitor_cannot_fabricate_positive_cancellation(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(job, "UID", os.geteuid())
    monkeypatch.setattr(job, "launch_monitor", lambda _: None)
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    job.start(inputs.body, state, workspace)
    # Bound this unavailable-monitor observation without sleeping ten seconds.
    ticks = iter([0, 11])
    monkeypatch.setattr(job.time, "monotonic", lambda: next(ticks))
    receipt = job.cancel({"attempt_id": inputs.request.attempt_id}, state)
    assert receipt.state == WorkerState.ACCEPTED and not receipt.process_stopped
    assert not (state / "cancellation.json").exists()


@pytest.mark.parametrize("specific", [True, False])
def test_real_child_specific_cuda_exception_is_not_generic_oom(inputs, tmp_path, monkeypatch, specific):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    job.write_json(state / "config.json", inputs.body["config"])
    job.write_json(state / "request.json", inputs.body["request"])
    monkeypatch.setattr(public, "process_boundary", lambda: None)

    class ActualCudaOOM(Exception):
        pass

    class Engine:
        def __init__(self, config):
            pass

        def execute(self, request, output):
            raise (
                ActualCudaOOM("synthetic specific CUDA allocator error")
                if specific
                else MemoryError("ordinary host RAM")
            )

    monkeypatch.setattr(public, "PublicCalibrationEngine", Engine)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=ActualCudaOOM)))
    read, write = os.pipe()
    os.write(write, b"1")
    os.close(write)
    job.run_child(read, state, workspace)
    receipt = job.child_result(workspace, inputs.request)
    assert (receipt.failure_kind, receipt.error_code) == (
        ("oom", "OutOfMemoryError") if specific else ("policy", "MemoryError")
    )
    assert not receipt.process_stopped  # Only the monitor may acknowledge stop.


def test_tensor_protocol_only_accepts_fixed_digest_and_exact_stream(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(job, "UID", os.geteuid())
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    # The fixed safetensors vector can be reproduced without Torch/numpy.
    import struct

    header = json.dumps(
        {"public_basis_direction": {"dtype": "F32", "shape": [2048], "data_offsets": [0, 8192]}}, separators=(",", ":")
    ).encode()
    header += b" " * ((8 - len(header) % 8) % 8)
    body = struct.pack("<Q", len(header)) + header + struct.pack("<f", 1.0) + b"\0" * (8192 - 4)
    assert len(body) == 8288 and "sha256:" + hashlib.sha256(body).hexdigest() == job.DIRECTION
    metadata = {"sha256": job.DIRECTION, "length": len(body)}
    uploaded = job.upload(metadata, io.BytesIO(body), state, workspace)
    assert uploaded["tensors"] == [{"tensor_name": "public_basis_direction", "shape": [2048], "dtype": "F32"}]
    assert job.read_tensor({"sha256": job.DIRECTION}, workspace) == body
    for raw in (body[:-1], body + b"x", b"x" * len(body)):
        with pytest.raises(ValueError, match="TENSOR_CHECKSUM_MISMATCH"):
            job.upload(metadata, io.BytesIO(raw), state, workspace)
    with pytest.raises(ValueError, match="FIXED_PUBLIC_DIRECTION_REQUIRED"):
        job.read_tensor({"sha256": "sha256:" + "0" * 64}, workspace)


def test_artifact_download_requires_stopped_success_and_manifest_hash(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(job, "UID", os.geteuid())
    monkeypatch.setattr(job, "launch_monitor", lambda _: None)
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    job.start(inputs.body, state, workspace)
    body = {"attempt_id": inputs.request.attempt_id, "path": "summary.json"}
    with pytest.raises(ValueError, match="STOPPED_SUCCESS_REQUIRED"):
        job.artifact(body, state, workspace)
    root = workspace / "job-output"
    root.mkdir()
    raw = b'{"passed":true}'
    (root / "summary.json").write_bytes(raw)
    manifest = json.loads((ROOT / "tests/fixtures/manifest.json").read_text())
    manifest["artifacts"] = [
        {"path": "summary.json", "sha256": hashlib.sha256(raw).hexdigest(), "retention_class": "derived"}
    ]
    receipt = {
        "job_id": inputs.request.job_id,
        "attempt_id": inputs.request.attempt_id,
        "state": "SUCCEEDED",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest,
        "process_stopped": True,
    }
    job.write_json(state / "receipt.json", receipt)
    assert job.artifact(body, state, workspace) == raw
    with pytest.raises(ValueError, match="ARTIFACT_NOT_DECLARED"):
        job.artifact(dict(body, path="../claim.json"), state, workspace)
    (root / "summary.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="ARTIFACT_CHECKSUM_MISMATCH"):
        job.artifact(body, state, workspace)


def test_numerical_child_cannot_acknowledge_cancellation_or_process_stop(inputs, tmp_path):
    receipt = job.receipt_failure(inputs.request, datetime.now(timezone.utc), "cancelled", "forged")
    job.write_json(tmp_path / "child-receipt.json", receipt.model_dump(mode="json"))
    with pytest.raises(ValueError, match="CHILD_RECEIPT_INVALID"):
        job.child_result(tmp_path, inputs.request)
