# Auto Interpretability Lab: Trusted worker and dispatcher

[Project overview](../README.md) · [Status](../STATUS.md) · [Validation](validation.md)

This guide covers Auto Interpretability Lab's worker/dispatcher contracts. The
managed HTTP worker described here has not passed a live canonical GPU case;
the separate supervised profile has. See [STATUS.md](../STATUS.md) for current
standing.

The worker executes fixed numerical operations using local Hugging Face weights and NNsight. The dispatcher follows the controller's already approved, consumed compute interval and publishes verified artifacts to the ledger. Neither process can authorize compute, provision a Pod, renew an allowance, or run agent-supplied Python. Provider actions belong to the separate controller; these instructions do not provision or start cloud resources.

## Deployment identities and configuration

Run the dispatcher as the same trusted identity that owns the ledger and retained input artifact store (`probe-trusted` in the example units). Keep its bearer secret, SSH key, known-hosts file, configuration, and transfer directory inaccessible to the research agent. The worker has a different dedicated identity on its execution host. Give that identity read-only model/dataset assets and private writable tensor and output directories. The controller sends no provider API key. The trusted image launcher removes
provider-injected credentials before starting worker and SSH processes.

Install from the reviewed source commit and locked environment. Before an acceptance run, commit the source and record that actual Git HEAD. A native CPU worker sets `container_image_digest` to null and `environment_lock_path` to the actual installed `uv.lock`; the manifest hashes those bytes. A container worker supplies the real pinned image digest from the trusted deployment and the reviewed code commit. The worker observes installed library versions and hashes config, tokenizer and weights. Git and image identities are trusted deployment attestations, not values a job can select. A lock-file hash is never used as an image digest.

`deploy/dispatcher.json.example` is a local CPU example. Replace every placeholder, create an owned mode `0600` configuration and secret file, and place the same high-entropy bearer secret at both ends. The service refuses unsafe secret/config ownership or permissions. A remote dispatcher replaces `base_url` with:

```json
{
  "ssh": {
    "host": "reviewed-worker-host.example",
    "user": "root",
    "identity_file": "/etc/probe-core/worker-ssh-key",
    "known_hosts_file": "/etc/probe-core/worker-known-hosts",
    "ssh_port": 22,
    "remote_port": 8080
  }
}
```

The example uses the reviewed GPU image's root SSH entry point; its model worker still runs as UID/GID10001. A separately configured native host can use its dedicated SSH account. The host key must be verified through a trusted channel before it is added. SSH uses an explicit key, explicit known-hosts file, strict host-key checks, no interactive authentication, and a loopback-only forward. The HTTP client ignores proxy environment variables and refuses redirects. Only a literal `http://127.0.0.1:<port>` URL is accepted. If connectivity fails, attempts remain unresolved until the worker can be queried or its termination is positively established; restarting the dispatcher never blindly resubmits them. When its owned SSH process exits, the dispatcher exits with an error so the supplied systemd unit restarts it and opens a new pinned-key tunnel. It then queries the existing attempt with the original approval and deadline. An HTTP outage while SSH remains alive is retried within the existing dispatcher process.

The worker configuration is a `WorkerConfig` JSON document with these fields:

| Field | Deployment value |
|---|---|
| `model_directory`, `model` | Read-only local safetensors checkpoint and exact `ModelIdentity` used in submitted jobs |
| `assets` | `{ "path": "relative-file", "sha256": "sha256:<actual digest>" }` for config, every weight shard, and any tokenizer assets |
| `datasets` | `{ "path": "/private/prompts.json", "sha256": "sha256:<actual digest>" }` for every allowed input dataset |
| `tensor_directory`, `output_directory` | Private writable input staging and durable attempt directories |
| `backend` | `nnsight` for execution; `reference` is the raw PyTorch parity implementation |
| `device` | `cpu` for local calibration or `cuda:0` for a separately approved GPU deployment |
| `code_git_commit` | Actual reviewed source commit |
| `container_image_digest` | Actual trusted image digest, or null for native CPU |
| `environment_lock_path` | Actual installed lock file, required when no container image is claimed |
| `provider_backend`, `region`, `live_price_usd_per_hour` | `local_cpu`, the local region label, and `0.0` for CPU; trusted observed RunPod values for CUDA |
| `cgroup_directory` | A delegated writable cgroup-v2 subtree for CUDA; optional for CPU |
| `max_request_bytes`, `max_tensor_bytes` | Optional deployment ceilings, default 256 KiB and 1 GiB |

