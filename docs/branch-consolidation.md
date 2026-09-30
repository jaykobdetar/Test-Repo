# Branch consolidation — September 30, 2026

[Overview](../README.md) · [Status](../STATUS.md) · [Contributing](../CONTRIBUTING.md)

The local integration branch is `consolidate/project-2026-09-30`. It brings the
existing Auto Interpretability Lab work in `jaykobdetar/Test-Repo` together for
review. It does not resume the retired Probe-MCP services or authorize a new
experiment. No branches, existing worktrees, uncommitted files or PRs were
deleted or rewritten. Publication remains a separate decision.

## Remote branches and pull requests

The following refs were fetched from `https://github.com/jaykobdetar/Test-Repo.git`
on September 30. GitHub's default branch remained `main`.

| Branch | Fetched commit | Disposition |
| --- | --- | --- |
| `main` | `b936b9b984058a51500ae1ffcb67607311f77146` | Included as an ancestor of the integration base |
| `feat/live-deployment` | `db923119460ef95a8dee353d29f1def2d8447753` | Included; controller, supervised calibration and deployment foundations |
| `redirect/milestone-0` | `30f8472491489308093db0fed888ad87cd444b8f` | Included; status consolidation, contribution rules and formatting |
| `redirect/milestone-1` | `c8d5bd7aab154224524bdde5505b4f843804e317` | Integration base; budgets, recipes, metrics and prepared experiment assets |
| `feat/posttrained-worker-image` | `fb7c04a0bc3d03f141934185b50283e30f221f32` | Patch already included as `d3b9af4`; merge records ancestry while retaining the newer integration tree |

The first four form one ancestry chain. The integration base contains 70 commits
not reachable from `main`. The worker-image commits `fb7c04a` and `d3b9af4`
have the same stable Git patch ID,
`e661aaf98e58d486096290f9e71b93382eb314c9`. An explicit `ours` merge strategy is
appropriate only for this verified duplicate: its old tree must not replace
later worker, recipe or documentation changes. The merge resolution was checked
to retain the base tree exactly before the consolidation documentation and
source-manifest updates were staged. The duplicate branch is the second parent;
runtime code, deployment scripts and tests retain the milestone base contents.

The three existing PRs were left unchanged:

