# Auto Interpretability Lab: Validation status and history

[Project overview](../README.md) · [Implementation boundaries](../IMPLEMENTATION.md) · [Deployment checklist](live-deployment-plan.md)

## Current evidence: September 20, 2026

The deployment branch through `7e01602` contains more functionality than the
original public-main foundation. Source presence, package validation, installed
acceptance and successful model execution are separate claims.

| Scope | Latest retained evidence | What it establishes |
| --- | --- | --- |
| Installed controller | Source `8a5486b748fc9b9f31b30408e01527729bc102bc`, wheel `c2fd62bfec0fbbb90dc556e34dac88d6969a59b5dfefb0846f31d0b691a6ee38`; all 34 application files matched the package | Exact installed controller bytes; worker provenance remains separate |
| CPU sandbox | All 16 installed checks passed | Containment, resource enforcement, attestation and independent cleanup after launcher crashes under the actual service profile |
| OS identities | All 26 installed checks passed | Intended socket access, credential/admin denial and audited research reads |
| Backup | Drive upload, full readback and offline restore passed; timer restored | Recoverable retained controller/provider history, not a scientific result |
| Region-only preparation | 95 focused checks and a separately verified installation | Fresh Romania proposal with unchanged application, worker image and scientific scope |
| GPU acceptance | Seven full-worker attempts ended before inference; all test Pods deleted | Zero successful live canonical cases; 18 plans are prepared, not passed |

The latest attempt ended with `REQUEST_CHANGED` during worker startup. The
controller requested stop before the watchdog recorded `uncertain_action`.
Its original reconciliation trigger was discarded. A concurrent provider-log
lookup returned HTTP503; the saved prior Pod passed exact RUNNING validation.
A local injected status-GET503/deletion503 sequence reproduces the shutdown
mechanism, but cannot prove which error initiated the live stop.

The startup controller release passed both CI runs with 1,867 tests passing and
10 real-container tests skipped. The installed sandbox gate provides separate
runtime evidence; skipped CI tests remain skips. On deployment source `7e01602`, one CI run
passed and another reported 1,912 passed, one failure and 10 skips: output-size
accounting raced an atomic `summary.json.partial` rename. The isolated correction
is not part of the installed worker image, and this documentation does not claim
that release passed every CI run. Focused and full-suite counts overlap.

The immediate target is the first successful Base parity run with retained
artifacts and confirmed provider deletion. Further orchestration is deferred.
Unattended host-loss shutdown, posttrained acceptance, live lifecycle/replacement
checks and scientific evaluation remain unproven. No project GPU is running at
the latest retained readback.

The operator retains `calibration-startup-verification.json`,
`calibration-retarget-verification.json`, `public-calibration-seventh-attempt.json`
and `seventh-stop-local-reproduction.json` with the private deployment records.
They document the claims above; private credentials, provider responses and
administrator receipts are not copied into this public repository.

## Earlier deployment preparation

The September 19 preparation gate passed **544 tests, 0 failures and 0 skips**
in 175.30 seconds. It covered the earlier local checks, guarded provider adapter,
pinned model preparation, backup/restore, service configuration and production
CPU acceptance command. This was a local preparation milestone, before permanent
service-account and live GPU acceptance.

An offline installation imported the production controller entry points using
Python 3.13.15 and SQLite 3.53.1 without host PyTorch. Both model inventories were
downloaded and rehashed. A synthetic snapshot completed Drive upload, download,
verification and restore. These tests created no paid resources and are distinct
from the later installed-service and supervised cloud evidence above.

## Earlier local milestone

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

## Limits of the earlier local milestone

The model tests in the 429-test milestone used a randomly initialized tiny
Qwen3 on CPU. The provider was a simulator. No cloud resources were created,
no GPU was started, and permanent services were not installed. Those results
did not establish live SSH, canonical checkpoint parity, CUDA limits or final
service identities. Later installed-service acceptance is listed
above; live numerical and lifecycle acceptance is still outstanding.

There is no blind scientific calibration result, hidden evaluator, or validated
scientific finding. Confirmation/replication execution is intentionally refused
until the trusted evaluator is implemented.

The GitHub workflow runs the general suite and builds the package. It does not
configure the mandatory real Podman gate. Skipped sandbox cases in ordinary CI
are not evidence that containment works on a deployment host.

See the [sandbox guide](../deploy/sandbox/README.md) for reproducing containment
checks and the [historical core report](../VALIDATION.md) for the earlier
277-test milestone.
