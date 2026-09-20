# Supervised public calibration

This is the smaller deployment milestone selected by the operator on September
20, 2026 after repeated startup failures. It runs the fixed `backend_parity_v1`
calibration on one disposable RunPod using the public, pinned Qwen3-1.7B assets.
It does not require a controller installation or nested cgroup delegation.

The host creates one price-capped Pod, verifies its SSH host key through the
provider's logs, submits one fixed command, copies the result files, verifies
their hashes, and deletes the Pod. A separate user service retains the original
15-minute deletion deadline if the foreground runner exits. The management key
stays on the host. Numerical code runs as UID10001 in a clean environment; the
image already contains its model assets, and model loading remains offline.

A successful result requires all 29 original numerical checks, including 25
exact comparisons, nine retained tensors, matching model/image provenance, an
observed process exit, and independent provider confirmation that the Pod is
absent. Numerical tolerances are unchanged. Outputs describe this as standalone
public calibration, with no scientific or held-out evaluation claim.

The Pod supplies the outer resource boundary. This profile does **not** establish
per-job CPU/RAM/PID enforcement, network denial, hostile-code containment,
controller-ledger execution, cancellation/recovery, or replacement acceptance.
Its 240-second numerical timeout is enforced by a separate root launcher, and
the host deletes the Pod at the original allowance deadline. Neither the local
guard nor RunPod's unverified scheduled stop establishes host-loss safety.

The general research worker and its stricter checks remain available unchanged.
This profile accepts no arbitrary operation, user-provided dataset, model code,
or private data. After Base numerical calibration succeeds, the next target is
the separately pinned posttrained checkpoint. Broader service and sandbox
acceptance can then proceed as separate milestones.

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