| PR | State at inspection | Branch relationship |
| --- | --- | --- |
| [#1: supervised calibration and deployment](https://github.com/jaykobdetar/Test-Repo/pull/1) | Open, draft | `feat/live-deployment` → `main` |
| [#2: Milestone 0](https://github.com/jaykobdetar/Test-Repo/pull/2) | Open | `redirect/milestone-0` → `feat/live-deployment` |
| [#3: Milestone 1](https://github.com/jaykobdetar/Test-Repo/pull/3) | Open | `redirect/milestone-1` → `redirect/milestone-0` |

## Existing local work

The September 22 checkout at
`2026-09-22/https-github-com-jaykobdetar-test-repo/work/Test-Repo` was clean and
detached at `5c8427f`. Its local `main` was `b936b9b`; it had no stashes.
The freshly fetched milestone tip adds the existing formatting commit
`028cdef` and status correction `c8d5bd7` to that checkout.

The September 19 repository shares this history, although its remote retains
the older `auto-interpretability-lab.git` URL. The following paths are relative
to `2026-09-19/read-this-architecture-document-do-not` under the local Codex
documents directory. All 17 registered worktrees were inspected without edits.

| Worktree | Local branch | Tip | State |
| --- | --- | --- | --- |
| `outputs/probe-core` | `main` | `7fb85ef` | Clean; included |
| `work/lab-branding-main` | `docs/auto-interpretability-lab-main` | `b936b9b` | Clean; included |
| `work/lab-branding-deployment` | `docs/auto-interpretability-lab-deployment` | `54ecaa2` | Clean; included |
| `work/probe-live-deployment` | `feat/live-deployment` | `db92311` | Clean; included |
| `work/probe-posttrained-image` | `feat/posttrained-worker-image` | `fb7c04a` | Clean; duplicate patch accounted for above |
| `work/probe-calibration-retarget` | `feat/calibration-region-retarget` | `82b950b` | Included; two untracked files preserved |
| `work/probe-gpu-acceptance-cases` | `feat/gpu-single-case-acceptance` | `82b950b` | Clean; included |
| `work/probe-direction-transfer` | `feat/gpu-direction-transfer` | `4baa7d1` | Clean; integrated counterpart below |
| `work/probe-replacement-acceptance` | `feat/gpu-replacement-acceptance` | `d634ab6` | Clean; integrated counterpart below |
| `work/probe-tunnel-recovery` | `feat/gpu-tunnel-recovery` | `12f88ed` | Clean; integrated counterpart below |
| `work/probe-suite-guards` | `feat/gpu-suite-guards` | `7e01602` | Included; two untracked files preserved and excluded |
| `work/probe-startup-idle` | `feat/verify-startup-idle` | `6eac846` | Clean; integrated counterpart below |
| `work/probe-startup-watchdog` | `fix/startup-aware-watchdog` | `bb56cb8` | Clean; integrated counterpart below |
| `work/probe-output-size-race` | `fix/output-size-rename-race` | `522b66e` | Clean; integrated counterpart below |
| `work/probe-reconcile-stop-cause` | `fix/reconcile-stop-cause` | `854af6e` | Clean; integrated counterpart below |
| `work/probe-retry-retarget` | `fix/retry-retarget-predecessor` | `8aa8144` | Clean; integrated counterpart below |
| `work/probe-rpc-close-race` | `fix/rpc-peer-close-race` | `f045c8c` | Clean; integrated counterpart below |

There were no stashes in the September 19 repository. Its ignored environments
and caches were left in place. No additional Codex worktree directory or
matching checkout was found in the inspected locations.

| Earlier local commit | Integrated counterpart | Evidence |
| --- | --- | --- |
| `4baa7d1` | `71403ec` | Patch equivalent |
| `d634ab6` | `7a0e4a0` | Replacement module and tests have identical Python ASTs in the milestone base |
| `12f88ed` | `77aa756` | Acceptance-actions module and tunnel tests have identical Python ASTs in the milestone base |
| `6eac846` | `8e6ffa4` | Patch equivalent |
| `bb56cb8` | `719a1a3` | Patch equivalent |
| `522b66e` | `cc05eab` | Patch equivalent |
| `854af6e` | `6f94e76` | Patch equivalent |
| `8aa8144` | `93a969f` | Patch equivalent |
| `f045c8c` | `4b19ac6` | Patch equivalent |

No additional cherry-picks were needed. These original local branches remain
intact in their original repository; their commit identities were not rewritten.

The untracked `deploy/retarget-gpu-calibration.py` and
`tests/test_gpu_calibration_retarget.py` in `probe-calibration-retarget` already
match the integrated behavior (identical ASTs; byte-identical to `db92311`).
The untracked `deploy/verify-gpu-suite-transition.py` and
`tests/test_gpu_suite_transition.py` in `probe-suite-guards` add a new managed
acceptance-chain guard. They remain outside this integration because
[the managed-worker lifecycle is deferred](../STATUS.md#deferred). All four
original files remain untouched and recoverable in their original worktrees.

## Validation and publication

The consolidation also corrects `MANIFEST.in` to include the tracked
`deploy/gpu/Dockerfile.*` files, their `bootstrap-requirements.txt` input,
`.dockerignore`, and `docs/evidence/*.json` in source distributions. The initial
archive inspection found those ten files missing despite their presence in Git.
Wheel runtime code and packaged recipe resources are unchanged.

Validation uses Python 3.13 and the repository's locked dependency versions in a
fresh local environment. Ordinary tests use tiny local models and mocked cloud
providers. No deployed services, GPU resources, image publication, credentials
or live acceptance runs are part of this consolidation.

| Check | Result |
| --- | --- |
| Locked dependency setup, all extras, offline | Passed; CPython 3.13.5, 120 resolved packages |
| Full ordinary test suite | **2,327 passed, 10 skipped**, 153.16 seconds; see [test evidence](evidence/2026-09-30-consolidation-tests.json) |
| Repository formatting gate (`ruff format --check .`) | Passed; 129 files already formatted; pinned deployment scripts remain excluded by project configuration |
| Source distribution and wheel (`uv build --offline`) | Passed; archive contents checked for runtime modules, recipe resources, deployment inputs and evidence |
| Relative Markdown file links and `git diff --check` | Passed |
| Broader advisory `ruff check .` | 687 existing findings, unchanged by this consolidation; this is not the repository's CI lint gate |

The ten skips are the explicitly configured real rootless-Podman checks; no live
GPU or installed-service acceptance was run. A first attempt in the restricted
execution environment failed because sockets were blocked, the long temporary
path exceeded Unix-socket limits, its parent permissions were unsuitable for
trusted-input fixtures, and an empty CUDA selector conflicted with a mocked
launcher test. The successful run used a short private `/tmp` directory, local
socket support, a clean credential-free environment and offline model settings.
Its numerical fixtures explicitly select CPU. No tests were removed or weakened.

The proposed publication is a normal push of `consolidate/project-2026-09-30`
followed, if requested, by a consolidated PR against `main`. Neither action has
been performed. Existing stacked PRs and the default branch stay unchanged until
the owner chooses how to retire the old review stack.

The integration branch's push would run test CI. A later merge to `main` can
trigger the existing image-publication workflows because the consolidated
changes include their watched files; that merge requires a separate decision.
