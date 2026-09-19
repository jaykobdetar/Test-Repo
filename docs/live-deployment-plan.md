# First live deployment

This is a deployment plan and acceptance checklist, not a claim that production
deployment has passed. The local implementation and the live provider must be
validated separately.

## Verified local deployment

On September 19, 2026, the Ubuntu controller installation completed local acceptance:

- The installed CPU service passed all 16 containment, attestation, resource-limit
  and cleanup checks. The crash test killed both launchers before their host
  timer could act, then observed independent process termination and removal.
- All 26 checks of the actual installed identities passed, including denied
  credential/admin access, allowed research status/discovery, and their audit
  records. The research socket directory's group is assigned in the same startup
  command that execs Python; a separate pre-start assignment was being reset by
  systemd before the service could accept research clients.
- Controller, research facade, independent watchdog and provider stop broker
  were enabled and running, with no upgrade guard overrides. The human
  administrative socket returned no compute requests.
- The installed backup identity uploaded a snapshot to Drive, downloaded it,
  verified its hashes and restored it offline. The daily backup timer is active.

The accepted application wheel is from `e6bfc71`, with the reviewed research
startup command correction. Later dispatcher tunnel recovery and deployment
renderer changes are committed but still need packaging for GPU service setup.
The verified pre-upgrade snapshots contain initial controller/provider state
rather than scientific results. The subsequent paid diagnostic is recorded below. These
local checks do not establish live CUDA, provider shutdown or unattended readiness.

The packaged application passed 758 local regression tests and all 16 CPU
acceptance conditions in a separate human-owned store before installation.
Subsequent dispatcher tests passed 19 cases, and service-template/recovery tests
passed 81 cases. These are separate, overlapping validation sets, not a summed
full-suite result for the latest commit.

## First GPU diagnostic

An initial supervised cloud diagnostic confirmed the expected hardware but
failed the worker resource-control prerequisite. The report was retained
privately, the test resources were removed, and the controller closed the run.
No persistent storage was purchased and no model inference ran.

Next, establish supported resource-control delegation for the worker identity
and repeat the bounded prerequisite test before buying persistent storage or
running model acceptance. This diagnostic does not establish shutdown during
controller host loss or unattended readiness.

## Selected setup

| Decision | Initial choice | Reason |
| --- | --- | --- |
| Controller | The operator's Ubuntu machine; local SQLite and retained artifacts | Avoid another server and hosting bill for the first deployment. The machine must stay on during supervised GPU runs. |
| Transport | Authenticated HTTP bound to loopback through an SSH tunnel with a pinned host key | Fits a single controller and worker without exposing a research API publicly. |
| GPU | One Secure Cloud RTX 4090, subject to a fresh price and capacity check | 24 GB is sufficient for the two 1.7B models loaded separately and short, bounded calibration jobs. |
| Data center | Revalidate before storage creation | Availability changes; no persistent location has been purchased. |
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
