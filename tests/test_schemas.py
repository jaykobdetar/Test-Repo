from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from probe_core.schemas import (
    Ablate,
    ApprovalNonce,
    Capture,
    FitProbe,
    HypothesisRecord,
    HypothesisState,
    JobSpec,
    Patch,
    RunManifest,
    Steer,
)


HASH = "sha256:" + "a" * 64
FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"


@pytest.fixture
def manifest_data():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def job_data(manifest_data):
    return {
        "idempotency_key": "experiment-001",
        "hypothesis_id": "H-0042",
        "model": manifest_data["model"],
        "inputs": manifest_data["inputs"],
        "operation": {
            "kind": "capture",
            "modules": [{"layer": 0, "component": "residual"}],
            "positions": ["last"],
        },
        "limits": {"max_runtime_seconds": 60, "max_output_bytes": 1000000},
    }


@pytest.fixture
def hypothesis_data(manifest_data, job_data):
    return {
        "hypothesis_id": "H-0042",
        "proposition": "Layer 0 contributes causally to the behavioral contrast.",
        "alignment_relevance": "Tests the effect on factual answers under pressure.",
        "predicted_causal_intervention": "Patch the final residual activation.",
        "predicted_direction": "increase",
        "predictions": ["Patching increases the registered target metric."],
        "falsifier": "The target effect does not exceed matched random controls.",
        "preregistration_plan": {
            "model": manifest_data["model"],
            "operation": job_data["operation"],
            "primary_metric": "target_behavior_delta",
            "minimum_effect": 0.01,
            "controls": manifest_data["controls"],
        },
    }


@pytest.fixture
def approval_data():
    issued = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    return {
        "approval_id": "WAKE-001",
        "token": "an-opaque-random-approval-token-1234567890",
        "pod_id": "pod-001",
        "batch_hash": HASH,
        "max_runtime_seconds": 600,
        "price_ceiling_usd_per_hour": 1.49,
        "expires_at": issued + timedelta(minutes=5),
        "issued_at": issued,
    }


def mutate(data, path, value):
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def test_complete_manifest_json_round_trip_and_deep_immutability(manifest_data):
    manifest = RunManifest.model_validate(manifest_data)
    assert RunManifest.model_validate_json(manifest.model_dump_json()) == manifest
    assert manifest.model_dump(mode="json") == manifest_data
    assert isinstance(manifest.inputs.prompt_ids, tuple)
    with pytest.raises(ValidationError):
        manifest.model.dtype = "float32"
    with pytest.raises(TypeError):
        manifest.inputs.prompt_ids[0] = "changed"


@pytest.mark.parametrize("section", [None, "run", "model", "software", "hardware", "inputs", "experiment", "controls", "results", "cost", "security"])
def test_manifest_rejects_unknown_fields_at_each_boundary(manifest_data, section):
    (manifest_data if section is None else manifest_data[section])["python"] = "print('execute')"
    with pytest.raises(ValidationError):
        RunManifest.model_validate(manifest_data)


@pytest.mark.parametrize(
    "path,value",
    [
        (("model", "revision_sha"), "main"),
        (("model", "revision_sha"), "a" * 39),
        (("model", "tokenizer_revision"), "b" * 7),
        (("software", "probe_mcp_git_commit"), "HEAD"),
        (("software", "container_image_digest"), "latest"),
        (("run", "preregistration_hash"), "sha256:abc"),
        (("experiment", "intervention_hash"), "a" * 64),
        (("inputs", "dataset_revision"), "main"),
        (("inputs", "prompt_set_hash"), "sha256:" + "g" * 64),
        (("model", "local_weight_hashes"), []),
        (("artifacts", 0, "sha256"), "abc"),
    ],
)
def test_manifest_requires_pinned_content_identity(manifest_data, path, value):
    mutate(manifest_data, path, value)
    with pytest.raises(ValidationError):
        RunManifest.model_validate(manifest_data)


