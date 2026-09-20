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

The download is resumable at verified whole-file boundaries. An existing `.partial` is preserved for explicit inspection rather than silently overwritten. A repeated download needs only the missing bytes. Published files are read-only regular files, with no symlinks or shared hardlinks. The default worker image bakes the Base inventory and two synthetic public prompts into root-owned, read-only paths. A separately managed persistent deployment can instead transfer a reviewed inventory without changing its bytes.

Base uses raw text or explicit token IDs. Posttrained inventories require `--thinking true` or `--thinking false`; the selected mode is bound into `ModelIdentity`, and the actual tokenizer template is hashed. The worker applies the template with that flag. Qwen documents this switch in its [official model card](https://huggingface.co/Qwen/Qwen3-1.7B#switching-between-thinking-and-non-thinking-mode). The worker caps context at 32,768 tokens even though the pinned posttrained config advertises a larger positional limit.

## Infrastructure diagnostic before model execution

`deploy/gpu/Dockerfile.diagnostic` uses the observed Linux/amd64 Python slim manifest digest and a dated Debian package snapshot. It installs SSH and libseccomp, with no PyTorch or models. `.github/workflows/diagnostic-image.yml` builds and publishes it; the resulting artifact records the immutable image digest separately from any execution result.

The only required startup input is `PUBLIC_KEY`, containing a single trusted Ed25519 public key. `PROBE_CGROUP_ROOT` optionally selects the proposed delegated cgroup directory. Expose only `22/tcp`; `/workspace` carries the persisted JSON report. The startup script prints SSH host-key fingerprints, runs `nvidia-smi`, probes an empty cgroup child, removes that child, and leaves key-authenticated SSH available for observing the separately scheduled provider stop. It never performs inference, downloads a model, holds a provider credential, or claims that its own process exit proves billing stopped.

The pinned PyTorch wheel uses CUDA 13. NVIDIA lists driver branch 580 as the minimum for that major version; the diagnostic checks the actual driver, exactly one GPU, and compute capability at least 8.0 for native BF16. See [NVIDIA's compatibility table](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html). The planned short-prompt worker batch reserves 20 GiB VRAM, 24 GiB RAM and four CPU cores; choose hardware with additional RAM for the supervisor and operating system.

The worker requires a real writable **delegated cgroup-v2 subtree**, with CPU, memory and PID controllers enabled for children. RunPod may supply a writable namespace root with these controllers available but not yet enabled for children. The trusted bootstrap now prepares that delegation before dropping privileges: it moves the Pod's existing processes into `probe-bootstrap`, enables the three controllers, and creates `probe-jobs/supervisor`. The worker owns only the delegated job subtree; the provider's outer resource limits are preserved. Read-only mounts, v1-only hosts, missing delegation and unexpected pre-existing groups still block model execution.

Before the empty-child probe, the diagnostic now records bounded cgroup mount,
membership, namespace, ownership and permission observations. It opens the three
management files for writing without writing any bytes, creating files or
truncating them; the exact access error distinguishes a read-only mount from
permission denial. Namespace identifiers and mount flags are observations, not
proof that the Pod exclusively owns the hierarchy.

When started as root, it also runs inspection-only code as numeric UID/GID10001
with no supplementary groups, inherited descriptors or capabilities. That child
reopens paths after dropping privileges. Root permission checks never certify
worker access. `worker_prerequisites_passed` requires the active empty-child
probe to have run as the verified worker identity, including `cgroup.kill`
availability and successful cleanup; a root diagnostic intentionally cannot
grant that result. These probes still do not prove enforcement under load.
The diagnostic itself remains observational except for its temporary empty-child
probe. The separate trusted bootstrap validates the provider namespace before
preparing its child groups. Linux distinguishes controller availability from
enabling controllers for children; see [cgroup v2 delegation](https://docs.kernel.org/admin-guide/cgroup-v2.html#delegation).

`deploy/gpu/accept-resources.py` provides a separate bounded live acceptance gate:
CPU throttling under load, memory exhaustion, process creation refusal and whole
group cleanup, including a descendant in a separate session. It runs as UID10001
from the supervisor leaf and modifies only freshly created test groups. Passing
local tests or reading configured limits does not substitute for this live result.

## Full worker image

The `.github/workflows/worker-image.yml` workflow requires the reviewed diagnostic image **including its digest**. Manual runs accept that reference as an input; changes to the image recipe or workflow on the configured branches use the digest pinned in the workflow. It builds `deploy/gpu/Dockerfile.worker` for Linux/amd64, installs the exact worker dependency graph from `uv.lock`, and records the actual source commit and lock hash inside the image. The uv bootstrap wheel is hash-pinned in `bootstrap-requirements.txt`. The final registry digest is recorded by the workflow. The build-time `public-assets.json` inventory binds each public asset by byte count and SHA256. `bake-assets.py` downloads only frozen public Hugging Face URLs and verifies every file before publishing the image. These assets are cached under `/opt/probe-assets`; model license files are retained. This follows [uv's locked Docker deployment pattern](https://docs.astral.sh/uv/guides/integration/docker/).

The equivalent reviewed local build is:

```sh
docker build --platform linux/amd64 --file deploy/gpu/Dockerfile.worker --build-arg DIAGNOSTIC_IMAGE=ghcr.io/OWNER/probe-mcp-diagnostic@sha256:EXACT_DIAGNOSTIC_DIGEST --build-arg SOURCE_COMMIT=EXACT_REVIEWED_COMMIT --tag OWNER/probe-mcp-worker:REVIEWED_VERSION .
```

Replace placeholders with verified values. Publish only after reviewing the source commit; use the returned registry digest in the controller's deployment request and `WorkerConfig.container_image_digest`. A build alone does not establish CUDA compatibility or successful inference.

Manual image builds select an `asset_profile`: `base` (the default, using `public-assets.json`) or `posttrained` (using `public-assets-posttrained.json`). Each profile contains one checkpoint and the same public calibration prompts. The posttrained inventory pins Qwen3-1.7B revision `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`; its separate worker configuration must bind the tokenizer/chat template and an explicit thinking mode. Posttrained tags use `posttrained-git-COMMIT`, while Base retains `git-COMMIT`. Both build receipts record the selected profile and actual manifest hash. Deployments use the resolved digest, never a mutable tag. For an equivalent local posttrained build, add `--build-arg PUBLIC_ASSETS_MANIFEST=deploy/gpu/public-assets-posttrained.json`. Publishing either profile does not modify any installed controller or existing Pod.

The image bootstrap runs trusted SSH as container root, then the worker supervisor and numerical children as fixed UID/GID **10001** with a private home directory. Before dropping privileges, each supervisor enters `probe-jobs/supervisor`; its numerical children can then move into individual attempt groups beneath the same delegated parent. It checks cgroup access under that unprivileged identity. A failed check exits; the external controller must still delete the paid Pod. The image does not contain cloud-management credentials, a Docker socket or an untrusted code endpoint. These GPU limits govern the fixed trusted operations; arbitrary untrusted Python continues to use the separate CPU sandbox.

Before the GPU diagnostic, a separate no-model subprocess under UID10001 must traverse/read every declared model and dataset asset, read its private config/token, and create/fsync/remove small probes in the private tensor/output directories. It also rejects model/dataset files owned by or writable by the worker. A root-owned `0440` file in a root-only directory is insufficient: staged parent traversal and GID10001 read access must both be correct. The bootstrap performs no automatic recursive ownership changes.

The disposable worker uses these fixed permissions:

| Volume path | Owner/access |
|---|---|
| `/opt/probe-assets/models` and `/opt/probe-assets/datasets` | Root-owned image assets; directories `0755`, files `0444`; worker cannot modify them |
| `/workspace/probe/tensors` and `/workspace/probe/attempts` | UID/GID 10001, directories `0700` |
| `/workspace/probe/config/worker.json` and `worker-token` | UID/GID 10001, files `0600`, private parent directory |

Build `worker.json` from the verified inventory's `model` and `assets`, registered dataset hashes, actual live price/region, exact image digest and source commit, `device: "cuda:0"`, `backend: "nnsight"`, and `cgroup_directory: "/sys/fs/cgroup/probe-jobs"`. The config/token paths are fixed as shown above. After validating the Pod cgroup namespace, bootstrap starts authenticated SSH and waits under the original approval deadline. The trusted controller stages a bounded data-only config/token bundle through pinned SSH, then invokes `gpu_launch --configure` at a fixed path. A root-owned ready record binds that exact bundle; an uncertain reply cannot authorize a different configuration or a second provisioning request. Configuration validation, actual worker file access and all live resource gates precede the numerical supervisor. The execution API binds only to loopback port 8080. The SSH server permits forwarding to that port; the controller still supplies its separate bearer secret.

The bootstrap restarts a crashed **local supervisor** at most three times while SSH remains available. It neither restarts a Pod nor resubmits a job. The replacement supervisor adopts persisted PID/start-time/boot identity and the same attempt/deadline. Graceful SIGTERM closes the supervisor and terminates its children. The trusted lifecycle helper verifies `/run/probe-worker-supervisor.pid` against the actual process, its credentials, command and bootstrap parent before a restart test.

## Installed single-job calibration

`gpu_acceptance_runner` provides a narrow installed path for one public fixed case: backend parity, capture retention, hard deadline, output limit, VRAM limit, cancellation, supervisor restart, tunnel reconnection or public direction transfer. A `probe-research` one-shot submits the fixed job and compute request through the existing facade; a separate trusted process waits for the existing human approval, binds the observed Pod and SSH host key, stages the configuration, dispatches the exact job, seals its evidence and deletes the Pod with independent absence readback. Neither runner mode approves compute.

Prepare each additional runtime case by selecting exactly one case from the generated plan, preserving its label and complete case contents. Pin the resulting plan bytes in the runner configuration before submission. Capture, limit and lifecycle cases must match the generated recipe exactly, including action, operation, deterministic inputs, limits and idempotency key. Each run uses its own existing approval and at most 900 seconds, with startup and collection/deletion sharing that allowance. The runner still refuses multi-case batches and replacement requests.

For cancellation and supervisor restart, the runner keeps the dispatcher renewing the same lease while a bounded observer waits for the worker's verified execution-start marker. It then calls the existing one-shot action helper for the exact job, attempt and approval. The helper configuration hash comes from the acknowledged canonical worker configuration, whose bytes may differ from the public input file's whitespace. The private `actions` directory under the run's trusted state retains the intent, acknowledgment and result. Both the expected terminal job outcome and revalidated action proof are required to pass. A fast completion, missing marker, lost action response or missing transcript cannot count as a successful lifecycle test. Teardown closes the action gate before deleting the Pod; it cannot signal a different attempt or extend either deadline. These cases establish neither transport reconnection nor provider replacement.

The separate `tunnel-reconnect` case also waits for the exact live execution-start evidence. After its durable intent, the dispatcher replaces only its owned local SSH process between ticks, using the same host, SSH port, key binding and loopback port. It renews the existing lease only while still valid, within the unchanged execution deadline, and refuses a reconnect with less than 25 seconds of execution time remaining. It does not repeat configuration, tensor staging or job submission. Passing requires the old SSH process to be stopped, a different owned SSH process, unchanged worker child/supervisor and request identities, a fresh running receipt, and the final successful capture manifest. Missing or altered transcripts, an expired lease, fast completion or an uncertain connection remain inconclusive. The standalone lifecycle-action CLI cannot perform this runner-owned transport action. Provider replacement remains separate evidence.

Capture acceptance requires a retained `tensors.safetensors` artifact with verified bytes and the expected model/image/source/region/price provenance. An expected limit failure has no success manifest: it passes only with matching ledger state and failure code and an independently read, stopped receipt for the exact job and attempt. The worker must report `ExecutionDeadlineExceeded` for the wall deadline, `OutputLimitExceeded` for output, or `OutOfMemoryError` for the CUDA allocation limit. A generic policy failure, host RAM limit or CPU time limit cannot satisfy those checks. The child uses `TimeoutError` for both CPU and wall limits, so that ambiguous code alone is insufficient. The hard-deadline case also accepts the ledger-derived reason `execution deadline elapsed`, because the dispatcher expires the one-second lease before retaining the worker outcome; the exact stopped `ExecutionDeadlineExceeded` receipt is still required. Successful case results retain the worker receipt hash; failure cases remain engineering evidence, not numerical or scientific success.

The separate `fixed_direction_plan` constructor preserves the prepared `public-direction-transfer` plan: its label is also its idempotency key, and it uses the existing 120-second capture inputs/limits with `Steer` at layer 14 residual, last token, strength 0.5. Its sole input is the previously prepared 8288-byte float32 basis vector `e_0`, width 2048, named `public_basis_direction`, with SHA256 `3a8c413d0c6d097b35489c6a17d110116eb49135b671fc3109927238fced4a44`. Direction transfer remains separate from the ordinary eight-case runtime generator. This vector is public calibration data and was not learned from model activations or behavior.

Before this case is approved, the existing prepared source must be registered with `ArtifactStore.register(source, expected_sha256=...)` as `probe-trusted` in `/var/lib/probe-core/input-artifacts`. This requires a separately reviewed administrator setup step; the runner neither imports the human-owned preparation directory nor creates a missing registry. Preserve its private directories and immutable files. No worker image change is required.

Direction staging verifies the registered controller bytes, inspects the remote tensor store through the already pinned SSH connection to confirm it is empty, and uses the normal authenticated tensor PUT. It retains the actual upload receipt, checks its digest/name/shape/dtype, and independently reads and hashes the exact remote file through a bounded fixed SSH command before the execution POST. The readback refuses symlinks, hardlinks, unexpected files, wrong ownership/permissions and excessive size. `direction-transfer.json` binds those observations to the complete execution request, including its attempt, approval and original deadline. Collection rechecks the retained controller bytes, the stopped worker receipt, the sealed steering manifest's full intervention hash and its output artifact hashes. A success receipt without this transfer proof cannot pass. The result remains engineering evidence with `scientific_evidence:false`; it does not claim a meaningful behavioral effect for the basis vector.

The two calibration units are started explicitly, never enabled for automatic boot. `activate-gpu-calibration.py` installs their pinned inputs only after a verified application upgrade, idle history checks and installed identity acceptance. The public plan and worker config contain no bearer token; private SSH/token files remain accessible only to the trusted account. An interrupted or failed calibration is not silently replayed.

Startup failures retain fixed transport diagnostics: upload/configure/tunnel phase, integer exit status when observed, timeout status and a fixed classification. Nonzero command output is accepted only when its complete stdout is the launcher's exact `WORKER_BOOTSTRAP_FAILED` JSON record, with an allowed exception type and integer source line. Raw stdout, stderr, arguments, addresses and the private configuration bundle are never copied into diagnostics. A failed configuration keeps its diagnostic beside the existing intent and cannot replay the upload or remote configure command. Before deleting a failed startup, the runner makes one best-effort provider-log observation, limited to four such records and one second of caller wait. A stalled or failed observation cannot prevent deletion; the observation is skipped when the approval deadline leaves no time.

Provider lookup and log HTTP failures retain only their fixed phase, integer HTTP status, content type category (`json`, `html` or `other`), numeric `Retry-After` seconds when bounded to one day, and whether the exact `cf-mitigated: challenge` header was present. Error bodies are not read, and raw headers, addresses and keys are not retained. These observations distinguish a JSON refusal from an HTML challenge without inferring its cause. They do not change retry timing, authentication refusal handling, the original deadline or provider deletion.

The verified host key uses a bare address at port 22 and `[address]:port` otherwise, matching the [OpenSSH known_hosts format](https://man.openbsd.org/sshd#SSH_KNOWN_HOSTS_FILE_FORMAT). Both IPv4 and IPv6 retain strict host-key verification; correcting the lookup format does not authorize an unknown key.

## Approved numerical and runtime acceptance

`backend_parity` is a typed calibration-only operation included in the exact approved batch. Its version-one suite has no configurable code or tolerance. It compares native HF, raw hooks and NNsight without intervention, then tests all four supported component types with captures, identity patches, zero ablation, zero/nonzero steering and hook removal. It also compares greedy native generation with the NNsight path. Comparisons are exact because the paths use the same eager operations and dtype. The token budget includes both generated sequences. The suite rejects canonical acceptance on CPU, wrong dtype, non-finite output and prompts exceeding 256 real tokens. It emits retained safetensors and a calibration JSON report with `scientific_evidence: false`.

Create an exact eight-job acceptance plan after worker configuration and public calibration prompts are fixed:

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
8. A running capture whose owned SSH tunnel is replaced while preserving the same fenced attempt and worker processes.

Use two public text prompts of different rendered lengths so the real tokenizer and padding path are exercised. The plan generator selects the first two registered prompts.

### Cancellation and supervisor restart

Run `probe_core.gpu_acceptance_actions` as the trusted ledger owner while the normal dispatcher continues renewing leases and reconciling receipts. This command targets an explicit already-running calibration attempt. It cannot approve compute, submit a job or call the provider.

Copy `deploy/gpu/actions.json.example` into a private, owned `0600` settings file. Replace its placeholders with the existing loopback tunnel endpoint, private bearer-secret path, verified SSH identity and host key, worker config/token paths, and SHA256 of the exact worker config file bytes. The lifecycle helper uses the image's trusted root SSH entry point; the supervisor and job still run as UID/GID10001. The example is for the reviewed GPU image, whose project interpreter is `/opt/probe-core/venv/bin/python`.

For each action case, obtain its case name from the plan and its current job, attempt and approval IDs from the trusted ledger. For example:

```sh
python -m probe_core.gpu_acceptance_actions --plan base-plan.json --ledger /var/lib/probe-core/research.sqlite --settings /etc/probe-core/gpu-actions.json --action-directory /var/lib/probe-core/gpu-actions --case EXACT_CASE_NAME --job-id EXACT_JOB_ID --attempt-id EXACT_ATTEMPT_ID --approval-id EXACT_APPROVAL_ID
```

The command checks the active approval, both leases, execution deadline, worker identity and execution-start marker before recording its intent. Cancellation uses the exact attempt's authenticated worker endpoint. Restart uses a fixed SSH helper command that verifies the same supervisor again, then sends SIGKILL through its process descriptor. Only the supervisor is signaled; the child must remain alive with the same request, approval, PID identity and deadlines under the replacement supervisor.

Both hosts write exclusive action records with file and directory synchronization. The worker's restart records are in `/run/probe-lifecycle`: they survive a supervisor restart, but are not promised to survive Pod replacement. The controller's private intent, acknowledgment and result files are the retained acceptance transcript; preserve that directory on durable controller storage. A retry cannot repeat a signal after an intent has been recorded, including when its response was lost. An acknowledged action may be observed again within a bounded interval; an intent without an acknowledgment remains uncertain. A job that completes before cancellation, a missing stop proof, or an unavailable replacement remains inconclusive. Cancellation passes only with a worker-written signal record and positive whole-job termination evidence. None of these outcomes proves provider shutdown or replacement.

While the worker is reachable through the authenticated SSH tunnel, collect actual receipts and sealed artifact checks:

```sh
python -m probe_core.gpu_acceptance collect --plan base-plan.json --ledger /var/lib/probe-core/research.sqlite --worker-url http://127.0.0.1:LOCAL_FORWARDED_PORT --token-file /etc/probe-core/worker-token --action-directory /var/lib/probe-core/gpu-actions --output base-observations.json
```

The collector refuses missing/mismatched jobs, missing stop acknowledgments, wrong terminal outcomes, changed artifact bytes, and absent canonical GPU/image provenance. It includes observed usage receipts when the endpoint is supplied. Without that endpoint it can still inspect the authoritative ledger after shutdown, but expected failures remain inconclusive: the ledger alone does not prove the actual worker outcome. Cancellation requires an exact job/attempt receipt showing `CANCELLED`, `failure_kind=cancelled` and positive stop evidence. A job that finished before cancellation arrived cannot pass that check. When an action directory is supplied, the collector separately revalidates its retained cancellation and restart transcripts without issuing any action. Missing or malformed transcripts do not pass. It deliberately leaves `lifecycle_acceptance_complete: false` until separate provider replacement and storage readback evidence is available.

Complete the lifecycle gate with actual before/after supervisor PID and same-attempt records, provider-confirmed stopped state, and a separately approved restart/replacement. For disposable research, use the same verified public image and preserve canonical artifacts on the controller. Re-read all pinned model hashes and retained controller artifact hashes after the new worker starts, and rerun its bounded parity job under the new allowance. Preserve the previous accepted manifests and cloud identity readbacks. A simulated provider result or a copied success JSON is not replacement evidence.

Use separate bounded allowances for Base and posttrained workers; `WorkerConfig` intentionally binds one model identity at a time. Stop and read back the old resource before replacement. Additional thinking/non-thinking contrasts require their explicit model identities and their own reviewed batches. These are engineering calibration checks, not hidden-holdout evaluation, independent scientific replication or a discovery claim.

## Startup and supervised retry

The controller may observe RunPod `status=RUNNING` before the runtime and direct SSH
port are published. Missing or null connection details remain inside the bounded
startup wait; malformed details or mismatched image, price, region or host key
still stop the run. No configuration is uploaded until the connection is verified.

On September 20, 2026, the installed controller update passed a fresh Drive
upload/download/restore, 16 CPU containment checks and 26 account checks. The first
queued canonical calibration failed during connection discovery and was deleted
before SSH configuration or model execution. A null-runtime response reproduces
the failure locally; the original generic error did not retain its traceback.
That failed attempt is not parity evidence. Its consumed approval and original records must be retained; a retry
uses a fresh job and approval under the same cumulative operator budget.

The next attempt reached the old three-minute startup cutoff while RunPod was
still downloading and extracting the pinned image, then confirmed Pod deletion
without running a model. The image is 5.90 GB compressed. Startup now uses the
existing approval deadline minus the full job runtime and a 120-second allowance
for result collection and deletion. For the current 900-second approval and
240-second job, this gives up to 540 seconds from approval consumption to worker
readiness and dispatch. It does not extend the paid allowance. Connection,
configuration and readiness must all finish before that cutoff; a late worker
cannot start a job. A subsequent live attempt loaded the image after about three
and a half minutes, then failed before SSH or inference at the credential guard.

[RunPod automatically supplies a Pod-scoped API key](https://docs.runpod.io/pods/templates/environment-variables),
even though the controller does not send its management credential in the launch
environment. The observed credential-guard failure is consistent with this
injection; the guard did not log the triggering key or value.
The corrected launcher re-executes its fixed interpreter in isolated mode with
an allowlisted environment before any cgroup, SSH or model work. SSH and every
worker process also receive explicitly constructed environments. This removes
inherited credentials from their exec-time environment, including the bytes
exposed through [Linux process environment files](https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html).
The next immutable image still needs live acceptance; the failed Pod was deleted
and produced no numerical evidence.

The incident-specific `deploy/retry-gpu-calibration.py` helper has two phases
around the normal wheel upgrade. It first verifies the stopped request, closed
allowance, provider absence and zero execution attempts, then cancels only that
pending job through the research service. After a verified upgrade, it preserves
the original state directories and prepares a new calibration in separate
subdirectories. The first two recovery schemas allow only a new plan label,
idempotency key and state/configuration paths; the model, worker image, limits
and service permissions remain fixed. Neither phase issues a compute approval.
The second recovery also pins the completed first recovery's manifest, receipts
and nested configuration paths, and verifies the exact startup-timeout result.
Each failed job and consumed approval remains in the ledger; none is replayed.
The third recovery additionally pins the complete canonical failed result and
the previous two completed recoveries. It may replace only the worker image and
software provenance, with the same model, data, operations, limits and deployment
scope. Its fresh public worker configuration contains no bearer token or key.
The fourth recovery pins the exact configuration/tunnel failure and all three
completed recoveries. It changes only the controller software and fresh job/state
identities, preserving the third recovery's worker configuration, image digest
and source identity. Controller and remote-worker source commits are recorded
separately. The ordinary backup, sandbox and account checks still precede the
fresh calibration, and the recovery itself cannot start paid compute.

## Local verification

The offline suite covers locked asset preparation, hash/size/path rejection, diagnostic failure modes, real tiny-Qwen parity, budget contracts, timeout classification and actual supervisor adoption:

```sh
python -m pytest -q tests/test_model_assets.py tests/test_gpu_diagnostic.py tests/test_backend_parity.py tests/test_gpu_acceptance.py tests/test_gpu_acceptance_actions.py tests/test_gpu_lifecycle.py tests/test_worker_cancellation.py tests/test_worker_execution_started.py tests/test_worker_timeouts.py tests/test_worker_process_restart.py
```

The process-restart test kills a real supervisor subprocess while a bounded CPU fixture runs through the production child entry point. Its replacement adopts the same live PID/start/boot identity, persisted request, attempt, approval and deadlines without submitting again, then stops the original child at its original deadline. It verifies supervision and recovery; the numerical payload is deliberately synthetic.

The helper tests exercise owned local processes and inject malformed identity, file, deadline and replay states. The controller action tests use a real ledger with simulated remote observations. Cancellation tests include actual CPU process cleanup and simulated GPU cgroup failures. These checks do not substitute for running the helper over the deployed SSH connection or enforcing limits on a real GPU host.

These tests use synthetic CPU fixtures where appropriate and cannot certify live CUDA execution. The live report must name the real checkpoint revisions, source commit, image digest, device/driver, approved allowance and observed artifact hashes.
