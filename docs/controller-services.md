# Trusted controller and independent stop service

The development backend is a persistent provider simulator. A separate guarded
[RunPod adapter](runpod-provider.md) supports short supervised acceptance runs.
Initial creation, replacement, and any supported restart require the human
administrative socket. Research clients can submit a request, inspect its
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

## Creation and replacement identity

`DeploymentSpec` freezes GPU model/count, image digest, volume identity/size, and region. A provisioning request reserves a local logical worker ID and binds the exact configuration hash, job-batch hash, runtime, and replacement target. `ApprovalNonce.pod_id` refers to this stable logical worker ID. The provider must map it uniquely to the actual provider Pod ID and retain the creation request key and configuration hash for reconciliation. Replacing an existing worker requires it to be confirmed stopped and uses a new logical identity and a new human approval. The old resource and volume are retained; replacement never silently deletes them.

The same approved request can attempt `create` or `start` only once. The absolute core deadline and `STARTING` intent are committed before the call. A process interruption, timeout, unknown status, or lost response never triggers another paid call. Startup reconciliation locates the logical identity and stops/readbacks the result. If an ambiguous creation is still absent, the interval remains unresolved: absence does not prove a delayed create cannot appear. Later reconciliation can find and stop it. There is no unsafe automatic reset of that interval.

## Stopping and failure recovery

The watchdog stops at the absolute deadline even when a job is active. It also stops after five minutes without an active execution. Idle timers survive watchdog restarts. Once a stop decision is durable, subsequent activity cannot revoke it. Each stop is followed by a provider status readback; failures remain pending and are retried.

Known workers and deadlines are cached in the watchdog's own durable database before acknowledgment. If the main ledger becomes unreadable, the watchdog immediately stops cached active workers and reports unhealthy status. It does not forget the schedules. A fresh or healthy-looking heartbeat without the exact approval acknowledgment cannot authorize compute.

After a real provider confirms its worker and executor processes are off, the controller can acknowledge termination in the core and close the compute interval. The simulator changes resource metadata only: it cannot prove that a local CPU executor terminated. Simulated shutdown therefore cancels active jobs and remains `STOP_REQUESTED` until the dispatcher obtains a verified worker process-stop receipt; a later controller reconciliation closes the interval. Stopped `FINALIZING` work remains eligible for CPU publication. The watcher has no renewal method, and retries never extend an approval deadline.

## Research client contract

`ControllerClient(socket_path, expected_server_uid=...)` exposes:

- `request_start(worker_id, job_ids, max_runtime_seconds)`
- `request_provision(deployment, job_ids, max_runtime_seconds, replaces_worker_id=None)`
- `status()`
- `stop_gpu(worker_id=None)`

Wire methods have those same names and accept named parameters. There is no research approval method. Run the controller with `python -m probe_core.controller serve ...` and the independent watcher with `python -m probe_core.controller watchdog ...`; their complete flags are available with `--help`.
