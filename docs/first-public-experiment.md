# First supervised public experiment

This is the experiment for Milestone 1d of the [redirect plan](../REDIRECT-PLAN.md),
which runs it as a registered recipe after budget envelopes, recipes and metrics
exist. It is a proposed fixed recipe, not an executed experiment or a claim about
what the model represents; see [STATUS.md](../STATUS.md) for whether it has run.

## Question and measurement

Measure how zeroing the layer-14 MLP output at the last prompt token changes
subject–verb agreement on a small, public prompt set. Use the difference between
the next-token logit for the grammatically expected verb and its alternative.
Report the intervention's change in that difference for every prompt, plus the
paired mean. This measures a local causal effect on these prompts; it does not
identify a dedicated grammar circuit.

Freeze singular/plural pairs such as “The key near the cabinets” and “The keys
near the cabinet” before running. The exact prompt list, expected alternatives,
token IDs, model revisions and seed must appear in a versioned recipe. Verify
that each alternative is one next token under that checkpoint's tokenizer; if
not, revise and freeze the recipe before GPU execution. Posttrained formatting
must be explicit and separate from Base formatting.

## Smallest implementation

Add one fixed recipe and matching collector alongside the calibration command.
Reuse the existing model-loading and intervention methods, immutable images,
clean unprivileged environment, bounded runtime, copied artifact verification
and Pod deletion. The current standalone command intentionally accepts only
`backend_parity_v1`; do not silently reinterpret calibration output as research.

For each prompt retain three conditions: unmodified forward, a no-op hook, and
zero ablation. Require the no-op logits to match the reference before interpreting
the ablation. Save the expected/alternative logits, paired effect, tokenization,
configuration, software/model hashes and all failed checks. Report every prompt;
do not select only large effects.

Run Base first under a new short allowance. Collect and verify its artifacts before deleting the Pod, then independently
back up the local bundle before accepting the experiment. Then repeat the
same declared comparison on the separately pinned posttrained checkpoint, with
its own explicit formatting. Report the checkpoints separately; a difference
between them is not by itself evidence about training's causal effect.

## Frozen recipes (approved 2026-09-22)

The operator approved these recipes at the Milestone 1 checkpoint. They are
registered in `probe_core/resources/recipes/` and pinned by canonical SHA-256:

| Suite | Recipe hash | Dataset hash |
| --- | --- | --- |
| `subject_verb_l14_mlp_base_v1` | `sha256:6239613c389f42a806e8ed600f568591720ff136e70aca0fd76dc28337317f07` | `sha256:26c9461012769835029542de99f79e0d0ecbee041d09ca94aa4b36e34e720c9b` |
| `subject_verb_l14_mlp_posttrained_v1` | `sha256:f354fc8937c73917c9e04c793e8d7bc732b978d9edac40bdb0daf9f1486f9dae` | `sha256:b8e7d542f46650077e228005f621803b2771ae74996af04e5ceda38cbe0d8b1a` |

- Prompts: 12 singular/plural pairs, each with an opposite-number attractor,
  such as "The key near the cabinets" and "The keys near the cabinet" (24 prompts).
- Base formatting: the raw text; metric tokens `" is"` (374) and `" are"` (525).
- Posttrained formatting: the pinned chat template with thinking disabled and the
  user message "Continue this sentence with exactly one word: " followed by the
  text; metric tokens `"is"` (285) and `"are"` (546), because the reply starts
  without a leading space.
- Every token is a single token under its checkpoint's pinned tokenizer, and both
  tokenizers were checked against their locked hashes.
- Conditions: unmodified baseline, the automatic exact no-op check, zero ablation
  of the layer-14 MLP output at the last prompt token (primary), and three
  norm-matched random displacements of that activation (seeds 1 to 3, controls).
- Primary metric: correct-minus-incorrect verb logit difference per prompt and
  its paired mean change. Target log-probability, KL to baseline and the top-5
  tokens are also reported.

Images built from source `efa0b5819fc4e399c9641f508d478827e47caab6` (not yet
accepted on a GPU):

| Model | Worker image | Derived supervised image |
| --- | --- | --- |
| Base | `sha256:89e593abdeb443c88b6677cad04e9a4b3a693b18f7b91c6f041aed424a5e08e7` | `sha256:b936d83996967c58a386c9c45156e23d2de50a792800815d45a8972165a02510` |
| Posttrained | `sha256:6b893213801dc2c8aae0a1ba8621aa75548cc750d13c10119cd291b70c43fec3` | `sha256:bd4750c5a74a07e6267d421ad7bcb0bc8d03442897fb3f66dcb75d4a4ae3058f` |

Each run is charged to budget envelope `m1-exploratory-001`. The estimate is
about $0.25 for both Pods; the envelope reserves up to $0.27 per Pod.

## Completion criterion

Produce one reproducible exploratory report with per-prompt results, controls,
artifact hashes, verified backup and confirmed provider deletion. No positive
scientific outcome is required: small effects or failed controls must be reported.

The supervised experiment does not require another controller installation,
managed cancellation/replacement acceptance, a private evaluator or autonomous
research agents. Those remain later work for their respective claims. This
public prompt set is development data and must never be relabelled held-out
confirmation data.
