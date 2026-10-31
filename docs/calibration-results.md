# Supervised GPU calibration results

On September 20, 2026, Qwen3-1.7B-Base and Qwen3-1.7B each passed the fixed
`backend_parity_v1` calibration on a separate RunPod Secure Cloud RTX 4090 in
EU-RO-1. These are numerical engineering checks on two synthetic public prompts,
not scientific findings or full managed-service acceptance.

| Result | Base | Posttrained |
| --- | --- | --- |
| Numerical checks | 29/29 passed | 29/29 passed |
| Exact comparisons | 25/25, zero maximum error | 25/25, zero maximum error |
| Retained tensors | 9 | 9 |
| Numerical process exit | 0 | 0 |
| Copied outputs verified | Yes | Yes |
| Pod deletion independently confirmed | Yes | Yes |
| Thinking mode | Not applicable | Disabled, pinned chat template |

The checks compare native Hugging Face execution, raw hooks and NNsight; they
cover activation capture, identity patching, zero ablation, steering, hook
removal and greedy generation. The four other checks require ablation to change
logits. Numerical tolerances were not relaxed. Two short prompts do not establish
correctness across all models, inputs or interventions.

## Retained evidence

- Base: [check results](evidence/2026-09-20-base-summary.json) and
  [numerical manifest](evidence/2026-09-20-base-manifest.json).
- Posttrained: [check results](evidence/2026-09-20-posttrained-summary.json) and
  [numerical manifest](evidence/2026-09-20-posttrained-manifest.json).

The manifests record exact model revisions, weight and prompt hashes, immutable
image digests, numerical source commits, package versions, outputs and their
hashes. The complete local bundles retain tensors, process reports, host
verification and provider deletion receipts. The worker manifest's
`process_exit_and_pod_deletion_verified: false` is intentional: the worker cannot
observe its own later exit or provider deletion. Those were verified separately
by the host and an independent provider read.

The Base's first host report was a false rejection: the verifier expected an
operation without its `suite_version: 1` field. Source `d41cb4c` corrected that
contract and verified the original saved files, with no GPU reexecution or
numerical changes. The original failed host report and the bound correction are
both retained. Posttrained passed directly with the corrected verifier.

## Independent backup

Both complete public artifact bundles were uploaded to the operator's private
Google Drive, downloaded again, hash-checked and restored locally. The Base
archive contains ten files including its preserved false-rejection report and
correction; the posttrained archive contains nine. Credentials and provider
state databases were excluded. See the
[backup verification receipt](evidence/2026-09-20-backup-verification.json).

## What changed to make this work

The selected [supervised profile](supervised-public-calibration.md) executes one
fixed command over authenticated SSH on a disposable Pod. It avoids nested
cgroup delegation, a long-running HTTP worker service and another installed
controller upgrade for this milestone. The derived images include the C compiler
required by the pinned PyTorch/Triton stack. Models and inputs remain pinned;
code runs as UID10001 with a clean environment and offline model loading.

The command has a 240-second timeout, with a separate root launcher killing its
process group after at most 250 seconds. A separate host service retains the
original 15-minute Pod deletion deadline. The host must remain online and the
run supervised. The provider-management key stays on the host.

This result does not establish hostile-code containment, nested cgroup resource
limits, network denial, installed-ledger execution, cancellation/recovery,
replacement, private held-out evaluation or unattended host-loss shutdown.
Those remain separate milestones. See [STATUS.md](../STATUS.md) for the
current milestone.

## Software validation

Runtime source `d41cb4c` passed both GitHub verification workflows. The PR workflow
reported 2,157 tests passed and 10 environment-specific skips, plus source and
wheel builds. Skips and local tests are not additional live containment evidence.
[Verification run](https://github.com/jaykobdetar/auto-interpretability-lab/actions/runs/35544276039).
