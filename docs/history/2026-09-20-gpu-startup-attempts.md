# Managed-worker startup attempts and incident recoveries (September 2026)

[Status](../../STATUS.md) · [History index](README.md) · [GPU deployment](../gpu-deployment.md)

Moved unchanged from the "Startup and supervised retry" section of
`docs/gpu-deployment.md` at source `db92311`, apart from relative link paths.
Current capability is recorded only in [STATUS.md](../../STATUS.md).

Deferred: not required by current roadmap.

## Startup and supervised retry

This section records recovery contracts and historical startup failures. The
installed controller is source `4b19ac6`, with verified backup, 16 CPU checks and
26 identity checks. The eighth attempt reached approved dispatch but failed
CUDA initialization. The narrow AF_UNIX creation correction has passed local
driver and BF16 kernel checks; the rebuilt image still needs RunPod acceptance.
A retry preserves the completed failure and stopped attempt, binds a new immutable
worker image, and creates fresh job/request/approval identities. See
[the current deployment checklist](2026-09-20-live-deployment-checklist.md).

On September 20, 2026, the installed controller update passed a fresh Drive
upload/download/restore, 16 CPU containment checks and 26 account checks. The first
queued canonical calibration failed during connection discovery and was deleted
before SSH configuration or model execution. A null-runtime response reproduces
the failure locally; the original generic error did not retain its traceback.
That failed attempt is not parity evidence. Its consumed approval and original records must be retained; a retry
uses a fresh job and approval under the same cumulative operator budget.

The next attempt reached the old three-minute startup cutoff while RunPod was
still downloading and extracting the pinned image, then confirmed Pod deletion
without running a model. The image is 5.90 GB compressed. Startup now uses the
existing approval deadline minus the full job runtime and a 120-second allowance
for result collection and deletion. For the current 900-second approval and
240-second job, this gives up to 540 seconds from approval consumption to worker
readiness and dispatch. It does not extend the paid allowance. Connection,
configuration and readiness must all finish before that cutoff; a late worker
cannot start a job. A subsequent live attempt loaded the image after about three
and a half minutes, then failed before SSH or inference at the credential guard.

[RunPod automatically supplies a Pod-scoped API key](https://docs.runpod.io/pods/templates/environment-variables),
even though the controller does not send its management credential in the launch
environment. The observed credential-guard failure is consistent with this
injection; the guard did not log the triggering key or value.
The corrected launcher re-executes its fixed interpreter in isolated mode with
an allowlisted environment before any cgroup, SSH or model work. SSH and every
worker process also receive explicitly constructed environments. This removes
inherited credentials from their exec-time environment, including the bytes
exposed through [Linux process environment files](https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html).
The corrected immutable Base image is published and used by later attempts,
but still has no successful numerical acceptance result. The credential-guard
failure produced no numerical evidence.

The incident-specific `deploy/retry-gpu-calibration.py` helper has two phases
around the normal wheel upgrade. It first verifies the stopped request, closed
allowance, provider absence and zero execution attempts, then cancels only that
pending job through the research service. After a verified upgrade, it preserves
the original state directories and prepares a new calibration in separate
subdirectories. The first two recovery schemas allow only a new plan label,
idempotency key and state/configuration paths; the model, worker image, limits
and service permissions remain fixed. Neither phase issues a compute approval.
The second recovery also pins the completed first recovery's manifest, receipts
and nested configuration paths, and verifies the exact startup-timeout result.
Each failed job and consumed approval remains in the ledger; none is replayed.
The third recovery additionally pins the complete canonical failed result and
the previous two completed recoveries. It may replace only the worker image and
software provenance, with the same model, data, operations, limits and deployment
scope. Its fresh public worker configuration contains no bearer token or key.
The fourth recovery pins the exact configuration/tunnel failure and all three
completed recoveries. It changes only the controller software and fresh job/state
identities, preserving the third recovery's worker configuration, image digest
and source identity. Controller and remote-worker source commits are recorded
separately. The ordinary backup, sandbox and account checks still precede the
fresh calibration, and the recovery itself cannot start paid compute.

