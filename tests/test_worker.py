"""Real, offline, randomly initialized HF Qwen numerical acceptance tests."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
import torch
from safetensors.torch import load_file, save_file
from transformers import Qwen3Config, Qwen3ForCausalLM

from probe_core.schemas import JobSpec
from probe_core.worker import WorkerEngine, prompt_set_hash, sha256_file
from probe_core.worker_contracts import ExecutionRequest, PromptDataset, WorkerConfig, WorkerRequestError


@pytest.fixture(scope="module")
def tiny_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("actual-tiny-qwen")
    model_directory = root / "model"
    torch.manual_seed(19)
    model = Qwen3ForCausalLM(
        Qwen3Config(
            vocab_size=64,
            hidden_size=32,
            intermediate_size=64,
            num_hidden_layers=2,
            num_attention_heads=4,
            num_key_value_heads=2,
            head_dim=8,
            max_position_embeddings=128,
            pad_token_id=0,
            eos_token_id=63,
        )
    ).eval()
    model.save_pretrained(model_directory, safe_serialization=True)
    dataset_path = root / "prompts.json"
    dataset = PromptDataset.model_validate(
        {
            "prompts": [
                {"prompt_id": "short", "token_ids": [1, 2, 3]},
                {"prompt_id": "long", "token_ids": [4, 5, 6, 7, 8]},
            ]
        }
    )
    dataset_path.write_text(dataset.model_dump_json())
    tensor_root = root / "tensor-inputs"
    tensor_root.mkdir()
    identity = {
        "repo": "probe/testing-tiny-qwen3",
        "revision_sha": sha256_file(model_directory / "config.json").removeprefix("sha256:"),
        "local_weight_hashes": [sha256_file(path) for path in sorted(model_directory.glob("*.safetensors"))],
        "tokenizer_revision": hashlib.sha256(b"explicit-token-id-fixture-no-tokenizer").hexdigest(),
        "dtype": "float32",
        "quantized": False,
        "chat_template_hash": None,
        "thinking_mode": None,
    }
    project = Path(__file__).resolve().parents[1]
    source_commit = os.environ.get("PROBE_TEST_SOURCE_COMMIT")
    if source_commit is None:
        source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    config = WorkerConfig(
        model_directory=str(model_directory),
        model=identity,
        assets=[
            {"path": path.name, "sha256": sha256_file(path)} for path in model_directory.iterdir() if path.is_file()
        ],
        datasets=[{"path": str(dataset_path), "sha256": sha256_file(dataset_path)}],
        tensor_directory=str(tensor_root),
        output_directory=str(root / "outputs"),
        backend="nnsight",
        device="cpu",
        code_git_commit=source_commit,
        environment_lock_path=str(project / "uv.lock"),
        provider_backend="local_cpu",
        region="cpu-test",
        live_price_usd_per_hour=0.0,
    )
    return config, dataset


@pytest.fixture
def make_request(tiny_bundle):
    config, dataset = tiny_bundle

    def make(operation=None, *, key="job-1"):
        spec = JobSpec.model_validate(
            {
                "idempotency_key": key,
                "experiment_stage": "calibration",
                "model": config.model.model_dump(mode="json"),
                "inputs": {
                    "dataset_revision": config.datasets[0].sha256,
                    "prompt_set_hash": prompt_set_hash(dataset, ("short", "long")),
                    "prompt_ids": ["short", "long"],
                    "random_seed": 123,
                    "generation": {"temperature": 0.0, "max_new_tokens": 3},
                },
                "operation": operation
                or {
                    "kind": "capture",
                    "modules": [{"layer": 0, "component": "residual"}],
                    "positions": ["last"],
                    "reduction": "none",
                },
                "limits": {"max_runtime_seconds": 60, "max_output_bytes": 1000000, "max_cpu_cores": 1},
            }
        )
        return ExecutionRequest(
            job_id=key,
            attempt_id=key + "-attempt",
            worker_id="worker-1",
            approval_id="wake-1",
            deadline=datetime.now(timezone.utc) + timedelta(seconds=60),
            spec=spec,
        )

    return make


@pytest.fixture
def engine(tiny_bundle):
    return WorkerEngine(tiny_bundle[0])


def test_actual_hf_nnsight_noop_and_capture_parity(engine, make_request):
    request = make_request()
    inputs, lengths = engine._inputs(request)
    reference, captures = engine.forward(
        inputs, lengths, request.spec.operation, backend="reference", limits=request.spec.limits
    )
    traced, saved = engine.forward(
        inputs, lengths, request.spec.operation, backend="nnsight", limits=request.spec.limits
    )
    torch.testing.assert_close(traced, reference, rtol=1e-5, atol=1e-6)
    torch.testing.assert_close(saved["layer_0_residual"], captures["layer_0_residual"], rtol=1e-5, atol=1e-6)
    assert saved["layer_0_residual"].shape == (2, 1, 32)


@pytest.mark.parametrize(
    "component,head", [("residual", None), ("attention_output", None), ("mlp_output", None), ("attention_head", 1)]
)
def test_ablation_matches_raw_hooks_for_all_supported_modules(engine, make_request, component, head):
    target = {"layer": 0, "component": component, "head": head}
    request = make_request({"kind": "ablate", "target": target, "positions": ["last"], "method": "zero"})
    inputs, lengths = engine._inputs(request)
    baseline, _ = engine.forward(inputs, lengths, backend="reference")
    reference, _ = engine.forward(
        inputs, lengths, request.spec.operation, backend="reference", limits=request.spec.limits
    )
    traced, _ = engine.forward(inputs, lengths, request.spec.operation, backend="nnsight", limits=request.spec.limits)
    torch.testing.assert_close(traced, reference, rtol=1e-5, atol=1e-6)
    assert not torch.equal(baseline, reference)
    after, _ = engine.forward(inputs, lengths, backend="reference")
    torch.testing.assert_close(after, baseline, rtol=0, atol=0)


def test_cpu_execution_reports_honest_provenance_and_real_artifacts(engine, make_request, tmp_path):
    receipt = engine.execute(make_request(), tmp_path / "result")
    assert receipt.manifest.hardware.provider_backend == "local_cpu"
    assert receipt.manifest.hardware.gpu_count == 0
    assert receipt.manifest.cost.gpu_seconds == 0
    assert receipt.manifest.software.transformers_version.startswith("5.")
    assert receipt.manifest.software.container_image_digest is None
    assert receipt.manifest.software.environment_lock_hash == sha256_file(Path(engine.config.environment_lock_path))
    for artifact in receipt.manifest.artifacts:
        assert sha256_file(tmp_path / "result" / artifact.path) == "sha256:" + artifact.sha256


def tensor_reference(tiny_bundle, name, tensors):
    root = Path(tiny_bundle[0].tensor_directory)
    path = root / (name + ".safetensors")
    save_file(tensors, str(path))
    return {"path": path.name, "sha256": sha256_file(path), "tensor_name": next(iter(tensors))}


def test_patch_replays_captured_state_and_steering_zero_is_noop(engine, make_request, tiny_bundle):
    request = make_request()
    inputs, lengths = engine._inputs(request)
    baseline, captured = engine.forward(
        inputs, lengths, request.spec.operation, backend="reference", limits=request.spec.limits
    )
    source = tensor_reference(tiny_bundle, "captured", {"states": captured["layer_0_residual"].cpu()})
    patch = make_request(
        {"kind": "patch", "target": {"layer": 0, "component": "residual"}, "positions": ["last"], "source": source}
    )
    replay, _ = engine.forward(inputs, lengths, patch.spec.operation, limits=patch.spec.limits)
    torch.testing.assert_close(replay, baseline, rtol=1e-5, atol=1e-6)
    direction = tensor_reference(tiny_bundle, "direction", {"direction": torch.ones(32)})
    steer = make_request(
        {
            "kind": "steer",
            "target": {"layer": 0, "component": "residual"},
            "positions": ["last"],
            "direction": direction,
            "strength": 0.0,
        }
    )
    unchanged, _ = engine.forward(inputs, lengths, steer.spec.operation, limits=steer.spec.limits)
    torch.testing.assert_close(unchanged, baseline, rtol=0, atol=0)


def test_nonzero_steering_matches_reference(engine, make_request, tiny_bundle):
    direction = tensor_reference(
        tiny_bundle, "nonzero-direction", {"direction": torch.arange(32, dtype=torch.float32) / 32}
    )
    request = make_request(
        {
            "kind": "steer",
            "target": {"layer": 0, "component": "mlp_output"},
            "positions": [0, "last"],
            "direction": direction,
            "strength": 0.3,
        }
    )
    inputs, lengths = engine._inputs(request)
    actual, _ = engine.forward(inputs, lengths, request.spec.operation, limits=request.spec.limits)
    expected, _ = engine.forward(
        inputs, lengths, request.spec.operation, backend="reference", limits=request.spec.limits
    )
    torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-6)


def test_tensor_checksum_and_shape_are_enforced(engine, make_request, tiny_bundle):
    source = tensor_reference(tiny_bundle, "bad-shape", {"states": torch.ones(3)})
    operation = {
        "kind": "patch",
        "target": {"layer": 0, "component": "residual"},
        "positions": ["last"],
        "source": source,
    }
    request = make_request(operation)
    inputs, lengths = engine._inputs(request)
    with pytest.raises(WorkerRequestError, match="patch must"):
        engine.forward(inputs, lengths, request.spec.operation, limits=request.spec.limits)
    operation["source"]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(WorkerRequestError, match="checksum"):
        engine.forward(inputs, lengths, make_request(operation).spec.operation, limits=request.spec.limits)


def test_padding_positions_never_select_padding(engine, make_request):
    request = make_request(
        {"kind": "capture", "modules": [{"layer": 0, "component": "residual"}], "positions": [3], "reduction": "none"}
    )
    inputs, lengths = engine._inputs(request)
    with pytest.raises(WorkerRequestError, match="real prompt"):
        engine.forward(inputs, lengths, request.spec.operation, limits=request.spec.limits)


@pytest.mark.parametrize("kind", ["generate", "module_manifest", "weight_stats", "tensor_slice"])
def test_generation_and_readonly_inspection_are_real_bounded_operations(engine, make_request, tmp_path, kind):
    operation = {"kind": kind}
    if kind == "weight_stats":
        operation["modules"] = ["model.layers.0"]
    if kind == "tensor_slice":
        operation.update(parameter="model.embed_tokens.weight", starts=[0, 0], sizes=[2, 3])
    receipt = engine.execute(make_request(operation, key=kind), tmp_path / kind)
    summary = json.loads((tmp_path / kind / "summary.json").read_text())
    if kind == "generate":
        assert 0 < receipt.generated_tokens <= 6
        assert len(summary["generations"]) == 2
    if kind == "tensor_slice":
        assert load_file(str(tmp_path / kind / "tensors.safetensors"))["weight_slice"].shape == (2, 3)
    if kind == "weight_stats":
        assert summary["weights"]
    if kind == "module_manifest":
        assert any(item["name"] == "model.layers.0" for item in summary["modules"])


@pytest.mark.parametrize("algorithm", ["ridge", "logistic_regression"])
def test_probe_fitting_uses_safetensors_and_reproducible_disjoint_split(
    engine, make_request, tiny_bundle, tmp_path, algorithm
):
    torch.manual_seed(7)
    x = torch.randn(100, 3)
    y = x[:, 0] * 2 if algorithm == "ridge" else (x[:, 0] > 0).long()
    features = tensor_reference(tiny_bundle, "features-" + algorithm, {"features": x})
    labels = tensor_reference(tiny_bundle, "labels-" + algorithm, {"labels": y})
    request = make_request(
        {
            "kind": "fit_probe",
            "activations": features,
            "labels": labels,
            "algorithm": algorithm,
            "l2_penalty": 0.01,
            "max_iterations": 20,
        },
        key=algorithm,
    )
    first = engine.execute(request, tmp_path / "first")
    engine.execute(request, tmp_path / "second")
    a = load_file(str(tmp_path / "first" / "tensors.safetensors"))
    b = load_file(str(tmp_path / "second" / "tensors.safetensors"))
    assert not set(a["train_rows"].tolist()) & set(a["test_rows"].tolist())
    torch.testing.assert_close(a["probe_weights"], b["probe_weights"], rtol=0, atol=0)
    assert first.manifest.results.heldout is False


def test_spawned_supervisor_executes_and_positive_stop_receipt(tiny_bundle, make_request, tmp_path):
    import time
    from probe_core.worker import Supervisor

    config = tiny_bundle[0].model_copy(update={"output_directory": str(tmp_path / "supervised")})
    supervisor = Supervisor(config)
    try:
        request = make_request()
        first = supervisor.submit(request)
        assert first.attempt_id == request.attempt_id
        assert supervisor.submit(request).attempt_id == request.attempt_id
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status = supervisor.status(request.attempt_id)
            if status.process_stopped:
                break
            time.sleep(0.05)
        assert status.process_stopped
        assert status.state.value == "SUCCEEDED", status
        assert supervisor.artifact(request.attempt_id, "tensors.safetensors").is_file()
    finally:
        supervisor.close()


def test_native_cpu_environment_provenance_hashes_real_lock_bytes(tiny_bundle, make_request, tmp_path):
    lock = tmp_path / "environment.lock"
    lock.write_text("test fixture environment lock; exact content hashing is under test\n")
    config = tiny_bundle[0].model_copy(update={"container_image_digest": None, "environment_lock_path": str(lock)})
    receipt = WorkerEngine(config).execute(make_request(), tmp_path / "native")
    assert receipt.manifest.software.container_image_digest is None
    assert receipt.manifest.software.environment_lock_hash == sha256_file(lock)


def test_restart_missing_process_identity_cannot_invent_stop_receipt(tiny_bundle, make_request, tmp_path):
    from probe_core.worker import Supervisor
    from probe_core.worker_contracts import ExecutionReceipt, WorkerBusyError, WorkerState

    request = make_request(key="unknown-spawn")
    root = tmp_path / "restarted-worker"
    directory = root / request.attempt_id
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(request.model_dump_json())
    receipt = ExecutionReceipt(
        job_id=request.job_id,
        attempt_id=request.attempt_id,
        state=WorkerState.ACCEPTED,
        started_at=datetime.now(timezone.utc),
    )
    (directory / "receipt.json").write_text(receipt.model_dump_json())
    supervisor = Supervisor(tiny_bundle[0].model_copy(update={"output_directory": str(root)}))
    try:
        with pytest.raises(WorkerRequestError, match="termination remains unresolved"):
            supervisor.status(request.attempt_id)
        with pytest.raises(WorkerRequestError, match="termination remains unresolved"):
            supervisor.cancel(request.attempt_id)
        with pytest.raises(WorkerBusyError):
            supervisor.submit(make_request(key="must-not-dispatch"))
        assert not ExecutionReceipt.model_validate_json((directory / "receipt.json").read_text()).process_stopped
    finally:
        supervisor.close(terminate=False)


def test_preencoded_tokens_cannot_claim_unverified_chat_or_thinking_mode(tiny_bundle, make_request):
    config, dataset = tiny_bundle
    model_data = config.model.model_dump(mode="json")
    model_data.update(thinking_mode=True, chat_template_hash="sha256:" + "e" * 64)
    body = config.model_dump(mode="json")
    body["model"] = model_data
    modified = WorkerConfig.model_validate(body)
    request = make_request().model_dump(mode="json")
    request["spec"]["model"] = model_data
    with pytest.raises(WorkerRequestError, match="caller-supplied token IDs"):
        WorkerEngine(modified)._inputs(ExecutionRequest.model_validate(request))


def test_text_inputs_apply_the_verified_thinking_flag(tiny_bundle, make_request, tmp_path):
    import shutil
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast

    config, _ = tiny_bundle
    model_directory = tmp_path / "chat-model"
    shutil.copytree(config.model_directory, model_directory)
    tokenizer_object = Tokenizer(WordLevel({"[UNK]": 0, "think": 1, "hello": 2}, unk_token="[UNK]"))
    tokenizer_object.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=tokenizer_object, unk_token="[UNK]")
    tokenizer.chat_template = "{% if enable_thinking %}think {% endif %}{{ messages[0]['content'] }}"
    tokenizer.save_pretrained(model_directory)
    dataset = PromptDataset.model_validate({"prompts": [{"prompt_id": "text", "text": "hello"}]})
    dataset_path = tmp_path / "text-prompts.json"
    dataset_path.write_text(dataset.model_dump_json())
    expected = []
    for mode in (True, False):
        body = config.model_dump(mode="json")
        body["model_directory"] = str(model_directory)
        body["model"]["thinking_mode"] = mode
        body["model"]["chat_template_hash"] = "sha256:" + hashlib.sha256(tokenizer.chat_template.encode()).hexdigest()
        body["model"]["tokenizer_revision"] = sha256_file(model_directory / "tokenizer.json").removeprefix("sha256:")
        body["assets"] = [
            {"path": path.name, "sha256": sha256_file(path)} for path in model_directory.iterdir() if path.is_file()
        ]
        body["datasets"] = [{"path": str(dataset_path), "sha256": sha256_file(dataset_path)}]
        worker_config = WorkerConfig.model_validate(body)
        request = make_request().model_dump(mode="json")
        request["spec"]["model"] = body["model"]
        request["spec"]["inputs"].update(
            dataset_revision=sha256_file(dataset_path),
            prompt_set_hash=prompt_set_hash(dataset, ("text",)),
            prompt_ids=["text"],
        )
        inputs, _ = WorkerEngine(worker_config)._inputs(ExecutionRequest.model_validate(request))
        expected.append(inputs["input_ids"].tolist())
    assert expected == [[[1, 2]], [[2]]]


def test_multi_capture_is_ordered_and_includes_head_preprojection_tensors(engine, make_request):
    operation = {
        "kind": "capture",
        "modules": [
            {"layer": 1, "component": "residual"},
            {"layer": 0, "component": "residual"},
            {"layer": 0, "component": "attention_head", "head": 1},
            {"layer": 0, "component": "attention_head", "head": 0},
            {"layer": 0, "component": "attention_output"},
            {"layer": 0, "component": "mlp_output"},
        ],
        "positions": [0, "last"],
        "reduction": "none",
    }
    request = make_request(operation)
    inputs, lengths = engine._inputs(request)
    a, reference = engine.forward(
        inputs, lengths, request.spec.operation, backend="reference", limits=request.spec.limits
    )
    b, actual = engine.forward(inputs, lengths, request.spec.operation, backend="nnsight", limits=request.spec.limits)
    torch.testing.assert_close(a, b, rtol=1e-5, atol=1e-6)
    assert len(actual) == 6
    for name in actual:
        torch.testing.assert_close(actual[name], reference[name], rtol=1e-5, atol=1e-6)
    assert actual["layer_0_attention_head_0"].shape == (2, 2, 8)


def test_seccomp_blocks_network_sockets_in_an_actual_subprocess():
    import subprocess
    import sys

    script = "from probe_core.worker import _block_network; import socket; _block_network();\ntry:\n socket.socket()\nexcept PermissionError:\n print('blocked')\nelse:\n raise SystemExit('network policy did not apply')\n"
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, timeout=10, check=True)
    assert result.stdout.strip() == "blocked"
