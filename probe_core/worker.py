"""Trusted bounded Qwen executor, process supervisor, and loopback HTTP service.

Interventions address the *prefill* pass only. Positions are zero-based within each
unpadded prompt; ``last`` means its last real token. Padding is on the left and
never selected. ``residual`` is decoder-layer output after both residual adds;
``attention_output`` is attention output after o_proj; ``mlp_output`` is MLP output;
``attention_head`` is one query-head slice immediately BEFORE o_proj. Captures use
[B, selected_positions, feature_width]. Mean/variance reduce the batch axis only.
Generation is a separate primitive with no hidden intervention and a hard token cap.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import atexit
import ctypes
import errno
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import resource
import re
import struct
import tempfile
import signal
import stat
import threading
import time
import warnings
from typing import Any
from urllib.parse import urlsplit

from .audit import canonical_json
from .ledger import Ledger
from .schemas import RunManifest
from .worker_contracts import (
    ExecutionReceipt, ExecutionRequest, PromptDataset, WorkerBusyError, WorkerConfig,
    WorkerRequestError, WorkerState,
)

UTC = timezone.utc
_NNSIGHT_CLEANUP_REGISTERED = False


def _nnsight_module():
    """Contain two known NNsight0.7/Python3.13 compatibility issues.

    astor enumerates deprecated AST aliases while importing; it does not use them
    in our trace body. NNsight owns a global devnull stream without a close hook.
    Neither workaround changes tracing or model numerical operations.
    """
    global _NNSIGHT_CLEANUP_REGISTERED
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"ast\.(Num|Str|Bytes|NameConstant|Ellipsis).*deprecated.*", category=DeprecationWarning)
        import nnsight
    if not _NNSIGHT_CLEANUP_REGISTERED:
        from nnsight.intervention.tracing import util
        stream = getattr(util, "_devnull", None)
        if stream is not None:
            atexit.register(stream.close)
        _NNSIGHT_CLEANUP_REGISTERED = True
    return nnsight


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def prompt_set_hash(dataset: PromptDataset, prompt_ids: tuple[str, ...]) -> str:
    by_id = {prompt.prompt_id: prompt for prompt in dataset.prompts}
    try:
        selected = [by_id[key].model_dump(mode="json") for key in prompt_ids]
    except KeyError as exc:
        raise WorkerRequestError("unknown prompt identifier") from exc
    return "sha256:" + hashlib.sha256(canonical_json(selected).encode()).hexdigest()


def _path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")) or "\\" in relative:
        raise WorkerRequestError("unsafe asset path")
    current = root.absolute()
    if current.is_symlink():
        raise WorkerRequestError("asset roots cannot be symlinks")
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            raise WorkerRequestError("asset symlinks are forbidden")
    info = current.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkerRequestError("asset must be an unshared regular file")
    return current


def _json_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(canonical_json(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class WorkerEngine:
    """Synchronous numerical engine. Public execution goes through Supervisor.

    This in-process entry point is also used by numerical parity tests. It does
    not claim process isolation; the Supervisor establishes that boundary.
    """
    def __init__(self, config: WorkerConfig):
        self.config = WorkerConfig.model_validate_json(config.model_dump_json())
        self.model = None
        self.tokenizer = None
        self._verify_model_bundle()

    def _verify_model_bundle(self) -> None:
        root = Path(self.config.model_directory)
        specified = {entry.path: entry.sha256 for entry in self.config.assets}
        if "config.json" not in specified:
            raise WorkerRequestError("the model configuration must be pinned by content hash")
        weights = sorted(root.glob("*.safetensors"))
        if not weights or sorted(sha256_file(path) for path in weights) != sorted(self.config.model.local_weight_hashes):
            raise WorkerRequestError("model safetensors hashes do not match the approved identity")
        for entry in self.config.assets:
            path = _path(root, entry.path)
            if sha256_file(path) != entry.sha256:
                raise WorkerRequestError("model or tokenizer asset checksum mismatch")
        if any(path.name not in specified for path in weights):
            raise WorkerRequestError("all model shards must appear in the pinned asset inventory")
        if any(root.glob("*.bin")) or any(root.glob("*.pt")) or any(root.glob("*.pth")):
            raise WorkerRequestError("executable/pickle model formats are forbidden")
        metadata = json.loads((root / "config.json").read_text())
        if metadata.get("model_type") != "qwen3" or metadata.get("auto_map"):
            raise WorkerRequestError("worker supports local standard Qwen3 models only")
        if self.config.model.quantized or metadata.get("quantization_config"):
            raise WorkerRequestError("quantized model execution has no validated backend")
        if self.config.model.repo != "probe/testing-tiny-qwen3":
            if metadata.get("num_hidden_layers") != 28 or metadata.get("num_attention_heads") != 16 or metadata.get("num_key_value_heads") != 8:
                raise WorkerRequestError("canonical Qwen3-1.7B architecture mismatch")
        self.model_config = metadata

    def _load_model(self, spec) -> Any:
        if self.model is not None:
            return self.model
        import torch
        from transformers import AutoModelForCausalLM
        if self.config.device.startswith("cuda"):
            if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
                raise WorkerRequestError("worker requires exactly one visible CUDA device")
            total = torch.cuda.get_device_properties(0).total_memory
            if spec.limits.max_vram_bytes > total:
                raise WorkerRequestError("declared VRAM limit exceeds available device capacity")
            torch.cuda.set_per_process_memory_fraction(spec.limits.max_vram_bytes / total, 0)
            torch.cuda.reset_peak_memory_stats(0)
        self.model = AutoModelForCausalLM.from_pretrained(
            self.config.model_directory, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, dtype=getattr(torch, self.config.model.dtype),
            attn_implementation="eager",
        ).to(self.config.device).eval()
        if any(parameter.requires_grad for parameter in self.model.parameters()):
            self.model.requires_grad_(False)
        return self.model

    def _dataset(self, request: ExecutionRequest):
        revision = request.spec.inputs.dataset_revision
        entry = next((item for item in self.config.datasets if item.sha256 == revision), None)
        if entry is None:
            raise WorkerRequestError("dataset is not registered with this worker")
        path = Path(entry.path)
        if path.is_symlink() or sha256_file(path) != revision:
            raise WorkerRequestError("dataset checksum mismatch")
        dataset = PromptDataset.model_validate_json(path.read_text())
        ids = request.spec.inputs.prompt_ids
        if prompt_set_hash(dataset, ids) != request.spec.inputs.prompt_set_hash:
            raise WorkerRequestError("ordered prompt set does not match its declared hash")
        by_id = {p.prompt_id: p for p in dataset.prompts}
        return [by_id[key] for key in ids]

    def _inputs(self, request: ExecutionRequest):
        import torch
        records = self._dataset(request)
        if any(prompt.text is not None for prompt in records):
            specified = {asset.path for asset in self.config.assets}
            if not {"tokenizer.json", "tokenizer_config.json"} <= specified:
                raise WorkerRequestError("text prompts require a pinned tokenizer inventory")
            if self.tokenizer is None:
                from transformers import AutoTokenizer
                self.tokenizer = AutoTokenizer.from_pretrained(self.config.model_directory, local_files_only=True, trust_remote_code=False)
        uses_chat = self.config.model.chat_template_hash is not None or self.config.model.thinking_mode is not None
        if uses_chat:
            if any(prompt.token_ids is not None for prompt in records):
                raise WorkerRequestError("chat/thinking provenance cannot be verified for caller-supplied token IDs")
            template = self.tokenizer.chat_template
            if not isinstance(template, str) or "sha256:" + hashlib.sha256(template.encode()).hexdigest() != self.config.model.chat_template_hash:
                raise WorkerRequestError("active tokenizer chat template does not match its pinned hash")
            tokens = [self.tokenizer.apply_chat_template([{"role": "user", "content": prompt.text}], tokenize=True, return_dict=False, add_generation_prompt=True, enable_thinking=self.config.model.thinking_mode) for prompt in records]
        else:
            tokens = [list(prompt.token_ids) if prompt.token_ids is not None else self.tokenizer.encode(prompt.text, add_special_tokens=False) for prompt in records]
        model = self._load_model(request.spec)
        cap = min(32768, model.config.max_position_embeddings)
        generated = request.spec.inputs.generation.max_new_tokens if request.spec.operation.kind == "generate" else 0
        if any(not row or len(row) + generated > cap for row in tokens):
            raise WorkerRequestError("prompt plus generation exceeds the model context limit")
        if any(token < 0 or token >= model.config.vocab_size for row in tokens for token in row):
            raise WorkerRequestError("input token outside the loaded model vocabulary")
        width = max(map(len, tokens))
        # Reject prefill allocations that cannot plausibly fit (eager attention is quadratic).
        estimated = len(tokens) * width * width * model.config.num_attention_heads * 4 + len(tokens) * width * model.config.vocab_size * 4
        limit = request.spec.limits.max_vram_bytes if self.config.device.startswith("cuda") else request.spec.limits.max_ram_bytes
        if estimated > limit // 2:
            raise WorkerRequestError("prefill attention/logit estimate exceeds the reserved memory budget")
        pad = model.config.pad_token_id or 0
        input_ids = torch.full((len(tokens), width), pad, dtype=torch.long, device=self.config.device)
        mask = torch.zeros_like(input_ids)
        for index, row in enumerate(tokens):
            input_ids[index, -len(row):] = torch.tensor(row, dtype=torch.long, device=self.config.device)
            mask[index, -len(row):] = 1
        positions = mask.cumsum(-1) - 1
        positions.masked_fill_(mask == 0, 0)
        return {"input_ids": input_ids, "attention_mask": mask, "position_ids": positions, "use_cache": False}, tuple(map(len, tokens))

    def _target(self, reference):
        model = self.model
        if reference.layer >= len(model.model.layers):
            raise WorkerRequestError("layer does not exist in the loaded model")
        base = f"model.layers.{reference.layer}"
        if reference.component == "residual":
            return base, "output", None
        if reference.component == "mlp_output":
            return base + ".mlp", "output", None
        if reference.component == "attention_output":
            return base + ".self_attn", "output", None
        heads = model.config.num_attention_heads
        if reference.head is None or reference.head >= heads:
            raise WorkerRequestError("query head does not exist in the loaded model")
        width = model.config.head_dim
        return base + ".self_attn.o_proj", "input", (reference.head * width, (reference.head + 1) * width)

    @staticmethod
    def _indices(lengths, positions, width, device):
        import torch
        selected = []
        for length in lengths:
            logical = [length - 1 if value == "last" else value for value in positions]
            if len(set(logical)) != len(logical) or any(value >= length for value in logical):
                raise WorkerRequestError("positions must select distinct real prompt tokens")
            selected.append([width - length + value for value in logical])
        return torch.tensor(selected, dtype=torch.long, device=device)

    def _tensor_asset(self, reference, limit: int):
        import torch
        from safetensors import safe_open
        path = _path(Path(self.config.tensor_directory), reference.path)
        if path.suffix != ".safetensors" or path.stat().st_size > limit or sha256_file(path) != reference.sha256:
            raise WorkerRequestError("tensor artifact format, size or checksum rejected")
        with safe_open(path, framework="pt", device="cpu") as stream:
            if reference.tensor_name not in stream.keys():
                raise WorkerRequestError("named tensor does not exist")
            tensor = stream.get_tensor(reference.tensor_name)
        if tensor.dtype not in {torch.float16, torch.float32, torch.float64, torch.bfloat16, torch.int64, torch.int32}:
            raise WorkerRequestError("unsupported tensor dtype")
        if tensor.is_floating_point() and not torch.isfinite(tensor).all():
            raise WorkerRequestError("tensor contains non-finite values")
        return tensor

    def _intervention(self, operation, lengths, input_width, limits):
        import torch
        target = operation.target
        _, _, head_slice = self._target(target)
        width = self.model.config.head_dim if head_slice else self.model.config.hidden_size
        selected = self._indices(lengths, operation.positions, input_width, self.config.device)
        shape = (len(lengths), len(operation.positions), width)
        value = None
        if operation.kind == "patch":
            value = self._tensor_asset(operation.source, limits.max_ram_bytes)
            if tuple(value.shape) != shape or not value.is_floating_point():
                raise WorkerRequestError("patch must be floating [batch, positions, feature_width]")
        elif operation.kind == "steer":
            value = self._tensor_asset(operation.direction, limits.max_ram_bytes)
            if tuple(value.shape) != (width,) or not value.is_floating_point():
                raise WorkerRequestError("steering direction must be floating [feature_width]")
        elif operation.method == "mean":
            value = self._tensor_asset(operation.baseline, limits.max_ram_bytes)
            if tuple(value.shape) != (width,) or not value.is_floating_point():
                raise WorkerRequestError("mean-ablation baseline must be floating [feature_width]")
        if value is not None:
            value = value.to(device=self.config.device, dtype=next(self.model.parameters()).dtype)
            if not torch.isfinite(value).all():
                raise WorkerRequestError("tensor overflows the execution dtype")
        return selected, head_slice, value

    @staticmethod
    def _edit(value, operation, selected, head_slice, replacement):
        import torch
        tensor = value[0] if isinstance(value, tuple) else value
        changed = tensor.clone()
        batch = torch.arange(tensor.shape[0], device=tensor.device)[:, None]
        start, stop = head_slice if head_slice else (0, tensor.shape[-1])
        if operation.kind == "steer":
            changed[batch, selected, start:stop] += operation.strength * replacement
        elif replacement is None:
            changed[batch, selected, start:stop] = 0
        else:
            changed[batch, selected, start:stop] = replacement
        return (changed, *value[1:]) if isinstance(value, tuple) else changed

    @staticmethod
    def _capture(value, selected, head_slice):
        import torch
        tensor = value[0] if isinstance(value, tuple) else value
        rows = torch.arange(tensor.shape[0], device=tensor.device)[:, None]
        captured = tensor[rows, selected]
        if head_slice:
            captured = captured[..., head_slice[0]:head_slice[1]]
        return captured.detach().clone()

    def forward(self, inputs, lengths, operation=None, *, backend=None, limits=None):
        """Return last-real-token logits and optional selected activations."""
        import torch
        backend = backend or self.config.backend
        captures = {}
        target = None
        editing = operation is not None and operation.kind in {"patch", "ablate", "steer"}
        if editing:
            target = self._target(operation.target)
            edit_parameters = self._intervention(operation, lengths, inputs["input_ids"].shape[1], limits)
        refs = []
        if operation is not None and operation.kind == "capture":
            rank = {"attention_head": 0, "attention_output": 1, "mlp_output": 2, "residual": 3}
            refs = sorted(operation.modules, key=lambda item: (item.layer, rank[item.component], item.head or 0))
        with torch.inference_mode():
            if backend == "reference":
                handles = []
                try:
                    if editing:
                        name, direction, _ = target
                        module = self.model.get_submodule(name)
                        if direction == "input":
                            handles.append(module.register_forward_pre_hook(lambda module, args: (self._edit(args[0], operation, *edit_parameters), *args[1:])))
                        else:
                            handles.append(module.register_forward_hook(lambda module, args, output: self._edit(output, operation, *edit_parameters)))
                    for reference in refs:
                        name, direction, head_slice = self._target(reference)
                        selected = self._indices(lengths, operation.positions, inputs["input_ids"].shape[1], self.config.device)
                        key = f"layer_{reference.layer}_{reference.component}" + (f"_{reference.head}" if reference.head is not None else "")
                        def capture_hook(module, args, output=None, *, key=key, selected=selected, head_slice=head_slice, direction=direction):
                            captures[key] = self._capture(args[0] if direction == "input" else output, selected, head_slice)
                        module = self.model.get_submodule(name)
                        handles.append(module.register_forward_pre_hook(capture_hook) if direction == "input" else module.register_forward_hook(capture_hook))
                    logits = self.model(**inputs).logits[:, -1, :].detach().clone()
                finally:
                    for handle in handles:
                        handle.remove()
            else:
                nnsight = _nnsight_module()
                wrapped = nnsight.NNsight(self.model)
                # NNsight interprets this data-only trusted code; no request supplies
                # a callback, Python source, pickled object or remote trace target.
                with wrapped.trace(**inputs):
                    if editing:
                        envoy = wrapped
                        for component in target[0].split("."):
                            envoy = envoy[int(component)] if component.isdigit() else getattr(envoy, component)
                        if target[1] == "input":
                            envoy.input = self._edit(envoy.input, operation, *edit_parameters)
                        else:
                            envoy.output = self._edit(envoy.output, operation, *edit_parameters)
                    for reference in refs:
                        name, direction, head_slice = self._target(reference)
                        envoy = wrapped
                        for component in name.split("."):
                            envoy = envoy[int(component)] if component.isdigit() else getattr(envoy, component)
                        selected = self._indices(lengths, operation.positions, inputs["input_ids"].shape[1], self.config.device)
                        key = f"layer_{reference.layer}_{reference.component}" + (f"_{reference.head}" if reference.head is not None else "")
                        captures[key] = self._capture(envoy.input if direction == "input" else envoy.output, selected, head_slice)
                    logits = wrapped.output.logits[:, -1, :].save()
                    captures = nnsight.save(captures)
        return logits, captures

    def _fit_probe(self, request):
        import torch
        operation = request.spec.operation
        features = self._tensor_asset(operation.activations, request.spec.limits.max_ram_bytes).float()
        labels = self._tensor_asset(operation.labels, request.spec.limits.max_ram_bytes)
        if features.ndim != 2 or features.shape[0] < 4 or features.shape[1] < 1 or labels.shape[0] != features.shape[0]:
            raise WorkerRequestError("probe requires aligned [N,D] activations and at least four rows")
        if features.shape[1] ** 2 * 8 > request.spec.limits.max_ram_bytes // 2:
            raise WorkerRequestError("probe design matrix exceeds memory budget")
        generator = torch.Generator().manual_seed(request.spec.inputs.random_seed)
        order = torch.randperm(features.shape[0], generator=generator)
        split = max(1, min(len(order) - 1, int(len(order) * operation.train_fraction)))
        train, test = order[:split], order[split:]
        x = torch.cat([features, torch.ones(len(features), 1)], dim=1)
        if operation.algorithm == "ridge":
            if not labels.is_floating_point() or labels.ndim not in {1, 2}:
                raise WorkerRequestError("ridge labels must be floating [N] or [N,outputs]")
            y = labels.float().reshape(len(labels), -1)
            if y.shape[1] > 64:
                raise WorkerRequestError("probe output dimension exceeds 64")
            regularizer = torch.eye(x.shape[1]) * operation.l2_penalty
            regularizer[-1, -1] = 0
            weights = torch.linalg.solve(x[train].T @ x[train] + regularizer, x[train].T @ y[train])
            score = torch.mean((x[test] @ weights - y[test]) ** 2).item()
            name = "test_mean_squared_error"
        else:
            if labels.ndim != 1 or labels.dtype not in {torch.int64, torch.int32} or not set(labels.tolist()) <= {0, 1}:
                raise WorkerRequestError("logistic labels must be binary integer [N]")
            if len(set(labels[train].tolist())) != 2:
                raise WorkerRequestError("training split requires both binary classes")
            weights = torch.zeros(x.shape[1], requires_grad=True)
            optimizer = torch.optim.LBFGS([weights], max_iter=operation.max_iterations, line_search_fn="strong_wolfe")
            def closure():
                optimizer.zero_grad()
                logits = x[train] @ weights
                loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, labels[train].float()) + operation.l2_penalty * weights[:-1].square().mean()
                loss.backward()
                return loss
            optimizer.step(closure)
            weights = weights.detach()
            score = ((x[test] @ weights >= 0).long() == labels[test]).float().mean().item()
            name = "test_accuracy"
        return {"probe_weights": weights.contiguous(), "train_rows": train, "test_rows": test}, {name: score, "split_seed": request.spec.inputs.random_seed, "split_unit": "rows", "confirmatory_evidence": False}

    def execute(self, request: ExecutionRequest, output: Path) -> ExecutionReceipt:
        import torch
        from safetensors.torch import save_file
        started = datetime.now(UTC)
        monotonic_start = time.monotonic()
        if request.deadline <= started:
            raise WorkerRequestError("execution deadline has expired")
        if request.spec.model != self.config.model:
            raise WorkerRequestError("job model is not the worker's verified model")
        if request.spec.experiment_stage.value in {"confirmatory", "replication"}:
            raise WorkerRequestError("confirmatory execution requires the separate trusted evaluator")
        self._dataset(request)
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        torch.manual_seed(request.spec.inputs.random_seed)
        torch.set_num_threads(min(request.spec.limits.max_cpu_cores, len(os.sched_getaffinity(0))))
        operation = request.spec.operation
        tensors, summary = {}, {}
        generated_tokens = 0
        if operation.kind == "fit_probe":
            self._dataset(request)
            tensors, summary = self._fit_probe(request)
        else:
            model = self._load_model(request.spec)
            # Loading an uncached model can consume random state. Execution
            # randomness must be identical for cold and already loaded engines.
            torch.manual_seed(request.spec.inputs.random_seed)
            if operation.kind == "module_manifest":
                summary["modules"] = [{"name": name, "class": type(module).__name__} for name, module in model.named_modules()][:4096]
            elif operation.kind == "weight_stats":
                summary["weights"] = []
                for name, parameter in model.named_parameters():
                    if any(name == prefix or name.startswith(prefix + ".") for prefix in operation.modules):
                        values = parameter.detach().float()
                        summary["weights"].append({"name": name, "shape": list(values.shape), "count": values.numel(), "mean": values.mean().item(), "std": values.std(unbiased=False).item(), "min": values.min().item(), "max": values.max().item()})
                if not summary["weights"]:
                    raise WorkerRequestError("weight selectors did not match any parameter")
            elif operation.kind == "tensor_slice":
                parameters = dict(model.named_parameters())
                if operation.parameter not in parameters:
                    raise WorkerRequestError("unknown model parameter")
                tensor = parameters[operation.parameter]
                if len(operation.starts) != tensor.ndim or any(start + size > extent for start, size, extent in zip(operation.starts, operation.sizes, tensor.shape)):
                    raise WorkerRequestError("tensor slice exceeds parameter dimensions")
                tensors["weight_slice"] = tensor[tuple(slice(start, start + size) for start, size in zip(operation.starts, operation.sizes))].detach().cpu().contiguous()
            else:
                inputs, lengths = self._inputs(request)
                if operation.kind == "generate":
                    rows = []
                    # Independent sequences eliminate right/left padding ambiguity
                    # after EOS and make the seed/budget behavior explicit.
                    for row, length in enumerate(lengths):
                        tokens = inputs["input_ids"][row:row+1, -length:].clone()
                        produced = []
                        for _ in range(request.spec.inputs.generation.max_new_tokens):
                            if datetime.now(UTC) >= request.deadline:
                                raise TimeoutError("execution deadline reached")
                            mask = torch.ones_like(tokens)
                            logits, _ = self.forward({"input_ids": tokens, "attention_mask": mask, "position_ids": mask.cumsum(-1)-1, "use_cache": False}, (tokens.shape[1],))
                            temperature = request.spec.inputs.generation.temperature
                            next_token = logits.argmax(-1) if temperature == 0 else torch.multinomial(torch.softmax(logits / temperature, dim=-1), 1).flatten()
                            value = int(next_token.item())
                            produced.append(value)
                            generated_tokens += 1
                            if generated_tokens > request.spec.limits.max_generated_tokens:
                                raise WorkerRequestError("generation token budget exceeded")
                            tokens = torch.cat([tokens, next_token.reshape(1, 1)], dim=1)
                            eos = model.config.eos_token_id
                            if value in (eos if isinstance(eos, list) else [eos]):
                                break
                        rows.append({"prompt_id": request.spec.inputs.prompt_ids[row], "generated_token_ids": produced})
                    summary["generations"] = rows
                else:
                    logits, captures = self.forward(inputs, lengths, operation, limits=request.spec.limits)
                    tensors["next_token_logits"] = logits.detach().cpu().float().contiguous()
                    for name, captured in captures.items():
                        values = captured.detach().cpu().float()
                        if operation.reduction == "none":
                            tensors[name] = values.contiguous()
                        else:
                            tensors[name + "_mean"] = values.mean(dim=0).contiguous()
                            if operation.reduction == "mean_and_variance":
                                tensors[name + "_variance"] = values.var(dim=0, unbiased=False).contiguous()
        if any(value.is_floating_point() and not torch.isfinite(value).all() for value in tensors.values()):
            raise WorkerRequestError("execution produced non-finite output tensors")
        if tensors:
            save_file({name: value.detach().cpu().contiguous() for name, value in tensors.items()}, str(output / "tensors.safetensors"))
        summary.update(backend=self.config.backend, semantics="prefill_unpadded_positions_v1", generated_tokens=generated_tokens)
        _json_write(output / "summary.json", summary)
        files = sorted(path for path in output.iterdir() if path.is_file())
        total_bytes = sum(path.stat().st_size for path in files)
        if total_bytes > request.spec.limits.max_output_bytes:
            raise WorkerRequestError("retained output exceeds byte budget")
        elapsed = time.monotonic() - monotonic_start
        if datetime.now(UTC) >= request.deadline:
            raise TimeoutError("execution deadline reached")
        cuda = self.config.device.startswith("cuda")
        hardware = {"provider_backend": self.config.provider_backend, "gpu_model": torch.cuda.get_device_name(0) if cuda else "CPU", "gpu_count": 1 if cuda else 0, "region": self.config.region, "live_price_usd_per_hour": self.config.live_price_usd_per_hour}
        names = []
        if operation.kind == "capture":
            names = [self._target(item)[0] for item in operation.modules]
        elif operation.kind in {"patch", "ablate", "steer"}:
            names = [self._target(operation.target)[0]]
        else:
            names = ["model"]
        tools = {"capture": "capture_activation", "patch": "activation_patch", "ablate": "ablate_component", "steer": "steer_direction", "fit_probe": "fit_probe", "generate": "generate_batch", "weight_stats": "weight_stats", "tensor_slice": "tensor_slice", "module_manifest": "module_manifest"}
        science = request.science
        manifest = RunManifest.model_validate({
            "schema_version": 1,
            "run": {"run_id": request.job_id, "parent_run_id": None, "started_at": started, "experiment_stage": request.spec.experiment_stage, "hypothesis_id": request.spec.hypothesis_id, "preregistration_hash": science.preregistration_hash, "approval_id": request.approval_id, "explorer_session_id": science.session_id, "replicator_blinded": science.replicator_blinded},
            "model": self.config.model.model_dump(mode="json"),
            "software": {"probe_mcp_git_commit": self.config.code_git_commit, "container_image_digest": self.config.container_image_digest, "environment_lock_hash": sha256_file(Path(self.config.environment_lock_path)) if self.config.environment_lock_path else None, "python_version": platform.python_version(), "torch_version": torch.__version__, "transformers_version": importlib.metadata.version("transformers"), "nnsight_version": importlib.metadata.version("nnsight"), "cuda_version": torch.version.cuda or "0+cpu"},
            "hardware": hardware,
            "inputs": request.spec.inputs.model_dump(mode="json"),
            "experiment": {"tool": tools[operation.kind], "modules": list(dict.fromkeys(names)), "positions": list(getattr(operation, "positions", ("last",))), "intervention_hash": Ledger.operation_hash(request.spec), "predicted_direction": science.predicted_direction, "primary_metric": science.primary_metric, "falsifier": science.falsifier, "alternative_explanations": list(science.alternative_explanations)},
            "controls": science.controls.model_dump(mode="json"),
            "results": {"effect_size": None, "confidence_interval": None, "heldout": False, "replication_status": "not_applicable"},
            "cost": {"gpu_seconds": math.ceil(elapsed) if cuda else 0, "estimated_compute_usd": elapsed * self.config.live_price_usd_per_hour / 3600, "bytes_persisted": total_bytes},
            "artifacts": [{"path": path.name, "sha256": sha256_file(path).removeprefix("sha256:"), "retention_class": "derived"} for path in files],
            "security": {"outbound_network_attempts": 0, "policy_denials": 0, "secret_access_attempts": 0},
        })
        return ExecutionReceipt(job_id=request.job_id, attempt_id=request.attempt_id, state=WorkerState.SUCCEEDED, started_at=started, finished_at=datetime.now(UTC), manifest=manifest, wall_seconds=elapsed, generated_tokens=generated_tokens, peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024, peak_vram_bytes=torch.cuda.max_memory_allocated(0) if cuda else 0)


def _block_network() -> None:
    """Install an irreversible Linux seccomp deny rule for new network sockets."""
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise WorkerRequestError("cannot create network-denial seccomp policy")
    try:
        for name in (b"socket", b"connect", b"sendto", b"sendmsg"):
            syscall = library.seccomp_syscall_resolve_name(name)
            if syscall < 0 or library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, syscall, 0) != 0:
                raise WorkerRequestError("cannot install network-denial syscall rule")
        if library.seccomp_load(context) != 0:
            raise WorkerRequestError("cannot activate network-denial seccomp policy")
    finally:
        library.seccomp_release(context)


def _process_identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]  # starttime (field 22), after removing pid and comm.
    except (FileNotFoundError, ProcessLookupError):
        return None


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _same_process(metadata) -> bool:
    return metadata.get("boot_id") == _boot_id() and _process_identity(metadata["pid"]) == metadata["identity"]


def _rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def _output_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink()) if path.exists() else 0


def _child_entry(config_json: str, request_json: str, directory: str, ready, release) -> None:
    os.setsid()
    ready.set()
    release.wait()
    request = ExecutionRequest.model_validate_json(request_json)
    config = WorkerConfig.model_validate_json(config_json)
    start = datetime.now(UTC)
    result = None
    try:
        # Do not inherit management credentials or arbitrary caller environment.
        keep = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES"}}
        os.environ.clear()
        os.environ.update(keep)
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false", WANDB_MODE="disabled")
        os.environ["OMP_NUM_THREADS"] = str(request.spec.limits.max_cpu_cores)
        os.environ["MKL_NUM_THREADS"] = str(request.spec.limits.max_cpu_cores)
        available = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, available[:request.spec.limits.max_cpu_cores])
        resource.setrlimit(resource.RLIMIT_CPU, (request.spec.limits.max_runtime_seconds, request.spec.limits.max_runtime_seconds))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max(request.spec.limits.max_output_bytes, 65536),) * 2)
        if config.device == "cpu":
            resource.setrlimit(resource.RLIMIT_AS, (request.spec.limits.max_ram_bytes,) * 2)
        _block_network()
        engine = WorkerEngine(config)
        result = engine.execute(request, Path(directory) / "artifacts")
    except BaseException as exc:
        kind = "timeout" if isinstance(exc, TimeoutError) else "oom" if isinstance(exc, MemoryError) or "OutOfMemory" in type(exc).__name__ else "policy" if isinstance(exc, WorkerRequestError) else "scientific"
        result = ExecutionReceipt(job_id=request.job_id, attempt_id=request.attempt_id, state=WorkerState.FAILED, started_at=start, finished_at=datetime.now(UTC), failure_kind=kind, error_code=type(exc).__name__)
    _json_write(Path(directory) / "result.json", result.model_dump(mode="json"))


class Supervisor:
    """Durable asynchronous single-process supervisor, with positive stop receipts.

    Restart adopts only a matching Linux PID/starttime identity. Unknown/missing
    processes become failed infrastructure attempts rather than being rerun.
    """
    def __init__(self, config: WorkerConfig, *, monitor_interval: float = 0.05):
        self.config = config
        self.root = Path(config.output_directory).absolute()
        if self.root.is_symlink():
            raise WorkerRequestError("output root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._processes = {}
        self._requests = {}
        self._stop = threading.Event()
        self.monitor_interval = monitor_interval
        for directory in self.root.iterdir():
            if directory.is_dir() and (directory / "request.json").exists():
                request = ExecutionRequest.model_validate_json((directory / "request.json").read_text())
                if directory.name != request.attempt_id:
                    raise WorkerRequestError("persisted attempt directory mismatch")
                self._requests[request.attempt_id] = request
        self._monitor = threading.Thread(target=self._watch, name="probe-worker-supervisor", daemon=True)
        self._monitor.start()

    def _directory(self, attempt_id: str) -> Path:
        if attempt_id not in self._requests:
            raise KeyError("unknown attempt")
        return self.root / attempt_id

    def _read(self, attempt_id: str) -> ExecutionReceipt:
        return ExecutionReceipt.model_validate_json((self._directory(attempt_id) / "receipt.json").read_text())

    def _cgroup(self, request, pid):
        if self.config.cgroup_directory is None:
            return None
        root = Path(self.config.cgroup_directory)
        if root.is_symlink() or not (root / "cgroup.controllers").exists():
            raise WorkerRequestError("a real delegated cgroup v2 directory is required")
        path = root / ("probe-" + request.attempt_id)
        path.mkdir(mode=0o700)
        try:
            for name, value in (("memory.max", request.spec.limits.max_ram_bytes), ("memory.swap.max", 0), ("pids.max", 128), ("cpu.max", f"{request.spec.limits.max_cpu_cores * 100000} 100000"), ("cgroup.procs", pid)):
                (path / name).write_text(str(value))
        except BaseException:
            try:
                path.rmdir()
            except OSError:
                pass
            raise WorkerRequestError("delegated cgroup resource enforcement could not be established")
        return str(path)

    def submit(self, request: ExecutionRequest) -> ExecutionReceipt:
        request = ExecutionRequest.model_validate_json(request.model_dump_json())
        now = datetime.now(UTC)
        if request.spec.model != self.config.model or request.deadline <= now:
            raise WorkerRequestError("model mismatch or expired execution deadline")
        with self._lock:
            if request.attempt_id in self._requests:
                if self._requests[request.attempt_id] != request:
                    raise WorkerRequestError("attempt identifier already has different content")
                return self.status(request.attempt_id)
            if any(not self._read(key).process_stopped for key in self._requests):
                raise WorkerBusyError("an execution remains unresolved")
            directory = self.root / request.attempt_id
            directory.mkdir(mode=0o700)
            self._requests[request.attempt_id] = request
            _json_write(directory / "request.json", request.model_dump(mode="json"))
            receipt = ExecutionReceipt(job_id=request.job_id, attempt_id=request.attempt_id, state=WorkerState.ACCEPTED, started_at=now)
            _json_write(directory / "receipt.json", receipt.model_dump(mode="json"))
            context = multiprocessing.get_context("spawn")
            ready, release = context.Event(), context.Event()
            process = context.Process(target=_child_entry, args=(self.config.model_dump_json(), request.model_dump_json(), str(directory), ready, release), daemon=False)
            try:
                process.start()
                self._processes[request.attempt_id] = process
                if not ready.wait(timeout=10):
                    raise WorkerRequestError("execution process did not initialize")
                cgroup = self._cgroup(request, process.pid)
                identity = _process_identity(process.pid)
                if identity is None:
                    raise WorkerRequestError("execution process exited during initialization")
                deadline = min(request.deadline.timestamp(), now.timestamp() + request.spec.limits.max_runtime_seconds)
                _json_write(directory / "process.json", {"pid": process.pid, "identity": identity, "boot_id": _boot_id(), "deadline": deadline, "monotonic_deadline": time.monotonic() + max(0, deadline - time.time()), "cgroup": cgroup})
                receipt = receipt.model_copy(update={"state": WorkerState.RUNNING})
                _json_write(directory / "receipt.json", receipt.model_dump(mode="json"))
                release.set()
                return receipt
            except BaseException:
                if process.pid:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    # A spawn that failed before setsid has no private process
                    # group yet. Kill the known child directly and verify exit.
                    if process.is_alive():
                        process.kill()
                    process.join(timeout=5)
                    if process.is_alive():
                        raise WorkerRequestError("initializing process termination could not be confirmed") from None
                stopped = receipt.model_copy(update={"state": WorkerState.FAILED, "failure_kind": "infrastructure", "error_code": "ProcessInitializationFailed", "finished_at": datetime.now(UTC), "process_stopped": True})
                _json_write(directory / "receipt.json", stopped.model_dump(mode="json"))
                raise

    def _terminate(self, attempt_id: str, failure_kind: str, error_code: str):
        directory = self._directory(attempt_id)
        receipt = self._read(attempt_id)
        metadata_path = directory / "process.json"
        metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else None
        if metadata is None:
            # A supervisor may have crashed after spawn but before identity
            # publication. Missing metadata is never evidence that child exited.
            raise WorkerRequestError("process identity is missing; termination remains unresolved")
        if metadata and _same_process(metadata):
            group = Path(metadata["cgroup"]) if metadata.get("cgroup") else None
            if group and (group / "cgroup.kill").exists():
                (group / "cgroup.kill").write_text("1")
            try:
                os.killpg(metadata["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            process = self._processes.get(attempt_id)
            if process:
                process.join(timeout=5)
            deadline = time.monotonic() + 5
            while _same_process(metadata) and time.monotonic() < deadline:
                time.sleep(0.01)
            if _same_process(metadata):
                raise WorkerRequestError("process termination could not be confirmed")
        receipt = receipt.model_copy(update={"state": WorkerState.CANCELLED if failure_kind == "cancelled" else WorkerState.FAILED, "failure_kind": failure_kind, "error_code": error_code, "finished_at": datetime.now(UTC), "process_stopped": True})
        _json_write(directory / "receipt.json", receipt.model_dump(mode="json"))
        return receipt

    def _refresh(self, attempt_id):
        receipt = self._read(attempt_id)
        if receipt.process_stopped:
            return receipt
        directory = self._directory(attempt_id)
        metadata_path = directory / "process.json"
        if not metadata_path.exists():
            return self._terminate(attempt_id, "infrastructure", "MissingProcessIdentity")
        metadata = json.loads(metadata_path.read_text())
        request = self._requests[attempt_id]
        alive = _same_process(metadata)
        if alive:
            if time.time() >= metadata["deadline"] or time.monotonic() >= metadata["monotonic_deadline"]:
                return self._terminate(attempt_id, "timeout", "ExecutionDeadlineExceeded")
            if _rss(metadata["pid"]) > request.spec.limits.max_ram_bytes:
                return self._terminate(attempt_id, "oom", "RAMLimitExceeded")
            if _output_size(directory / "artifacts") > request.spec.limits.max_output_bytes:
                return self._terminate(attempt_id, "policy", "OutputLimitExceeded")
            return receipt
        process = self._processes.get(attempt_id)
        if process:
            process.join(timeout=0)
        result_path = directory / "result.json"
        if result_path.exists():
            result = ExecutionReceipt.model_validate_json(result_path.read_text())
            if result.job_id != request.job_id or result.attempt_id != attempt_id:
                return self._terminate(attempt_id, "infrastructure", "ResultIdentityMismatch")
            result = result.model_copy(update={"process_stopped": True})
        else:
            result = receipt.model_copy(update={"state": WorkerState.FAILED, "failure_kind": "infrastructure", "error_code": "ProcessExitedWithoutResult", "finished_at": datetime.now(UTC), "process_stopped": True})
        _json_write(directory / "receipt.json", result.model_dump(mode="json"))
        if metadata.get("cgroup"):
            try:
                Path(metadata["cgroup"]).rmdir()
            except OSError:
                pass
        return result

    def _watch(self):
        while not self._stop.wait(self.monitor_interval):
            with self._lock:
                for attempt_id in tuple(self._requests):
                    try:
                        self._refresh(attempt_id)
                    except (OSError, ValueError):
                        # A status call surfaces malformed persistent state; never
                        # infer termination or dispatch another execution from it.
                        continue

    def status(self, attempt_id: str) -> ExecutionReceipt:
        with self._lock:
            return self._refresh(attempt_id)

    def cancel(self, attempt_id: str) -> ExecutionReceipt:
        with self._lock:
            receipt = self._read(attempt_id)
            if receipt.process_stopped:
                return receipt
            return self._terminate(attempt_id, "cancelled", "OperatorCancelled")

    def upload_tensor(self, digest: str, stream, length: int) -> dict:
        """Stage immutable safetensors by content hash; no caller-selected host path."""
        from safetensors import safe_open, SafetensorError
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not 8 <= length <= self.config.max_tensor_bytes:
            raise WorkerRequestError("invalid content hash or tensor upload size")
        root = Path(self.config.tensor_directory).absolute()
        if root.is_symlink():
            raise WorkerRequestError("tensor root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = root / digest
        if destination.is_symlink():
            raise WorkerRequestError("tensor destination cannot be a symlink")
        destination.mkdir(exist_ok=True, mode=0o700)
        target = destination / "tensor.safetensors"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=destination)
        temporary = Path(temporary_name)
        hasher = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as output:
                remaining = length
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise WorkerRequestError("incomplete tensor upload")
                    remaining -= len(chunk)
                    hasher.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if hasher.hexdigest() != digest:
                raise WorkerRequestError("tensor upload checksum mismatch")
            with temporary.open("rb") as data:
                header_bytes = struct.unpack("<Q", data.read(8))[0]
            if header_bytes > min(8 * 1024**2, length - 8):
                raise WorkerRequestError("safetensors header exceeds validation bound")
            descriptions = []
            with safe_open(temporary, framework="pt", device="cpu") as tensors:
                if not 1 <= len(tensors.keys()) <= 128:
                    raise WorkerRequestError("tensor upload requires 1..128 named tensors")
                for name in tensors.keys():
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", name):
                        raise WorkerRequestError("tensor name is not a bounded identifier")
                    view = tensors.get_slice(name)
                    shape, dtype = view.get_shape(), view.get_dtype()
                    if len(shape) > 8 or dtype not in {"F16", "BF16", "F32", "F64", "I32", "I64"}:
                        raise WorkerRequestError("tensor upload has unsupported rank or dtype")
                    descriptions.append({"tensor_name": name, "shape": shape, "dtype": dtype})
            if target.exists():
                if _path(root, digest + "/tensor.safetensors") != target or sha256_file(target) != "sha256:" + digest:
                    raise WorkerRequestError("existing content-addressed tensor is corrupt")
            else:
                os.chmod(temporary, 0o400)
                # Hardlink publication is exclusive; immediately unlink staging so
                # retained input has exactly one hardlink and cannot be replaced.
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if sha256_file(target) != "sha256:" + digest:
                        raise WorkerRequestError("concurrent tensor publication conflicts")
                temporary.unlink()
                parent = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            return {"path": digest + "/tensor.safetensors", "sha256": "sha256:" + digest, "tensors": descriptions}
        except SafetensorError as exc:
            raise WorkerRequestError("invalid safetensors payload") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def artifact(self, attempt_id: str, relative: str) -> Path:
        with self._lock:
            receipt = self.status(attempt_id)
            if receipt.state != WorkerState.SUCCEEDED or receipt.manifest is None:
                raise WorkerRequestError("successful stopped result required")
            if relative not in {artifact.path for artifact in receipt.manifest.artifacts}:
                raise WorkerRequestError("artifact is not in this attempt's retained manifest")
            return _path(self._directory(attempt_id) / "artifacts", relative)

    def close(self, *, terminate: bool = True):
        self._stop.set()
        self._monitor.join(timeout=5)
        if terminate:
            with self._lock:
                for key in tuple(self._requests):
                    if not self._read(key).process_stopped:
                        self._terminate(key, "cancelled", "SupervisorStopped")


class WorkerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, supervisor: Supervisor, bearer_secret: str, *, host="127.0.0.1", port=0):
        if host != "127.0.0.1" or not 32 <= len(bearer_secret) <= 512:
            raise WorkerRequestError("worker HTTP requires loopback binding and a strong shared secret")
        self.supervisor = supervisor
        self._bearer_secret = bearer_secret
        super().__init__((host, port), _WorkerHandler)


class _WorkerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *args):
        pass  # Never log authorization headers or request bodies.

    def _response(self, status, body):
        encoded = canonical_json(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self):
        provided = self.headers.get("Authorization", "")
        valid = hmac.compare_digest(provided, "Bearer " + self.server._bearer_secret)
        if not valid:
            self._response(401, {"error": "unauthorized"})
        return valid

    def do_POST(self):
        if not self._authorized():
            return
        try:
            if self.headers.get("Transfer-Encoding") or not self.headers.get("Content-Length", "").isdigit():
                return self._response(400, {"error": "bounded_content_length_required"})
            length = int(self.headers["Content-Length"])
            if length > self.server.supervisor.config.max_request_bytes:
                return self._response(413, {"error": "request_too_large"})
            raw = self.rfile.read(length)
            if self.path == "/v1/jobs":
                receipt = self.server.supervisor.submit(ExecutionRequest.model_validate_json(raw))
                return self._response(202, receipt.model_dump(mode="json"))
            parts = self.path.split("/")
            if len(parts) == 5 and parts[1:3] == ["v1", "jobs"] and parts[4] == "cancel" and raw in (b"", b"{}"):
                return self._response(200, self.server.supervisor.cancel(parts[3]).model_dump(mode="json"))
            self._response(404, {"error": "not_found"})
        except WorkerBusyError:
            self._response(409, {"error": "worker_busy"})
        except KeyError:
            self._response(404, {"error": "not_found"})
        except (ValueError, OSError):
            self._response(400, {"error": "invalid_request"})

    def do_PUT(self):
        if not self._authorized():
            return
        try:
            match = re.fullmatch(r"/v1/tensors/([0-9a-f]{64})", self.path)
            if not match or self.headers.get("Transfer-Encoding") or not self.headers.get("Content-Length", "").isdigit():
                return self._response(400, {"error": "invalid_tensor_upload"})
            length = int(self.headers["Content-Length"])
            if length > self.server.supervisor.config.max_tensor_bytes:
                return self._response(413, {"error": "tensor_too_large"})
            uploaded = self.server.supervisor.upload_tensor(match.group(1), self.rfile, length)
            return self._response(200, uploaded)
        except (ValueError, OSError):
            self._response(400, {"error": "invalid_tensor_upload"})

    def do_GET(self):
        if not self._authorized():
            return
        try:
            parts = self.path.split("/")
            if len(parts) == 4 and parts[1:3] == ["v1", "jobs"]:
                return self._response(200, self.server.supervisor.status(parts[3]).model_dump(mode="json"))
            if len(parts) >= 6 and parts[1:3] == ["v1", "jobs"] and parts[4] == "artifacts":
                path = self.server.supervisor.artifact(parts[3], "/".join(parts[5:]))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with path.open("rb") as stream:
                    while chunk := stream.read(65536):
                        self.wfile.write(chunk)
                return
            self._response(404, {"error": "not_found"})
        except KeyError:
            self._response(404, {"error": "not_found"})
        except (ValueError, OSError):
            self._response(400, {"error": "invalid_request"})


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run the trusted loopback-only Probe worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    token_path = Path(args.token_file)
    info = token_path.stat()
    if token_path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise WorkerRequestError("bearer secret file must be owned and mode0600")
    config = WorkerConfig.model_validate_json(Path(args.config).read_text())
    supervisor = Supervisor(config)
    server = WorkerHTTPServer(supervisor, token_path.read_text().strip(), port=args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        supervisor.close()


if __name__ == "__main__":
    main()