@pytest.mark.parametrize(
    "path,value",
    [
        (("run", "started_at"), "2026-09-19T12:00:00"),
        (("schema_version",), True),
        (("hardware", "gpu_count"), True),
        (("hardware", "gpu_count"), 2),
        (("hardware", "live_price_usd_per_hour"), 1.50),
        (("hardware", "live_price_usd_per_hour"), 0),
        (("hardware", "live_price_usd_per_hour"), float("nan")),
        (("inputs", "generation", "temperature"), float("inf")),
        (("inputs", "generation", "max_new_tokens"), 100000),
        (("inputs", "random_seed"), True),
        (("inputs", "prompt_ids"), ["same", "same"]),
        (("results", "effect_size"), float("nan")),
        (("results", "confidence_interval"), [1.0, -1.0]),
        (("cost", "gpu_seconds"), -1),
        (("cost", "estimated_compute_usd"), float("inf")),
        (("security", "policy_denials"), -1),
        (("model", "quantized"), "false"),
    ],
)
def test_manifest_rejects_ambiguous_or_unbounded_values(manifest_data, path, value):
    mutate(manifest_data, path, value)
    with pytest.raises(ValidationError):
        RunManifest.model_validate(manifest_data)


def test_offset_timestamps_are_normalized_to_utc(manifest_data):
    manifest_data["run"]["started_at"] = "2026-09-19T08:00:00-04:00"
    result = RunManifest.model_validate(manifest_data)
    assert result.run.started_at == datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    assert result.run.started_at.tzinfo == timezone.utc


@pytest.mark.parametrize(
    "path,value",
    [
        (("model", "dtype"), "float32"),
        (("model", "quantized"), True),
        (("run", "preregistration_hash"), None),
        (("run", "hypothesis_id"), None),
        (("results", "heldout"), False),
    ],
)
def test_confirmation_requires_frozen_identity_and_canonical_model(manifest_data, path, value):
    mutate(manifest_data, path, value)
    with pytest.raises(ValidationError):
        RunManifest.model_validate(manifest_data)


def test_replication_requires_blinding(manifest_data):
    manifest_data["run"].update(experiment_stage="replication", replicator_blinded=False)
    with pytest.raises(ValidationError):
        RunManifest.model_validate(manifest_data)


def test_exploration_can_be_unregistered_and_nonheldout(manifest_data):
    manifest_data["run"].update(experiment_stage="exploratory", hypothesis_id=None, preregistration_hash=None)
    manifest_data["results"]["heldout"] = False
    manifest_data["model"]["dtype"] = "float32"
    assert RunManifest.model_validate(manifest_data).run.hypothesis_id is None


@pytest.mark.parametrize("path", ["../secret", "artifacts/../../secret", "/etc/passwd", "C:\\secret", "artifacts//file", "./file", "artifacts/./file", "artifacts/%2e%2e/secret", "file\x00", "artifacts/file?token=secret"])
def test_artifact_paths_cannot_escape_or_hide_encoding(manifest_data, path):
    manifest_data["artifacts"][0]["path"] = path
    with pytest.raises(ValidationError):
        RunManifest.model_validate(manifest_data)


def test_duplicate_artifact_paths_are_rejected(manifest_data):
    manifest_data["artifacts"].append(copy.deepcopy(manifest_data["artifacts"][0]))
    with pytest.raises(ValidationError):
        RunManifest.model_validate(manifest_data)


TENSOR = {"path": "artifacts/vector.safetensors", "sha256": HASH, "tensor_name": "direction"}
TARGET = {"layer": 27, "component": "attention_head", "head": 15}


