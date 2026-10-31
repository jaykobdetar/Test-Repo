"""Real staging/receipt protocol; only the numerical result is synthetic."""

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from safetensors.numpy import save_file

from probe_core import gpu_acceptance_runner as runner
from probe_core.artifact_store import ArtifactStore
from probe_core.audit import canonical_json
from probe_core.direction_transfer import (
    DIRECTION_BYTES,
    DIRECTION_NAME,
    DIRECTION_SHA256,
    READBACK_PROGRAM,
    controller_direction,
    direction_reference,
)
from probe_core.dispatcher import WorkerClient
from probe_core.gpu_acceptance import AcceptancePlan, collect, fixed_direction_plan, fixed_plan
from probe_core.worker import Supervisor
from probe_core.worker_contracts import ExecutionRequest
from test_gpu_acceptance_runner import Endpoint, Tunnel, approve_fixture, setup
from test_runpod_provider import runpod
from test_schemas import manifest_data


@pytest.fixture
def prepared(setup, monkeypatch):
    s = setup
    inputs = s.plan.cases[0].spec.inputs
    s.plan = fixed_direction_plan(
        s.plan.model, "fixed-public-direction-v1", inputs.dataset_revision, inputs.prompt_set_hash, inputs.prompt_ids
    )
    raw = s.plan.model_dump_json().encode()
    Path(s.config.plan_path).write_bytes(raw)
    s.config = s.config.model_copy(update={"plan_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()})
    source = s.root / "direction.safetensors"
    values = np.zeros(2048, dtype=np.float32)
    values[0] = 1.0
    save_file({DIRECTION_NAME: values}, str(source))
    assert source.stat().st_size == DIRECTION_BYTES
    assert "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest() == DIRECTION_SHA256
    s.registry = s.root / "input-artifacts"
    s.registration = ArtifactStore(s.registry).register(source, expected_sha256=DIRECTION_SHA256)
    s.remote = s.root / "remote-tensors"
    s.remote.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "LEDGER", s.ledger.path)
    return s


class Wire:
    def __init__(self, s, *, change_upload=None):
        self.s = s
        self.execution = Endpoint(s)
        self.events = []
        self.change_upload = change_upload
        self.secret = Path(s.config.bearer_secret_file).read_text()
        self.client = WorkerClient("http://127.0.0.1:45678", self.secret)
        self.client._opener = self

    def open(self, request, timeout):
        assert request.get_header("Authorization") == "Bearer " + self.secret
        path = request.full_url.removeprefix(self.client.base_url)
        if request.method == "PUT":
            self.events.append("upload")
            assert path == "/v1/tensors/" + DIRECTION_SHA256.removeprefix("sha256:")
            receipt = Supervisor.upload_tensor(
                SimpleNamespace(
                    config=SimpleNamespace(tensor_directory=str(self.s.remote), max_tensor_bytes=DIRECTION_BYTES)
                ),
                path.rsplit("/", 1)[1],
                request.data,
                int(request.get_header("Content-length")),
            )
            if self.change_upload:
                self.change_upload(receipt)
            return io.BytesIO(canonical_json(receipt).encode())
        if request.method == "POST":
            self.events.append("execute")
            assert path == "/v1/jobs"
            proof = json.loads((self.s.root / "runner/direction-transfer.json").read_bytes())
            assert proof["request"] == json.loads(request.data)
            receipt = self.execution.submit(ExecutionRequest.model_validate_json(request.data))
        elif "/artifacts/" in path:
            relative = path.split("/artifacts/", 1)[1]
            return io.BytesIO(self.execution.contents[relative])
        else:
            assert path.startswith("/v1/jobs/") and request.method == "GET"
            receipt = self.execution.status(path.rsplit("/", 1)[1])
        return io.BytesIO(receipt.model_dump_json().encode())

    def readback(self, settings, phase, *, deadline, clock):
        self.events.append(phase)
        assert clock() < deadline
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                READBACK_PROGRAM,
                str(self.s.remote),
                DIRECTION_SHA256.removeprefix("sha256:"),
                str(os.geteuid()),
                phase,
            ],
            capture_output=True,
            timeout=3,
        )
        if completed.returncode:
            raise ValueError("synthetic remote readback refused")
        return json.loads(completed.stdout)


def execute(s, wire, state, *, readback=None):
    return runner.run(
        s.config,
        s.plan,
        s.ledger,
        s.backend,
        SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)),
        state,
        clock=lambda: s.clock().timestamp(),
        sleep=lambda _: None,
        endpoint=lambda *_: {},
        tunnel_factory=Tunnel,
        client_factory=lambda *_a, **_k: wire.client,
        readiness=lambda *_a, **_k: None,
        configure=lambda *_a, **_k: {"configured": True},
        direction_readback=readback or wire.readback,
    )


