# Project status

Updated September 30, 2026, on local, unpublished branch
`consolidate/project-2026-09-30` in
[jaykobdetar/Test-Repo](https://github.com/jaykobdetar/Test-Repo).

Agent Workspace replaces the former interpretability infrastructure with a local
task coordination board. The intended workflow is submit → claim → complete or
fail, with explicit cancellation and trusted-owner release for recovery. Agents
perform all actual work through their own tools.

The active package is `probe-core` 0.3.0 with the `probe-agent` command, an offline
demo, a Unix-socket service, a JSON client and an optional stdio MCP adapter.
The base package has no third-party runtime dependencies.

Qwen/model execution, RunPod management, sandbox execution, scientific
certification, service deployment and image publishing are intentionally removed.
There are no automatic runtime or cost limits, watchdogs or task lease expiry.
Cancellation does not stop external work or resources.

The [simplification record](docs/simplification.md) records the exact retained
scope, removed surface and checks run for this branch. Historical test totals,
GPU results, host installations and budget approvals do not validate or authorize
this new project. Earlier source and evidence remain in [Git history](docs/history/README.md).

No deployment or remote publication is part of this local change.
