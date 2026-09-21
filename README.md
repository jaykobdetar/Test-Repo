# Auto Interpretability Lab

Auto Interpretability Lab is experimental infrastructure for agent-assisted
research into language models. It lets a research agent request model inspections
and controlled interventions, records what ran and what it produced, and keeps
paid compute behind a separate approval process.

Both **Qwen3-1.7B-Base** and **Qwen3-1.7B** have passed supervised GPU numerical
calibration. The project is not yet an autonomous research lab, and it has
produced no validated scientific findings.

> This page describes the deployment work in
> [the current draft pull request](https://github.com/jaykobdetar/auto-interpretability-lab/pull/1).
> The `main` branch contains the earlier local foundation. Deployment acceptance
> is still in progress; use the documentation from the same branch as your code.

## What the software does

- **Records experiments:** SQLite stores job states, execution attempts, model
  identities, retained artifacts and an append-only audit trail.
- **Inspects and intervenes in models:** fixed operations cover activation capture,
  patching, ablation, steering, probe fitting, bounded generation and weight
  inspection. Hugging Face/PyTorch provides a reference for NNsight comparisons.
- **Exposes research tools through MCP:** an agent can submit work, inspect results
  and record hypotheses. A compute request does not itself authorize a GPU start.
- **Runs bounded jobs:** the worker and dispatcher enforce job limits and verify
  returned artifacts. Arbitrary CPU Python runs in a rootless Podman sandbox;
  arbitrary agent-written GPU Python is not supported.
- **Separates operational authority:** research, controller, watchdog and backup
  processes use distinct accounts. The controller handles approvals and provider
  credentials; the research agent and model worker do not receive those credentials.

These are implemented engineering capabilities. Their validation scope is described
below and in the [validation guide](docs/validation.md).

## Current status

As of September 20, 2026:

| Area | Evidence and remaining work |
| --- | --- |
| Ubuntu controller | Installed. Its latest upgrade passed 16 CPU containment/crash checks and 26 account-boundary checks. |
| Independent backups | Controller backups and both standalone calibration bundles passed Google Drive upload, download, hash verification and local restore. |
| RunPod containment | Managed-worker resource controls and cleanup passed on a compatible host; that profile requires checks on each new host. The supervised profile uses the disposable Pod boundary and does not claim nested cgroup enforcement. |
| Model images | Separate Base and posttrained images use pinned public assets. Both supervised runs verified model/image provenance and returned artifact hashes. |
| Supervised GPU execution | **Both models passed:** each completed 29 numerical checks, including 25 exact comparisons and nine retained tensors, followed by verified collection, process exit and confirmed Pod deletion. |
| Managed-worker acceptance | Incomplete. The supervised result does not establish installed-ledger dispatch, resource-limit enforcement, cancellation, recovery or replacement. |
| Scientific workflow | Private held-out evaluation, independent Explorer/Skeptic/Replicator sessions and blind scientific calibration remain future work. |

The selected path is [supervised public calibration](docs/supervised-public-calibration.md),
which runs one fixed command without another controller installation or nested
cgroup requirement. Both models passed on RunPod RTX 4090s in EU-RO-1. The
[calibration results](docs/calibration-results.md) record the evidence, including
a corrected Base host-verifier mismatch with the original report preserved.

Next, implement the [first fixed public exploratory experiment](docs/first-public-experiment.md).
The [deployment checklist](docs/live-deployment-plan.md) retains the
original five managed-service steps separately. Its 18 prepared acceptance plans
remain useful for that larger milestone; they are not prerequisites for a
bounded, supervised public experiment.

RunPod support currently permits short, supervised runs. Unattended operation,
provider shutdown during controller-host loss, direct Pod resumes and private
confirmation/replication evaluation are not accepted capabilities. Interventions
apply to prompt prefill; generation is a separate, unmodified operation.

## How the pieces fit

The managed-service architecture is:

```text
Research agent ──MCP──► research service ──► experiment ledger
                              │
                         compute request
                              ▼
Human approval ──────► trusted controller ──► RunPod
                              │                ▲
                           dispatcher     watchdog stop
                              │
                    authenticated HTTP over SSH
                              ▼
                          model worker

Research service ────► rootless CPU sandbox
Controller state ────► verified independent backups
```

The supervised profile instead uses authenticated SSH for one fixed command,
a separate host deadline guard, and standalone reports outside the installed
ledger. It keeps the provider-management key on the host.

Accepted outputs are copied back to the Ubuntu host and verified before a run can
succeed. The selected GPU setup uses disposable Pod disks and public model assets
baked into immutable images; it does not require a network volume. Losing a Pod
before output collection leaves the result failed or inconclusive.

Distinct OS accounts and installed file permissions enforce the service boundaries.
Running all roles under one account is a development setup. Keep the active SQLite
ledger on local controller storage and retain independent backups.

## Development setup

Use Linux, Git, [uv](https://docs.astral.sh/uv/) and Python 3.13. The worker
extras include PyTorch and require several gigabytes of disk. See the
[core guide](docs/core-guide.md) for SQLite requirements and the
[worker guide](docs/worker-dispatcher.md#runtime-enforcement-and-acceptance-gate)
for process and cgroup requirements.

```sh
git clone https://github.com/jaykobdetar/auto-interpretability-lab.git
cd auto-interpretability-lab
git switch --track origin/feat/live-deployment
uv python install 3.13
uv sync --locked --all-extras
uv run --locked python -m pytest -q
```

Run these commands on the branch you intend to test. Development model tests use
a small, locally generated model; they need no cloud credentials or downloaded
canonical checkpoint. Some tests bind local sockets. Real sandbox tests skip
without their required environment, so a default test run does not establish
containment or live GPU readiness. Historical test totals and their exact scope
are recorded in the [validation guide](docs/validation.md).

To require the real sandbox checks after building its pinned image:

```sh
PROBE_SANDBOX_REQUIRED=1 \
PROBE_SANDBOX_IMAGE="sha256:REPLACE_WITH_64_HEX_IMAGE_ID" \
uv run --locked python -m pytest tests/test_sandbox.py tests/test_sandbox_service.py -q
```

Replace the image placeholder before running. The
[sandbox guide](deploy/sandbox/README.md) covers image preparation, user mappings,
cgroups and service requirements. Required checks fail instead of skipping when
`PROBE_SANDBOX_REQUIRED=1`.

The repository name is `auto-interpretability-lab`. Existing executable, package,
service and deployed image identifiers retain their original names for compatibility:
`probe-mcp`, `probe-core`, `probe_core` and `probe-*`. The branding change does not
rename runtime components.

## Deployment and documentation

Installing the package does not configure a lab or approve paid compute. Start
with the deployment checklist, then follow the relevant component guide:

| Need | Guide |
| --- | --- |
| Inspect the two passing numerical calibrations | [Calibration results](docs/calibration-results.md) |
| Run the smaller fixed public GPU calibration | [Supervised public calibration](docs/supervised-public-calibration.md) |
| Prepare the next exploratory milestone | [First public experiment](docs/first-public-experiment.md) |
| Understand what is complete and what is still required | [Live deployment checklist](docs/live-deployment-plan.md) |
| Understand the implemented scope and boundaries | [Implementation overview](IMPLEMENTATION.md) |
| Set up the Ubuntu accounts and services | [Host installation](docs/host-installation.md) |
| Understand compute requests, approvals and shutdown | [Controller services](docs/controller-services.md) |
| Prepare and check GPU workers | [GPU deployment](docs/gpu-deployment.md) and [RunPod provider](docs/runpod-provider.md) |
| Understand dispatch, execution and returned artifacts | [Worker and dispatcher](docs/worker-dispatcher.md) |
| Configure independent recoverable backups | [Backup and restore](docs/backup-restore.md) |
| Use the queue, manifests and audit APIs | [Core guide](docs/core-guide.md) |
| Interpret test and deployment evidence | [Validation](docs/validation.md) |

The controller uses the `controller` and `mcp` extras; it does not need the GPU
worker's PyTorch installation. The host installer expects a reviewed release
bundle containing pinned manifests, a Python runtime and locked offline dependencies.

Once the research service exists, an MCP client can launch `probe-mcp` with
`--socket` pointing to its research socket and `--service-uid` set to the trusted
service's actual UID. Keep the human approval interface separate from that client.

Model weights and credentials are not stored in this repository. Worker image
recipes fetch pinned public model files and synthetic calibration prompts; they
contain no hidden evaluation dataset.

## After the first working GPU path

The next useful milestone is the [first public experiment](docs/first-public-experiment.md),
with a declared question, metric, matched control and retained per-prompt results.
This needs a separate reviewed experiment recipe;
the current standalone command accepts only the numerical calibration. Reuse
the supervised deadline, credential separation, verified collection and Pod
deletion, and back up its standalone artifacts explicitly.

Managed resource, cancellation, recovery and replacement checks remain deferred
work for the full service profile. Private held-out evaluation and independent
research roles are later requirements for confirmatory research. Neither set of
work blocks a bounded public exploratory experiment, which must remain labelled
exploratory. SAE/Qwen-Scope, circuit tracing and automatic novelty assessment
remain outside the current implementation.

Contributions should state the behavior being changed and provide evidence for
claims about correctness, containment and scientific results. Changes to the
execution model or research protocol should describe their acceptance criteria.

## License

[MIT](LICENSE). Model weights and third-party dependencies retain their own licenses.
