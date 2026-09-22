# Supervised public calibration

This is the smaller deployment milestone selected by the operator on September
20, 2026 after repeated startup failures. It runs one registered suite on one
disposable RunPod using the public, pinned Qwen3-1.7B assets: the fixed
`backend_parity_v1` calibration, or a frozen exploratory recipe. It does not
require a controller installation or nested cgroup delegation.

## Registered suites and budget envelopes

The command accepts only a suite named in `probe_core/resources/recipes/index.json`
(`probe_core/recipe_registry.py`). Each entry pins its public prompt dataset by
content hash, the checkpoints it may run on, and, for a recipe, the frozen recipe
file by the SHA-256 of its canonical JSON. An unknown suite, changed recipe file
or unregistered model is refused. An image may bake several registered datasets
and nothing else. Set `"suite"` in `run.json`; it defaults to `backend_parity_v1`.

A recipe run must be charged to an open budget envelope in an operator-owned
ledger named by `"budget_ledger"` in `run.json`. Issue, inspect and close it with:

```sh
python -m probe_core.budget --ledger ~/probe-budget/budget.sqlite issue --envelope-file envelope.json
python -m probe_core.budget --ledger ~/probe-budget/budget.sqlite status
python -m probe_core.budget --ledger ~/probe-budget/budget.sqlite close --envelope-id m1-exploratory-001
```

The envelope file has the fields shown in
[controller services](controller-services.md#budget-envelopes). Before creating
the Pod, the runner reserves `(remaining deadline + 300 s) × envelope hourly
ceiling` and refuses without creating anything if no envelope is open, the model
or stage is outside it, or the remainder cannot cover the reservation. Creation
uses the lower of $0.80/hour and the envelope's ceiling. After the provider
confirms deletion, the reservation settles at the measured interval times the
quoted price; if deletion is unconfirmed, the full reservation stays held. The
same accounting code serves the installed controller.

Images built from this branch also bake the registered experiment datasets. A
managed-launcher configuration for such an image must list every baked dataset,
because `probe_core/gpu_launch.py` requires an exact match (that profile is
deferred).

A recipe result is reported as `completed`, never `passed`: the host verifies
provenance, the no-op checks, the retained tensor inventory and that every prompt
is reported for every metric, but the measurements are exploratory.

The host creates one price-capped Pod, verifies its SSH host key through the
provider's logs, submits one fixed command, copies the result files, verifies
their hashes, and deletes the Pod. A separate user service retains the original
15-minute deletion deadline if the foreground runner exits. The management key
stays on the host. Numerical code runs as UID10001 in a clean environment; the
image already contains its model assets, and model loading remains offline.

A successful calibration result requires all 29 original numerical checks, including 25
exact comparisons, nine retained tensors, matching model/image provenance, an
observed process exit, and independent provider confirmation that the Pod is
absent. Numerical tolerances are unchanged. Outputs describe this as standalone
public calibration, with no scientific or held-out evaluation claim.

The Pod supplies the outer resource boundary. This profile does **not** establish
per-job CPU/RAM/PID enforcement, network denial, hostile-code containment,
controller-ledger execution, cancellation/recovery, or replacement acceptance.
The numerical command has a 240-second timeout; a separate root launcher kills
its process group after at most 250 seconds, capped by the original Pod deadline.
The host deletes the Pod at the original allowance deadline. Neither the local
guard nor RunPod's unverified scheduled stop establishes host-loss safety.

The general research worker and its stricter checks remain available unchanged.
This profile accepts no arbitrary operation, user-provided dataset, model code,
or private data. Both the Base and separately pinned posttrained checkpoint passed on September
20, 2026. See the [results and exact scope](calibration-results.md). The
current milestone and next step are recorded in [STATUS.md](../STATUS.md).

The derived image includes a C compiler because the pinned PyTorch/Triton stack
compiles GPU kernels during inference. The build compiles and executes a small C
program to check that dependency before a paid run. This build check does not
replace numerical GPU calibration.

Files:

- `deploy/gpu/public-calibration.py`: fixed numerical command and standalone report.
- `deploy/gpu/public-calibration-entrypoint.py`: SSH and the unprivileged process timeout.
- `deploy/gpu/run-public-calibration.py`: supervised host run and separate deadline guard.
- `deploy/gpu/Dockerfile.public-calibration`: small image layer over a verified model image.

Keep the run configuration and provider state in a private directory. Start the
`guard` command as an independent user service before the `run` command. A
consumed creation marker must never be replayed. Reserve the run's maximum cost
in the project budget before creation, and do not start another Pod until
provider deletion is confirmed.

## Connecting the simplified worker to the ledger

The SSH adapter is implemented and locally tested. Its installed GPU acceptance
is still pending. The two successful standalone calibrations above are not
evidence that this new path has passed.

`probe_core/supervised_runner.py` retains the existing controller, approvals,
ledger, dispatcher, watchdog, and artifact validation. It replaces the worker
HTTP server and SSH tunnel with short SSH commands through
`probe_core/ssh_job_client.py`. The controller approves one exact job on one
disposable Pod. The runner cannot issue approvals or create a Pod itself.

The host stages a SHA-pinned `deploy/gpu/public-job.py` into the existing image.
This helper accepts only the fixed public acceptance recipes. It records an
exclusive attempt claim before launching a detached root monitor and a clean
UID10001 numerical child. Reconnecting reads the same attempt and original
deadline; it does not submit another execution. The monitor uses process
descriptors to stop only its own child tree. A cancellation pass requires a
running observation followed by evidence that the original child was signalled
and its descendants stopped.

This is a trusted, fixed-code execution profile. It does not claim per-job
cgroups, hard host-RAM limits, network denial, or hostile-code isolation. The
remaining GPU tests must separately prove the original execution deadline,
output limit, CUDA allocator limit, artifact retention, and provider deletion.
CPU process tests and simulated ledger tests do not substitute for those GPU
results. Connection recovery, coordinator restart, and fresh-Pod replacement
also remain acceptance requirements.

The additive sidecar installation uses the existing Python environment and
service identities. It does not replace the application wheel or change the
provider credentials. A pinned suite can queue several independent proposals;
each still requires its own approval. The runner stops the suite on a failed
case. Neither service is enabled to start at boot.
