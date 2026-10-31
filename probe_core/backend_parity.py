"""Version-one fixed numerical calibration, executed inside the normal supervisor.

All tensor comparisons are exact: each path uses the same eager model, operations
and dtype. This suite does not establish a scientific mechanism or held-out score.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile

from .schemas import Ablate, Capture, Patch, Steer
from .worker_contracts import WorkerRequestError


def run_parity(engine, request):
    import torch
    from safetensors.torch import save_file
    from transformers import GenerationConfig

    if request.spec.operation.kind != "backend_parity" or request.spec.experiment_stage.value != "calibration":
        raise WorkerRequestError("fixed backend parity requires a typed calibration request")
    if engine.config.model.repo != "probe/testing-tiny-qwen3" and (
        engine.config.device != "cuda:0" or engine.config.model.dtype != "bfloat16"
    ):
        raise WorkerRequestError("canonical parity acceptance requires actual BF16/CUDA")
    inputs, lengths = engine._inputs(request)
    if max(lengths) > 256:
        raise WorkerRequestError("backend parity prompts are limited to 256 actual input tokens")
    if len(lengths) < 2 or len(set(lengths)) < 2:
        raise WorkerRequestError("backend parity requires different real prompt lengths to exercise padding")
    model = engine.model
    expected_dtype = getattr(torch, engine.config.model.dtype)
    if any(parameter.dtype != expected_dtype for parameter in model.parameters()):
        raise WorkerRequestError("loaded parameter dtype differs from the approved identity")
    if engine.config.device == "cuda:0" and not torch.cuda.is_bf16_supported(including_emulation=False):
        raise WorkerRequestError("GPU lacks native BF16 support")
    checks = []
    tensors = {}
    generated = 0

    def deadline():
        if datetime.now(timezone.utc) >= request.deadline:
            raise TimeoutError("backend parity deadline reached")

    def compare(name, actual, expected):
        deadline()
        if not torch.isfinite(actual).all() or not torch.isfinite(expected).all():
            raise WorkerRequestError("backend parity found non-finite tensors")
        torch.testing.assert_close(actual, expected, rtol=0, atol=0)
        checks.append(
            {
                "name": name,
                "passed": True,
                "max_absolute_error": float((actual.float() - expected.float()).abs().max()),
                "rtol": 0,
                "atol": 0,
            }
        )

    prior = torch.are_deterministic_algorithms_enabled()
    torch.use_deterministic_algorithms(True)
    try:
        with torch.inference_mode():
            native = model(**inputs).logits[:, -1, :]
        reference, _ = engine.forward(inputs, lengths, backend="reference")
        traced, _ = engine.forward(inputs, lengths, backend="nnsight")
        compare("native_hf_vs_raw_hooks_noop", reference, native)
        compare("native_hf_vs_nnsight_noop", traced, native)
        tensors["baseline_logits"] = native.detach().cpu().float().contiguous()
        layer = model.config.num_hidden_layers // 2
        modules = [
            {"layer": layer, "component": name, **({"head": 0} if name == "attention_head" else {})}
            for name in ("residual", "attention_output", "mlp_output", "attention_head")
        ]
        capture = Capture(kind="capture", modules=modules, positions=("last",))
        _, raw_captures = engine.forward(inputs, lengths, capture, backend="reference", limits=request.spec.limits)
        _, captures = engine.forward(inputs, lengths, capture, backend="nnsight", limits=request.spec.limits)
        for name in captures:
            compare("capture_" + name, captures[name], raw_captures[name])
            tensors[name] = captures[name].detach().cpu().float().contiguous()
        root = Path(engine.config.tensor_directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="parity-", dir=root) as directory_name:
            directory = Path(directory_name)

            def artifact(name, tensor):
                path = directory / (name + ".safetensors")
                save_file({"value": tensor.detach().cpu().float().contiguous()}, str(path))
                return {
                    "path": str(path.relative_to(root)),
                    "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                    "tensor_name": "value",
                }

            for index, target in enumerate(modules):
                deadline()
                component = target["component"]
                key = f"layer_{layer}_{component}" + ("_0" if component == "attention_head" else "")
                source = artifact("patch-" + component, captures[key])
                patch = Patch(kind="patch", target=target, positions=("last",), source=source)
                patched, _ = engine.forward(inputs, lengths, patch, backend="nnsight", limits=request.spec.limits)
                compare("identity_patch_" + component, patched, native)
                ablate = Ablate(kind="ablate", target=target, positions=("last",), method="zero")
                raw, _ = engine.forward(inputs, lengths, ablate, backend="reference", limits=request.spec.limits)
                actual, _ = engine.forward(inputs, lengths, ablate, backend="nnsight", limits=request.spec.limits)
                compare("zero_ablation_" + component, actual, raw)
                if torch.equal(actual, native):
                    raise WorkerRequestError("calibration ablation unexpectedly made no observable logit change")
                checks.append({"name": "ablation_changes_logits_" + component, "passed": True})
                tensors["ablation_delta_" + component] = (actual - native).detach().cpu().float().contiguous()
                width = captures[key].shape[-1]
                direction = torch.arange(1, width + 1, dtype=torch.float32)
                direction /= direction.norm()
                reference = artifact("direction-" + component, direction)
                for strength in (0.0, 0.5):
                    steer = Steer(
                        kind="steer", target=target, positions=("last",), direction=reference, strength=strength
                    )
                    raw, _ = engine.forward(inputs, lengths, steer, backend="reference", limits=request.spec.limits)
                    actual, _ = engine.forward(inputs, lengths, steer, backend="nnsight", limits=request.spec.limits)
                    compare(f"steering_{component}_{strength}", actual, native if strength == 0 else raw)
            restored, _ = engine.forward(inputs, lengths, backend="reference")
            compare("all_intervention_hooks_removed", restored, native)
        generations = []
        for row, length in enumerate(lengths):
            deadline()
            prefix = inputs["input_ids"][row : row + 1, -length:].clone()
            settings = GenerationConfig(
                max_new_tokens=request.spec.inputs.generation.max_new_tokens,
                do_sample=False,
                use_cache=False,
                eos_token_id=model.config.eos_token_id,
                pad_token_id=model.config.pad_token_id or model.config.eos_token_id,
                bos_token_id=model.config.bos_token_id,
            )
            with torch.inference_mode():
                native_tokens = model.generate(
                    input_ids=prefix, attention_mask=torch.ones_like(prefix), generation_config=settings
                )[0, length:]
            produced = []
            current = prefix
            for _ in range(request.spec.inputs.generation.max_new_tokens):
                deadline()
                mask = torch.ones_like(current)
                logits, _ = engine.forward(
                    {
                        "input_ids": current,
                        "attention_mask": mask,
                        "position_ids": mask.cumsum(-1) - 1,
                        "use_cache": False,
                    },
                    (current.shape[1],),
                    backend="nnsight",
                )
                token = logits.argmax(-1)
                produced.append(int(token.item()))
                current = torch.cat((current, token.reshape(1, 1)), dim=1)
                eos = model.config.eos_token_id
                if produced[-1] in (eos if isinstance(eos, list) else [eos]):
                    break
            generated += len(produced) + len(native_tokens)
            if generated > request.spec.limits.max_generated_tokens:
                raise WorkerRequestError("native and traced generation exceeded the total token budget")
            actual_tokens = torch.tensor(produced, device=native_tokens.device, dtype=native_tokens.dtype)
            compare("greedy_generation_" + str(row), actual_tokens, native_tokens)
            generations.append({"prompt_id": request.spec.inputs.prompt_ids[row], "generated_token_ids": produced})
        return (
            tensors,
            {
                "suite": "backend_parity_v1",
                "passed": True,
                "scientific_evidence": False,
                "device": engine.config.device,
                "dtype": engine.config.model.dtype,
                "input_lengths": list(lengths),
                "chat_template_hash": engine.config.model.chat_template_hash,
                "thinking_mode": engine.config.model.thinking_mode,
                "checks": checks,
                "generations": generations,
                "generated_tokens_including_reference": generated,
            },
            generated,
        )
    finally:
        torch.use_deterministic_algorithms(prior)
