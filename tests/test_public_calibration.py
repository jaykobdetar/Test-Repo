"""Lightweight fixed-input and provenance tests; no Torch/model/GPU execution."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from probe_core.model_assets import canonical_locks
from probe_core.worker import WorkerEngine

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_calibration", ROOT / "deploy/gpu/public-calibration.py")
calibration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = calibration
spec.loader.exec_module(calibration)


@pytest.fixture
def public_data():
    lock = canonical_locks()[0]
    baked = json.loads((ROOT / "deploy/gpu/public-assets.json").read_bytes())
    prefix = "models/" + lock.repo.split("/")[-1] + "/" + lock.revision + "/"
    return {
        "model": {
            "repo": lock.repo,
            "revision_sha": lock.revision,
            "tokenizer_revision": lock.revision,
            "local_weight_hashes": [item.sha256 for item in lock.files if item.path.endswith(".safetensors")],
            "dtype": "bfloat16",
            "quantized": False,
            "chat_template_hash": None,
            "thinking_mode": None,
        },
        "assets": [
            {"path": item["path"][len(prefix) :], "sha256": item["sha256"]}
            for item in baked["assets"]
            if item["path"].startswith(prefix)
        ],
        "datasets": [
            {"path": "/opt/probe-assets/datasets/public-calibration-prompts.json", "sha256": calibration.DATASET_SHA}
        ],
        "code_git_commit": "f" * 40,
        "container_image_digest": "sha256:" + "a" * 64,
        "region": "EU-RO-1",
        "live_price_usd_per_hour": 0.74,
    }


@pytest.fixture
def dataset():
    baked = json.loads((ROOT / "deploy/gpu/public-assets.json").read_bytes())
    return calibration.PromptDataset.model_validate_json(base64.b64decode(baked["assets"][-1]["inline_base64"]))


@pytest.fixture
def config(public_data):
    return calibration.PublicCalibrationConfig.model_validate(public_data)


def test_minimal_config_has_no_cgroup_or_arbitrary_execution_contract(config, dataset):
    assert not hasattr(config, "cgroup_directory")
    assert config.device == "cuda:0" and config.backend == "nnsight"
    assert config.model_directory.endswith("/Qwen3-1.7B-Base/" + config.model.revision_sha)
    assert calibration.PublicCalibrationEngine.execute is WorkerEngine.execute
    assert calibration.PublicCalibrationEngine.forward is WorkerEngine.forward
    now = datetime.now(timezone.utc)
    request = calibration.execution_request(config, dataset, "public-calibration", now + timedelta(seconds=900), now)
    assert request.spec.operation.kind == "backend_parity"
    assert request.spec.experiment_stage.value == "calibration"
    assert request.deadline == now + timedelta(seconds=240)
    assert request.spec.inputs.prompt_ids == ("public-short", "public-long")
    assert request.spec.inputs.random_seed == 123
    assert request.spec.inputs.generation.max_new_tokens == 4
    assert request.spec.limits.max_runtime_seconds == 240
    assert request.spec.limits.max_output_bytes == 32 * 1024**2


@pytest.mark.parametrize(
    "change",
    [
        {"cgroup_directory": "/sys/fs/cgroup/false-claim"},
        {"operation": {"kind": "generate"}},
        {"model_directory": "/arbitrary"},
        {"callback": "anything"},
        {"live_price_usd_per_hour": 1.50},
        {"live_price_usd_per_hour": float("nan")},
        {"container_image_digest": "latest"},
    ],
)
def test_extra_scope_and_invalid_price_refused(public_data, change):
    with pytest.raises(ValueError):
        calibration.PublicCalibrationConfig.model_validate(dict(public_data, **change))


@pytest.mark.parametrize(
    "field,value",
    [
        ("repo", "probe/testing-tiny-qwen3"),
        ("revision_sha", "b" * 40),
        ("tokenizer_revision", "b" * 40),
        ("local_weight_hashes", ["sha256:" + "b" * 64]),
        ("dtype", "float32"),
        ("quantized", True),
        ("thinking_mode", False),
    ],
)
def test_model_must_be_exact_canonical_bf16(public_data, field, value):
    public_data["model"][field] = value
    with pytest.raises(ValueError):
        calibration.PublicCalibrationConfig.model_validate(public_data)


@pytest.mark.parametrize("mutation", ["extra_asset", "duplicate_asset", "missing_asset", "other_dataset", "other_path"])
def test_asset_inventory_cannot_expand(public_data, mutation):
    if mutation == "extra_asset":
        public_data["assets"].append({"path": "model.py", "sha256": "sha256:" + "b" * 64})
    if mutation == "duplicate_asset":
        public_data["assets"].append(public_data["assets"][0])
    if mutation == "missing_asset":
        public_data["assets"].pop()
    if mutation == "other_dataset":
        public_data["datasets"][0]["sha256"] = "sha256:" + "b" * 64
    if mutation == "other_path":
        public_data["datasets"][0]["path"] = "/private/heldout.json"
    with pytest.raises(ValueError):
        calibration.PublicCalibrationConfig.model_validate(public_data)


@pytest.mark.parametrize("seconds", [-1, 0, 901])
def test_expired_or_extended_deadline_refused(config, dataset, seconds):
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="ABSOLUTE_DEADLINE_INVALID"):
        calibration.execution_request(config, dataset, "run", now + timedelta(seconds=seconds), now)


def test_job_deadline_never_extends_shorter_original(config, dataset):
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(seconds=20)
    assert calibration.execution_request(config, dataset, "run", deadline, now).deadline == deadline
    with pytest.raises(ValueError):
        calibration.execution_request(config, dataset, "run", deadline.replace(tzinfo=None), now)


def test_engine_uses_unchanged_hash_verifier_not_worker_config_bypass(config, monkeypatch):
    observed = []
    monkeypatch.setattr(WorkerEngine, "_verify_model_bundle", lambda self: observed.append(self.config))
    engine = calibration.PublicCalibrationEngine(config)
    assert engine.config == config and observed == [config]
    assert engine.model is engine.tokenizer is None


@pytest.fixture
def asset_environment(config, monkeypatch):
    baked = json.loads((ROOT / "deploy/gpu/public-assets.json").read_bytes())
    dataset_raw = base64.b64decode(baked["assets"][-1]["inline_base64"])
    provenance = {"source_commit": config.code_git_commit, "environment_lock_hash": "sha256:" + "e" * 64}
    documents = {
        str(calibration.BAKED_MANIFEST): json.dumps(baked).encode(),
        str(calibration.PROVENANCE): json.dumps(provenance).encode(),
        config.datasets[0].path: dataset_raw,
    }
    monkeypatch.setattr(calibration, "immutable_file", lambda path: documents[str(path)])
    monkeypatch.setattr(calibration, "sha256_file", lambda _: "sha256:" + "e" * 64)
    file_paths = {str(calibration.ASSETS / item["path"]) for item in baked["assets"]}

    def lstat(path):
        return SimpleNamespace(st_mode=0o100444 if str(path) in file_paths else 0o40755, st_uid=0, st_nlink=1)

    monkeypatch.setattr(calibration.Path, "lstat", lstat)
    return SimpleNamespace(baked=baked, provenance=provenance, documents=documents)


def test_baked_image_provenance_and_public_dataset_validate(config, asset_environment):
    assert tuple(item.prompt_id for item in calibration.validate_assets(config).prompts) == calibration.PROMPTS


@pytest.mark.parametrize("fault", ["source", "lock", "asset_hash", "extra_asset", "duplicate_asset", "dataset"])
def test_runtime_asset_and_source_tampering_refused(config, asset_environment, fault):
    s = asset_environment
    if fault == "source":
        s.provenance["source_commit"] = "b" * 40
    if fault == "lock":
        s.provenance["environment_lock_hash"] = "sha256:" + "b" * 64
    if fault == "asset_hash":
        s.baked["assets"][0]["sha256"] = "sha256:" + "b" * 64
    if fault == "extra_asset":
        s.baked["assets"].append({"path": "private/heldout.json", "sha256": "sha256:" + "b" * 64})
    if fault == "duplicate_asset":
        s.baked["assets"].append(s.baked["assets"][0])
    if fault == "dataset":
        s.documents[config.datasets[0].path] += b" "
    s.documents[str(calibration.BAKED_MANIFEST)] = json.dumps(s.baked).encode()
    s.documents[str(calibration.PROVENANCE)] = json.dumps(s.provenance).encode()
    with pytest.raises(ValueError):
        calibration.validate_assets(config)


@pytest.mark.parametrize("mode", [0o120444, 0o100666])
def test_mutable_or_symlinked_asset_refused(config, asset_environment, monkeypatch, mode):
    original = calibration.Path.lstat
    filename = Path(config.model_directory) / "config.json"
    monkeypatch.setattr(
        calibration.Path,
        "lstat",
        lambda path: SimpleNamespace(st_uid=0, st_mode=mode, st_nlink=1) if path == filename else original(path),
    )
    with pytest.raises(ValueError, match="ASSET_NOT_IMMUTABLE"):
        calibration.validate_assets(config)


@pytest.mark.parametrize("thinking", [False, True])
def test_posttrained_mode_requires_explicit_template_identity(public_data, thinking):
    lock = canonical_locks()[1]
    baked = json.loads((ROOT / "deploy/gpu/public-assets-posttrained.json").read_bytes())
    prefix = "models/" + lock.repo.split("/")[-1] + "/" + lock.revision + "/"
    public_data["assets"] = [
        {"path": item["path"][len(prefix) :], "sha256": item["sha256"]}
        for item in baked["assets"]
        if item["path"].startswith(prefix)
    ]
    public_data["model"].update(
        repo=lock.repo,
        revision_sha=lock.revision,
        tokenizer_revision=lock.revision,
        local_weight_hashes=[item.sha256 for item in lock.files if item.path.endswith(".safetensors")],
        thinking_mode=thinking,
        chat_template_hash="sha256:" + "b" * 64,
    )
    assert calibration.PublicCalibrationConfig.model_validate(public_data).model.thinking_mode is thinking
    public_data["model"]["chat_template_hash"] = None
    with pytest.raises(ValueError, match="POSTTRAINED_TEMPLATE_MODE_REQUIRED"):
        calibration.PublicCalibrationConfig.model_validate(public_data)


def test_correct_dedicated_clean_process_boundary(monkeypatch):
    monkeypatch.setattr(calibration.os, "getresuid", lambda: (10001,) * 3)
    monkeypatch.setattr(calibration.os, "getresgid", lambda: (10001,) * 3)
    monkeypatch.setattr(calibration.os, "getgroups", lambda: [])
    monkeypatch.setattr(calibration.sys, "flags", SimpleNamespace(isolated=1))
    monkeypatch.setattr(calibration, "_environment", lambda *_: dict(os.environ))
    monkeypatch.setattr(calibration, "_initial_environment", lambda: dict(os.environ))
    calibration.process_boundary()
    monkeypatch.setattr(calibration, "_initial_environment", lambda: dict(os.environ, RUNPOD_API_KEY="synthetic"))
    with pytest.raises(ValueError, match="CLEAN_EXEC_ENVIRONMENT_REQUIRED"):
        calibration.process_boundary()


def test_root_and_supplementary_privileged_group_refused(monkeypatch):
    monkeypatch.setattr(calibration.os, "getresuid", lambda: (0,) * 3)
    with pytest.raises(ValueError, match="DEDICATED_NUMERICAL_IDENTITY_REQUIRED"):
        calibration.process_boundary()
    monkeypatch.setattr(calibration.os, "getresuid", lambda: (10001,) * 3)
    monkeypatch.setattr(calibration.os, "getresgid", lambda: (10001,) * 3)
    monkeypatch.setattr(calibration.os, "getgroups", lambda: [0])
    with pytest.raises(ValueError, match="DEDICATED_NUMERICAL_IDENTITY_REQUIRED"):
        calibration.process_boundary()


@pytest.fixture
def fake_run(config, dataset, monkeypatch, tmp_path):
    monkeypatch.setattr(calibration, "UID", os.geteuid())
    monkeypatch.setattr(calibration, "WORKSPACE", tmp_path)
    tmp_path.chmod(0o700)
    monkeypatch.setattr(calibration, "process_boundary", lambda: None)
    monkeypatch.setattr(calibration, "immutable_file", lambda *_: dataset.model_dump_json().encode())
    monkeypatch.setattr(calibration, "validate_assets", lambda *_: dataset)
    calls = []

    def execute(self, request, output):
        calls.append(request)
        output.mkdir()
        summary = {
            "suite": "backend_parity_v1",
            "passed": True,
            "scientific_evidence": False,
            "checks": [{"name": str(i), "passed": True} for i in range(29)],
        }
        (output / "summary.json").write_text(json.dumps(summary))
        # Synthetic artifact only. This test makes no numerical/tensor claim.
        (output / "tensors.safetensors").write_bytes(b"synthetic-not-a-real-tensor")

        class Value:
            def model_dump(self, **_):
                return {"synthetic": True}

        return SimpleNamespace(
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            manifest=SimpleNamespace(software=Value(), hardware=Value(), cost=Value(), artifacts=[]),
        )

    monkeypatch.setattr(calibration.PublicCalibrationEngine, "__init__", lambda self, _: None)
    monkeypatch.setattr(calibration.PublicCalibrationEngine, "execute", execute)
    return SimpleNamespace(
        config=config,
        output=tmp_path / "result",
        calls=calls,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=500),
    )


def test_one_shot_result_is_honest_standalone_evidence(fake_run):
    s = fake_run
    report = calibration.run(s.config, absolute_deadline=s.deadline, run_id="public-calibration", output=s.output)
    assert len(s.calls) == 1 and report["checks_passed"] == 29
    assert report["kind"] == "standalone_public_calibration"
    for field in (
        "scientific_evidence",
        "heldout_data_used",
        "installed_ledger_used",
        "lifecycle_acceptance",
        "nested_cgroup_limits_enforced",
        "process_exit_and_pod_deletion_verified",
    ):
        assert report[field] is False
    assert "approval_id" not in report and "job_id" not in report
    assert json.loads((s.output / "standalone-manifest.json").read_bytes()) == report
    with pytest.raises(ValueError, match="CALIBRATION_ALREADY_STARTED"):
        calibration.run(s.config, absolute_deadline=s.deadline, run_id="another-run", output=s.output)
    assert len(s.calls) == 1


def test_failed_attempt_is_irreversibly_claimed(fake_run, monkeypatch):
    def failed(*_):
        raise RuntimeError("synthetic-secret-must-not-be-published")

    monkeypatch.setattr(calibration.PublicCalibrationEngine, "execute", failed)
    s = fake_run
    with pytest.raises(RuntimeError):
        calibration.run(s.config, absolute_deadline=s.deadline, run_id="run", output=s.output)
    assert not (s.output / "standalone-manifest.json").exists()
    with pytest.raises(ValueError, match="CALIBRATION_ALREADY_STARTED"):
        calibration.run(s.config, absolute_deadline=s.deadline, run_id="run", output=s.output)


def test_cli_error_does_not_publish_exception_text(monkeypatch, capsys):
    def fail(*_):
        raise RuntimeError("synthetic-secret-must-not-be-published")

    monkeypatch.setattr(calibration, "immutable_file", fail)
    assert (
        calibration.main(
            [
                "--config",
                "/run/example.json",
                "--run-id",
                "run",
                "--absolute-deadline",
                "2026-09-21T00:00:00+00:00",
                "--output",
                "/tmp/public-calibration/result",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "synthetic-secret" not in output
    assert json.loads(output)["reason"] == "CALIBRATION_FAILED"
