# Redirect plan: from lab infrastructure to automated interpretability

Place this file at the repository root. It supersedes the ordering in
`docs/live-deployment-plan.md` and the "next step" guidance in `README.md`.
It does not supersede any safety property listed under "Invariants".

## Instructions to the coding agent

Read this entire document before changing code. Work one milestone at a time,
in order. Each milestone lists its purpose, the concrete changes, acceptance
criteria, and required documentation updates. A milestone is complete only
when every acceptance item is checked and the documentation matches the code.

Stop and ask the human operator at every point marked **CHECKPOINT**. Do not
spend money, change the threat model, configure an LLM API key or accept a
model license without an explicit human answer in the conversation.

Do not claim scientific findings. Exploratory results are labelled
exploratory. Only the trusted evaluator (Milestone 6) can promote a hypothesis.

## Why the direction changes

The branch is ~95% infrastructure and has produced no interpretability
results. The supervised GPU path works (two exact-parity passes on RunPod).
The managed-worker path has failed nine times. The project name promises
automated interpretability: an agent that forms hypotheses about model
internals, tests them, and reports scored, reproducible results.

New ordering principle: **science pulls infrastructure.** Each milestone must
end with a result an interpretability researcher would recognise. Build only
the infrastructure that result requires.

The shortest credible path to "the name is true" is automated explanation of
sparse-autoencoder features, because it has established scoring methods
(detection and fuzzing scoring, as used by EleutherAI's auto-interp work) and
published baselines to compare against. Behaviour localisation and circuit
work follow.

## Invariants (never weaken these)

1. The provider-management API key never leaves the controller host and is
   never visible to the research agent, the MCP process, or any Pod.
2. Paid GPU compute and paid LLM API usage require a human-approved budget
   envelope (see Milestone 1). Nothing auto-renews an envelope.
3. Held-out evaluation data is never placed on an exploratory Pod, in the
   research agent's readable storage, or in any MCP tool response.
4. The audit trail remains append-only. Failed runs are retained, never erased.
5. Every result manifest records exact model revision and weight hashes,
   dataset hash, code commit, image digest, and (for agent-written code) the
   code's SHA-256.
6. Model output, agent-written code output and research text are untrusted
   data, never instructions and never execution receipts.
7. Tests stay green. `uv run --locked python -m pytest -q` passes before every
   commit.

## Frozen work (keep, do not extend)

Keep this code and its tests passing, but do not add features, acceptance
plans or paid runs for it until a milestone below explicitly requires it:

- Managed-worker lifecycle: nested cgroup delegation, worker replacement,
  tunnel recovery, the 18 prepared plans in `probe_core/gpu_acceptance*.py`.
- Controller upgrade/resume tooling under `deploy/` beyond bug fixes.
- Unattended operation and host-loss shutdown.

Move their status text into `docs/history/` (Milestone 0) and mark them
"Deferred: not required by current roadmap."

---

## Milestone 0 — Redirect, consolidate status, make docs trustworthy

Purpose: one accurate place to learn project status, and a rule that keeps it
accurate.

Changes:

- Create `STATUS.md` as the single source of truth for current capability.
  Sections: *Works and evidenced*, *Implemented but unevidenced*, *Deferred*,
  *Current milestone*. Each claim links to its evidence file or test.
- Rewrite the status table in `README.md` as a three-line summary that links
  to `STATUS.md`. Remove duplicated status prose from `IMPLEMENTATION.md`,
  `docs/validation.md` and `docs/live-deployment-plan.md`.
- Move dated historical narrative (attempt-by-attempt failure logs, superseded
  plans) into `docs/history/` unchanged. Link it from `STATUS.md`. Resolve the
  existing contradiction where `docs/validation.md` reports "zero successful
  live canonical cases" while `docs/calibration-results.md` reports two passes:
  state explicitly that the supervised profile passed and the managed profile
  has not.
- Add this file to the repo as `REDIRECT-PLAN.md` and link it from `README.md`.
- Add `CONTRIBUTING.md` with the documentation rule: any commit that changes
  behaviour under `probe_core/` or `deploy/` updates `STATUS.md` and every
  affected guide *in the same commit*. Superseded text is moved to
  `docs/history/`, never left in place beside newer text.
- Add a CI step (`.github/workflows/test.yml`) that fails a PR which changes
  `probe_core/**` or `deploy/**` without changing `STATUS.md` or `docs/**`,
  overridable with a `docs-not-needed` PR label.
- Add `ruff format` for new and touched files only. Do one separate,
  behaviour-free formatting commit for `probe_core/` and `tests/`, verified by
  an unchanged test count.

Acceptance:

- [ ] `STATUS.md` exists; every claim in it links to evidence or a test.
- [ ] No two documents give different answers to "what works today".
- [ ] CI documentation check runs and passes on this PR.
- [ ] Formatting commit changes no test outcomes.

---

## Milestone 1 — Recipes, metrics and budget envelopes; run the first experiment

Purpose: run a real multi-step experiment on the working supervised path and
produce the first exploratory report.

### 1a. Budget envelopes (replace per-start approval for research work)

The current model approves one bounded compute interval at a time, which
cannot support an agent that needs many short jobs.

- Add `BudgetEnvelope` to `probe_core/schemas.py`: `envelope_id`,
  `max_gpu_usd`, `max_llm_usd` (0 until Milestone 3), `expires_at`,
  `allowed_models` (registry IDs), `allowed_stages` (exploratory only for now),
  `max_wall_seconds_per_pod`, `approved_by`.
- The controller consumes an envelope instead of a single grant. It reserves
  estimated cost before each Pod creation and refuses when the remaining
  envelope cannot cover the worst case (runtime ceiling × live price + deletion
  reserve). Existing price-ceiling, deadline and deletion-confirmation logic is
  reused, not rewritten.
- Spend is recorded in the ledger and shown by the `lab_status` MCP tool.

**CHECKPOINT:** show the human the envelope schema and the first proposed
envelope (suggested: $3 GPU, 24 hours, exploratory, both Qwen3-1.7B models).

### 1b. Recipes: multi-step experiments in one Pod session

`JobSpec.operation` is singular, so every step costs a dispatch round-trip.

- Add `Recipe` (versioned, frozen JSON): ordered `steps`, each an existing
  `Operation` plus named outputs; later steps may reference earlier outputs by
  name instead of by pre-existing `TensorArtifact`. Include `prompts`,
  `token_ids` for any metric, `controls`, `primary_metric`, `seed`.
- Add `recipe` as a new `Operation` kind whose worker handler runs the steps
  inside one loaded model, writing every intermediate to the artifact
  directory with hashes.
- Generalise the supervised command (`deploy/gpu/public-job.py`,
  `deploy/gpu/public-calibration.py`) from the hard-coded
  `backend_parity_v1` check to a registry of approved recipe versions.
  `backend_parity_v1` becomes the first registered recipe. Unknown recipes are
  rejected.
- Every recipe run automatically prepends a no-op-hook check that must match
  unmodified logits exactly before any intervention result is reported.

### 1c. Metrics

Operations currently return last-token logits. Experiments need scalar
measurements.

- Add `Metric` variants: `logit_diff(target_ids, alternative_ids)`,
  `log_prob(target_ids)`, `kl_to_baseline`, `top_k_tokens(k)`.
- Metrics are computed on the worker per prompt; results are small JSON plus
  the full logits tensor only when requested.

### 1d. Run the first public experiment

Implement `docs/first-public-experiment.md` exactly as written (layer-14 MLP
zero-ablation, subject–verb agreement, logit-difference metric, matched
controls, every prompt reported). Add a control condition: ablating a random
layer-14 MLP direction of matched norm (uses the existing `Controls` fields).

**CHECKPOINT:** show the frozen recipe JSON and cost estimate before the run.

Acceptance:

- [ ] Envelope accounting tested on CPU with the simulator provider, including
      refusal when remaining budget is insufficient.
- [ ] Recipe runner passes parity tests on the tiny Qwen fixture, including a
      recipe whose step 2 consumes step 1's output.
- [ ] One exploratory report per model in `docs/results/`, with per-prompt
      results, controls, hashes, verified backup and confirmed Pod deletion.
- [ ] `STATUS.md` lists the report under *Works and evidenced*, labelled
      exploratory.

---

## Milestone 2 — Model registry and SAE feature pipeline

Purpose: get sparse-autoencoder features and their top-activating examples,
which are the raw material for automated interpretability.

### 2a. Model registry (remove hard-coded model shape)

- Replace `ModelIdentity.repo: Literal[...]` with a registry ID validated
  against `probe_core/resources/canonical-models.json` (extend that file with
  architecture metadata: `num_layers`, `num_heads`, `head_dim`, `hidden_size`,
  module path templates).
- Replace hard-coded bounds in `ModuleRef` (`layer le=27`, `head le=15`) and
  `Capture.modules max_length=28` with registry-driven validation.
- Replace the hard-coded `model.model.layers.{i}` paths in
  `WorkerEngine._target()` with registry module templates. Evaluate `nnterp`
  (same ecosystem as the existing nnsight dependency) for this mapping; adopt
  it only if it passes the existing exact-parity suite.
- Existing Qwen3 entries and tests must behave identically.

### 2b. SAE assets

- Determine which public, pinned SAE suites exist for models the lab can run
  on one 24–48 GB GPU. Check first for the current Qwen3-1.7B checkpoints
  (the README already names Qwen-Scope). If none is suitable, add the smallest
  model with an established SAE suite through the registry.

  **CHECKPOINT:** report candidates, licences (some checkpoints are gated and
  require licence acceptance), sizes and hashes before choosing.
- Add SAE weights to the registry with pinned hashes and bake them into a new
  immutable image with `deploy/gpu/bake-assets.py`, exactly as model weights
  are handled today.

### 2c. SAE operations

- `sae_encode`: capture at the SAE's hook point, return top-k feature indices
  and activations per token.
- `sae_ablate_feature` and `sae_steer_feature`: edit the reconstruction and
  add back the SAE error term so an identity edit reproduces unmodified logits
  exactly (this becomes a parity check).
- `feature_dashboard`: stream a pinned public text corpus through the model and
  retain, per feature, the top-N activating contexts and a random sample of
  activating and non-activating contexts. Output is a sharded artifact.

Acceptance:

- [ ] All previous tests pass with the registry in place.
- [ ] SAE identity-edit parity passes exactly on a tiny SAE fixture.
- [ ] One `feature_dashboard` run over at least 1,000 features, backed up and
      hash-verified, with cost recorded against an envelope.
- [ ] `STATUS.md` and a new `docs/sae-pipeline.md` updated.

---

## Milestone 3 — The first automated interpretability loop

Purpose: the lab explains features automatically and scores its explanations.
This is the milestone at which the project name becomes accurate.

### 3a. Held-out split and trusted scorer

- The trusted research service splits each feature's dashboard into an
  **explanation set** (visible to the agent) and a **scoring set** (never
  visible to the agent). The split is seeded and recorded. The scoring set
  lives only in trusted storage (Invariant 3).
- Implement detection scoring and fuzzing scoring as trusted code: a separate
  LLM call, which never sees the explanation set, judges held-out contexts
  given only the explanation. Report balanced accuracy per feature.
- Record the scorer's LLM model ID and prompt hash in each manifest.

### 3b. Orchestrator

- Add `probe_core/orchestrator.py`: runs an LLM agent against the existing
  MCP tools. Model ID is configuration, recorded in every manifest.
- New MCP tools: `list_features`, `read_feature_examples` (explanation set
  only), `submit_explanation`, `read_feature_score` (score only, never
  scoring-set text).
- Loop per feature: explain from examples → optionally request targeted
  exploratory runs (steer or ablate the feature via recipes) → submit
  explanation → receive score → refine up to a fixed iteration cap.
- Baseline: one-shot explanation with no refinement, same explainer model.
  Report both.

**CHECKPOINT:** before configuring any LLM API key, present an estimated LLM
cost for the planned run and request an envelope with `max_llm_usd` set. The
existing deployment plan requires agent API spending to be approved separately.

Acceptance:

- [ ] Scoring-set isolation has tests proving no MCP tool can return scoring-set
      contexts, including via `read_manifest` and artifact summaries.
- [ ] One run over at least 200 features with per-feature scores for both the
      one-shot baseline and the refined agent.
- [ ] Report in `docs/results/` compares against published auto-interp scoring
      numbers where the method matches, and states where it does not.
- [ ] `STATUS.md`: first automated interpretability result, labelled exploratory.

---

## Milestone 4 — Broader fixed operations for behaviour localisation

Purpose: let the agent localise behaviours, not only describe features.

Add each as an `Operation` with a raw-hook reference implementation and an
exact parity test on the tiny fixture, following the existing pattern in
`WorkerEngine.forward`:

- `attention_pattern` (selected layers/heads/positions).
- `logit_lens` (residual stream projected through final norm and unembedding).
- `direct_logit_attribution` per component, for a given metric.
- `resample_ablation` (replace activations with those from another prompt).
- `patching_sweep`: activation patching across a layer × position × component
  grid from a corrupted run into a clean run, returning a metric matrix in
  one job. This is the workhorse of circuit discovery and must be one recipe
  step, not hundreds of jobs.

Acceptance:

- [ ] Parity tests for every new operation.
- [ ] One exploratory report where the agent, given a contrastive prompt set,
      proposes components responsible for a behaviour, with a patching sweep
      as evidence.
- [ ] Docs updated.

---

## Milestone 5 — GPU analysis sandbox for agent-written code (exploratory only)

Purpose: remove the ceiling where the agent can only compose operations a human
pre-approved. Every published interpretability agent works by writing nnsight
code; this lab currently cannot.

Threat-model argument to present to the human: an exploratory Pod holds only
public model weights, public data and no credentials, and is deleted after
use. The existing supervised profile already treats the disposable Pod as the
boundary. What must be protected is the provider key (never on the Pod),
held-out data (never on exploratory Pods), and the host (outputs treated as
untrusted data and hash-verified before import).

**CHECKPOINT:** write `docs/threat-model-v2.md` with this argument and get
explicit human approval before implementing. This relaxes the current
"never Python on the GPU" rule for the exploratory stage only.

Changes:

- New operation `analysis_program`: agent-supplied Python source plus declared
  input artifacts, run by an unprivileged UID on the Pod with the model loaded
  read-only, a hard deadline, an output size limit, and the existing worker
  seccomp profile (including the AF_UNIX-for-CUDA correction) so outbound
  internet sockets are denied.
- The program's SHA-256 is recorded in the manifest. Outputs are imported as
  untrusted artifacts through `import_run_artifact`.
- `analysis_program` is rejected for `confirmatory` and `replication` stages
  by schema validation. **Any finding from free-form code must be re-expressed
  as a registered recipe before it can be tested confirmatorily.** This keeps
  the rigour of the fixed vocabulary where it matters.

Acceptance:

- [ ] Tests that the provider key, held-out data and controller sockets are
      absent from the Pod environment.
- [ ] Tests that outbound TCP/UDP connect fails and CUDA initialises.
- [ ] Schema test rejecting `analysis_program` outside the exploratory stage.
- [ ] Docs and `STATUS.md` updated; threat model v2 linked.

---

## Milestone 6 — Skeptic, Replicator, evaluator: findings that can be trusted

Purpose: turn exploratory output into validated claims. The README already
names these roles as future work; the ledger already has `HypothesisState`,
`PreregistrationPlan` and `Controls`.

- **Explorer** (existing loop): proposes hypotheses, registers them as DRAFT.
- **Skeptic**: a separate agent session that reads a DRAFT hypothesis and must
  propose alternative explanations and controls; its objections are recorded
  on the hypothesis. A hypothesis cannot be frozen with unanswered objections.
- **Freeze**: preregistration fixes the recipe, primary metric, minimum effect
  and controls.
- **Replicator**: the trusted evaluator (not an agent with research tools) runs
  the frozen recipe on held-out prompts the Explorer never saw, blinded to the
  Explorer's exploratory results. Only the evaluator can set VALIDATED or
  FALSIFIED.
- **Blind scientific calibration**: a known-answer benchmark. Seed the agent
  with tasks whose answers are published (for example a well-studied circuit
  in a small model added through the registry) and score recovery without
  revealing the answer. Report recovery rate alongside every validated finding.

Acceptance:

- [ ] End-to-end test on the tiny fixture: draft → skeptic objection → answer
      → freeze → evaluator replication → FALSIFIED or VALIDATED.
- [ ] At least one known-answer task run, with the recovery result reported
      whether or not it succeeds.
- [ ] Docs updated; `STATUS.md` distinguishes exploratory from validated.

---

## Milestone 7 (later) — Attribution graphs and circuit tracing

Purpose: gradient-based circuit discovery with transcoders.

Do this inside the Milestone 5 analysis sandbox using an established
circuit-tracing library, not as fixed operations. The fixed worker loads the
model with `requires_grad_(False)` and runs under `torch.inference_mode()`,
which cannot support backward passes through the model; changing that would
invalidate the exact-parity contract. Findings from this tier reach validation
only after re-expression as recipes (for example, patching the identified
features), per Milestone 5.

Requires transcoder assets through the registry and likely a larger GPU; get a
new envelope and **CHECKPOINT** first.

---

## Cost guidance (estimates only; always refresh live prices)

At the observed $0.74/hour RTX 4090 rate: Milestone 1 experiments cost cents;
a Milestone 2 feature dashboard over a modest public corpus is likely under a
few dollars; Milestones 3 and 6 are dominated by LLM API cost, not GPU cost.
Present an estimate and request an envelope before each paid run. The existing
cumulative $20 GPU authorisation covers Milestones 1–2 at most.

## Definition of done for the redirect

The project is "an automated interpretability lab" when `STATUS.md` can
truthfully list, under *Works and evidenced*:

1. An agent that explains SAE features and is scored on held-out data it never
   saw, compared against a one-shot baseline.
2. An agent that localises a behaviour with patching evidence.
3. At least one hypothesis taken through freeze and blinded replication by the
   trusted evaluator, with the outcome reported either way.
4. A known-answer calibration result showing how often the lab recovers
   published mechanisms.
