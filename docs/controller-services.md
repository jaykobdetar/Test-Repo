# Auto Interpretability Lab: Controller and independent stop service

[Project overview](../README.md) · [Status](../STATUS.md) · [Validation](validation.md)

This is the service and authority contract for Auto Interpretability Lab.
The installed controller identities have passed their actual OS-boundary checks;
that does not certify a numerical GPU result. The development backend is a
persistent provider simulator. A separate guarded
[RunPod adapter](runpod-provider.md) supports short supervised acceptance runs.
Initial creation, replacement, and any supported restart require the human
administrative socket, or an open budget envelope that a human issued through it. Research clients can submit a request, inspect its
status, or stop compute. RunPod resumes and unattended launches remain disabled.

## Process and identity boundaries

Run the controller and trusted research facade as `probe-trusted`, because both own the authoritative ledger and its audit projection. Run the untrusted MCP client and research agent under a different account. The controller's `--research-uid` identifies the **trusted facade**, not the untrusted agent. It may equal the controller UID. The facade exposes its narrower research API to the untrusted account.

Run the independent watchdog as `probe-watchdog`. It reads the ledger with SQLite
`mode=ro` and owns its own schedule database and health file. It cannot register
or consume core approvals. Its provider interface exposes only status and stop.
The RunPod deployment uses a separate Unix broker because the provider has no
documented Pod-specific stop-only key. Only the trusted broker holds that key;
Python interface restriction alone is not a credential boundary.

The administrative Unix socket checks the peer's kernel-supplied UID. Its configured human UID must differ from the controller service, trusted facade, and untrusted agent identities. The controller requires `--agent-uid` and rejects configurations that reuse that UID for any trusted role. Set `AGENT_UID` to the research facade configuration's `research_uid`, and set `HUMAN_UID` to its `admin_uid`. The trusted facade may share the controller UID. The normal CLI does not offer the testing-only same-service-UID escape hatch. The RPC client also verifies the server UID.

## Local service installation contract

The original systemd examples use the simulator. The reviewed manual
[host installer](host-installation.md) renders the separate live templates using
actual account IDs, installs `/opt/probe-core/venv`, and starts the control and
backup services without purchasing compute. Keep configuration owned by the
administrator and unwritable by research identities.

Provision these directories before enabling the units:

| Path | Owner/group | Access |
|---|---|---|
| `/var/lib/probe-core` | `probe-trusted:probe-ledger-read` | `0750`; watchdog traverses and reads ledger only |
| `research.sqlite` and its WAL/SHM siblings | `probe-trusted:probe-ledger-read` | `0640`; apply to the preinitialized ledger before watchdog use |
| Audit JSONL and artifacts | `probe-trusted` | Keep original private ownership and write restrictions |
| `/var/lib/probe-watchdog` | `probe-watchdog:probe-watch-read` | `0750`; controller can read health, not change it |
| `/var/lib/probe-simulator` | `probe-trusted:probe-simulator` | `2770`; simulator DB `0660` for both simulated service processes |
| `/run/probe-controller` | `probe-trusted:probe-ipc` | `0750`; sockets `0660`, plus kernel peer-UID checks |

Initialize the controller's database tables and simulator database under the trusted identity before applying group read/write permissions. New SQLite WAL/SHM files inherit database permissions. The simulator intentionally shares its local model of provider state between the two processes; this is not a model for sharing live start credentials. Ensure systemd's runtime-directory group allows the human account's `probe-ipc` membership to traverse the directory.

Each Unix endpoint holds a private lifetime lock beside its socket. A second live instance is refused. After a crash, the next instance recovers only an owned socket that no longer accepts connections; regular files, symlinks, and unsafe lock files are never replaced. Lock files remain in place between runs so overlapping processes cannot acquire different lock inodes.

