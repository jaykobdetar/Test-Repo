# Research study template

Copy this into a study directory and replace the brackets. Use the existing board
for ownership, dependencies and results; files hold the actual research evidence.
The board does not execute work or validate a scientific claim.

## Question and scope

- Question: [Which published claim are we testing? Link the source.]
- Scope: [Exact reproduction, partial replication, or an explicitly named extension.]
- Success evidence: [Measurements and comparisons that would support the claim.]
- Limits: [What this study cannot establish; available compute and data.]

## People, agents and files

| Role | Writes | Responsibility |
| --- | --- | --- |
| Coordinator / methods | `spec/`, `report/` | Read sources, define scope, route claims, freeze the plan, report outcomes. |
| Compute owner | `code/`, `runs/` | Implement, run the pilot and experiments, retain outputs and actual exit evidence. |
| Analyst (optional) | `analysis/` | Read saved outputs; request additional runs through the compute owner. |
| Independent reviewer | `verification/` | Check source interpretation, implementation, raw results and final claims. |

Assign names before starting: coordinator [name], compute owner [name], reviewer
[name]. Combine roles when useful, but the final reviewer must be independent of
the compute author. There is exactly **one compute owner**: other agents do not
load duplicate models or launch competing runs. File ownership is an agreement,
not an access-control boundary; do not edit another role's outputs.

## Small input/output contract

Each run request identifies a stable job ID, the question/condition, input paths,
parameters/seeds where relevant, command arguments, working directory and expected
output paths. Agree on the few output fields analysts need before the pilot.
Keep secrets out of commands, board results and receipts.

The compute result identifies completed job IDs, output paths, observed exit
statuses and unresolved failures. Save large artifacts in files; put their paths
and a short summary in the board result. A simple optional run receipt is enough;
see [resumable execution](resumable_run.py). Use a new job ID for a changed
experiment; preserve completed outputs and explain which result it replaces.

## Checkpoints on the actual board

This checkpoint sketch uses owner-local `AgentWorkspace`; remote agents can use
the equivalent authenticated RPC/MCP methods in the [design](../docs/design.md).
Use it when agents execute through their existing tools. The runner is a separate
optional pattern with its own dedicated job board; do not point it at this
checkpoint board. If using both, the compute owner links the runner's receipts
and job results in the study/run result; do not duplicate per-job state here.

```python
from probe_core.tasks import AgentWorkspace

board = AgentWorkspace("study/state")
plan = board.submit("study/plan", "Coordinator: define the question and input/output contract in spec/.")
pilot = board.submit("study/pilot", "Compute owner: run a small pilot; save outputs, exits and practical sizing.",
                     depends_on=[plan["task_id"]])
freeze = board.submit("study/freeze", "Coordinator: review the pilot and record the fixed experiment plan in spec/.",
                      depends_on=[pilot["task_id"]])
run = board.submit("study/run", "Compute owner: execute the frozen jobs; record completed and unfinished work.",
                   depends_on=[freeze["task_id"]])
verify = board.submit("study/verify", "Independent reviewer: inspect sources, code, raw outputs and draft claims.",
                      depends_on=[run["task_id"]])
report = board.submit("study/report", "Coordinator: report evidence, deviations, failures and reviewer conclusions.",
                      depends_on=[verify["task_id"]])
```

Choose unique keys per study; reusing a key with the same instruction and
dependencies returns the same task. The coordinator routes the **oldest ready
task** to its agreed role: `board.claim(agent_id)` cannot select a task by ID or
enforce roles. Inspect the returned instruction before working. Keep its
`claim_token` private, and use it only to finish that claim:

```python
claim = board.claim("coordinator")  # First ready task in a fresh study: plan.
# Do the claimed work, then publish its real evidence:
board.complete(claim["task_id"], claim["claim_token"],
               {"summary": "Question and contract recorded", "paths": ["study/spec/plan.md"]})
```

Pilot outputs establish that the implementation works at a practical size; label
exploratory evidence. Freeze the chosen inputs, conditions, measurements and job
list before the main run. Record later deviations plainly; choose study-specific
statistics only when needed. Independent review should check scientific meaning
as well as numerical consistency, and disclose what cannot be reconstructed from
retained artifacts. A completed board task is not a scientific endorsement.

## Interruption and recovery

Record a known command failure with `board.fail(task_id, claim_token, reason)`.
If a completion response is lost, inspect `board.show(task_id)` before retrying.
A missing process or a partial output never proves success. A PID can be reused;
check the original session/process identity and evidence before acting on it.
An unobserved exit stays unknown, even if some outputs look plausible.

Before retrying, the operator coordinates or explicitly stops any external work,
preserves prior evidence, then uses `probe-agent release --state study/state TASK_ID`
for unfinished tasks. Release clears that attempt's board outcome and invalidates
its token; completed tasks remain immutable. Reclaim, inspect the receipt, and
rerun only unfinished jobs after isolating their partial artifacts. Cancellation
and release do **not** stop processes or resources. There are no task expiry,
runtime or spending limits: the operator and compute owner monitor and clean up.