def test_real_upload_hash_readback_and_sealed_manifest_precede_pass(prepared):
    s = prepared
    request = approve_fixture(s)
    wire = Wire(s)
    with runner.State(s.config.trusted_state_directory) as state:
        result = execute(s, wire, state)
        assert result["status"] == "passed", result
        proof = state.read("direction-transfer.json")
        observation = state.read("observations.json")["cases"][0]
        assert observation["direction_transfer"] == proof
        assert result["direction_transfer_sha256"] == runner.digest(proof)
        assert execute(s, wire, state) == result
    assert wire.events == ["before", "upload", "after", "execute"]
    assert (s.remote / direction_reference()["path"]).read_bytes() == (
        s.registry / direction_reference()["path"]
    ).read_bytes()
    job = s.ledger.get_job(request["job_ids"][0])
    assert proof["request"] == wire.execution.request.model_dump(mode="json")
    assert proof["request"]["approval_id"] == request["approval_id"]
    assert wire.execution.request.deadline.timestamp() < request["deadline"]
    assert job.attempt_count == 1 and job.retry_count == 0 and wire.execution.posts == 1
    assert s.ledger.get_manifest(job.job_id).experiment.intervention_hash == s.ledger.operation_hash(job.spec)
    assert result["teardown"]["confirmed"] is True and len(s.http.purchases) == 1
    assert result["scientific_evidence"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "controller_corrupt",
        "worker_preexisting",
        "upload_digest",
        "upload_shape",
        "upload_shape_float",
        "worker_corrupt",
        "readback_missing",
        "readback_timeout",
    ],
)
def test_failed_transfer_gate_never_sends_execution_post(prepared, fault):
    s = prepared
    request = approve_fixture(s)

    def alter(receipt):
        if fault == "upload_digest":
            receipt["sha256"] = "sha256:" + "f" * 64
        elif fault == "upload_shape":
            receipt["tensors"][0]["shape"] = [1]
        elif fault == "upload_shape_float":
            receipt["tensors"][0]["shape"] = [2048.0]

    wire = Wire(s, change_upload=alter)
    if fault == "controller_corrupt":
        source = s.registry / direction_reference()["path"]
        source.chmod(0o600)
        source.write_bytes(b"corrupt")
    elif fault == "worker_preexisting":
        (s.remote / "preexisting").write_bytes(b"not a transferred input")

    def readback(*args, **kwargs):
        if args[1] == "after":
            if fault == "worker_corrupt":
                path = s.remote / direction_reference()["path"]
                path.chmod(0o600)
                path.write_bytes(b"x" * DIRECTION_BYTES)
                path.chmod(0o400)
            elif fault == "readback_missing":
                return {"state": "absent"}
            elif fault == "readback_timeout":
                runner.command_bytes([sys.executable, "-I", "-c", "import time; time.sleep(5)"], timeout=0.05)
        return wire.readback(*args, **kwargs)

    with runner.State(s.config.trusted_state_directory) as state:
        result = execute(s, wire, state, readback=readback)
        assert state.read("direction-transfer.json") is None
    job = s.ledger.get_job(request["job_ids"][0])
    assert result["status"] == "failed" and result["teardown"]["confirmed"] is True
    assert job.failure_reason == "InputStagingFailed" and wire.execution.posts == 0
    assert "execute" not in wire.events and job.attempt_count == 1


@pytest.mark.parametrize(
    "fault",
    ["missing", "attempt", "deadline", "approval", "operation", "upload", "readback", "receipt", "controller_after"],
)
def test_collector_requires_exact_transfer_proof_and_unchanged_controller_bytes(prepared, fault):
    s = prepared
    approve_fixture(s)
    wire = Wire(s)
    with runner.State(s.config.trusted_state_directory) as state:
        assert execute(s, wire, state)["status"] == "passed"
        proof = state.read("direction-transfer.json")
    changed = deepcopy(proof)
    if fault == "missing":
        changed = None
    elif fault in {"attempt", "approval"}:
        changed["request"][fault + "_id"] = "wrong-identity"
    elif fault == "deadline":
        changed["request"]["deadline"] = "2099-01-01T00:00:00Z"
    elif fault == "operation":
        changed["request"]["spec"]["operation"]["strength"] = 1.0
    elif fault == "upload":
        changed["upload_receipt"]["tensors"][0]["tensor_name"] = "another-direction"
    elif fault == "readback":
        changed["worker_after"]["bytes"] = float(DIRECTION_BYTES)
    elif fault == "receipt":
        original = wire.execution.status

        def altered(attempt_id):
            receipt = original(attempt_id)
            experiment = receipt.manifest.experiment.model_copy(update={"intervention_hash": "sha256:" + "f" * 64})
            return receipt.model_copy(
                update={"manifest": receipt.manifest.model_copy(update={"experiment": experiment})}
            )

        wire.execution.status = altered
    else:
        path = s.registry / direction_reference()["path"]
        path.chmod(0o600)
        path.write_bytes(b"corrupt after acceptance")
    before = list(wire.events)
    observed = collect(s.ledger, s.plan, wire.client, direction_evidence=changed, input_artifact_root=s.registry)
    assert observed["case_results_passed"] is False
    assert observed["cases"][0]["reason"] == "controller direction transfer evidence is absent or mismatched"
    assert wire.events == before  # Collection never uploads or executes.
    assert collect(s.ledger, s.plan)["case_results_passed"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("strength", 0.0),
        ("positions", [0]),
        ("direction", {**direction_reference(), "sha256": "sha256:" + "e" * 64}),
        ("target", {"layer": 15, "component": "residual"}),
    ],
)
def test_runner_refuses_changed_direction_recipe_before_submission(prepared, field, value):
    s = prepared
    body = s.plan.model_dump(mode="json")
    body["cases"][0]["spec"]["operation"][field] = value
    plan = AcceptancePlan.model_validate(body)
    with pytest.raises(runner.RunnerError, match="FIXED_CALIBRATION_CASE_REQUIRED"):
        runner.validate_plan(s.config, plan)
    assert s.ledger.list_jobs() == [] and s.http.purchases == []


