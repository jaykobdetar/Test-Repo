# Core API and persistence guide

Probe's durable research foundation and the Phase 3–5 implementation: controller
and independent watchdog, trusted experiment execution, local stdio MCP, and a
rootless CPU sandbox. The provider supplied here is a persistent simulator;
real RunPod provisioning belongs to Phase 6. See [IMPLEMENTATION.md](../IMPLEMENTATION.md)
for service boundaries and verification status. The Phase 2 core remains usable
without loading the worker or MCP modules.

## Install and test

Requires Linux, Python 3.13, and local persistent storage. Direct dependencies and
the complete transitive environment are pinned in `pyproject.toml` and `uv.lock`.
Use a maintained SQLite build with the WAL-reset fix (3.51.3 or later, or an
officially fixed backport). The supplied verification was performed with Python
3.13.13, Pydantic 2.13.5, and SQLite 3.53.1.

```sh
uv sync --locked --all-extras
uv run --locked python -m pytest
```

`pyproject.toml` defines the installable `probe-core` distribution; imports use
`probe_core`. The core has no dependency on PyTorch, RunPod, a broker, or a GPU;
the `worker` and `mcp` extras install their respective runtimes.
The synthetic fixture in `tests/fixtures/manifest.json` demonstrates the complete
manifest shape; its hashes are examples, not real model or container revisions.

## Modules

| Module | Responsibility |
| --- | --- |
| `schemas.py` | Complete run manifest; bounded discriminated GPU operations; hypothesis/preregistration and approval contracts |
| `ledger.py` | Transactions, queue/attempt lifecycle, approvals, hypotheses, immutable accepted runs, sealed artifacts, backup |
| `audit.py` | Strict canonical JSON, secret-field rejection, SHA-256 chain, process-safe JSONL append/verification/reconciliation |
| `controller.py`, `provider.py` | Human-authorized compute requests, simulated provider, independent shutdown watchdog |
| `worker.py`, `dispatcher.py` | Real bounded model operations, authenticated execution, retained results and recovery |
| `research_api.py`, `research_service.py`, `mcp_server.py`, `rpc.py` | Research-only tools and authenticated local service boundaries |
| `sandbox.py`, `artifact_store.py` | Rootless CPU experiments and durable tensor inputs |

All models forbid unknown fields. Nested collections use tuples to avoid mutable
state inside otherwise frozen models. Validators reject non-finite numbers,
ambiguous hashes, naive timestamps, path traversal, and executable operation
fields. Serialization uses `model_dump(mode="json")` / `model_dump_json()`;
validation uses `model_validate()` / `model_validate_json()`.

`RunManifest` retains every field in the design's metadata schema, including run,
model, software, hardware, inputs, experiment, controls, results, cost, artifacts,
and security. Full Git/Hugging Face revisions are 40 or 64 lowercase hex
characters. Content hashes have a `sha256:` prefix except `artifacts[].sha256`,
which is the raw 64-character digest specified in the design. The two canonical
Qwen3-1.7B repositories are the scientific model allowlist. The explicit
`probe/testing-tiny-qwen3` fixture is restricted to calibration; it cannot be
submitted as a canonical confirmatory finding.

`JobSpec.operation` is a discriminated union on `kind`: `capture`, `patch`,
`ablate`, `steer`, `fit_probe`, `generate`, `weight_stats`, `tensor_slice`, or
`module_manifest`. Module references constrain Qwen's 28 layers
and 16 query heads; `positions` accepts bounded token indices or `"last"`.
Tensor inputs reference `.safetensors` artifacts. File format, hash, tensor
shape/dtype, and actual GPU memory checks belong to the trusted executor when it
loads inputs; schema validation alone does not open tensors. Runtime/output
limits are mandatory, and generation requests cannot exceed the declared token
budget. The worker enforces execution limits; schema validation alone does not
establish runtime containment.

## Queue use

```python
from probe_core import JobSpec, Ledger

# Build a validated JobSpec from the request received by your trusted controller.
spec = JobSpec.model_validate(request_document)
with Ledger("/var/lib/probe/research.sqlite") as ledger:
    submitted = ledger.submit_job(spec)
    same_job = ledger.submit_job(spec)
    assert submitted.job_id == same_job.job_id
```

Use a private controller-owned directory on a local filesystem. The constructor
rejects known Linux network-filesystem mounts and database symlinks, but mount
detection cannot recognize every third-party filesystem. Do not place the live
database or its WAL/SHM files on a RunPod network volume, NFS, SMB, or a synced
folder. Model caches and bulk reconstructible outputs can remain on the GPU
volume; retained artifacts passed into this core are copied to the controller's
private artifact store before acceptance.

