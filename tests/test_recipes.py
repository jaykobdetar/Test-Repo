"""Recipe runner tests on the real, randomly initialized tiny Qwen3 fixture."""

import json

import pytest
import torch
from pydantic import ValidationError
from safetensors.torch import load_file

from probe_core.recipe_runner import run_recipe
from probe_core.schemas import Ablate, JobSpec, Recipe
from probe_core.worker import WorkerEngine
from probe_core.worker_contracts import WorkerRequestError
from test_worker import make_request, tiny_bundle  # noqa: F401

LAYER = 1
MLP = {"layer": LAYER, "component": "mlp_output"}


def recipe(**overrides):
    body = {
        "recipe_id": "tiny_recipe",
        "version": 1,
        "description": "Fixture recipe exercising every step kind.",
        "prompts": [
            {"prompt_id": "short", "target_token_id": 5, "alternative_token_id": 9},
            {"prompt_id": "long", "target_token_id": 11, "alternative_token_id": 3},
        ],
        "steps": [
            {"name": "clean", "operation": {"kind": "capture", "modules": [MLP], "positions": ["last"]}},
            {
                "name": "patch_back",
                "operation": {
                    "kind": "patch",
                    "target": MLP,
                    "positions": ["last"],
                    "source": {"step": "clean", "tensor": f"layer_{LAYER}_mlp_output"},
                },
            },
            {
                "name": "zero",
                "operation": {"kind": "zero_ablate", "target": MLP, "positions": ["last"]},
                "retain_logits": True,
            },
            {
                "name": "mean",
                "operation": {
                    "kind": "mean_ablate",
                    "target": MLP,
                    "positions": ["last"],
                    "baseline": {"step": "clean", "tensor": f"layer_{LAYER}_mlp_output"},
                },
            },
            {
                "name": "random_control",
                "operation": {"kind": "random_norm_matched", "target": MLP, "positions": ["last"], "seed": 7},
            },
        ],
        "metrics": [
            {"kind": "logit_diff", "name": "agreement"},
            {"kind": "log_prob", "name": "target_log_prob"},
            {"kind": "kl_to_baseline", "name": "kl"},
            {"kind": "top_k_tokens", "name": "top", "k": 3},
        ],
        "primary_metric": "agreement",
        "primary_step": "zero",
        "control_steps": ["random_control"],
        "seed": 1,
    }
    body.update(overrides)
    return body


@pytest.fixture
def run(tiny_bundle, make_request):
    config, _ = tiny_bundle

    def execute(body=None, *, backend=None, engine=None):
        request = make_request({"kind": "recipe", "recipe": body or recipe()}, key="recipe-job")
        engine = engine or WorkerEngine(config)
        return run_recipe(engine, request, backend=backend), engine, request

    return execute


def step(summary, name):
    return next(item for item in summary["steps"] if item["name"] == name)


def test_step_two_consumes_step_one_and_identity_patch_is_exact(run):
    (tensors, summary), _, _ = run()
    assert all(check["passed"] for check in summary["noop_checks"]) and len(summary["noop_checks"]) == 2
    patched = step(summary, "patch_back")
    assert patched["logits_changed"] is False
    assert all(row["delta"] == 0 for row in patched["metrics"]["agreement"]["per_prompt"])
    assert step(summary, "mean")["logits_changed"] is True
    assert f"clean__layer_{LAYER}_mlp_output" in tensors and "zero__logits" in tensors
    assert tensors[f"clean__layer_{LAYER}_mlp_output"].shape == (2, 1, 32)


def test_reference_hooks_and_nnsight_produce_identical_recipes(run):
    (raw_tensors, raw), engine, _ = run(backend="reference")
    (traced_tensors, traced), _, _ = run(backend="nnsight", engine=engine)
    assert json.dumps(raw, sort_keys=True) == json.dumps(traced, sort_keys=True)
    assert raw_tensors.keys() == traced_tensors.keys()
    for name in raw_tensors:
        torch.testing.assert_close(traced_tensors[name], raw_tensors[name], rtol=0, atol=0)


