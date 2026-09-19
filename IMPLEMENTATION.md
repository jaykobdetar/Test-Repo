# Implementation and acceptance boundaries

The original Phase 3–5 milestone established local acceptance using a provider
simulator and a small HF/PyTorch model on CPU. The deployment work adds a guarded
RunPod adapter, pinned asset preparation, service installation, and backup/restore
tooling. Actual canonical BF16/CUDA execution and provider shutdown still require
separate live evidence; a successful build or CPU test does not supply it.

## Boundaries

`agent → stdio MCP → research Unix socket → trusted research facade → ledger`

`research facade → controller request socket → pending human approval`

`human OS identity → controller admin socket → one bounded compute interval`

`trusted dispatcher → authenticated loopback HTTP over SSH → trusted worker`

The independent watchdog reads durable deadlines and uses a stop-only adapter.
The researcher never receives the controller object, approval token, ledger
connection, worker credential, SSH key, or result-acceptance endpoint.

The controller and research facade share one trusted service UID because the
ledger and audit are owner-protected. The model-facing MCP process uses a
different UID. The watchdog uses another UID with read-only ledger access and
its own durable state. Human administration uses an explicitly allowlisted UID.
Filesystem permissions and socket peer credentials enforce these boundaries.
Running all roles under one user is a local test profile, not a secure deployment.

## Reproducible development

Use Linux and Python 3.13. `uv.lock` pins the complete dependency graph. Install
with `uv sync --locked --all-extras`, then run `uv run --locked python -m pytest`.
Local service tests require permission to bind Unix and loopback sockets.
The worker tests create a tiny random Qwen3 model locally; they do not download
the canonical scientific checkpoints. Manifests identify this fixture and
zero-GPU CPU execution explicitly.

The CI workflow installs the locked environment and runs the tests. Rootless
sandbox acceptance is a separate mandatory gate on a host with Podman and
delegated cgroup v2 CPU/memory/process controllers; see
[the sandbox instructions](deploy/sandbox/README.md). Skipped containment tests
do not prove Phase 5 complete.

## Phase 3 requirements

- The controller obtains price and storage estimates from its provider adapter.
  An unavailable/stale price, GPU price at or above $1.50/hour, or total idle
  estimate at or above $2/day blocks approval consumption.
- Initial creation, replacement, and restart all require an authenticated human
  action. A reserved logical worker identity binds the exact deployment digest,
  provider request key, batch, and duration before a physical Pod exists.
- Durable grants and absolute deadlines precede paid actions. Uncertain provider
  responses enter reconciliation; they never cause a blind second creation/start.
- Watchdog operation is independent of the controller process. Stop requests
  remain pending until provider readback confirms shutdown. Expiry alone is not
  a shutdown receipt. Idle work stops after five minutes.
- Provider credentials belong only to the trusted controller and its independent
  stop broker. The watchdog uses a narrow Unix socket instead of holding a full
  provider key. No credential is needed for the simulator.

## Phase 4 requirements

- The fixed operation vocabulary includes capture, patch, ablation, steering,
  fitting a probe, bounded generation, and read-only parameter inspection.
- NNsight is checked against raw HF/PyTorch. Test coverage must include no-op
  logits, module classes, patch replay, token position/padding semantics, tensor
  type/shape/hash validation, deterministic seeds, and actual generation bounds.
- The worker accepts data-only requests, never Python, callbacks, pickle, or shell.
  Model/tokenizer/dataset bytes are checked against trusted deployment configuration.
- The supervisor owns process termination and measured usage. Accepted results
  require positive process-stop evidence and validated retained artifacts.
- Cancellation, lost workers, reconnect, expired leases, and failed publication
  must preserve the queue's fencing and idempotency guarantees.
- CPU tests prove the local control path. Actual CUDA memory enforcement and
  canonical checkpoint parity are Phase 6 acceptance requirements.

## Phase 5 requirements

The MCP entry point is `probe-mcp --socket <research.sock> --service-uid <uid>`.
It speaks the pinned MCP SDK's stdio protocol and owns no persistent scientific
state. Disconnecting it does not cancel submitted GPU jobs.

The trusted facade runs as `probe-research-service --config <private.json>`.
Its administrator-controlled configuration pins the service and researcher UIDs,
socket group, ledger path, controller socket identity, and discovery dataset
hashes. An empty dataset allowlist permits no submissions. Confirmation and
replication inputs are not made visible by a client-supplied scientific-stage flag.
`deploy/research.json.example` uses illustrative IDs; replace them with the actual
`probe-trusted`, untrusted `probe-research`, and IPC group IDs. The facade's socket
directory is separate from the administrator's controller socket directory.
Enable the sandbox only after selecting the locally verified image ID. Both the
facade and dispatcher must use the same private `input_artifact_root`.

Research tools can submit/read/cancel jobs, register/freeze hypotheses, inspect
accessible manifests, request GPU start, request stop, and run bounded CPU Python.
They cannot approve starts, acknowledge process/Pod shutdown, accept manifests,
promote scientific validation, adjudicate novelty, or administer hidden evaluation.

CPU Python runs in rootless Podman with no network, no GPU devices, no host sockets,
read-only inputs/root filesystem, no capabilities, and hard CPU/RAM/process/time/
output limits. Its pipe broker resolves only administrator-selected registered
job specifications; submission still requires the normal human execution approval.
CPU artifact bytes and text are untrusted research output, never execution receipts.
Successful CPU outputs receive durable content-addressed artifact IDs. Valid
safetensors include exact `TensorArtifact` references usable in later jobs.
`import_run_artifact` does the same for accepted discovery/calibration outputs,
so a captured activation or CPU-generated direction survives worker replacement.
The dispatcher verifies and transfers those retained tensor inputs before execution.

## Remaining deployment stages

After these acceptance gates, Phase 6 supplies the real RunPod adapter, secured
service identities and credential provisioning on the chosen host, pinned worker
image, network volume and exact scientific checkpoint revisions, real GPU parity,
and a verified stopped state. Phase 7 supplies the private evaluator and its
scientific promotion rules. Neither is represented as implemented by the local
simulator or CPU acceptance tests.

The agreed v1 CPU sandbox plus fixed GPU primitives deliberately does not execute
arbitrary agent-written Python on the GPU. That original report capability needs
its own later design and containment acceptance.