The watchdog unit has its own restart policy and no `PartOf` or `BindsTo` dependency on the controller. It stays alive when the starter exits. Both services poll every two seconds. The controller refuses a paid action until the watchdog has durably cached that exact approval ID, logical worker ID, and absolute deadline and acknowledged them in a fresh health record.

## Human approval

Inspect the pending requests from the configured human account:

```sh
python -m probe_core.controller admin --socket /run/probe-controller/admin.sock --expected-server-uid 2001 --method status
```

Approve an exact request after reviewing its worker configuration, job batch, and runtime:

```sh
python -m probe_core.controller admin --socket /run/probe-controller/admin.sock --expected-server-uid 2001 --method approve --request-id request-EXACT_ID --price-ceiling 1.49
```

The provider supplies the live price; the caller cannot provide it. The provider simulator rechecks the approved price and storage ceilings atomically with the paid action. The GPU rate must remain strictly below $1.50/hour. Storage for all existing persistent volumes, including retained replacement volumes, plus configured non-storage overhead must remain strictly below $2/day. The simulator's storage price is a fixture, not a current RunPod price claim.

## Budget envelopes

A budget envelope is a human-approved spending limit for exploratory research.
While one is open, the controller may approve a pending disposable research Pod
without a per-start human action. It replaces only that action: the live quote,
$1.50/hour and $2/day limits, 15-minute approval, watchdog acknowledgement,
deadline and confirmed deletion all still apply to every Pod.

Issue one from the human account with a JSON file such as:

```json
{
  "envelope_id": "m1-exploratory-001",
  "max_gpu_usd": 3.0,
  "max_llm_usd": 0.0,
  "max_gpu_usd_per_hour": 0.80,
  "max_wall_seconds_per_pod": 900,
  "allowed_models": [
    {"repo": "Qwen/Qwen3-1.7B-Base", "revision_sha": "ea980cb0a6c2ae4b936e82123acc929f1cec04c1"},
    {"repo": "Qwen/Qwen3-1.7B", "revision_sha": "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"}
  ],
  "allowed_stages": ["exploratory"],
  "lifetime_hours": 24
}
```

```sh
python -m probe_core.controller admin --socket /run/probe-controller/admin.sock --expected-server-uid 2001 --method issue_envelope --envelope-file envelope.json
python -m probe_core.controller admin --socket /run/probe-controller/admin.sock --expected-server-uid 2001 --method budget_status
python -m probe_core.controller admin --socket /run/probe-controller/admin.sock --expected-server-uid 2001 --method close_envelope --envelope-id m1-exploratory-001
```

The controller sets `approved_by` from the admin socket's peer UID and the issue
and expiry times from its own clock; a client cannot supply them. Only one
envelope may be open. Nothing renews or extends it. `max_llm_usd` must be zero
until LLM spending is separately approved, and only the exploratory stage is
allowed. After expiry or closure no new Pod can be reserved; a running Pod keeps
its own deadline.

Accounting is append-only in the ledger (`probe_core/budget.py`). Before
creation, in the same transaction that consumes the request, the controller
reserves the worst case: `(runtime + 300 s deletion reserve) × max_gpu_usd_per_hour`.
It refuses the start when reserved plus settled spend would exceed `max_gpu_usd`,
and records the refusal. When the provider confirms deletion, the reservation
settles at the interval from reservation to confirmation times the quoted live
price, an upper bound on billed time. A request that fails before any provider
action settles at zero. An uncertain request keeps its full reservation until
reconciliation confirms deletion.

The installed identity gate expects `gpu_start_authority: false` from
`lab_status`, so run it with no envelope open.

## Creation and replacement identity

`DeploymentSpec` freezes GPU model/count, image digest, volume identity/size, and region. A provisioning request reserves a local logical worker ID and binds the exact configuration hash, job-batch hash, runtime, and replacement target. `ApprovalNonce.pod_id` refers to this stable logical worker ID. The provider must map it uniquely to the actual provider Pod ID and retain the creation request key and configuration hash for reconciliation. Replacing an existing worker requires it to be confirmed stopped and uses a new logical identity and a new human approval. For the selected disposable profile, the old Pod must already be confirmed
absent and its request stopped; accepted artifacts remain on the controller.
The optional persistent-volume profile retains its independent network volume.

