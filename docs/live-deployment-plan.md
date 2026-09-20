# Auto Interpretability Lab: Live deployment checklist

[Project overview](../README.md) · [Validation status](validation.md) · [GPU procedure](gpu-deployment.md)

This is the deployment branch's status and remaining acceptance checklist,
updated September 20, 2026. The branch is based on published source `7e01602`;
the original main-branch foundation is smaller. A checked-in implementation or
prepared plan does not count as a passed deployment gate.

## Current status and next gate

**Base and posttrained supervised public calibration have both passed.** Each
run on an RTX 4090 in EU-RO-1 passed all 29 numerical checks, including 25 exact
comparisons, and retained nine tensors. Model/image provenance, copied artifact
hashes and process exit were verified; the provider independently confirmed both
Pod deletions. See [calibration results](calibration-results.md) for the separate
receipts, model identities and evidence scope.

The original host report falsely rejected the operation because its verifier
omitted `suite_version: 1`. Verifier source `d41cb4c` accepted the same collected
artifacts after that correction. The original report remains preserved; neither
the numerical checks nor their tolerances changed, and the model was not rerun.
The posttrained run passed directly with the corrected verifier and explicit
`thinking_mode: false`.

The operator selected [supervised public calibration](supervised-public-calibration.md)
after repeated managed-worker startup failures. This path supersedes the original
five-step sequence as the immediate work plan. It defers nested cgroups and
repeated controller upgrades while retaining pinned assets, the budget, deadlines,
verified collection and Pod deletion. These results establish numerical
calibration in this smaller profile. The original managed-worker lifecycle
checklist below remains incomplete and deferred.

The installed controller is source `4b19ac6`, wheel
`a43e336773fe21155a890aa4a545a26ab70d71a6a7d01bf5e0b0619ac86434c3`.
Its 34 application files matched the package. After an unchanged CPU acceptance
rerun, all 16 containment/cleanup checks and all 26 identity checks passed.
Research and the verified Drive backup schedule are restored. The original
intermittent CPU timeout classification failure remains retained, not claimed fixed.

The eighth full-worker attempt reached verified configuration, SSH tunneling,
worker readiness and approved dispatch. CUDA initialization then failed with
error 304; result collection marked the case failed and deletion was independently
confirmed. The ninth managed attempt failed before SSH in cgroup bootstrap and
was deleted. Eighteen managed acceptance plans remain prepared; none has passed
through the full installed managed-worker path. The later standalone numerical
passes are separate evidence.

A local A/B reproduction identified a project-controlled failure: the worker's
seccomp filter denied CUDA's `socket(AF_UNIX, SOCK_SEQPACKET|SOCK_CLOEXEC, 0)`.
The unchanged filter returns CUDA304; permitting only AF_UNIX creation succeeds.
The narrow correction retains all `connect`, `sendto` and `sendmsg` denials and
passed a local BF16 matrix operation. Acceptance through the managed worker on
RunPod remains required to resolve that profile's failure; the standalone
numerical pass does not test its seccomp boundary.

The installed controller already retains initiating reconciliation causes and
allows one deadline-bounded retry for eligible transient status reads. Its eighth
run progressed through dispatch. The seventh attempt's discarded historical
trigger is still unknown; a prior local HTTP503 replay was mechanism evidence only.

The operator's retained evidence is named in [validation status](validation.md).
Do not replay a consumed allowance or erase these failed requests when preparing
a future run. Diagnosis and a matching regression must precede another paid retry.

## Verified milestones and historical failures

- Initial controller source `e6bfc71` passed installed CPU, identity and backup
  acceptance after the reviewed research startup-command repair. Later releases
  retained their own package and acceptance receipts.
- Bounded cloud diagnostics verified hardware and resource enforcement on a
  compatible delegated-cgroup-v2 host. Other assigned hosts were unsuitable;
  every new managed-worker Pod must pass the same gates. These tests ran no
  scientific model; the smaller supervised profile does not claim these gates.
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
  seventh attempt stopped during startup; its initiating error was not retained.

The corrected managed Base worker image is published at
`sha256:97a87f0ccf7baae5a77dd6dabaf1f7de1d476396449b40da4e96c8bef7140e41`,
from source `f031a674c688acad1e2a7fe84caed5b1580d2357`. Its public pull, immutable
registry identity and source provenance were verified. Its supervised derivative,
`sha256:b1abbb63a890fce5fbfa35dd42c3b166858ceed323e451579a4ff5b202fce853`,
passed the Base numerical calibration described above. This does not accept the
parent's managed lifecycle. The eighth attempt used the older `a795fe2e…` image.
The earlier separately pinned posttrained worker image was published at
`sha256:618792509a6aad88709d3ce6e1636ffc69a6a0db637202653117ee4bcc8d2152`,
from worker source `fb7c04a0bc3d03f141934185b50283e30f221f32`. Its registry digest
and source provenance were verified. Its supervised derivative,
`sha256:226ee49f81c5f07213d5e08ed1b66b357cb90bebec69ba35caccf17724ca5386`,
has now passed posttrained numerical calibration. Managed lifecycle acceptance
remains pending for both models.

## Immediate supervised sequence

Base and posttrained numerical calibration and their independent Drive backups
are complete. Both archives passed download, hash verification and local restore.
The next milestone is
the [first fixed public experiment](first-public-experiment.md):

1. Retain independent backups of both standalone result bundles with hashes and
   a verified readback before beginning the experiment.
   They live outside the installed ledger and are not automatically included in
   its normal snapshot.
2. Prepare one fixed public exploratory experiment: declare its question,
   prompts, intervention, primary metric and matched control before running it.
   Add a separate reviewed recipe and result verifier, because the existing
   standalone command only accepts `backend_parity_v1`.
3. Run the bounded experiment under the same supervised deadline and deletion
   procedure, retain per-prompt results, and produce a reproducible exploratory
   report. Keep the Ubuntu host online for the run.

This sequence does not require a new controller installation, the complete
managed lifecycle suite, a private evaluator or autonomous agent orchestration.
Those remain separate requirements for their respective service and scientific
claims. Public exploratory results are not held-out confirmation or validated
discoveries.

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

This is the original five-step **managed-service** acceptance checklist. It is
retained for that larger deployment goal; it does not block the supervised
sequence above or redefine the two numerical passes.

| Step | Current standing |
| --- | --- |
| 1. Infrastructure diagnostic | Passed on specific compatible hosts; each new managed assignment must be checked |
| 2. Separate services | Installed identity and CPU containment gates passed |
| 3. Recovery and backup | Installed Drive upload/readback/restore passed |
| 4. Pinned worker and assets | Both supervised runs verified their pinned model/image identities; each future managed run must verify its own exact deployment. |
| 5. Canonical GPU acceptance | Both standalone numerical calibrations passed. Full managed acceptance remains incomplete; resource and lifecycle evidence is still required for that profile. |

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
