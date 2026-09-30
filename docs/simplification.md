# Simplification — September 30, 2026

[Overview](../README.md) · [Design](design.md) · [Status](../STATUS.md)

The owner chose to replace the Qwen experiment and RunPod-management stack with
agent coordination, and explicitly removed automatic runtime/spending controls.
This branch implements that change locally. Nothing has been pushed or deployed.

## What remains

The base package is seven small Python modules: a SQLite task board, strict JSON
encoding, a CLI, a narrow task service, the authenticated Unix transport, an
optional MCP adapter, and package metadata. It has no third-party runtime
dependencies. MCP is an optional extra.

Agents submit tasks, claim ready work, use their own tools, and record a JSON
result or failure. Dependencies gate claiming; completed results are immutable.
State survives disconnects and process restarts. Four independent processes
competing for one task produce exactly one claim in the regression suite.

Owner recovery explicitly requeues unfinished work and revokes the previous
claim. It clears the previous attempt outcome; this is a current-state board,
not a full event-history or scientific-evidence ledger. External execution and
result correctness remain the agent's responsibility.

## Removed capabilities and checks

- Model loading, numerical interventions, recipes, calibration, scientific-stage
  gates, hypothesis certification, and canonical model/dataset requirements.
- RunPod provisioning and stopping, budget envelopes, price ceilings, compute
  approval grants, provider reconciliation and automatic spending reservations.
- Managed workers, SSH launch/collection, sandbox execution, automatic runtime
  deadlines, idle shutdown, watchdogs, and replacement/recovery acceptance plans.
- Host installers, systemd units, image recipes and automatic image publishing.
- The historical deployment/science test matrices, mandatory documentation-path
  checks and automatic formatting gate. One small CI job runs tests and builds
  the package. Formatting remains an optional developer tool.

These are actual component removals, not disabled test discovery or silently
successful replacements. The previous complete suite passed before this change:
2,327 passed and 10 environment-specific skips. Its source, checks and evidence
remain at `381d4f0b832ca72afa051dec8bf00111e213e642`; see
[the recovery instructions](history/README.md). Existing branches and original
worktrees remain intact.

This is an intentional API change, not an automatic migration of previous
experiment ledgers. Start a new workspace; old research clients, service
configurations and manifests remain part of the historical implementation.

No active code executes task instructions, shells, model runtimes or provider
calls. Removing obsolete executor policy files does not change any deployed
OS/network settings or install an unrestricted replacement executor.

## Explicit control semantics

There are **no task expiry timers, execution deadlines, automatic retries,
runtime ceilings, cost limits, budget approvals or shutdown watchdogs** in this
framework. A running claim persists indefinitely until someone completes,
fails, cancels or explicitly releases it.

`cancel_task` and owner `release` revoke result-publication authority; they do
**not** kill an external agent, terminate a process or delete a paid resource.
Those actions require the appropriate external tool. The framework does not
infer that failed communication means external work has stopped.

The remaining bounds protect transport and stored data: 16 KiB instructions,
64 KiB JSON results, 4 KiB failure reasons, 64 dependencies, and 100 summaries
per list page. Unix RPC retains peer-UID checks, private sockets, 1 MiB requests,
8 MiB responses and a 30-second communication timeout. SQLite lock contention
can fail an operation after five seconds. None of these waits changes task
status or shuts down external work. Claim tokens are stored as hashes and are
not included in listings or status responses.

The state owner can access the database directly. Agent labels are not separate
security identities: clients authorized for a board share that board. Use a
separate service UID and private state directory for isolation from clients;
`--local` explicitly trusts processes under the same OS account.

## Size and validation

Counts compare the consolidated snapshot `381d4f0` with this implementation.
Lines are physical source lines, including comments and blanks; environments,
caches, Git history and generated packages are excluded.

| Measure | Before | After |
| --- | ---: | ---: |
| Runtime Python modules / lines | 36 / 18,653 | 7 / 787 |
| Test files / lines | 68 / 27,187 | 3 / 416 |
| Test functions | 975 | 15 |
| Collected pytest cases | 2,337 | 15 |
| Deployment files | 58 | 0 |
| CI workflows | 4 | 1 |
| Packages in the lock, including optional extras | 120 | 38 |
| Base direct third-party runtime dependencies | 1 | 0 |

The old suite used 318 parameterization decorators, so the number of cases was
larger than the number of test functions. This change also removes the underlying
production and test source; it does not simply pack the old matrix into fewer
reported cases. The new tests exercise representative contracts, not exhaustive
historical combinations.

Validation completed locally with CPython 3.13.5 and locked offline dependencies:

- **15 tests passed in 3.91 seconds**, with no skips. They cover the three-agent
  dependency/result workflow, four-process atomic claims, persistence, manual
  recovery, stale-token rejection, private state, authentication and real MCP
  disconnect/reconnect behavior.
- Source and wheel builds pass. A fresh wheel installation in a virtual
  environment containing only `probe-core` runs the offline CLI demo and reopens
  its completed result; no model or provider packages are installed.
- Package contents, relative documentation links and whitespace were checked.
  A separate static reviewer examined the runtime and removal diff. Findings
  concerning demo reuse, list-response size and event-history wording were fixed.

No paid operations, live service acceptance, container publication or deployed
configuration changes were performed. The next publication decision is a normal
push of `consolidate/project-2026-09-30`; approval is still pending.
