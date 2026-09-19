# First live deployment

This is a deployment plan and acceptance checklist, not a claim that production
deployment has passed. The local implementation and the live provider must be
validated separately.

## Verified local deployment

On September 19, 2026, the first Ubuntu controller installation completed:

- The installed CPU acceptance service passed all 14 containment, attestation,
  resource-limit and normal-cleanup checks under the actual research service
  profile. No service security restriction was relaxed.
- Controller, research facade, independent watchdog and provider stop broker
  were enabled and running. The human administrative socket responded with no
  compute requests.
- The installed snapshot and backup identities uploaded the initial local
  snapshot to Drive, downloaded it, verified its hashes and restored it offline.
  The first transfer failed; a backup-only retry reused the pending archive and
  passed without changing code, credentials or permissions. Its specific initial
  transfer failure was not retained, so its cause remains undetermined.
- The daily backup timer was enabled. No paid compute was launched.

This initial snapshot contained empty controller/provider state, not scientific
results. Tests of denial under each installed identity, CPU cleanup after a
facade crash, and all live GPU/storage/provider acceptance conditions remain
open. The service installation does not establish unattended readiness.

A subsequent application update is prepared but has not yet been installed.
It adds a container-monitor deadline and automatic removal after a facade
crash. Its packaged wheel passed all 16 CPU acceptance conditions in a separate
human-owned rootless store, including killing both launchers before their host
timer could fire. The old launcher failed that crash test. The local regression
suite passed 758 tests; its ten real-container cases were configured separately.
The update also includes a 26-check administrator-run identity gate and an
infrastructure-only diagnostic that does not require buying a network volume.
Neither the installed update nor its actual-identity gate is claimed complete.

## Selected setup

| Decision | Initial choice | Reason |
| --- | --- | --- |
| Controller | The operator's Ubuntu machine; local SQLite and retained artifacts | Avoid another server and hosting bill for the first deployment. The machine must stay on during supervised GPU runs. |
| Transport | Authenticated HTTP bound to loopback through an SSH tunnel with a pinned host key | Fits a single controller and worker without exposing a research API publicly. |
| GPU | One Secure Cloud RTX 4090, subject to a fresh price and capacity check | 24 GB is sufficient for the two 1.7B models loaded separately and short, bounded calibration jobs. |
| Data center | US-IL-1, subject to revalidation before storage creation | The September 19, 2026 catalog showed both the GPU class and Standard network volumes. |
| Persistent GPU storage | One 100 GB Standard network volume | Keeps model assets and outputs across replacement Pods without overallocating storage. |
| Backup | A private Google Drive folder with a separate background uploader identity | A copy remains outside both RunPod and the controller machine. Each upload must be downloaded and verified. |
| Model inputs | Both canonical Qwen3-1.7B checkpoints, with exact revisions and file hashes | Preserve the distinction between Base and the released post-trained model. |
| Agent API spending | No autonomous paid agent API configured in this deployment stage | Scientific-agent orchestration is a later milestone; its budget must be approved separately. |

The observed GPU catalog rate was $0.74/hour. This is an observation, not a price
promise or an authorization to launch. The controller must refresh the quote and
enforce the approved ceiling at purchase. RunPod lists Standard network storage
below 1 TB at $0.07/GB/month, or $7/month for 100 GB (about $0.23/day). Retained Pod
disks, other volumes, backup charges, and controller charges also belong in the
idle cost calculation. Local electricity and an existing subscription are not
measured by the provider API. See [RunPod pricing](https://www.runpod.io/pricing).

## Required evidence, in order

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
   registry digest, download only the selected safe model assets, and verify
   their pinned hashes on the persistent volume. No model download occurs in a
   scientific job.
5. **Canonical GPU acceptance.** For each real model, run BF16/CUDA reference,
   native-hook, and NNsight parity; fixed interventions; cancellation; enforced
   resource limits; artifact transfer; tunnel recovery; provider stop; and
   replacement using the same volume. Every numerical operation belongs in the
   approved job batch. Retain reports and actual provider readbacks.

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

CUDA jobs currently require writable delegated cgroup v2 controls. A container
image cannot grant itself host delegation. If the actual Pod does not provide
the required controls, GPU acceptance fails and the deployment remains
incomplete. Changing that requirement needs an explicit engineering decision
and an equivalent tested enforcement mechanism.

An explicitly approved, short supervised infrastructure test can characterize
these unknowns. It does not establish unattended readiness or satisfy the
canonical GPU acceptance milestone.
