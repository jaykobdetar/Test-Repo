# Canonical GPU deployment and acceptance

This implementation prepares exact assets, publishes reproducible image recipes, and runs a fixed numerical calibration through the existing approved-job pipeline. A successful image build or CPU test is not a live GPU acceptance result. The provider diagnostic must run first under its own human-approved infrastructure allowance, with the independent controller/watchdog able to stop paid compute.

## Frozen checkpoints

`probe_core/resources/canonical-models.json` records public Hugging Face metadata for these exact revisions:

| Checkpoint | Revision | Weight bytes |
|---|---|---:|
| Qwen3-1.7B-Base | `ea980cb0a6c2ae4b936e82123acc929f1cec04c1` | 3,441,185,608 |
| Qwen3-1.7B | `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` | 4,063,515,592 |

All selected weights, tokenizer assets, configuration and license files total **7,532,122,402 bytes**. Weights use the published LFS SHA256. Smaller Git objects are verified against their Git blob identity and then receive a local SHA256 inventory. The preparation command never resolves `main`, executes remote code, reads an HF token, or downloads pickle weights. The frozen sources are the [Base revision](https://huggingface.co/Qwen/Qwen3-1.7B-Base/tree/ea980cb0a6c2ae4b936e82123acc929f1cec04c1) and [posttrained revision](https://huggingface.co/Qwen/Qwen3-1.7B/tree/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e).

Run these from an installed project environment. Planning is offline; download requires a reviewed byte allowance and at least 512 MiB of free space beyond missing assets:

```sh
python -m probe_core.model_assets plan --root /path/to/canonical-models
python -m probe_core.model_assets download --root /path/to/canonical-models --max-download-bytes 7532122402
python -m probe_core.model_assets inventory --root /path/to/canonical-models --repo Qwen/Qwen3-1.7B-Base > base-assets.json
python -m probe_core.model_assets inventory --root /path/to/canonical-models --repo Qwen/Qwen3-1.7B --thinking false > posttrained-assets.json
```

The download is resumable at verified whole-file boundaries. An existing `.partial` is preserved for explicit inspection rather than silently overwritten. A repeated download needs only the missing bytes. Published files are read-only regular files, with no symlinks or shared hardlinks. Transfer them to the network volume without changing their bytes, then rerun inventory there.

Base uses raw text or explicit token IDs. Posttrained inventories require `--thinking true` or `--thinking false`; the selected mode is bound into `ModelIdentity`, and the actual tokenizer template is hashed. The worker applies the template with that flag. Qwen documents this switch in its [official model card](https://huggingface.co/Qwen/Qwen3-1.7B#switching-between-thinking-and-non-thinking-mode). The worker caps context at 32,768 tokens even though the pinned posttrained config advertises a larger positional limit.

## Infrastructure diagnostic before model execution

`deploy/gpu/Dockerfile.diagnostic` uses the observed Linux/amd64 Python slim manifest digest and a dated Debian package snapshot. It installs SSH and libseccomp, with no PyTorch or models. `.github/workflows/diagnostic-image.yml` builds and publishes it; the resulting artifact records the immutable image digest separately from any execution result.

The only required startup input is `PUBLIC_KEY`, containing a single trusted Ed25519 public key. `PROBE_CGROUP_ROOT` optionally selects the proposed delegated cgroup directory. Expose only `22/tcp`; `/workspace` carries the persisted JSON report. The startup script prints SSH host-key fingerprints, runs `nvidia-smi`, probes an empty cgroup child, removes that child, and leaves key-authenticated SSH available for observing the separately scheduled provider stop. It never performs inference, downloads a model, holds a provider credential, or claims that its own process exit proves billing stopped.

The pinned PyTorch wheel uses CUDA 13. NVIDIA lists driver branch 580 as the minimum for that major version; the diagnostic checks the actual driver, exactly one GPU, and compute capability at least 8.0 for native BF16. See [NVIDIA's compatibility table](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html). The planned short-prompt worker batch reserves 20 GiB VRAM, 24 GiB RAM and four CPU cores; choose hardware with additional RAM for the supervisor and operating system.

The current worker requires a real writable **delegated cgroup-v2 subtree**, with CPU, memory and PID controllers enabled for children. The diagnostic checks the filesystem type, writes fixed limits into a temporary empty child and reads them back. It does not change host processes or enable controllers on behalf of the provider. An ordinary writable directory cannot imitate this result. A read-only mount or missing delegation blocks model execution. Public RunPod API documentation has not established a way to request this delegation; an image cannot grant itself missing host capabilities. Provider-specific alternatives require measured evidence and a separate reviewed implementation.

## Full worker image

The `.github/workflows/worker-image.yml` workflow requires the reviewed diagnostic image **including its digest**. Manual runs accept that reference as an input; changes to the image recipe or workflow on the configured branches use the digest pinned in the workflow. It builds `deploy/gpu/Dockerfile.worker` for Linux/amd64, installs the exact worker dependency graph from `uv.lock`, and records the actual source commit and lock hash inside the image. The uv bootstrap wheel is hash-pinned in `bootstrap-requirements.txt`. The final registry digest is recorded by the workflow. Model assets stay on the volume, outside the image. This follows [uv's locked Docker deployment pattern](https://docs.astral.sh/uv/guides/integration/docker/).

The equivalent reviewed local build is:

```sh
docker build --platform linux/amd64 --file deploy/gpu/Dockerfile.worker --build-arg DIAGNOSTIC_IMAGE=ghcr.io/OWNER/probe-mcp-diagnostic@sha256:EXACT_DIAGNOSTIC_DIGEST --build-arg SOURCE_COMMIT=EXACT_REVIEWED_COMMIT --tag OWNER/probe-mcp-worker:REVIEWED_VERSION .
```

Replace placeholders with verified values. Publish only after reviewing the source commit; use the returned registry digest in the controller's deployment request and `WorkerConfig.container_image_digest`. A build alone does not establish CUDA compatibility or successful inference.

The image bootstrap runs trusted SSH as container root, then the worker supervisor and numerical children as fixed UID/GID **10001** with a private home directory. It checks cgroup delegation under that unprivileged identity. A failed check exits; the external controller must still stop the paid Pod. The image does not contain cloud-management credentials, a Docker socket or an untrusted code endpoint.

Before the GPU diagnostic, a separate no-model subprocess under UID10001 must traverse/read every declared model and dataset asset, read its private config/token, and create/fsync/remove small probes in the private tensor/output directories. It also rejects model/dataset files owned by or writable by the worker. A root-owned `0440` file in a root-only directory is insufficient: staged parent traversal and GID10001 read access must both be correct. The bootstrap performs no automatic recursive ownership changes.

Prepare volume permissions before launch:

| Volume path | Owner/access |
|---|---|
| `/workspace/probe/models` and `/workspace/probe/datasets` | Trusted root ownership; group 10001 can traverse/read; worker cannot modify files |
| `/workspace/probe/tensors` and `/workspace/probe/attempts` | UID/GID 10001, directories `0700` |
| `/workspace/probe/config/worker.json` and `worker-token` | UID/GID 10001, files `0600`, private parent directory |

Build `worker.json` from the verified inventory's `model` and `assets`, registered dataset hashes, actual live price/region, exact image digest and source commit, `device: "cuda:0"`, `backend: "nnsight"`, and the measured delegated cgroup path. The default config/token paths are shown above; trusted startup variables `PROBE_WORKER_CONFIG` and `PROBE_WORKER_TOKEN_FILE` may override them. The execution API binds only to loopback port 8080. The SSH server permits forwarding to that port; the controller still supplies its separate bearer secret.

The bootstrap restarts a crashed **local supervisor** at most three times while SSH remains available. It neither restarts a Pod nor resubmits a job. The replacement supervisor adopts persisted PID/start-time/boot identity and the same attempt/deadline. Graceful SIGTERM closes the supervisor and terminates its children. `/run/probe-worker-supervisor.pid` records the current supervisor PID for a trusted operator's lifecycle test.

## Approved numerical and runtime acceptance

`backend_parity` is a typed calibration-only operation included in the exact approved batch. Its version-one suite has no configurable code or tolerance. It compares native HF, raw hooks and NNsight without intervention, then tests all four supported component types with captures, identity patches, zero ablation, zero/nonzero steering and hook removal. It also compares greedy native generation with the NNsight path. Comparisons are exact because the paths use the same eager operations and dtype. The token budget includes both generated sequences. The suite rejects canonical acceptance on CPU, wrong dtype, non-finite output and prompts exceeding 256 real tokens. It emits retained safetensors and a calibration JSON report with `scientific_evidence: false`.

Create an exact seven-job acceptance plan after worker configuration and public calibration prompts are fixed:

```sh
python -m probe_core.gpu_acceptance plan --worker-config worker.json --label base-acceptance --output base-plan.json
python -m probe_core.gpu_acceptance submit --plan base-plan.json --ledger /var/lib/probe-core/research.sqlite --output base-queued.json
```

Submission queues jobs only. It does not consume approval or start compute. Review the emitted exact batch hash, worker/image identity and runtime request through the normal human controller. The independent watchdog must acknowledge the allowance before the provider action. The plan contains:

1. The fixed backend parity suite.
2. Capture with retained artifact hashes.
3. A running capture to cancel through the trusted research/controller path.
4. A one-second execution deadline that must yield a positively stopped timeout.
5. A one-byte output budget that must fail policy without publishing accepted output.
6. A one-GiB VRAM budget that must yield a stopped OOM outcome.
7. A running capture whose supervisor is restarted while preserving the same fenced attempt.

Use two public text prompts of different rendered lengths so the real tokenizer and padding path are exercised. The plan generator selects the first two registered prompts. Keep the cancellation and supervisor-restart actions coordinated with the dispatcher: observe `RUNNING`, perform the named action, and retain the observed status/PID records. For the supervisor restart, a trusted operator can kill only the PID recorded in `/run/probe-worker-supervisor.pid`; the bootstrap should launch a new supervisor, while the child attempt and original absolute deadline remain unchanged. Do not replace a failed execution with a new attempt and call that restart adoption.

While the worker is reachable through the authenticated SSH tunnel, collect actual receipts and sealed artifact checks:

```sh
python -m probe_core.gpu_acceptance collect --plan base-plan.json --ledger /var/lib/probe-core/research.sqlite --worker-url http://127.0.0.1:LOCAL_FORWARDED_PORT --token-file /etc/probe-core/worker-token --output base-observations.json
```

The collector refuses missing/mismatched jobs, missing stop acknowledgments, wrong terminal outcomes, changed artifact bytes, and absent canonical GPU/image provenance. It includes observed usage receipts when the endpoint is supplied. Without that endpoint it can still inspect the authoritative ledger after shutdown. It deliberately leaves `lifecycle_acceptance_complete: false`: ledger rows alone cannot prove an operator changed the supervisor PID or a provider replaced a Pod.

Complete the lifecycle gate with actual before/after supervisor PID and same-attempt records, provider-confirmed stopped state, and a separately approved restart/replacement. Keep the same network volume. Re-read all pinned model hashes and retained artifact hashes after the new worker starts, and rerun its bounded parity job under the new allowance. Preserve the previous accepted manifests and cloud identity readbacks. A simulated provider result or a copied success JSON is not replacement evidence.

Use separate bounded allowances for Base and posttrained workers; `WorkerConfig` intentionally binds one model identity at a time. Stop and read back the old resource before replacement. Additional thinking/non-thinking contrasts require their explicit model identities and their own reviewed batches. These are engineering calibration checks, not hidden-holdout evaluation, independent scientific replication or a discovery claim.

## Local verification

The offline suite covers locked asset preparation, hash/size/path rejection, diagnostic failure modes, real tiny-Qwen parity, budget contracts, timeout classification and actual supervisor adoption:

```sh
python -m pytest -q tests/test_model_assets.py tests/test_gpu_diagnostic.py tests/test_backend_parity.py tests/test_gpu_acceptance.py tests/test_worker_timeouts.py
```

These tests use synthetic CPU fixtures where appropriate and cannot certify live CUDA execution. The live report must name the real checkpoint revisions, source commit, image digest, device/driver, approved allowance and observed artifact hashes.
