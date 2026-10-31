"""Run a frozen multi-step recipe inside one loaded model.

Order of work, all under deterministic algorithms and ``inference_mode``:

1. Baseline: native Hugging Face logits for every prompt.
2. No-op check: the configured backend's unhooked forward, and an identity patch
   at every intervention target, must reproduce the baseline logits exactly.
   A failure raises before any intervention result exists.
3. Steps in order. Captures are kept in memory by step name so later steps can
   consume them. Each intervention step's metrics are computed per prompt
   against the baseline.

Every prompt is reported; nothing is filtered by effect size. Results are
exploratory measurements on the declared prompts, not held-out evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
from typing import Any

from .audit import canonical_json
from .schemas import Recipe, capture_key
from .worker_contracts import WorkerRequestError


def recipe_hash(recipe: Recipe) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(recipe.model_dump(mode="json")).encode()).hexdigest()


def _metric_values(metric, logits, baseline, prompts):
    import torch

    values = logits.float()
    reference = baseline.float()
    target = torch.tensor([p.target_token_id for p in prompts], device=values.device)
    alternative = torch.tensor([p.alternative_token_id for p in prompts], device=values.device)
    rows = torch.arange(values.shape[0], device=values.device)
    if metric.kind == "logit_diff":
        return (values[rows, target] - values[rows, alternative]).tolist()
    if metric.kind == "log_prob":
        return torch.log_softmax(values, dim=-1)[rows, target].tolist()
    if metric.kind == "kl_to_baseline":
        base = torch.log_softmax(reference, dim=-1)
        condition = torch.log_softmax(values, dim=-1)
        return (base.exp() * (base - condition)).sum(-1).tolist()
    top = torch.topk(values, metric.k, dim=-1)
    return [{"token_ids": ids, "logits": scores} for ids, scores in zip(top.indices.tolist(), top.values.tolist())]


def _report(metric, condition, baseline_values, prompts):
    if metric.kind == "top_k_tokens":
        return {
            "kind": metric.kind,
            "per_prompt": [
                {"prompt_id": p.prompt_id, "baseline": b, "condition": c}
                for p, b, c in zip(prompts, baseline_values, condition)
            ],
        }
    rows = [
        {"prompt_id": p.prompt_id, "baseline": b, "condition": c, "delta": c - b}
        for p, b, c in zip(prompts, baseline_values, condition)
    ]
    count = len(rows)
    return {
        "kind": metric.kind,
        "per_prompt": rows,
        "mean_baseline": sum(row["baseline"] for row in rows) / count,
        "mean_condition": sum(row["condition"] for row in rows) / count,
        "mean_delta": sum(row["delta"] for row in rows) / count,
    }


def run_recipe(engine, request, *, backend: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    import torch

    operation = request.spec.operation
    if operation.kind != "recipe":
        raise WorkerRequestError("recipe runner requires a recipe operation")
    recipe = operation.recipe
    limits = request.spec.limits
    inputs, lengths = engine._inputs(request)
    model = engine.model
    width = inputs["input_ids"].shape[1]
    device = engine.config.device
    dtype = next(model.parameters()).dtype
    prompts = recipe.prompts
    vocabulary = model.config.vocab_size
    if any(max(p.target_token_id, p.alternative_token_id) >= vocabulary for p in prompts):
        raise WorkerRequestError("a metric token is outside the loaded model vocabulary")

    def deadline():
        if datetime.now(timezone.utc) >= request.deadline:
            raise TimeoutError("recipe deadline reached")

    def run(op=None, prepared=None):
        deadline()
        return engine.forward(inputs, lengths, op, backend=backend, limits=limits, prepared=prepared)

    def selected(positions):
        return engine._indices(lengths, positions, width, device)

    tensors: dict[str, Any] = {}
    checks = []
    prior = torch.are_deterministic_algorithms_enabled()
    torch.use_deterministic_algorithms(True)
    try:
        with torch.inference_mode():
            baseline = model(**inputs).logits[:, -1, :].detach().clone()
        tensors["baseline_logits"] = baseline.cpu().float().contiguous()

        def exact(name, actual):
            passed = bool(torch.equal(actual, baseline))
            checks.append({"name": name, "passed": passed})
            return passed

        exact("unhooked_forward_matches_baseline", run()[0])
        # Identity patch at every intervention target with its unmodified value.
        unmodified = {}
        for step in recipe.steps:
            op = step.operation
            if op.kind == "capture":
                continue
            key = (capture_key(op.target), op.positions)
            if key in unmodified:
                continue
            probe = SimpleNamespace(kind="capture", modules=(op.target,), positions=op.positions)
            _, captured = run(probe)
            value = captured[capture_key(op.target)]
            unmodified[key] = value
            identity = SimpleNamespace(kind="patch", target=op.target, positions=op.positions)
            _, _, head_slice = engine._target(op.target)
            logits, _ = run(identity, (selected(op.positions), head_slice, value))
            exact(f"identity_patch_{key[0]}_matches_baseline", logits)
        if not all(check["passed"] for check in checks):
            raise WorkerRequestError("no-op hook check failed; no intervention results are reported")

        baseline_values = {m.name: _metric_values(m, baseline, baseline, prompts) for m in recipe.metrics}
        outputs: dict[str, dict[str, Any]] = {}
        steps = []
        for step in recipe.steps:
            op = step.operation
            if op.kind == "capture":
                logits, captured = run(SimpleNamespace(kind="capture", modules=op.modules, positions=op.positions))
                if not torch.equal(logits, baseline):
                    raise WorkerRequestError("a capture step changed the logits")
                outputs[step.name] = captured
                for key, value in captured.items():
                    tensors[f"{step.name}__{key}"] = value.cpu().float().contiguous()
                steps.append({"name": step.name, "kind": op.kind, "captured": sorted(captured)})
                continue
            _, _, head_slice = engine._target(op.target)
            feature = model.config.head_dim if head_slice else model.config.hidden_size
            indices = selected(op.positions)
            details: dict[str, Any] = {}
            if op.kind == "zero_ablate":
                edit, replacement = SimpleNamespace(kind="ablate", method="zero"), None
            elif op.kind == "mean_ablate":
                source = outputs[op.baseline.step][op.baseline.tensor]
                replacement = source.float().mean(dim=(0, 1)).to(dtype)
                edit = SimpleNamespace(kind="ablate", method="mean")
            elif op.kind == "patch":
                replacement = outputs[op.source.step][op.source.tensor]
                edit = SimpleNamespace(kind="patch")
            elif op.kind == "steer":
                source = outputs[op.direction.step][op.direction.tensor]
                replacement = source.float().mean(dim=(0, 1)).to(dtype)
                edit = SimpleNamespace(kind="steer", strength=op.strength)
            else:
                original = unmodified[(capture_key(op.target), op.positions)].float()
                generator = torch.Generator(device="cpu").manual_seed(op.seed)
                noise = torch.randn(original.shape, generator=generator, dtype=torch.float32).to(original.device)
                norms = original.norm(dim=-1, keepdim=True)
                replacement = (noise / noise.norm(dim=-1, keepdim=True) * norms).to(dtype)
                edit = SimpleNamespace(kind="steer", strength=1.0)
                details["displacement_norms"] = norms.squeeze(-1).cpu().tolist()
            if replacement is not None and replacement.shape[-1] != feature:
                raise WorkerRequestError("a referenced tensor does not match the target's feature width")
            edit.target, edit.positions = op.target, op.positions
            logits, _ = run(edit, (indices, head_slice, replacement))
            if not torch.isfinite(logits).all():
                raise WorkerRequestError("an intervention produced non-finite logits")
            if step.retain_logits:
                tensors[f"{step.name}__logits"] = logits.cpu().float().contiguous()
            metrics = {
                m.name: _report(m, _metric_values(m, logits, baseline, prompts), baseline_values[m.name], prompts)
                for m in recipe.metrics
            }
            steps.append(
                {
                    "name": step.name,
                    "kind": op.kind,
                    "role": "primary"
                    if step.name == recipe.primary_step
                    else "control"
                    if step.name in recipe.control_steps
                    else "secondary",
                    "target": capture_key(op.target),
                    "positions": list(op.positions),
                    "logits_changed": not bool(torch.equal(logits, baseline)),
                    "metrics": metrics,
                    **details,
                }
            )
    finally:
        torch.use_deterministic_algorithms(prior)
    primary = next(step for step in steps if step["name"] == recipe.primary_step)
    summary = {
        "suite": "recipe",
        "recipe_id": recipe.recipe_id,
        "recipe_version": recipe.version,
        "recipe_sha256": recipe_hash(recipe),
        "experiment_stage": request.spec.experiment_stage.value,
        # Only the trusted evaluator can produce validated evidence.
        "scientific_evidence": False,
        "evidence_label": "exploratory" if request.spec.experiment_stage.value == "exploratory" else "calibration",
        "heldout": False,
        "noop_checks": checks,
        "prompt_ids": [p.prompt_id for p in prompts],
        "input_lengths": list(lengths),
        "steps": steps,
        "primary": {
            "step": recipe.primary_step,
            "metric": recipe.primary_metric,
            "mean_delta": primary["metrics"][recipe.primary_metric]["mean_delta"],
        },
        "controls": {
            name: next(step for step in steps if step["name"] == name)["metrics"][recipe.primary_metric]["mean_delta"]
            for name in recipe.control_steps
        },
    }
    return tensors, summary
