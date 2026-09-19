# Probe-MCP

Probe-MCP is an experimental toolkit for giving a research agent controlled
access to a language model's internals. It combines model inspection and
activation interventions with a persistent experiment ledger, bounded execution,
and a separate human approval path for compute.

The intended research subjects are **Qwen3-1.7B-Base** and **Qwen3-1.7B**. The
current implementation has been tested locally with a small, randomly initialized
Qwen3 model on CPU. It is an early research infrastructure project: live RunPod
deployment, hidden evaluation, and the independent scientific workflow remain
unfinished. No scientific discovery is claimed.

## What works today

- **Persistent experiments:** a local SQLite ledger with job states, leases,
  cancellation, recovery, immutable accepted manifests, and a hash-chained audit log.
- **Model operations:** activation capture, patching, ablation, steering, probe
  fitting, bounded generation, weight statistics, tensor slices, and module inspection.
- **Two numerical backends:** Hugging Face/PyTorch as a reference and NNsight
  for interventions, with local parity tests.
- **Trusted execution:** a separate worker process, authenticated loopback HTTP,
  a controller-side dispatcher, and hash-checked artifact transfer and retention.
- **Research tools over MCP:** stdio access to jobs, history, hypotheses, artifacts,
  CPU experiments, and compute requests. Submitting a job does not approve a GPU start.
- **CPU Python sandbox:** rootless Podman with restricted mounts, no network or GPU,
  and enforced time, memory, process, CPU, and output limits.
- **Compute-control foundation:** one-time human approvals, price/runtime checks,
  and an independent watchdog, currently connected to a persistent provider simulator.

The repository is named `probe-mcp`; the Python distribution and import package
are `probe-core` and `probe_core`, respectively.

## Current limits

| Area | Status |
| --- | --- |
| Cloud provider | Simulator only; no live RunPod adapter is included |
| GPU execution | CUDA paths exist; real GPU limits, SSH deployment, and canonical checkpoint parity still require validation |
| Flexible experiments | Arbitrary CPU Python plus fixed worker operations; no arbitrary agent-written GPU Python |
| Interventions | Apply to the prompt's prefill pass; generation is a separate, unmodified operation |
| Scientific evaluation | Confirmation/replication execution is refused until a private evaluator is implemented |
| Research workflow | Hypothesis storage and freezing exist; Explorer/Skeptic/Replicator orchestration and blind calibration remain pending |
| Advanced methods | SAE/Qwen-Scope, circuit tracing, and automated novelty adjudication are not implemented |

Model weights, credentials, research datasets, and built container images are not
included. Deployment files are examples and do not install or start services.

## Architecture

```text
Research agent
    │ stdio MCP
    ▼
Research facade ──────────► local ledger and retained artifacts
    │ compute request
    ▼
Human approval ───────────► trusted controller ──► provider simulator
                                  │                   ▲
                         approved job batch      watchdog stop
                                  ▼
                              dispatcher
                                  │ authenticated loopback HTTP
                                  │ (SSH tunnel for a remote deployment)
                                  ▼
                              model worker

Research facade ──────────► rootless CPU Python sandbox
```

The research interface cannot consume approvals, accept worker results, certify
shutdown, or administer hidden evaluation. Production separation depends on
distinct OS identities and correctly installed permissions; running every role
under one account is a development setup. The live ledger belongs on local
controller storage, with external backups, rather than a network filesystem.

## Install and run the development tests

Requirements: Linux, Git, [uv](https://docs.astral.sh/uv/), and Python 3.13.
The worker dependencies include PyTorch and require several gigabytes of disk.
Use a maintained SQLite build; see the [core guide](docs/core-guide.md).

```sh
git clone https://github.com/jaykobdetar/probe-mcp.git
cd probe-mcp
uv python install 3.13
uv sync --locked --all-extras
uv run --locked python -m pytest -q
```

These tests create their small model locally and need no cloud credentials or
downloaded model checkpoint. Tests involving services bind local sockets. The
nine real sandbox integration tests skip unless a suitable Podman environment
and image are configured; a default test run does not validate containment.

The full local acceptance run on September 19, 2026 passed **429 tests with no
skips**, including the real sandbox gate. This verifies the tested engineering
paths, not scientific calibration or production readiness. See
[verification details](docs/validation.md).

To run the mandatory sandbox gate after preparing its pinned image:

```sh
PROBE_SANDBOX_REQUIRED=1 \
PROBE_SANDBOX_IMAGE="sha256:REPLACE_WITH_64_HEX_IMAGE_ID" \
uv run --locked python -m pytest tests/test_sandbox.py tests/test_sandbox_service.py -q
```

Replace the image placeholder before running. The [sandbox guide](deploy/sandbox/README.md)
covers the image build, subordinate user mappings, delegated cgroups, and service
runtime requirements. With `PROBE_SANDBOX_REQUIRED=1`, missing capabilities fail
the tests instead of skipping them.

## Configure the services

Installation alone does not create a running lab. Follow these guides to prepare
private configuration, service accounts, sockets, worker assets, and directories:

- [Controller, approvals, and watchdog](docs/controller-services.md)
- [Worker and dispatcher](docs/worker-dispatcher.md)
- [CPU sandbox and containment verification](deploy/sandbox/README.md)
- [Implementation scope and service boundaries](IMPLEMENTATION.md)
- [Core API, queue, manifests, audit, and backups](docs/core-guide.md)

After the research service is configured, an MCP client can launch `probe-mcp`
with `--socket` pointing to its research socket and `--service-uid` set to the
trusted service's actual UID. The controller approval interface belongs to the
human account, not the research agent's MCP configuration.

## Next milestones

1. Implement the live RunPod adapter and deploy separate trusted services,
   pinned model assets, storage, backups, and verified shutdown.
2. Validate canonical BF16/CUDA execution, resource limits, and remote recovery.
3. Implement scientific metrics, matched controls, private held-out evaluation,
   and trusted promotion rules.
4. Add fresh Explorer, Skeptic, and Replicator sessions; pass blind calibration
   before beginning open-ended discovery.
5. Run one narrow behavioral/mechanistic pilot, then expand into paired model
   and thinking-mode studies, advanced methods, and scientific-efficiency reporting.

Contributions should keep new claims tied to evidence, preserve the separation
between research and compute authority, and include relevant regression tests.
Larger changes to the execution model or scientific protocol benefit from an
issue describing the proposed behavior and acceptance criteria first.

## License

[MIT](LICENSE). Model weights and third-party dependencies retain their own licenses.
