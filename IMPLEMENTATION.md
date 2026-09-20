# Auto Interpretability Lab: Implementation and acceptance boundaries

[Project overview](README.md) · [Validation status](docs/validation.md) · [Deployment checklist](docs/live-deployment-plan.md)

This guide describes the deployment branch through source `7e01602`, including
the guarded RunPod adapter, installed service boundaries, pinned worker images
and backup tooling. The public main branch began with the smaller local foundation;
its earlier milestones are recorded in [the historical core report](VALIDATION.md).
Package names, Python imports, service accounts and paths retain their `probe-*`
identifiers despite the Auto Interpretability Lab display name.

As of September 20, 2026, controller source `8a5486b` is installed and has passed
16 CPU containment checks, 26 identity checks and verified Drive backup/restore.
Seven full-worker GPU attempts ended before inference; no canonical GPU case has
passed. Provider deletion is confirmed for those attempts, but shutdown during
controller host loss is unverified. See the deployment checklist for the current
blocker and the distinction between prepared code and live evidence.

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
  a shutdown receipt. Ordinary idle work stops after five minutes. Initial
  startup of one never-dispatched disposable calibration job uses the same fixed
  dispatch cutoff as the runner, within the original approval deadline.
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

## Remaining deployment work

The RunPod adapter, protected host services, verified backups and pinned public
assets are implemented. The selected GPU storage design uses disposable Pod disks
and immutable image assets; it supersedes the original network-volume proposal.
The next acceptance target is one successful Base parity job with retained,
verified artifacts and confirmed Pod deletion. Posttrained parity, the other
runtime cases and replacement evidence follow that result. Eighteen case plans
are prepared across the two model identities; none has passed live.

The private evaluator and scientific promotion rules remain future work. Broader
agent orchestration is deferred while the first Base path is made reliable.
Local simulator and CPU tests do not supply either scientific or live GPU evidence.

The agreed v1 CPU sandbox plus fixed GPU primitives deliberately does not execute
arbitrary agent-written Python on the GPU. That original report capability needs
its own later design and containment acceptance.