def test_separate_direction_plan_preserves_the_runtime_case_plan(prepared):
    s = prepared
    case = s.plan.cases[0]
    assert case.spec.idempotency_key == s.plan.label and case.name == "public-direction-transfer"
    assert case.spec.operation.direction.model_dump(mode="json") == direction_reference()
    inputs = case.spec.inputs
    assert (
        len(
            fixed_plan(
                s.plan.model, s.plan.label, inputs.dataset_revision, inputs.prompt_set_hash, inputs.prompt_ids
            ).cases
        )
        == 8
    )
    assert controller_direction(s.registry) == s.registration


def test_fixed_ssh_command_has_only_the_pinned_path_and_bounded_time(prepared):
    s = prepared
    settings = {
        "host": "fixture.example",
        "ssh_port": 40103,
        "identity_file": s.root / "key",
        "known_hosts_file": s.root / "known-hosts",
    }
    commands = []

    def command(argv, *, timeout):
        commands.append(argv)
        assert 0 < timeout <= 5
        assert argv[-4:] == ["/workspace/probe/tensors", DIRECTION_SHA256.removeprefix("sha256:"), "10001", "before"]
        assert shlex.split(argv[-5]) == [READBACK_PROGRAM]
        return '{"state":"absent"}'

    assert runner.read_worker_direction(
        settings, "before", deadline=s.clock().timestamp() + 10, clock=lambda: s.clock().timestamp(), command=command
    ) == {"state": "absent"}
    assert len(commands) == 1 and "StrictHostKeyChecking=yes" in commands[0]
    with pytest.raises(runner.RunnerError, match="DIRECTION_READBACK_DEADLINE"):
        runner.read_worker_direction(
            settings, "after", deadline=s.clock().timestamp(), clock=lambda: s.clock().timestamp(), command=command
        )
    assert len(commands) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "symlink_file",
        "symlink_directory",
        "hardlink_file",
        "public_directory",
        "writable_file",
        "oversized_file",
        "extra_file",
    ],
)
def test_actual_readback_program_refuses_unsafe_or_unbounded_remote_input(prepared, fault):
    s = prepared
    source = s.registry / direction_reference()["path"]
    directory = s.remote / DIRECTION_SHA256.removeprefix("sha256:")
    directory.mkdir(mode=0o700)
    target = directory / "tensor.safetensors"
    target.write_bytes(source.read_bytes())
    target.chmod(0o400)
    if fault == "symlink_file":
        target.unlink()
        target.symlink_to(source)
    elif fault == "symlink_directory":
        target.unlink()
        directory.rmdir()
        directory.symlink_to(source.parent, target_is_directory=True)
    elif fault == "hardlink_file":
        os.link(target, s.root / "other-link")
    elif fault == "public_directory":
        directory.chmod(0o755)
    elif fault == "writable_file":
        target.chmod(0o600)
    elif fault == "oversized_file":
        target.chmod(0o600)
        target.write_bytes(b"x" * (DIRECTION_BYTES + 1))
        target.chmod(0o400)
    else:
        (directory / "unrelated").write_bytes(b"x")
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            READBACK_PROGRAM,
            str(s.remote),
            DIRECTION_SHA256.removeprefix("sha256:"),
            str(os.geteuid()),
            "after",
        ],
        capture_output=True,
        timeout=3,
    )
    assert completed.returncode != 0 and completed.stdout == b""