@pytest.mark.parametrize(
    "operation,expected_type",
    [
        ({"kind": "capture", "modules": [TARGET], "positions": [0, "last"]}, Capture),
        ({"kind": "patch", "target": TARGET, "positions": [0], "source": TENSOR}, Patch),
        ({"kind": "ablate", "target": TARGET, "positions": [0]}, Ablate),
        ({"kind": "ablate", "target": TARGET, "positions": [0], "method": "mean", "baseline": TENSOR}, Ablate),
        ({"kind": "steer", "target": TARGET, "positions": [0], "direction": TENSOR, "strength": 0.5}, Steer),
        ({"kind": "fit_probe", "activations": TENSOR, "labels": TENSOR, "algorithm": "ridge"}, FitProbe),
    ],
)
def test_job_discriminator_only_constructs_fixed_operations(job_data, operation, expected_type):
    job_data["operation"] = operation
    spec = JobSpec.model_validate(job_data)
    assert isinstance(spec.operation, expected_type)
    assert JobSpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "python", "code": "print('execute')"},
        {"kind": "capture", "modules": [TARGET], "positions": [0], "callback": "eval"},
        {"kind": "capture", "modules": [{"layer": 28, "component": "residual"}], "positions": [0]},
        {"kind": "capture", "modules": [{"layer": 0, "component": "attention_head"}], "positions": [0]},
        {"kind": "capture", "modules": [{"layer": 0, "component": "residual", "head": 0}], "positions": [0]},
        {"kind": "capture", "modules": [TARGET], "positions": [-1]},
        {"kind": "capture", "modules": [TARGET], "positions": [32768]},
        {"kind": "capture", "modules": [TARGET], "positions": [True]},
        {"kind": "patch", "target": TARGET, "positions": [0], "source": {**TENSOR, "path": "weights.pkl"}},
        {"kind": "patch", "target": TARGET, "positions": [0], "source": {**TENSOR, "path": "../weights.safetensors"}},
        {"kind": "ablate", "target": TARGET, "positions": [0], "method": "mean"},
        {"kind": "ablate", "target": TARGET, "positions": [0], "method": "zero", "baseline": TENSOR},
        {"kind": "steer", "target": TARGET, "positions": [0], "direction": TENSOR, "strength": float("inf")},
        {"kind": "steer", "target": TARGET, "positions": [0], "direction": TENSOR, "strength": 100.1},
        {"kind": "fit_probe", "activations": TENSOR, "labels": TENSOR, "algorithm": "eval"},
        {"kind": "fit_probe", "activations": TENSOR, "labels": TENSOR, "algorithm": "ridge", "max_iterations": 10001},
    ],
)
def test_job_rejects_executable_or_out_of_range_operations(job_data, operation):
    job_data["operation"] = operation
    with pytest.raises(ValidationError):
        JobSpec.model_validate(job_data)


@pytest.mark.parametrize("field,value", [("max_runtime_seconds", 0), ("max_runtime_seconds", True), ("max_runtime_seconds", 86401), ("max_output_bytes", -1), ("max_cpu_cores", 33), ("max_vram_bytes", 49 * 1024**3), ("max_generated_tokens", 255)])
def test_declared_job_budgets_are_bounded(job_data, field, value):
    job_data["limits"][field] = value
    with pytest.raises(ValidationError):
        JobSpec.model_validate(job_data)


def test_job_stage_defaults_to_exploration(job_data):
    job_data["hypothesis_id"] = None
    assert JobSpec.model_validate(job_data).experiment_stage.value == "exploratory"


@pytest.mark.parametrize("stage", ["confirmatory", "replication"])
def test_registered_canonical_job_can_declare_confirmation_or_replication(job_data, stage):
    job_data["experiment_stage"] = stage
    assert JobSpec.model_validate(job_data).experiment_stage.value == stage


@pytest.mark.parametrize("stage", ["confirmatory", "replication"])
@pytest.mark.parametrize("path,value", [(("hypothesis_id",), None), (("model", "dtype"), "float32"), (("model", "quantized"), True)])
def test_confirmatory_job_requires_hypothesis_and_canonical_checkpoint(job_data, stage, path, value):
    job_data["experiment_stage"] = stage
    mutate(job_data, path, value)
    with pytest.raises(ValidationError):
        JobSpec.model_validate(job_data)


