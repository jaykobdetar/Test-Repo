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

## Completion criterion

Produce one reproducible exploratory report with per-prompt results, controls,
artifact hashes, verified backup and confirmed provider deletion. No positive
scientific outcome is required: small effects or failed controls must be reported.

The supervised experiment does not require another controller installation,
managed cancellation/replacement acceptance, a private evaluator or autonomous
research agents. Those remain later work for their respective claims. This
public prompt set is development data and must never be relabelled held-out
confirmation data.