The same approved request can attempt `create` or `start` only once. The absolute core deadline and `STARTING` intent are committed before the call. A process interruption, timeout, unknown status, or lost response never triggers another paid call. Startup reconciliation locates the logical identity and stops/readbacks the result. If an ambiguous creation is still absent, the interval remains unresolved: absence does not prove a delayed create cannot appear. Later reconciliation can find and stop it. There is no unsafe automatic reset of that interval.

## Stopping and failure recovery

The watchdog stops at the absolute deadline even when a job is active. Ordinary
idle time is limited to five minutes. One pending, never-dispatched calibration
job on a fresh disposable worker instead uses the runner's fixed startup cutoff:
approval deadline minus the full job runtime and 120 seconds for collection and
deletion. The first attempt permanently ends this exception. Idle timers and
startup bounds survive watchdog restarts. Once a stop decision is durable, subsequent activity cannot revoke it. Each stop is followed by a provider status readback; failures remain pending and are retried.

Known workers and deadlines are cached in the watchdog's own durable database before acknowledgment. If the main ledger becomes unreadable, the watchdog immediately stops cached active workers and reports unhealthy status. It does not forget the schedules. A fresh or healthy-looking heartbeat without the exact approval acknowledgment cannot authorize compute.

After a real provider confirms its worker and executor processes are off, the controller can acknowledge termination in the core and close the compute interval. The simulator changes resource metadata only: it cannot prove that a local CPU executor terminated. Simulated shutdown therefore cancels active jobs and remains `STOP_REQUESTED` until the dispatcher obtains a verified worker process-stop receipt; a later controller reconciliation closes the interval. Stopped `FINALIZING` work remains eligible for CPU publication. The watcher has no renewal method, and retries never extend an approval deadline.

The seventh full-worker attempt exposed a diagnostic gap: reconciliation can
request a stop after a provider-status error without retaining that original
error. A later deletion failure is recorded as `ProviderUncertain`, and the
watchdog then records `uncertain_action`. A local injected HTTP503 sequence
reproduces this mechanism, but it does not identify the historical first error.
The source correction records the initiating cause in the same audit transaction
as `STOP_REQUESTED`, before any deletion. It includes only fixed reason codes,
an exception category and bounded HTTP metadata, never response bodies or raw
headers. Whether it is installed is recorded in [STATUS.md](../STATUS.md).

For an already running, physically identified RunPod, reconciliation may retry
one status read after HTTP 429, 502, 503 or 504. It waits two seconds only when
the complete request timeout still fits inside the original deadline. A longer
or unparsed `Retry-After`, challenge, second failure, expired deadline or changed
binding receives no further retry. Normal creation and stop/readback calls keep
their existing one-shot behavior. This does not extend an approval or allow
cached status to stand in for provider confirmation.

## Research client contract

`ControllerClient(socket_path, expected_server_uid=...)` exposes:

- `request_start(worker_id, job_ids, max_runtime_seconds)`
- `request_provision(deployment, job_ids, max_runtime_seconds, replaces_worker_id=None)`
- `status()`
- `stop_gpu(worker_id=None)`
- `budget_status()`
- `start_within_envelope(request_id)`

Wire methods have those same names and accept named parameters. There is no research approval method; `start_within_envelope` succeeds only inside an open human-issued envelope. The research facade exposes it to the agent as the `start_gpu_within_envelope` MCP tool and adds the envelope and its spend to `lab_status`. Run the controller with `python -m probe_core.controller serve ...` and the independent watcher with `python -m probe_core.controller watchdog ...`; their complete flags are available with `--help`.