def test_hypothesis_registration_round_trip_and_terminal_evidence(hypothesis_data):
    draft = HypothesisRecord.model_validate(hypothesis_data)
    assert draft.status == HypothesisState.DRAFT
    registered = {**draft.model_dump(), "status": "FROZEN", "frozen_at": datetime.now(timezone.utc), "preregistration_hash": HASH}
    frozen = HypothesisRecord.model_validate(registered)
    assert HypothesisRecord.model_validate_json(frozen.model_dump_json()) == frozen
    for status in ("TESTING", "REPLICATING", "FALSIFIED"):
        assert HypothesisRecord.model_validate({**registered, "status": status}).status.value == status
    with pytest.raises(ValidationError):
        HypothesisRecord.model_validate({**registered, "status": "VALIDATED"})
    assert HypothesisRecord.model_validate({**registered, "status": "VALIDATED", "replication_ids": ["run-2"], "novelty_status": "N0"}).replication_ids == ("run-2",)


@pytest.mark.parametrize("changes", [{"status": "FROZEN"}, {"status": "TESTING"}, {"frozen_at": "2026-09-19T12:00:00Z", "preregistration_hash": HASH}, {"novelty_status": "novel"}, {"predicted_direction": "uncertain"}])
def test_hypothesis_rejects_inconsistent_registration(hypothesis_data, changes):
    with pytest.raises(ValidationError):
        HypothesisRecord.model_validate({**hypothesis_data, **changes})


def test_empty_prediction_cannot_be_frozen(hypothesis_data):
    hypothesis_data.update(status="FROZEN", frozen_at="2026-09-19T12:00:00Z", preregistration_hash=HASH, predictions=[])
    with pytest.raises(ValidationError):
        HypothesisRecord.model_validate(hypothesis_data)


def test_hypothesis_can_be_drafted_without_a_plan_but_cannot_be_frozen(hypothesis_data):
    hypothesis_data["preregistration_plan"] = None
    assert HypothesisRecord.model_validate(hypothesis_data).preregistration_plan is None
    hypothesis_data.update(status="FROZEN", frozen_at="2026-09-19T12:00:00Z", preregistration_hash=HASH)
    with pytest.raises(ValidationError):
        HypothesisRecord.model_validate(hypothesis_data)


@pytest.mark.parametrize("minimum_effect", [-0.01, float("nan"), float("inf"), 1e13, True])
def test_preregistered_minimum_effect_must_be_finite_nonnegative_and_bounded(hypothesis_data, minimum_effect):
    hypothesis_data["preregistration_plan"]["minimum_effect"] = minimum_effect
    with pytest.raises(ValidationError):
        HypothesisRecord.model_validate(hypothesis_data)


def test_preregistration_plan_is_typed_and_deeply_immutable(hypothesis_data):
    hypothesis = HypothesisRecord.model_validate(hypothesis_data)
    assert isinstance(hypothesis.preregistration_plan.operation, Capture)
    with pytest.raises(ValidationError):
        hypothesis.preregistration_plan.controls.random_component = False
    hypothesis_data["preregistration_plan"]["operation"]["callback"] = "eval"
    with pytest.raises(ValidationError):
        HypothesisRecord.model_validate(hypothesis_data)


def test_approval_token_is_redacted_and_never_dumped(approval_data):
    nonce = ApprovalNonce.model_validate(approval_data)
    secret = approval_data["token"]
    assert nonce.token.get_secret_value() == secret
    assert secret not in repr(nonce)
    assert secret not in nonce.model_dump_json()
    assert "token" not in nonce.model_dump()


@pytest.mark.parametrize("changes", [{"token": "short"}, {"token": "x" * 513}, {"max_runtime_seconds": 0}, {"price_ceiling_usd_per_hour": 1.5001}, {"price_ceiling_usd_per_hour": float("nan")}, {"price_ceiling_usd_per_hour": 0}, {"batch_hash": "main"}, {"issued_at": "2026-09-19T12:00:00"}, {"expires_at": "2026-09-19T11:59:00Z"}, {"expires_at": "2026-09-19T12:16:00Z"}])
def test_approval_rejects_unbounded_or_ambiguous_authority(approval_data, changes):
    with pytest.raises(ValidationError):
        ApprovalNonce.model_validate({**approval_data, **changes})