def test_metrics_match_an_independent_zero_ablation(run, make_request):
    (tensors, summary), engine, request = run()
    inputs, lengths = engine._inputs(request)
    ablate = Ablate(kind="ablate", target=MLP, positions=("last",), method="zero")
    expected, _ = engine.forward(inputs, lengths, ablate, limits=request.spec.limits)
    torch.testing.assert_close(tensors["zero__logits"], expected.float(), rtol=0, atol=0)
    base = tensors["baseline_logits"]
    zero = step(summary, "zero")["metrics"]
    for row, index, (target, alternative) in zip(zero["agreement"]["per_prompt"], range(2), [(5, 9), (11, 3)]):
        assert row["baseline"] == pytest.approx(float(base[index, target] - base[index, alternative]))
        assert row["condition"] == pytest.approx(float(expected[index, target] - expected[index, alternative]))
        log_probs = torch.log_softmax(expected.float()[index], -1)
        assert zero["target_log_prob"]["per_prompt"][index]["condition"] == pytest.approx(float(log_probs[target]))
    assert all(row["condition"] > 0 for row in zero["kl"]["per_prompt"])
    assert summary["primary"] == {"step": "zero", "metric": "agreement", "mean_delta": zero["agreement"]["mean_delta"]}
    assert summary["controls"]["random_control"] == step(summary, "random_control")["metrics"]["agreement"]["mean_delta"]
    assert len(zero["top"]["per_prompt"][0]["condition"]["token_ids"]) == 3


def test_random_control_matches_the_zero_ablation_displacement_norm(run):
    (tensors, summary), _, _ = run()
    clean = tensors[f"clean__layer_{LAYER}_mlp_output"]
    norms = step(summary, "random_control")["displacement_norms"]
    torch.testing.assert_close(torch.tensor(norms), clean.norm(dim=-1), rtol=1e-6, atol=1e-6)
    assert step(summary, "random_control")["logits_changed"] is True
    (_, again), _, _ = run()
    assert again == summary


def test_failed_noop_check_reports_no_intervention_results(run, monkeypatch):
    original = WorkerEngine.forward

    def perturbed(self, inputs, lengths, operation=None, **kwargs):
        logits, captures = original(self, inputs, lengths, operation, **kwargs)
        if operation is not None and operation.kind == "patch":
            logits = logits + 1e-3
        return logits, captures

    monkeypatch.setattr(WorkerEngine, "forward", perturbed)
    with pytest.raises(WorkerRequestError, match="no-op hook check failed"):
        run()


def test_execute_writes_manifest_and_summary(tiny_bundle, make_request, tmp_path):
    config, _ = tiny_bundle
    request = make_request({"kind": "recipe", "recipe": recipe()}, key="recipe-execute")
    receipt = WorkerEngine(config).execute(request, tmp_path / "out")
    assert receipt.manifest.experiment.tool == "recipe"
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["recipe_id"] == "tiny_recipe" and summary["evidence_label"] == "calibration"
    assert summary["scientific_evidence"] is False
    assert "baseline_logits" in load_file(tmp_path / "out" / "tensors.safetensors")


@pytest.mark.parametrize(
    "change,message",
    [
        ({"primary_step": "clean"}, "primary step must be an intervention"),
        ({"primary_metric": "top"}, "primary metric must be a declared scalar"),
        ({"control_steps": ["zero"]}, "control steps"),
        ({"steps_reverse": True}, "earlier step"),
        ({"tensor": "layer_0_mlp_output"}, "earlier step"),
        ({"patch_positions": [0]}, "exactly the patched positions"),
        ({"duplicate_step": "baseline"}, "not reserved"),
    ],
)
def test_recipe_schema_rejects_unresolvable_or_ambiguous_recipes(change, message):
    body = recipe()
    if "steps_reverse" in change:
        body["steps"] = body["steps"][1:] + body["steps"][:1]
    elif "tensor" in change:
        body["steps"][1]["operation"]["source"]["tensor"] = change["tensor"]
    elif "patch_positions" in change:
        body["steps"][1]["operation"]["positions"] = change["patch_positions"]
    elif "duplicate_step" in change:
        body["steps"][0]["name"] = change["duplicate_step"]
    else:
        body.update(change)
    with pytest.raises(ValidationError, match=message):
        Recipe.model_validate(body)


def test_job_prompts_must_match_recipe_and_stage_is_not_confirmatory(make_request):
    spec = make_request({"kind": "recipe", "recipe": recipe()}).spec.model_dump(mode="json")
    reordered = dict(spec, inputs=dict(spec["inputs"], prompt_ids=["long", "short"]))
    with pytest.raises(ValidationError, match="ordered prompt IDs"):
        JobSpec.model_validate(reordered)
    confirmatory = dict(spec, experiment_stage="confirmatory", hypothesis_id="H-1")
    with pytest.raises(ValidationError):
        JobSpec.model_validate(confirmatory)
