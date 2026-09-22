"""Strict, immutable wire contracts for the offline Probe control plane.

These contracts describe data and a small fixed vocabulary of interventions.
They deliberately cannot carry executable Python, serialized objects, or host paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, validate_default=True)


Identifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4096)]
ShortText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=256)]
GitSHA = Annotated[str, StringConstraints(strict=True, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
SHA256 = Annotated[str, StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
RawSHA256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Seconds = Annotated[int, Field(strict=True, ge=1, le=86400)]
FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-1e12, le=1e12)]
Money = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0, le=1e9)]
TokenIndex = Annotated[int, Field(strict=True, ge=0, le=32767)]
TokenPosition = TokenIndex | Literal["last"]


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


UTCTimestamp = Annotated[AwareDatetime, AfterValidator(_utc)]


def _safe_relative_path(value: str) -> str:
    # Keep path handling independent of a platform, URL decoding, or normalization.
    if len(value) > 512 or "\\" in value or "\x00" in value:
        raise ValueError("artifact path must be a short POSIX relative path")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(p in {"", ".", ".."} for p in parts):
        raise ValueError("absolute paths, empty segments and traversal are forbidden")
    if any(not all(c.isascii() and (c.isalnum() or c in "_.-") for c in p) for p in parts):
        raise ValueError("artifact paths accept only ASCII letters, digits, dots, underscores and hyphens")
    return value


RelativePath = Annotated[str, StringConstraints(strict=True, min_length=1), AfterValidator(_safe_relative_path)]
ModuleName = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=200, pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*$"
    ),
]


class PredictedDirection(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    NO_CHANGE = "no_change"


class ExperimentStage(StrEnum):
    EXPLORATORY = "exploratory"
    CONFIRMATORY = "confirmatory"
    REPLICATION = "replication"
    CALIBRATION = "calibration"


class ModelIdentity(FrozenModel):
    repo: Literal["Qwen/Qwen3-1.7B-Base", "Qwen/Qwen3-1.7B", "probe/testing-tiny-qwen3"]
    revision_sha: GitSHA
    local_weight_hashes: Annotated[tuple[SHA256, ...], Field(min_length=1, max_length=64)]
    tokenizer_revision: GitSHA
    dtype: Literal["bfloat16", "float32", "float16"]
    quantized: StrictBool
    chat_template_hash: SHA256 | None
    thinking_mode: StrictBool | None

    @model_validator(mode="after")
    def base_has_no_chat_mode(self) -> Self:
        if self.repo.endswith("-Base") and (self.chat_template_hash is not None or self.thinking_mode is not None):
            raise ValueError("the canonical Base checkpoint does not have a chat template or thinking mode")
        return self


class GenerationSettings(FrozenModel):
    temperature: Annotated[float, Field(strict=True, ge=0, le=2, allow_inf_nan=False)]
    max_new_tokens: Annotated[int, Field(strict=True, ge=1, le=4096)]


class ExperimentInputs(FrozenModel):
    dataset_revision: SHA256
    prompt_set_hash: SHA256
    prompt_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=4096)]
    random_seed: Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]
    generation: GenerationSettings

    @field_validator("prompt_ids")
    @classmethod
    def unique_prompts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("prompt_ids must be unique")
        return values


class RunDetails(FrozenModel):
    run_id: Identifier
    parent_run_id: Identifier | None
    started_at: UTCTimestamp
    experiment_stage: ExperimentStage
    hypothesis_id: Identifier | None
    preregistration_hash: SHA256 | None
    approval_id: Identifier
    explorer_session_id: Identifier
    replicator_blinded: StrictBool

    @model_validator(mode="after")
    def enforce_registration(self) -> Self:
        if self.experiment_stage in {ExperimentStage.CONFIRMATORY, ExperimentStage.REPLICATION}:
            if self.hypothesis_id is None or self.preregistration_hash is None:
                raise ValueError("confirmatory and replication runs require a registered hypothesis")
        if self.experiment_stage == ExperimentStage.REPLICATION and not self.replicator_blinded:
            raise ValueError("replication runs require a blinded replicator")
        if self.parent_run_id == self.run_id:
            raise ValueError("a run cannot be its own parent")
        return self


Version = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=80, pattern=r"^[0-9][A-Za-z0-9_.+!-]*$")
]


class SoftwareIdentity(FrozenModel):
    probe_mcp_git_commit: GitSHA
    container_image_digest: SHA256 | None
    environment_lock_hash: SHA256 | None = Field(default=None, exclude_if=lambda value: value is None)
    python_version: Version
    torch_version: Version
    transformers_version: Version
    nnsight_version: Version
    cuda_version: Version

    @model_validator(mode="after")
    def environment_is_identified(self) -> Self:
        if self.container_image_digest is None and self.environment_lock_hash is None:
            raise ValueError("native execution requires an environment lock hash")
        return self


class HardwareIdentity(FrozenModel):
    provider_backend: Literal["runpod", "local_cpu"]
    gpu_model: ShortText
    gpu_count: Literal[0, 1]
    region: Identifier
    live_price_usd_per_hour: Annotated[float, Field(strict=True, ge=0, lt=1.50, allow_inf_nan=False)]

    @model_validator(mode="after")
    def device_matches_provider(self) -> Self:
        if self.provider_backend == "runpod":
            if self.gpu_count != 1 or self.live_price_usd_per_hour <= 0:
                raise ValueError("RunPod requires one GPU and a positive verified price")
        elif self.gpu_count != 0 or self.live_price_usd_per_hour != 0:
            raise ValueError("local CPU verification has no GPU or compute charge")
        return self

    @field_validator("gpu_count", mode="before")
    @classmethod
    def integer_gpu_count(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("gpu_count must be an integer")
        return value


class ExperimentMetadata(FrozenModel):
    tool: Literal[
        "capture_activation",
        "activation_patch",
        "ablate_component",
        "steer_direction",
        "fit_probe",
        "generate_batch",
        "weight_stats",
        "tensor_slice",
        "module_manifest",
        "backend_parity",
    ]
    modules: Annotated[tuple[ModuleName, ...], Field(min_length=1, max_length=112)]
    positions: Annotated[tuple[TokenPosition, ...], Field(min_length=1, max_length=1024)]
    intervention_hash: SHA256
    predicted_direction: PredictedDirection
    primary_metric: Identifier
    falsifier: Text
    alternative_explanations: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]


class Controls(FrozenModel):
    random_component: StrictBool
    norm_matched_direction: StrictBool
    unrelated_behavior_suite: Identifier


class RunResults(FrozenModel):
    effect_size: FiniteNumber | None
    confidence_interval: tuple[FiniteNumber, FiniteNumber] | None
    heldout: StrictBool
    replication_status: Literal["pending", "passed", "failed", "not_applicable"]

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        if self.confidence_interval is not None:
            low, high = self.confidence_interval
            if low > high:
                raise ValueError("confidence interval lower bound must not exceed upper bound")
            if self.effect_size is None:
                raise ValueError("a confidence interval requires an effect size")
        return self


class RunCost(FrozenModel):
    gpu_seconds: NonnegativeInt
    estimated_compute_usd: Money
    bytes_persisted: NonnegativeInt


class ArtifactRecord(FrozenModel):
    path: RelativePath
    sha256: RawSHA256
    retention_class: Literal["temporary", "derived", "validated", "audit"]


class RunSecurity(FrozenModel):
    outbound_network_attempts: NonnegativeInt
    policy_denials: NonnegativeInt
    secret_access_attempts: NonnegativeInt


class RunManifest(FrozenModel):
    schema_version: Literal[1]
    run: RunDetails
    model: ModelIdentity
    software: SoftwareIdentity
    hardware: HardwareIdentity
    inputs: ExperimentInputs
    experiment: ExperimentMetadata
    controls: Controls
    results: RunResults
    cost: RunCost
    artifacts: Annotated[tuple[ArtifactRecord, ...], Field(max_length=1024)]
    security: RunSecurity

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def confirmatory_constraints(self) -> Self:
        if self.experiment.tool == "backend_parity" and (
            self.run.experiment_stage != ExperimentStage.CALIBRATION
            or self.run.hypothesis_id is not None
            or self.results.heldout
            or self.results.replication_status != "not_applicable"
        ):
            raise ValueError("backend parity is calibration and never held-out or replication evidence")
        if self.model.repo == "probe/testing-tiny-qwen3" and self.run.experiment_stage != ExperimentStage.CALIBRATION:
            raise ValueError("test fixture models are restricted to calibration")
        if self.run.experiment_stage in {ExperimentStage.CONFIRMATORY, ExperimentStage.REPLICATION}:
            if self.model.quantized or self.model.dtype != "bfloat16":
                raise ValueError("confirmation and replication require the canonical unquantized BF16 checkpoint")
            if not self.results.heldout:
                raise ValueError("confirmation and replication require held-out inputs")
        if len({a.path for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact paths must be unique")
        return self


class ModuleRef(FrozenModel):
    layer: Annotated[int, Field(strict=True, ge=0, le=27)]
    component: Literal["residual", "attention_output", "mlp_output", "attention_head"]
    head: Annotated[int, Field(strict=True, ge=0, le=15)] | None = None

    @model_validator(mode="after")
    def head_matches_component(self) -> Self:
        if (self.component == "attention_head") != (self.head is not None):
            raise ValueError("head is required exactly for attention_head components")
        return self


Positions = Annotated[tuple[TokenPosition, ...], Field(min_length=1, max_length=256)]


class TensorArtifact(FrozenModel):
    path: RelativePath
    sha256: SHA256
    tensor_name: Identifier

    @field_validator("path")
    @classmethod
    def safe_tensor_format(cls, value: str) -> str:
        if not value.endswith(".safetensors"):
            raise ValueError("intervention inputs must use the safetensors format")
        return value


class Capture(FrozenModel):
    kind: Literal["capture"]
    modules: Annotated[tuple[ModuleRef, ...], Field(min_length=1, max_length=28)]
    positions: Positions
    reduction: Literal["none", "mean", "mean_and_variance"] = "mean_and_variance"


class Patch(FrozenModel):
    kind: Literal["patch"]
    target: ModuleRef
    positions: Positions
    source: TensorArtifact


class Ablate(FrozenModel):
    kind: Literal["ablate"]
    target: ModuleRef
    positions: Positions
    method: Literal["zero", "mean"] = "zero"
    baseline: TensorArtifact | None = None

    @model_validator(mode="after")
    def mean_requires_baseline(self) -> Self:
        if (self.method == "mean") != (self.baseline is not None):
            raise ValueError("baseline is required exactly for mean ablation")
        return self


class Steer(FrozenModel):
    kind: Literal["steer"]
    target: ModuleRef
    positions: Positions
    direction: TensorArtifact
    strength: Annotated[float, Field(strict=True, ge=-100, le=100, allow_inf_nan=False)]


class FitProbe(FrozenModel):
    kind: Literal["fit_probe"]
    activations: TensorArtifact
    labels: TensorArtifact
    algorithm: Literal["ridge", "logistic_regression"]
    l2_penalty: Annotated[float, Field(strict=True, ge=1e-6, le=1e6, allow_inf_nan=False)] = 1.0
    train_fraction: Annotated[float, Field(strict=True, ge=0.1, le=0.9, allow_inf_nan=False)] = 0.8
    max_iterations: Annotated[int, Field(strict=True, ge=1, le=10000)] = 1000


class Generate(FrozenModel):
    kind: Literal["generate"]


class WeightStats(FrozenModel):
    kind: Literal["weight_stats"]
    modules: Annotated[tuple[ModuleName, ...], Field(min_length=1, max_length=32)]


class TensorSlice(FrozenModel):
    kind: Literal["tensor_slice"]
    parameter: ModuleName
    starts: Annotated[tuple[NonnegativeInt, ...], Field(min_length=1, max_length=4)]
    sizes: Annotated[tuple[Annotated[int, Field(strict=True, ge=1, le=4096)], ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def bounded_slice(self) -> Self:
        import math

        if len(self.starts) != len(self.sizes) or math.prod(self.sizes) > 1048576:
            raise ValueError("slice rank must match and contain at most 1048576 elements")
        return self


class ModuleManifest(FrozenModel):
    kind: Literal["module_manifest"]


class BackendParity(FrozenModel):
    """Fixed trusted calibration suite; no caller-selected code or tolerances."""

    kind: Literal["backend_parity"]
    suite_version: Literal[1] = 1


Operation = Annotated[
    Capture | Patch | Ablate | Steer | FitProbe | Generate | WeightStats | TensorSlice | ModuleManifest | BackendParity,
    Field(discriminator="kind"),
]


class JobLimits(FrozenModel):
    max_runtime_seconds: Seconds
    max_output_bytes: Annotated[int, Field(strict=True, ge=1, le=100 * 1024**3)]
    max_cpu_cores: Annotated[int, Field(strict=True, ge=1, le=32)] = 4
    max_ram_bytes: Annotated[int, Field(strict=True, ge=1024**3, le=256 * 1024**3)] = 16 * 1024**3
    max_vram_bytes: Annotated[int, Field(strict=True, ge=1024**3, le=48 * 1024**3)] = 24 * 1024**3
    max_generated_tokens: Annotated[int, Field(strict=True, ge=1, le=1048576)] = 32768


class JobSpec(FrozenModel):
    idempotency_key: Identifier
    hypothesis_id: Identifier | None = None
    experiment_stage: ExperimentStage = ExperimentStage.EXPLORATORY
    model: ModelIdentity
    inputs: ExperimentInputs
    operation: Operation
    limits: JobLimits

    @model_validator(mode="after")
    def generation_within_budget(self) -> Self:
        if self.model.repo == "probe/testing-tiny-qwen3" and self.experiment_stage != ExperimentStage.CALIBRATION:
            raise ValueError("test fixture models are restricted to calibration")
        requested = len(self.inputs.prompt_ids) * self.inputs.generation.max_new_tokens
        if self.operation.kind == "backend_parity":
            if self.experiment_stage != ExperimentStage.CALIBRATION or self.hypothesis_id is not None:
                raise ValueError("backend parity is calibration only and cannot supply hypothesis evidence")
            if (
                not 2 <= len(self.inputs.prompt_ids) <= 4
                or self.inputs.generation.max_new_tokens > 8
                or self.inputs.generation.temperature != 0.0
            ):
                raise ValueError("backend parity requires two to four prompts and at most eight greedy tokens")
            requested *= 2  # Native HF and NNsight both generate actual tokens.
            if self.model.repo != "probe/testing-tiny-qwen3" and (
                self.model.dtype != "bfloat16" or self.model.quantized
            ):
                raise ValueError("canonical backend parity requires unquantized BF16")
        if requested > self.limits.max_generated_tokens:
            raise ValueError("prompt count times max_new_tokens exceeds the declared generation limit")
        if self.experiment_stage in {ExperimentStage.CONFIRMATORY, ExperimentStage.REPLICATION}:
            if self.hypothesis_id is None:
                raise ValueError("confirmatory and replication jobs require a hypothesis_id")
            if self.model.quantized or self.model.dtype != "bfloat16":
                raise ValueError("confirmatory and replication jobs require the canonical unquantized BF16 checkpoint")
        return self


class HypothesisState(StrEnum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    TESTING = "TESTING"
    REPLICATING = "REPLICATING"
    VALIDATED = "VALIDATED"
    FALSIFIED = "FALSIFIED"


class NoveltyTag(StrEnum):
    N0 = "N0"
    N1 = "N1"
    N2 = "N2"
    N3 = "N3"


class PreregistrationPlan(FrozenModel):
    model: ModelIdentity
    operation: Operation
    primary_metric: Identifier
    minimum_effect: Annotated[float, Field(strict=True, ge=0, le=1e12, allow_inf_nan=False)]
    controls: Controls


class HypothesisRecord(FrozenModel):
    hypothesis_id: Identifier
    status: HypothesisState = HypothesisState.DRAFT
    proposition: Text
    alignment_relevance: Text
    originating_observations: Annotated[tuple[Identifier, ...], Field(max_length=256)] = ()
    competing_hypotheses: Annotated[tuple[Text, ...], Field(max_length=64)] = ()
    predicted_causal_intervention: Text
    predicted_direction: PredictedDirection
    predictions: Annotated[tuple[Text, ...], Field(max_length=64)] = ()
    falsifier: Text
    preregistration_plan: PreregistrationPlan | None = None
    frozen_at: UTCTimestamp | None = None
    preregistration_hash: SHA256 | None = None
    novelty_status: NoveltyTag | None = None
    nearest_prior_work: Annotated[tuple[Text, ...], Field(max_length=128)] = ()
    replication_ids: Annotated[tuple[Identifier, ...], Field(max_length=256)] = ()

    @model_validator(mode="after")
    def registration_matches_state(self) -> Self:
        if self.status == HypothesisState.DRAFT:
            if self.frozen_at is not None or self.preregistration_hash is not None:
                raise ValueError("draft hypotheses must not contain preregistration metadata")
        elif (
            self.frozen_at is None
            or self.preregistration_hash is None
            or not self.predictions
            or self.preregistration_plan is None
        ):
            raise ValueError(
                "non-draft hypotheses require frozen_at, preregistration_hash, predictions and a preregistration_plan"
            )
        if self.status == HypothesisState.VALIDATED and not self.replication_ids:
            raise ValueError("validated hypotheses require replication evidence")
        return self


class ApprovalNonce(FrozenModel):
    approval_id: Identifier
    token: SecretStr = Field(repr=False, exclude=True)
    pod_id: Identifier
    batch_hash: SHA256
    purpose: Literal["research", "infrastructure_preflight"] = "research"
    max_runtime_seconds: Seconds
    price_ceiling_usd_per_hour: Annotated[float, Field(strict=True, gt=0, le=1.50, allow_inf_nan=False)]
    expires_at: UTCTimestamp
    issued_at: UTCTimestamp

    @field_validator("token")
    @classmethod
    def minimum_token_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32 or len(value.get_secret_value()) > 512:
            raise ValueError("approval token must contain 32 to 512 characters")
        return value

    @model_validator(mode="after")
    def expiry_follows_issue(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")
        if (self.expires_at - self.issued_at).total_seconds() > 900:
            raise ValueError("approval lifetime must not exceed 15 minutes")
        return self


class PinnedModel(FrozenModel):
    repo: Literal["Qwen/Qwen3-1.7B-Base", "Qwen/Qwen3-1.7B"]
    revision_sha: GitSHA


class BudgetEnvelope(FrozenModel):
    """A human-approved spending envelope for exploratory research compute.

    Within an open, unexpired envelope the controller may approve disposable
    research Pods without a per-start human action. Every per-Pod price, idle,
    deadline, watchdog and deletion check still applies. Nothing renews or
    extends an envelope; the controller sets ``approved_by`` from the admin
    socket's peer identity.
    """

    envelope_id: Identifier
    max_gpu_usd: Annotated[float, Field(strict=True, gt=0, le=20, allow_inf_nan=False)]
    max_llm_usd: Annotated[float, Field(strict=True, ge=0, le=0, allow_inf_nan=False)]
    max_gpu_usd_per_hour: Annotated[float, Field(strict=True, gt=0, lt=1.50, allow_inf_nan=False)]
    max_wall_seconds_per_pod: Annotated[int, Field(strict=True, ge=60, le=900)]
    allowed_models: Annotated[tuple[PinnedModel, ...], Field(min_length=1, max_length=16)]
    allowed_stages: Annotated[tuple[Literal[ExperimentStage.EXPLORATORY], ...], Field(min_length=1, max_length=1)]
    issued_at: UTCTimestamp
    expires_at: UTCTimestamp
    approved_by: Annotated[str, StringConstraints(strict=True, pattern=r"^uid:[0-9]{1,10}$")]

    @model_validator(mode="after")
    def bounded_lifetime(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")
        if (self.expires_at - self.issued_at).total_seconds() > 7 * 86400:
            raise ValueError("an envelope must expire within seven days")
        if len(set(self.allowed_models)) != len(self.allowed_models):
            raise ValueError("allowed models must be distinct")
        return self