The writer owns one SQLite connection on a dedicated thread. Public calls are
synchronous and thread-safe. Every schema change and DML mutation uses an
explicit `BEGIN IMMEDIATE` transaction with commit/rollback. Connection setup
explicitly executes `PRAGMA journal_mode=WAL;`, `PRAGMA busy_timeout=5000;`,
`PRAGMA synchronous=FULL;`, and `PRAGMA foreign_keys=ON;`. Connection configuration
precedes transactions because journal mode cannot be changed inside one.

`read_connection()` supplies a separate read-only connection for reports and
explicit WAL read snapshots. It cannot mutate the database. Multiple `Ledger`
instances on the same local database serialize writes through SQLite; unique
indexes enforce one active job and one execution not yet confirmed stopped.
Always close a ledger, preferably with its context manager.

## Approvals and dispatch

1. Submit jobs and compute `ledger.batch_hash(job_ids)`.
2. The human-only approval service creates an `ApprovalNonce` with a
   cryptographically random `token` (use `secrets.token_urlsafe(32)`), exact worker,
   batch hash, runtime, price ceiling, and expiration within 15 minutes.
3. `register_approval(nonce)` stores a SHA-256 digest of the secret, never its raw
   value. `ApprovalNonce` excludes the token from repr and all ordinary dumps.
4. `consume_approval(approval_id, token, pod_id=..., job_ids=...,
   live_price_usd_per_hour=..., requested_runtime_seconds=...)` atomically records
   a single-use start intent and absolute deadline. The price must be below
   $1.50/hour and at or below the nonce's tighter ceiling. Denied decisions are
   audited without submitted credentials.
5. The controller may request the cloud start only after this durable grant.
   It must run the independent watchdog and enforce actual Pod shutdown. This
   ledger module never starts, stops, or schedules cloud resources. The supplied
   controller uses a simulated provider; real provider integration is Phase 6.
6. `dispatch_next(worker_id, approval_id=..., lease_seconds=30)` claims the oldest
   eligible pending job that fits the remaining approved interval. It returns
   `None` while another execution/finalization is unresolved or no job fits.

Expired approvals cannot authorize dispatch. A deadline is **not** evidence of
Pod shutdown: before consuming any subsequent approval, the trusted controller
must positively confirm the previous Pod is off and call `end_approval(id)`.
Worker-stop acknowledgement and Pod-stop acknowledgement are separate facts.
For initial provisioning the Phase 3 controller reserves a logical worker ID
before the physical Pod exists, and binds it to an immutable deployment request.
The core's `pod_id` field carries this stable logical ID; provider IDs are separate.
Human authentication, live provider-price lookup, idle-cost accounting, the
five-minute idle watchdog, and protected OS identities are integration concerns;
the core trusts the controller that calls these methods.

## Execution states, leases, and recovery

The normal path is:

`PENDING → DISPATCHED → RUNNING → FINALIZING → COMPLETED`

Cancellation or failure may move any nonterminal state to `FAILED`. The only
allowed path back is `FAILED → PENDING` for one acknowledged infrastructure
retry. Scientific, OOM, timeout, cancellation, and policy failures are terminal.

Use `start_job(job_id, attempt_id, worker_id)` and
`begin_finalization(job_id, attempt_id, worker_id)` to advance. Repeating an
already accepted transition is idempotent while its lease remains valid.
`heartbeat(...)` renews a lease for 1–300 seconds, capped by the original
execution deadline. Runtime includes dispatch and execution up to the supervisor's
stop acknowledgement. Neither heartbeats nor retries extend the consumed approval.

`recover_expired()` records expired attempts as failed; it does **not** assume
the worker died or requeue it automatically. A lease lost before the execution
deadline is an infrastructure failure; hitting the execution deadline is a
timeout. The trusted supervisor must stop/reconcile the original execution and
call `confirm_stopped(job_id, attempt_id)`. Only then can `retry_job(...)` put an
infrastructure failure back into the same approved interval, once. A retry
receives a new attempt ID; obsolete IDs and wrong worker IDs cannot heartbeat,
advance, or publish. A queued retry cannot receive a fresh approval. Deliberate
replications require a new idempotency key and job.

Execution can occur more than once after failure. Submission and result
acceptance are idempotent; this is not an exactly-once execution claim.

## Finalizing and retaining artifacts