The model uses the canonical unquantized Qwen3 configuration. `probe/testing-tiny-qwen3` is reserved for offline CPU calibration. Tensor weights must be safetensors; pickle weights and remote-code loading are rejected. For posttrained chat inputs, list and hash both `tokenizer.json` and `tokenizer_config.json`. Dataset JSON has `schema_version: 1` and a `prompts` array; each prompt has a unique `prompt_id` and exactly one of `text` or `token_ids`. The dataset revision hashes the entire dataset file. `prompt_set_hash` hashes the ordered selected prompt records using `worker.prompt_set_hash`.

The example units are installation artifacts, not installed services. Create the users, private directories and configuration first. Install `deploy/probe-dispatcher.service` on the controller host and, for native CPU acceptance, `deploy/probe-worker.service` on the execution host. Install the package into `/opt/probe-core/venv`. A GPU container can use the same worker CLI as its supervised entry point:

```sh
python -m probe_core.worker --config /etc/probe-core/worker.json --token-file /etc/probe-core/worker-token --port 8080
python -m probe_core.dispatcher --config /etc/probe-core/dispatcher.json
```

The worker listens only on `127.0.0.1`; do not publish its HTTP port externally. The controller database must already be initialized. The dispatcher configuration's `worker_id` must equal the logical worker bound into the approved controller request. The service dispatches only while that exact request is `RUNNING`, with one globally unresolved attempt. Its configured poll interval must be less than one third of the lease duration. Stopping the dispatcher does not renew execution authority: the independent controller watchdog and worker's absolute deadline remain in force.

## Numerical and artifact contract

All positions refer to each unpadded prompt. Integer zero is the first real token and `last` is the final real token. Batches use left padding with matching attention masks and position IDs; selecting padding is rejected. A declared thinking mode requires text inputs and a hash-verified chat template, invoked with the actual `enable_thinking` flag. Preencoded token IDs cannot claim that the worker applied a chat/thinking template.

| Component | Tensor being observed or edited |
|---|---|
| `residual` | Decoder-layer output after attention and MLP residual additions |
| `attention_output` | Attention result after its output projection |
| `mlp_output` | MLP output before its residual addition |
| `attention_head` | One query-head slice of the attention output-projection input |

Capture returns `[batch, selected_positions, width]`. Mean and variance reductions operate across the batch axis only. Patch tensors must have exactly that shape; steering and mean-ablation baselines must have shape `[width]`. Tensor hashes, names, dtype, shape, finite values, size and safe paths are checked. Capture, patch, ablate and steer produce selected observations and last-real-token logits from a prefill forward pass. Generation is a separate bounded, unmodified autoregressive operation; interventions are not silently applied to generated tokens. Probe fitting uses a deterministic disjoint row split and returns its split indices with fitted safetensors weights. It does not claim held-out confirmation.

Module manifests, weight statistics and bounded parameter slices are read-only operations. `generate` respects the submitted generation limit, EOS and the model context limit. Model loading is local-only. The main backend uses real NNsight traces; raw PyTorch hooks provide a parity reference. A narrow compatibility adapter closes NNsight 0.7's process-lifetime devnull handle and handles astor's deprecated AST checks on Python 3.13; it does not disable general warnings.

The controller's private artifact store holds each tensor file at `<raw SHA256>/tensor.safetensors`. Jobs bind that relative path, its prefixed SHA256 and tensor name before compute approval. The dispatcher verifies each registered file and uploads it over the authenticated connection before submitting execution. The worker validates the safetensors header and publishes it under an immutable content-addressed path. Thus a retained capture or CPU-generated direction can be used by a fresh worker without accepting arbitrary controller host paths.

