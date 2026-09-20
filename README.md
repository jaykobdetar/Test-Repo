# Auto Interpretability Lab

Auto Interpretability Lab is experimental infrastructure for agent-assisted
research into language models. It lets a research agent request model inspections
and controlled interventions, records what ran and what it produced, and keeps
paid compute behind a separate approval process.

The intended models are **Qwen3-1.7B-Base** and **Qwen3-1.7B**. The project is still
being brought up on real GPU infrastructure. It is not yet an autonomous research
lab, and it has produced no validated scientific findings.

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
| Independent backups | Google Drive upload, download, hash verification and offline restore have passed. |
| RunPod containment | Resource controls and cleanup passed on one compatible host. Every newly assigned host must repeat the checks. |
| Model images | Separate Base and posttrained images are published with pinned public assets and verified registry provenance. Verification inside a live worker is still required. |
| End-to-end GPU execution | **Not established.** Seven calibration attempts stopped before model inference. No passing canonical GPU result is claimed. |
| Scientific workflow | Private held-out evaluation, independent Explorer/Skeptic/Replicator sessions and blind scientific calibration remain future work. |

The immediate milestone is one complete Base-model calibration: startup,
BF16/CUDA inference and interventions, verified result collection, and confirmed
Pod deletion. Eighteen ordinary acceptance plans are prepared across the two
models, but prepared plans and local tests are not live acceptance results.
See the [deployment checklist](docs/live-deployment-plan.md) for the remaining gates.

RunPod support currently permits short, supervised runs. Unattended operation,
provider shutdown during controller-host loss, direct Pod resumes and private
confirmation/replication evaluation are not accepted capabilities. Interventions
apply to prompt prefill; generation is a separate, unmodified operation.

## How the pieces fit

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

Accepted outputs are copied back to the controller and verified before a run can
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

Complete the remaining numerical, resource-limit and recovery checks for both
models, including replacement with a fresh Pod and retained artifacts. Then add
scientific metrics, matched controls, private held-out evaluation and independent
research roles before attempting open-ended discovery. SAE/Qwen-Scope, circuit
tracing and automatic novelty assessment remain outside the current implementation.

Contributions should state the behavior being changed and provide evidence for
claims about correctness, containment and scientific results. Changes to the
execution model or research protocol should describe their acceptance criteria.

## License

[MIT](LICENSE). Model weights and third-party dependencies retain their own licenses.
