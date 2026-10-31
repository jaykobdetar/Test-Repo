# Contributing

[Status](STATUS.md) · [Redirect plan](REDIRECT-PLAN.md)

## Documentation rule

[STATUS.md](STATUS.md) is the single source of truth for what works today.

- Any commit that changes behaviour under `probe_core/` or `deploy/` updates
  `STATUS.md` and every affected guide **in the same commit**.
- Guides describe how things work and link to `STATUS.md`. They do not restate
  current standing, test totals or "next step" plans.
- Superseded text moves unchanged to [`docs/history/`](docs/history/README.md)
  and is linked from `STATUS.md` or the history index. It is never left in place
  beside newer text. Only relative link paths and a provenance note may change.
- Every claim in `STATUS.md` links to retained evidence or to the tests that
  check it. Exploratory results are labelled exploratory. Only the trusted
  evaluator can promote a hypothesis to validated.

CI enforces the first rule: a pull request that changes `probe_core/**` or
`deploy/**` without changing `STATUS.md` or `docs/**` fails the documentation
check. A maintainer can apply the `docs-not-needed` label when a change has no
documentation effect, such as a pure refactor or a test-only fix under those
paths.

## Before every commit

```sh
uv run --locked python -m pytest -q
git diff --cached --name-only --diff-filter=ACMR -- '*.py' | xargs -r uv run --locked ruff format --check
```

Tests must pass. Format new and touched Python files with
`uv run --locked ruff format <files>`; CI checks formatting only on the Python
files a pull request changes. Keep formatting-only changes in their own commit,
separate from behaviour changes. `deploy/` is excluded from formatting because
its scripts are staged into images and pinned by SHA-256.

## Describing changes

State the behaviour being changed and give evidence for claims about
correctness, containment and scientific results. Changes to the execution model
or research protocol describe their acceptance criteria. Never weaken an
invariant in [REDIRECT-PLAN.md](REDIRECT-PLAN.md#invariants-never-weaken-these).
