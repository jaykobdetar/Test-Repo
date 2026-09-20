# Auto Interpretability Lab: Live deployment checklist

[Project overview](../README.md) · [Validation status](validation.md) · [GPU procedure](gpu-deployment.md)

This is the deployment branch's status and remaining acceptance checklist,
updated September 20, 2026. The branch is based on published source `7e01602`;
the original main-branch foundation is smaller. A checked-in implementation or
prepared plan does not count as a passed deployment gate.

## Current status and next gate

The installed controller is source `8a5486b`, wheel
`c2fd62bfec0fbbb90dc556e34dac88d6969a59b5dfefb0846f31d0b691a6ee38`.
Its 34 application files matched the package. The actual service environment
passed all 16 CPU containment/cleanup checks, all 26 identity checks, and Drive
upload/readback/restore. The backup timer is active. A subsequent region-only
setup reused this application and repeated identity and backup acceptance.

Seven full-worker attempts ended before model inference. There are 18 prepared
case plans and **zero successful live canonical GPU cases**. The latest Pod is
confirmed deleted, its allowance is closed, and no project GPU is running.
The next goal is one successful Base parity job, retained artifacts and confirmed
deletion. Broader orchestration is deferred until that path works reliably.

The seventh attempt in `EU-RO-1` ended at worker startup with `REQUEST_CHANGED`.
The controller entered `STOP_REQUESTED`, then `UNCERTAIN` with `ProviderUncertain`;
the watchdog subsequently recorded `uncertain_action`. The initial reconciliation
error was not retained. A same-time log lookup returned HTTP503, while a saved
prior Pod observation passed exact RUNNING validation. Injected HTTP503 status
and deletion failures reproduce this sequence locally. That proves a failure
mechanism, not the historical first trigger. No transient-status retry policy or
cause-audit correction is represented as installed.

The operator's retained evidence is named in [validation status](validation.md).
Do not replay a consumed allowance or erase these failed requests when preparing
a future run. Diagnosis and a matching regression must precede another paid retry.

## Verified milestones and historical failures

- Initial controller source `e6bfc71` passed installed CPU, identity and backup
  acceptance after the reviewed research startup-command repair. Later releases
  retained their own package and acceptance receipts.
- Bounded cloud diagnostics verified hardware and resource enforcement on a
  compatible delegated-cgroup-v2 host. Other assigned hosts were unsuitable;
  every new Pod must pass the same gates. These tests ran no scientific model.
- Early full-worker failures exposed null-runtime handling, insufficient cold
  startup time and inherited provider credentials. The fixed image and controller
  changes require their own live result; the earlier failures are not parity evidence.
- The fourth attempt reached verified SSH discovery but failed configuration.
  The fifth stopped on a provider endpoint refusal whose HTTP status was lost.
  Controller source `82b950b` added bounded HTTP diagnostics and passed its own
  16/26 installed checks and verified backup.
- The sixth attempt hit the watchdog's old five-minute idle rule during image
  extraction. The accepted `8a5486b` update shares the runner's fixed startup
  cutoff: 900 seconds total minus 240 for execution and 120 for collection/deletion
  leaves at most 540 seconds for startup. Its installer verified the historical
  durable idle-stop reason. This does not extend the original approval.
- Czech capacity was unavailable for the next proposal. The reviewed region-only
  retarget changed the region and fresh proposal paths/IDs, preserved the worker
  image and scientific scope, and did not upgrade application code. The resulting
  seventh attempt is the unresolved startup failure described above.

The existing Base worker image
(`sha256:a795fe2eb429d5453c0687d116707cc8a1b0eeca2cea2c77f37a6dbe5248b437`)
is unchanged; the new acceptance orchestration runs on the controller and does
not require rebuilding that image.
The separately pinned posttrained worker image has been published at
`sha256:618792509a6aad88709d3ce6e1636ffc69a6a0db637202653117ee4bcc8d2152`,
from worker source `fb7c04a0bc3d03f141934185b50283e30f221f32`. Its registry digest
and source provenance were verified, but it has not passed live GPU acceptance.

## Selected setup

| Decision | Initial choice | Reason |
| --- | --- | --- |
| Controller | The operator's Ubuntu machine; local SQLite and retained artifacts | Avoid another server and hosting bill for the first deployment. The machine must stay on during supervised GPU runs. |
| Transport | Authenticated HTTP bound to loopback through an SSH tunnel with a pinned host key | Fits a single controller and worker without exposing a research API publicly. |
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
it, and keep shutdown/storage uncertainty within that total. Do not request a
new human approval for a test already covered by that authorization.

The initial 100 GB network-volume proposal is superseded by disposable research
Pods. The temporary empty 20 GB test volume was deleted. No model download occurs
inside a scientific job. Replacement uses the same immutable worker image and
rechecks model hashes and previously retained controller artifacts before a fresh
bounded parity run. Pod loss makes any uncollected output failed or inconclusive;
it cannot silently replay the old job or approval. Backups remain independent
of both the Pod and controller through verified Google Drive copies.

## Required evidence, in order

| Step | Current standing |
| --- | --- |
| 1. Infrastructure diagnostic | Passed on specific compatible hosts; each new assignment must be checked |
| 2. Separate services | Installed identity and CPU containment gates passed |
| 3. Recovery and backup | Installed Drive upload/readback/restore passed |
| 4. Pinned worker and assets | Base and posttrained images prepared and published; in-worker readback remains part of each acceptance run |
| 5. Canonical GPU acceptance | Not passed; first Base parity is the immediate target |

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

CUDA jobs currently require writable delegated cgroup v2 controls. A container
image cannot grant itself host delegation. If the actual Pod does not provide
the required controls, GPU acceptance fails and the deployment remains
incomplete. Changing that requirement needs an explicit engineering decision
and an equivalent tested enforcement mechanism.

An explicitly approved, short supervised infrastructure test can characterize
these unknowns. It does not establish unattended readiness or satisfy the
canonical GPU acceptance milestone.
