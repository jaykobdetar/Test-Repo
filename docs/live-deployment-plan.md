# Auto Interpretability Lab: Managed-service deployment requirements

[Project overview](../README.md) · [Status](../STATUS.md) · [GPU procedure](gpu-deployment.md)

This guide records the selected deployment setup and the requirements the full
managed-service profile must meet. Its work ordering is superseded by
[REDIRECT-PLAN.md](../REDIRECT-PLAN.md); the managed-worker lifecycle and
unattended operation are **Deferred: not required by current roadmap.** Current
standing is recorded only in [STATUS.md](../STATUS.md). The attempt-by-attempt
record, published image digests and the superseded supervised sequence are in
[the 2026-09-20 checklist snapshot](history/2026-09-20-live-deployment-checklist.md).

## Selected setup

| Decision | Initial choice | Reason |
| --- | --- | --- |
| Controller | The operator's Ubuntu machine; local SQLite and retained artifacts | Avoid another server and hosting bill for the first deployment. The machine must stay on during supervised GPU runs. |
| Transport | Supervised: one fixed command over pinned-key SSH. Managed: authenticated loopback HTTP through SSH | The supervised path needs no persistent research API or installed dispatcher. |
| GPU | One Secure Cloud RTX 4090, subject to a fresh price and capacity check | 24 GB is sufficient for the two 1.7B models loaded separately and short, bounded calibration jobs. |
| Data center | Revalidate before every Pod creation | Capacity and compatible driver availability change; bind the chosen location to that run. |
| GPU storage | Disposable Pod disk with immutable public assets baked into the worker image; no network volume | This later operator-approved choice avoids persistent GPU storage. Fetch, verify and seal outputs on the controller before declaring success. |
| Backup | A private Google Drive folder with a separate background uploader identity | A copy remains outside both RunPod and the controller machine. Each upload must be downloaded and verified. |
| Model inputs | Both canonical Qwen3-1.7B checkpoints, with exact revisions and file hashes | Preserve the distinction between Base and the released post-trained model. |
| Agent API spending | No autonomous paid agent API configured in this deployment stage | Scientific-agent orchestration is a later milestone; its budget must be approved separately. |

The observed GPU catalog rate was $0.74/hour. This is an observation, not a price
promise. The controller refreshes the quote and enforces the approved ceiling
before creation. The operator has authorized this project's bounded GPU tests
under a cumulative $20 ceiling; reserve each run's maximum cost before approving
it, and keep shutdown/storage uncertainty within that total. Under the
[redirect plan](../REDIRECT-PLAN.md), each paid run also needs a cost estimate
and a human-approved budget envelope within that total; the $20 authorization
covers Milestones 1–2 at most.

The initial 100 GB network-volume proposal is superseded by disposable research
Pods. The temporary empty 20 GB test volume was deleted. No model download occurs
inside a scientific job. Replacement uses the same immutable worker image and
rechecks model hashes and previously retained controller artifacts before a fresh
bounded parity run. Pod loss makes any uncollected output failed or inconclusive;
it cannot silently replay the old job or approval. Backups remain independent
of both the Pod and controller through verified Google Drive copies.

## Managed-service acceptance requirements

The original phase numbers in historical records differ from these five
deployment steps. Use the following requirements when judging completion.

1. **Infrastructure diagnostic.** Freeze a small diagnostic image and script,
   exact resource request, price ceiling, and short runtime. Obtain human
   approval. Measure the NVIDIA driver, visible GPU, and actual writable cgroup
   controls. Check provider shutdown behavior; do not infer that a container
   exiting means compute billing stopped.
2. **Separate services.** Install administrator-owned code and configuration,
   distinct research/trusted/watchdog/backup identities, and peer-checked Unix
   sockets. The untrusted research identity and GPU receive no cloud-management
   key. Test access denial as those actual identities.
3. **Recovery and backup.** Make a consistent SQLite snapshot with its retained
   artifacts and audit evidence. Upload it to Drive, download it, verify every
   file hash, and restore into a separate private directory. Expired backup
   authentication must produce a visible failure.
4. **Pinned worker and assets.** Build the worker image, resolve its immutable
   registry digest, bake only the selected safe public model assets, and verify
   their pinned hashes as the actual worker inside the deployed image. Preserve
   separate Base and posttrained identities. No model download occurs in a
   scientific job.
5. **Canonical GPU acceptance.** For each real model, run BF16/CUDA reference,
   native-hook, and NNsight parity; fixed interventions; cancellation; enforced
   resource limits; artifact transfer; tunnel recovery; provider stop; and
   replacement using the same immutable image, verified model hashes and retained
   controller-artifact hashes. Every numerical operation belongs in its exact
   approved job batch. Use separate short allowances when the full case set and
   startup/collection/deletion reserve cannot fit one allowance. Retain reports
   and actual provider readbacks.

## Conditions that prevent unattended operation

The initial REST v2 review did not find a purchase price ceiling or a verifiable
provider-side shutdown deadline. The published GraphQL interface exposes a
creation price ceiling and a scheduled-stop field, but the latter's behavior
still requires validation. A same-host watchdog survives a controller process
crash; it does not by itself survive the host losing power or internet access.

RunPod does not document a Pod-specific stop-only API key in its current key
guide. Restricting methods on a Python object is insufficient: a watchdog needs
a separate credential boundary, such as a narrow Unix broker whose process
owns the management credential. See [RunPod API keys](https://docs.runpod.io/get-started/api-keys).

Managed CUDA jobs require writable delegated cgroup v2 controls. A container
image cannot grant itself host delegation. If the actual Pod does not provide
those controls, managed acceptance fails. The explicitly selected supervised
profile uses the disposable Pod boundary instead and makes no nested cgroup or
hostile-code containment claim. Its numerical pass does not remove the managed
profile's requirements.

An explicitly approved, short supervised infrastructure test can characterize
these unknowns. It does not establish unattended readiness or satisfy the
canonical GPU acceptance milestone.
