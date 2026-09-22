# Project status

[Project overview](README.md) · [Redirect plan](REDIRECT-PLAN.md) · [Contributing](CONTRIBUTING.md) · [History](docs/history/README.md)

This is the only place that records what the lab can do today. Guides describe
how things work; they link here instead of restating status. Every claim below
links to retained evidence or to the tests that check it. A claim changes here in
the same commit as the code or evidence that changes it (see
[CONTRIBUTING.md](CONTRIBUTING.md)).

Last updated: September 22, 2026, on branch `redirect/milestone-1`.

**Summary.** The **supervised** GPU profile has passed exact numerical
calibration on both Qwen3-1.7B checkpoints. The **managed** worker profile has
not passed any live canonical case and is deferred. The lab has produced no
interpretability results and no validated scientific findings.

## Works and evidenced

| Capability | Scope of the claim | Evidence |
| --- | --- | --- |
| Supervised GPU numerical calibration, Qwen3-1.7B-Base | `backend_parity_v1`, 2026-09-20, RTX 4090 EU-RO-1: 29/29 checks, 25/25 exact comparisons with zero maximum error, 9 retained tensors, verified collection, confirmed Pod deletion. Engineering check on two synthetic prompts, not a scientific result. | [Check results](docs/evidence/2026-09-20-base-summary.json), [manifest](docs/evidence/2026-09-20-base-manifest.json), [calibration results](docs/calibration-results.md) |
| Supervised GPU numerical calibration, Qwen3-1.7B (posttrained) | Same suite and scope, thinking mode disabled with a pinned chat template. | [Check results](docs/evidence/2026-09-20-posttrained-summary.json), [manifest](docs/evidence/2026-09-20-posttrained-manifest.json), [calibration results](docs/calibration-results.md) |
| Independent backup of both calibration bundles | Google Drive upload, download, hash verification and local restore. | [Backup receipt](docs/evidence/2026-09-20-backup-verification.json) |
| Installed Ubuntu controller | Source `4b19ac6`, wheel `a43e336773fe21155a890aa4a545a26ab70d71a6a7d01bf5e0b0619ac86434c3`; 34 application files matched the package. Includes the stop-cause audit (`6f94e76`) and bounded status retry (`4fe9d97`). | Operator-retained receipts named in [validation status (2026-09-20)](docs/history/2026-09-20-validation-status.md); checked by [tests/test_controller_stop_cause.py](tests/test_controller_stop_cause.py), [tests/test_runpod_status_retry.py](tests/test_runpod_status_retry.py) |
| Installed CPU sandbox and account boundaries | 16/16 containment and cleanup checks and 26/26 identity checks under the installed service accounts. The earlier intermittent timeout-classification failure is retained, not claimed fixed. | Operator-retained receipts named in [validation status (2026-09-20)](docs/history/2026-09-20-validation-status.md); acceptance code in [tests/test_sandbox_acceptance.py](tests/test_sandbox_acceptance.py), [tests/test_installed_identities.py](tests/test_installed_identities.py) |
| Controller state backup and restore | Drive upload, full readback and offline restore of controller and provider history. | Same retained receipts; [tests/test_backup.py](tests/test_backup.py), [tests/test_backup_transport.py](tests/test_backup_transport.py) |
| Local control plane on CPU | Ledger, append-only audit chain, approvals, leases, simulator provider with price/idle gates, independent watchdog. | [tests/test_ledger.py](tests/test_ledger.py), [tests/test_audit.py](tests/test_audit.py), [tests/test_controller.py](tests/test_controller.py), [tests/test_startup_watchdog.py](tests/test_startup_watchdog.py) |
| Fixed operations on a tiny random Qwen3 (CPU) | Capture, patch, ablate, steer, fit probe, bounded generation, weight inspection; HF/raw-hook/NNsight parity. | [tests/test_worker.py](tests/test_worker.py), [tests/test_backend_parity.py](tests/test_backend_parity.py) |
| MCP research interface (CPU) | Real stdio MCP subprocess, peer-identity checks, restricted methods, private-job denial. | [tests/test_mcp.py](tests/test_mcp.py), [tests/test_research_api.py](tests/test_research_api.py) |
| Test suite | The full suite passes under CI at runtime source `d41cb4c` (2,157 passed, 10 environment-specific skips). Skips are not containment evidence. | [CI run](https://github.com/jaykobdetar/auto-interpretability-lab/actions/runs/35544276039) |

## Implemented but unevidenced

Code and tests exist, but no live or installed run has passed.

| Capability | What is missing | Tests |
| --- | --- | --- |
| Ledger-connected supervised public job over SSH (`supervised_runner`, `ssh_job_client`, `deploy/gpu/public-job.py`, additive sidecar installer) | Any live GPU run through the installed ledger; deadline, output, CUDA allocator, cancellation and deletion evidence on RunPod | [tests/test_supervised_runner.py](tests/test_supervised_runner.py), [tests/test_ssh_job_client.py](tests/test_ssh_job_client.py), [tests/test_public_job.py](tests/test_public_job.py), [tests/test_install_supervised.py](tests/test_install_supervised.py) |
| Worker seccomp correction allowing CUDA's AF_UNIX socket creation while keeping network denials | A published image and a live RunPod run under the managed worker; local driver and BF16 kernel checks only | [tests/test_worker_network_policy.py](tests/test_worker_network_policy.py) |
| RunPod provider adapter with price ceiling and deletion readback | Exercised by the supervised passes above; unattended use and `stopAfter` host-loss behaviour are unverified | [tests/test_runpod_provider.py](tests/test_runpod_provider.py) |
| Budget envelopes: human-issued GPU spending limit, automatic approval of disposable exploratory Pods within it, append-only worst-case reservation and settlement, refusal when the remainder cannot cover a Pod, spend in `lab_status` | No live use yet. The standalone supervised command does not yet charge the envelope (Milestone 1d). Tested only on CPU with the simulator provider. | [tests/test_budget.py](tests/test_budget.py) |
| Real CPU sandbox in ordinary CI | CI skips the real Podman checks; only the installed gate above counts | [tests/test_sandbox.py](tests/test_sandbox.py), [tests/test_sandbox_service.py](tests/test_sandbox_service.py) |

## Deferred

**Deferred: not required by current roadmap.** This code and its tests are kept
passing. It gets no new features, acceptance plans or paid runs until a milestone
in [REDIRECT-PLAN.md](REDIRECT-PLAN.md) requires it.

| Work | Last standing | History |
| --- | --- | --- |
| Managed-worker lifecycle: nested cgroup delegation, replacement, tunnel recovery, the 18 prepared plans in `probe_core/gpu_acceptance*.py` | Nine live attempts, none passed. The eighth reached dispatch and failed CUDA initialization (error 304); the ninth failed in cgroup bootstrap. All Pods deleted. | [Live deployment checklist](docs/history/2026-09-20-live-deployment-checklist.md), [startup attempts](docs/history/2026-09-20-gpu-startup-attempts.md) |
| Controller upgrade and resume tooling under `deploy/` | Bug fixes only | [Validation status (2026-09-20)](docs/history/2026-09-20-validation-status.md) |
| Unattended operation and host-loss shutdown | Not accepted; runs must stay supervised with the host online | [RunPod provider](docs/runpod-provider.md) |

## Current milestone

**Milestone 1: recipes, metrics and budget envelopes**
([plan](REDIRECT-PLAN.md#milestone-1--recipes-metrics-and-budget-envelopes-run-the-first-experiment)).
Milestone 0 is complete ([PR #2](https://github.com/jaykobdetar/Test-Repo/pull/2)).

- 1a, budget envelopes: implemented and tested on the simulator. The operator
  approved the design on 2026-09-22, including automatic approval within an
  envelope and a first envelope of $3 GPU, 24 hours, $0.80/hour, 900 s per Pod,
  exploratory stage and both pinned Qwen3-1.7B checkpoints. It has not been
  issued on the installed controller.
- 1b recipes, 1c metrics, 1d the [first public experiment](docs/first-public-experiment.md):
  not started. The experiment has not been run.

GPU budget: the operator's cumulative authorization is $20, of which about $3
was spent before envelopes existed (operator report, 2026-09-22).

Not yet started: SAE features (M2), an automated explanation loop (M3), a
private held-out evaluator, Explorer/Skeptic/Replicator roles and blind
scientific calibration (M6).