The supervisor must stop the experiment process, move the job to `FINALIZING`,
and call `confirm_stopped(...)` before `complete_job(...)`. Keep the execution
lease alive until the stop acknowledgement is recorded. Once positively stopped,
the latest `FINALIZING` attempt can finish CPU artifact publication after its
execution lease or approval expires; `recover_expired` preserves it for this
reconciliation. This does not renew compute authorization. The Pod can be shut
down and its interval closed while publication finishes. No later job dispatches
or new approval is consumed until finalization is resolved.

The manifest must match the submitted model, inputs, hypothesis, scientific
stage, approval, and primitive. Use `Ledger.operation_hash(spec)` for the
canonical intervention hash. Started time and reported compute are bounded by
the attempt. The retained artifact byte count must match `cost.bytes_persisted`
and remain within `spec.limits.max_output_bytes`.

`complete_job(..., manifest, artifact_root=source_directory)` walks source paths
using directory descriptors, rejects symlinks/nonregular files/hardlinks,
verifies hashes while copying, and seals a private read-only bundle under
`<database-stem>.artifacts/<job_id>/<attempt_id>/`. It fsyncs files and directories
and publishes the bundle before committing the manifest, job completion, and
audit event together. Only retained manifest artifacts are copied. Keep
reconstructible caches out of this bundle. These controller-owned bundles must
never be mounted writable into a research sandbox.

After restart, `get_manifest(job_id)` and `get_artifact_root(job_id)` locate the
accepted result. Changes to the source directory do not alter the sealed copy.
A crash before the database commit may leave a complete orphan bundle; repeating
the same finalization safely verifies and reuses it. Interrupted temporary
`.stage-*` directories are never accepted as results. Garbage collection is an
operator maintenance action after checking attempts and accepted manifests.

## Hypothesis lifecycle

`register_hypothesis()` accepts `DRAFT` records. Before freezing, the record must
contain predictions and a typed `PreregistrationPlan` with exact model,
operation, primary metric, minimum effect, and controls. The
`transition_hypothesis(id, "FROZEN")` transaction hashes the full draft definition
and records a UTC freeze time. Subsequent state transitions preserve that plan:

`DRAFT → FROZEN → TESTING → REPLICATING → VALIDATED`

`TESTING` and `REPLICATING` may instead transition to `FALSIFIED`.
Confirmation jobs require `TESTING`; replication jobs require `REPLICATING`.
Both must match the frozen model/operation, and their accepted manifests must
preserve registered metrics, controls, predicted direction, and falsifier.
Validation requires `transition_hypothesis(id, "VALIDATED",
replication_ids=[...])` with completed, passed replication **run IDs** belonging
to the same preregistration. Statistical scoring and role authorization remain
the trusted evaluator's responsibility. Novelty uses N0–N3 (or `None` before
adjudication); this engine does not perform prior-art review.

## Audit durability and integrity

Every state mutation appends a canonical event inside the same SQL transaction.
An event contains sequence, UTC timestamp, type, payload, previous hash, and its
own hash. SHA-256 covers the canonical entire record excluding its own `hash`;
the first `previous_hash` is 64 zeroes. SQLite triggers prevent event/manifest
updates and deletion through ordinary SQL. Tool calls and policy decisions can
be recorded with `record_event("tool_call", payload)` or
`record_event("policy_evaluation", payload)`.

After commit, JSONL synchronization appends only missing events. Exporters
serialize the database snapshot and file operation to avoid stale-prefix races.
A failed file export never makes a committed mutation appear rolled back:
inspect `ledger.audit_export_error`, repair storage, and call `sync_audit()`.
Startup reconciles a valid prefix and fails closed on corruption or export
failure. Never append directly to a ledger-owned JSONL file with a standalone
`AuditLog`; use `record_event` so SQLite remains authoritative.

The standalone `AuditLog` uses thread locks and Linux `flock`, append-only writes,
secure path traversal, and fsync. `verify()` detects modified/reordered/interior
deleted records and partial lines. Complete suffix deletion requires a trusted
tip: `verify(expected_sequence=..., expected_hash=...)`, or comparison to the
database with `sync_records`. Hash chaining is tamper evidence, not a digital
signature; an attacker controlling the database, log, and all trusted backups
can rewrite history.

The recursive redaction scanner **rejects** secret/credential keys, including
camelCase variants and nested arrays, before writing. It permits ordinary model
token-count metadata. It cannot identify a credential hidden in an innocuously
named string. Pass reviewed fields or argument hashes, not raw environment
variables, arbitrary tool arguments, or unfiltered exception output.

Use `ledger.backup(new_path)` for a consistent SQLite snapshot. Copying only an
open `.sqlite` file can lose WAL data. Back up accepted artifact bundles and a
trusted audit tip separately to storage outside the GPU provider; the database
backup does not copy artifacts.