The versioned endpoints are `POST /v1/jobs`, `GET /v1/jobs/{attempt_id}`, `POST /v1/jobs/{attempt_id}/cancel`, `GET /v1/jobs/{attempt_id}/artifacts/{retained_path}` and `PUT /v1/tensors/{raw_sha256}`. All require the bearer secret. Request/receipt bodies are defined by `ExecutionRequest` and `ExecutionReceipt`; unknown fields are rejected. Submission is asynchronous and idempotent for the same exact attempt. Status and downloads have controller-side byte limits; tensor uploads are streamed with an explicit bounded content length. No endpoint accepts Python code or pickle.

## Runtime enforcement and acceptance gate

The supervisor launches each job in a separate process, clears inherited secrets, installs seccomp restrictions, applies CPU/RAM/file limits, and monitors wall time, resident memory and output bytes. Before CUDA initialization, seccomp denies `socket()` for every domain except `AF_UNIX`; local UNIX socket creation is required by the CUDA driver. It continues to deny every `connect()`, `sendto()`, and `sendmsg()` call, including on UNIX sockets and inherited descriptors. This does not exempt descriptor numbers or move CUDA initialization ahead of the filter. CUDA configuration additionally requires delegated cgroup-v2 memory, CPU and PID enforcement and applies the requested PyTorch VRAM allocation ceiling. On timeout or cancellation it kills the process group (and delegated cgroup where configured) and confirms termination before issuing a stopped receipt. Restart reconciliation fences persisted process identity with boot ID and process start ticks. A terminal state without a positive stop receipt cannot free a ledger execution slot.

Controller cancellation is polled even when issued through a separate research process. The provider simulator cannot prove that a real CPU child exited: its stop leaves the interval pending until the dispatcher obtains the worker's stopped receipt. Successful results also require stopped execution, bounded hash-checked downloads, full ledger manifest validation, and sealing into the authoritative artifact directory.

Cancellation first reconciles completion and deadline outcomes under the supervisor lock. A completed result keeps its original outcome. A live cancellation signals through a descriptor for the original process, then requires the entire job scope to be empty before acknowledging stop. For a CPU worker without a delegated cgroup, descriptor-based process-group signaling requires the Linux 6.9+ `PIDFD_SIGNAL_PROCESS_GROUP` capability. An older kernel explicitly refuses live cancellation; it does not fall back to a potentially reused numeric PID. CUDA workers require the validated attempt cgroup's kill and empty-state checks. These are model-worker requirements, separate from the rootless Podman sandbox.

The child writes `execution-started.json` only after release and runtime setup, before loading the numerical engine. It binds the attempt, process identity and canonical request/config hashes. A successful live cancellation writes `cancellation.json` after its stopped receipt, binding the signal and whole-job cleanup to that execution. Completion races and ordinary timeout results never manufacture cancellation proof. If a crash prevents proof publication, acceptance remains inconclusive even when the stop receipt exists. These records sit outside the scientific artifact tree. The [GPU acceptance guide](gpu-deployment.md#cancellation-and-supervisor-restart) describes the trusted action runner and retained evidence collector.

If a leader exits while descendants remain, the supervisor cleans the validated attempt cgroup independently of the leader. A CPU worker without a cgroup retains a process descriptor captured while the leader's identity matched, so it can stop that original group after the leader exits. A newly started CPU supervisor that first encounters an already-dead leader has no such retained identity. It leaves that orphaned scope unresolved rather than signaling a potentially reused numeric process group. Deployments needing recovery across that combined failure require delegated cgroups.

Run the offline acceptance gate from the committed project with:

```sh
.venv/bin/python -m pytest -q tests/test_worker.py tests/test_dispatcher.py
```

These tests execute a randomly initialized, tiny real HF Qwen3 on CPU, including NNsight no-op/intervention parity, safetensors capture-to-patch and CPU-direction-to-steer transfer, process termination, actual authenticated loopback HTTP, and a separate dispatcher daemon driven by a controller and independent watchdog process. Receipts record the actual repository HEAD and lock-file hash. They do not establish CUDA/RunPod performance, live GPU memory enforcement, or a scientific result. Every assigned GPU host must pass its own cgroup and SSH gates before model
execution; passing those gates on a prior host cannot certify a new assignment. Confirmatory and replication jobs are explicitly refused until the separate trusted evaluator supplies the held-out scoring boundary.
