# Agent Workspace

Agent Workspace is a lightweight coordination board for agents that do their work
through their own tools. Submit a task, claim it, and record a result or failure.
The workspace keeps task state, ownership and saved results; it does not execute tasks.

Source: [jaykobdetar/Test-Repo](https://github.com/jaykobdetar/Test-Repo).
The consolidated branch is `consolidate/project-2026-09-30`.
The Python package remains `probe-core` (`probe_core`), version 0.3.0.

## Try it locally

Use Linux, Python 3.13 and [uv](https://docs.astral.sh/uv/). From this checkout:

```sh
uv sync --locked --extra mcp
uv run --locked probe-agent demo --state "$(mktemp -d)/state"
```

Choose a fresh state path for the demo. It demonstrates agents passing work
through the board entirely offline, without credentials or external resources.
The base package uses Python's standard library; the `mcp` extra adds the MCP adapter.

For a new checkout, clone `https://github.com/jaykobdetar/Test-Repo.git` and select
`consolidate/project-2026-09-30` or a reviewed commit.

## Connect an agent

Start the local development service in one terminal:

```sh
uv run --locked probe-agent serve --state /tmp/probe-agent-board \
  --socket /tmp/probe-agent-board/agent.sock --local
```

Configure your MCP client to launch this command as the same OS user:

```sh
uv run --locked probe-agent mcp --socket /tmp/probe-agent-board/agent.sock \
  --service-uid "$(id -u)"
```

`--local` allows the service and its clients to share one OS account. For separate
identities, replace it with `--agent-uid UID --socket-gid GID`, keep state private
to the service owner, and make the socket directory traversable by the client.
Clients pin the actual service owner's UID with `--service-uid`.

Inspect the board from another terminal with the JSON client:

```sh
uv run --locked probe-agent call --socket /tmp/probe-agent-board/agent.sock \
  --service-uid "$(id -u)" list_tasks '{}'
```

See [design](docs/design.md) for method names, transitions and the identity boundary.

## What it controls

Tasks have explicit submit, claim, complete, fail and cancel actions, plus list
and show operations. A claim remains active until one of those actions changes
it. There are no leases, expiry timers, automatic retries, cost controls or watchdogs.
If an agent disappears, the trusted state owner can release its claim or requeue
a failed or cancelled task:

```sh
uv run --locked probe-agent release --state /tmp/probe-agent-board TASK_ID
```

Cancelling a task invalidates its claim and prevents a later result from being
accepted. It does **not** stop an external agent, process or cloud resource.
Agents and their operators manage all execution, credentials, resources and cleanup.

The former Qwen experiments, model operations, RunPod provisioning and stopping,
sandbox execution, scientific certification, service installation and image
publishing have been removed from the active project. Their source remains in
[Git history](docs/history/README.md).

## Two reusable examples

The [research study template](examples/research-study.md) defines role and file
ownership, one compute owner, a small input/output contract, and pilot → frozen
experiment → independent verification checkpoints using real board tasks.

The optional [resumable runner](examples/resumable_run.py) illustrates how a trusted
compute owner can execute local commands and publish results through
`AgentWorkspace`. It is a standalone example, outside the core package. Save this
tiny plan as `jobs.json`:

```json
[
  {"id": "pilot", "command": ["python3", "-c", "from pathlib import Path; Path('result.txt').write_text('done')"], "outputs": ["result.txt"]}
]
```

```sh
uv run --locked python examples/resumable_run.py jobs.json --state /tmp/my-experiment
```

Use a dedicated private directory and one runner. Commands are argument lists,
without a shell, and run in a fresh `work/` directory per attempt, with receipts
and logs alongside it. Use absolute paths for scripts and input files; output
paths are relative to that working directory.
Keep credentials out of commands and logs. Commands inherit your environment
and permissions: this example is not an execution sandbox.

Each job gets a real board claim and a receipt recording its command, working
directory, diagnostic PID, stdout/stderr paths, declared outputs and actual
observed exit code (`null` when unknown; negative values indicate signals).
Successful completion requires an observed zero exit and the declared files.
That checks artifact presence, not scientific correctness. Completed jobs are
skipped on resume. The plan stays fixed; use a new directory for changed jobs.

After interruption, inspect the receipts and external work. A missing process,
reused PID or partial output does not prove success. Only after independently
confirming the old job and its process tree have stopped, explicitly recover it:

```sh
uv run --locked python examples/resumable_run.py jobs.json --state /tmp/my-experiment \
  --recover-stopped pilot
```

Recovery reclaims the unfinished task. A saved observed success is published
without rerunning its command; otherwise a new attempt preserves the old files.
An unknown exit stays unknown even if the files look complete. Repeated execution
can repeat external side effects, so commands must tolerate retries or be
reconciled manually. A child may outlive the runner or launch detached work;
neither this example nor board cancellation stops it. There are no watchdogs,
automatic retries, runtime limits or spending controls. Monitor and stop external
work yourself. The example's board is its job ledger; receipts are evidence,
not another scheduler. Do not mix unrelated tasks into it.

## Development

```sh
uv sync --locked --extra test --extra mcp
uv run --locked python -m pytest -q
uv build --no-sources
```

Read [CONTRIBUTING.md](CONTRIBUTING.md), [status](STATUS.md), and the
[simplification record](docs/simplification.md) for scope and validation evidence.

[MIT](LICENSE).
