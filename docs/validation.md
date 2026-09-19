# Local implementation verification

The full local acceptance run on September 19, 2026 completed with **429 passed,
0 failed, 0 errors, and 0 skipped** in 157.41 seconds. It tested source commit
`a639716cec57ecc5c4afd82a4baf25ebb74ce4e9`, before the public README and MIT license
were added. All 277 original core tests are included in this count.

| Area | Evidence |
| --- | --- |
| Ledger and audit | Transactions, concurrent access, leases, approvals, cancellation, recovery, immutable manifests, sealed artifacts, and hash-chain integrity |
| Controller | Simulated provisioning/start/stop, price and idle-cost gates, separate approval authority, durable deadlines, and independent watchdog process |
| Worker and dispatcher | Real tiny-Qwen CPU operations, HF/NNsight parity, token/head/padding semantics, thinking-template handling, authenticated loopback HTTP, separate execution processes, and a controller-driven dispatcher daemon |
| Artifact reuse | Captured tensors replayed by patching; registered CPU direction transferred before steering; immutable artifacts reused after temporary outputs were removed |
| Research interface | Real stdio MCP subprocess and reconnect, Unix peer identity checks, restricted methods, corpus/stage checks, and private-job access denial |
| CPU sandbox | Nine real integration tests covering network/socket denial, secret/GPU absence, read-only mounts, resource/time/output bounds, cancellation, approved-job broker, scientific libraries, and retained artifact reuse |
| Installed package | Separate offline installation; 15 module imports, five module CLIs, three console scripts, matching packaged source/policy bytes, and an approved-job/artifact/audit lifecycle |

The native environment used Python 3.13.13, SQLite 3.53.1, Pydantic 2.13.5,
PyTorch 2.14.0, Transformers 5.17.0, NNsight 0.7.0, safetensors 0.8.0, MCP 2.2.0,
and pytest 9.1.1. The dependency graph is pinned in `uv.lock`.

The full gate required the real sandbox environment using
`PROBE_SANDBOX_REQUIRED=1`. It ran in a temporary unprivileged user service with
delegated cgroup v2 and Ubuntu's existing Podman AppArmor profile. The pinned
scientific image and build-input hashes are recorded in
[`deploy/sandbox/image-lock.json`](../deploy/sandbox/image-lock.json). No containers
remained after the run.

## Limits of this evidence

The model tests used a randomly initialized tiny Qwen3 on CPU. The provider
backend was a simulator. No cloud resources were created, no GPU was started,
and the example permanent services were not installed. Live SSH, canonical
checkpoint parity, CUDA limits, and deployment under final service identities
still require their own acceptance runs.

There is no blind scientific calibration result, hidden evaluator, or validated
scientific finding. Confirmation/replication execution is intentionally refused
until the trusted evaluator is implemented.

The GitHub workflow runs the general suite and builds the package. It does not
configure the mandatory real Podman gate. Skipped sandbox cases in ordinary CI
are not evidence that containment works on a deployment host.

See the [sandbox guide](../deploy/sandbox/README.md) for reproducing containment
checks and the [historical core report](../VALIDATION.md) for the earlier
277-test milestone.
