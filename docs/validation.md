# Auto Interpretability Lab: Interpreting validation evidence

[Project overview](../README.md) · [Status](../STATUS.md) · [Implementation boundaries](../IMPLEMENTATION.md)

This guide explains what each kind of evidence can and cannot establish. It
does not record current results: those are in [STATUS.md](../STATUS.md), and
earlier evidence records are in [project history](history/README.md).

## Separate claims

Source presence, a passing test, an installed acceptance gate, a live GPU run
and a scientific result are separate claims. Each needs its own evidence.

| Evidence | What it can establish | What it cannot establish |
| --- | --- | --- |
| Ordinary CI (`uv run --locked python -m pytest -q`) | The code's local contracts on CPU with a simulator provider and a tiny random Qwen3 | Containment, live GPU behaviour, canonical checkpoint parity or any scientific claim. Skipped sandbox cases are not evidence. |
| Real sandbox gate (`PROBE_SANDBOX_REQUIRED=1`) | Rootless Podman containment on the host that ran it | Containment under a different host or service profile |
| Installed acceptance receipts | The exact installed bytes and service accounts behaved as tested | Behaviour of later source that is not installed |
| Supervised GPU run with retained manifest | Numerical parity for the exact model, image, prompts and suite recorded in its manifest | Managed-worker resource limits, cancellation, recovery, replacement, hostile-code containment or host-loss shutdown |
| Exploratory research report | What happened on the declared public prompts under the declared recipe | Held-out confirmation, replication or a validated finding |
| Trusted evaluator replication (future) | Promotion of a frozen hypothesis to VALIDATED or FALSIFIED | Anything outside the frozen recipe and held-out data |

A result's manifest must record exact model revision and weight hashes, dataset
hash, code commit and image digest. Failed runs are retained and reported, never
erased or replaced by a later success.

## Reproducing checks

Run the general suite on the branch you intend to test:

```sh
uv sync --locked --all-extras
uv run --locked python -m pytest -q
```

To require the real sandbox checks after building its pinned image, follow the
[sandbox guide](../deploy/sandbox/README.md). The GitHub workflow runs the general
suite and builds the package; it does not configure the real Podman gate.

## Where evidence lives

Public evidence files are in [`docs/evidence/`](evidence/). Private credentials,
provider responses and administrator receipts stay with the operator's private
deployment records and are only named, never copied, in this repository.
