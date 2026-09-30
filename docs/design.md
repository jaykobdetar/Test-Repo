# Design

Agent Workspace stores a shared task board for agents working through external
tools. The board records coordination decisions; it does not schedule or execute
work and does not call providers, shells, model runtimes or callbacks.

## Task lifecycle

| RPC / MCP method | Meaning |
| --- | --- |
| `submit_task` | Add an instruction with an idempotency key and optional dependencies |
| `claim_task` | Take one pending task whose dependencies are completed; receive a claim token |
| `complete_task` | Record a result using the current claim token |
| `fail_task` | Record a failure using the current claim token |
| `cancel_task` | End a task and invalidate its outstanding claim |
| `list_tasks` / `task_status` | Inspect board state |

The owner-only CLI `release` command requeues an unfinished task and invalidates
its old claim; it is not exposed over RPC or MCP.

Tasks move from `pending` to `running` to `completed` or `failed`; cancellation
sets `cancelled`. Completed tasks are immutable. Owner release can requeue
pending, running, failed or cancelled tasks. Claims do not expire: external agents
decide when to inspect or claim work, and the owner decides when to requeue it.
Cancellation and release invalidate the old claim so a late result cannot
complete it. Neither action interrupts external work. Coordinate with the
external agent before releasing work that might still be running.

Results are records supplied by agents. They are not proof that a command ran,
an artifact is correct, or a scientific finding is validated. Task and result
text are data, not instructions for the service to execute.

## Storage and transport

`AgentWorkspace(state_dir)` stores current task state and outcomes in `tasks.sqlite3`.
It exposes timestamps and attempt counts, not a complete event log. Owner release
clears the previous attempt outcome so the task can be retried; completed results
remain immutable.
Results are bounded JSON; timestamps are informational and do not trigger actions.
The CLI exposes an offline demo and owner recovery. The Unix-socket service exposes the agent-facing
operations through JSON RPC; the optional MCP adapter forwards those operations.
There is no TCP listener or hosted service deployment in the repository.

`list_tasks` returns compact summaries. Use `task_status` for full instructions,
results and failure reasons. This keeps board listings small without truncating
saved results.

On Linux, the transport checks peer OS identities. Clients pin the expected
service UID; the service allows configured agent UIDs. The `agent_id` on a claim
is a coordination label, not an authenticated identity. Agents with workspace
access share the board and are not isolated from one another. A socket group controls
filesystem access to the socket. Its parent directory must be owned by the
service and must not be group- or world-writable.

Use a separate service identity when clients must not read or modify state
directly. Keep the state directory private to that identity and place the socket
in a separately traversable service-owned directory. Do not give clients access
to the state owner's account. `--local` explicitly permits a shared OS identity
for development and provides no separation between that user's processes.

The owner-only `release` command operates on local state, outside the agent RPC
surface. External tools retain responsibility for their own permissions,
credentials, execution, timeouts, spending and cleanup. Transport I/O timeouts
only bound socket communication; they are not task deadlines or resource controls.

See [status](../STATUS.md) and the [simplification record](simplification.md) for
validation and the [history index](history/README.md) for the retired implementation.
