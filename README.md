# Auto Interpretability Lab

Auto Interpretability Lab is experimental infrastructure for agent-assisted
research into language models. It lets a research agent request model inspections
and controlled interventions, records what ran and what it produced, and keeps
paid compute behind a separate approval process.

The intended research subjects are **Qwen3-1.7B-Base** and **Qwen3-1.7B**.
The project is not yet an automated interpretability lab; the
[redirect plan](REDIRECT-PLAN.md) sets out how it becomes one.

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
  and record hypotheses. A compute request does not itself authorize a GPU start:
  a human approves it, or it fits an open human-issued budget envelope, and
  every per-Pod price, deadline and deletion check still applies.
- **Runs bounded jobs:** the worker and dispatcher enforce job limits and verify
  returned artifacts. Arbitrary CPU Python runs in a rootless Podman sandbox;
  arbitrary agent-written GPU Python is not supported.
- **Separates operational authority:** research, controller, watchdog and backup
  processes use distinct accounts. The controller handles approvals and provider
  credentials; the research agent and model worker do not receive those credentials.

These are implemented engineering capabilities. Their validation scope is described
below and in the [validation guide](docs/validation.md).

## Current status

- The supervised GPU profile has passed exact numerical calibration on both models; the managed-worker profile has not passed and is deferred.
- No interpretability results or validated scientific findings exist yet.
- [STATUS.md](STATUS.md) is the single source of truth for what works, with evidence; the current milestone is listed there.

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
with [STATUS.md](STATUS.md), then follow the relevant component guide:

| Need | Guide |
| --- | --- |
| Learn what works today and the current milestone | [Status](STATUS.md) |
| Understand the roadmap and its invariants | [Redirect plan](REDIRECT-PLAN.md) |
| Inspect the two passing numerical calibrations | [Calibration results](docs/calibration-results.md) |
| Run the smaller fixed public GPU calibration | [Supervised public calibration](docs/supervised-public-calibration.md) |
| Prepare the next exploratory milestone | [First public experiment](docs/first-public-experiment.md) |
| Understand the managed-service deployment requirements (deferred) | [Managed-service requirements](docs/live-deployment-plan.md) |
| Understand the implemented scope and boundaries | [Implementation overview](IMPLEMENTATION.md) |
| Set up the Ubuntu accounts and services | [Host installation](docs/host-installation.md) |
| Understand compute requests, approvals and shutdown | [Controller services](docs/controller-services.md) |
| Prepare and check GPU workers | [GPU deployment](docs/gpu-deployment.md) and [RunPod provider](docs/runpod-provider.md) |
| Understand dispatch, execution and returned artifacts | [Worker and dispatcher](docs/worker-dispatcher.md) |
| Configure independent recoverable backups | [Backup and restore](docs/backup-restore.md) |
| Use the queue, manifests and audit APIs | [Core guide](docs/core-guide.md) |
| Interpret test and deployment evidence | [Validation](docs/validation.md) |
| Read superseded plans and attempt logs | [History](docs/history/README.md) |

The controller uses the `controller` and `mcp` extras; it does not need the GPU
worker's PyTorch installation. The host installer expects a reviewed release
bundle containing pinned manifests, a Python runtime and locked offline dependencies.

Once the research service exists, an MCP client can launch `probe-mcp` with
`--socket` pointing to its research socket and `--service-uid` set to the trusted
service's actual UID. Keep the human approval interface separate from that client.

Model weights and credentials are not stored in this repository. Worker image
recipes fetch pinned public model files and synthetic calibration prompts; they
contain no hidden evaluation dataset.

## Roadmap and contributing

Work follows the [redirect plan](REDIRECT-PLAN.md): each milestone must end with
a result an interpretability researcher would recognise, and builds only the
infrastructure that result needs. Its invariants on credentials, budgets,
held-out data and the audit trail are never weakened.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing code. Changes to
`probe_core/` or `deploy/` update [STATUS.md](STATUS.md) and every affected
guide in the same commit, and superseded text moves to
[docs/history/](docs/history/README.md).

## License

[MIT](LICENSE). Model weights and third-party dependencies retain their own licenses.
