# Agent Workspace

Agent Workspace is a lightweight coordination board for agents that do their work
through their own tools. Submit a task, claim it, and record a result or failure.
The workspace keeps task state, ownership and saved results; it does not execute tasks.

Source: [jaykobdetar/Test-Repo](https://github.com/jaykobdetar/Test-Repo).
This local integration branch is `consolidate/project-2026-09-30`; it has not been
published. The Python package remains `probe-core` (`probe_core`), version 0.3.0.

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
the reviewed branch or commit. A fresh clone will not contain this unpublished
integration branch.

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

## Development

```sh
uv sync --locked --extra test --extra mcp
uv run --locked python -m pytest -q
uv build --no-sources
```

Read [CONTRIBUTING.md](CONTRIBUTING.md), [status](STATUS.md), and the
[simplification record](docs/simplification.md) for scope and validation evidence.

[MIT](LICENSE).
