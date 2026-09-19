"""Versioned, data-only contracts between the trusted dispatcher and executor."""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from .schemas import (
    ArtifactRecord, Controls, FrozenModel, GitSHA, Identifier, JobSpec, ModelIdentity,
    SHA256, SoftwareIdentity, HardwareIdentity, UTCTimestamp, RunManifest,
)


class Prompt(FrozenModel):
    prompt_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=131072)] | None = None
    token_ids: Annotated[tuple[Annotated[int, Field(strict=True, ge=0)], ...], Field(min_length=1, max_length=32768)] | None = None

    @model_validator(mode="after")
    def exactly_one_input(self):
        if (self.text is None) == (self.token_ids is None):
            raise ValueError("prompt requires exactly one of text or token_ids")
        return self


class PromptDataset(FrozenModel):
    schema_version: Literal[1] = 1
    prompts: Annotated[tuple[Prompt, ...], Field(min_length=1, max_length=100000)]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({p.prompt_id for p in self.prompts}) != len(self.prompts):
            raise ValueError("prompt identifiers must be unique")
        return self


class DatasetAsset(FrozenModel):
    path: str
    sha256: SHA256


class FileDigest(FrozenModel):
    path: str
    sha256: SHA256

    @field_validator("path")
    @classmethod
    def safe_relative(cls, value):
        path = Path(value)
        if path.is_absolute() or not path.parts or any(part in {"..", "."} for part in value.split("/")) or "\\" in value:
            raise ValueError("model assets must be relative paths without traversal")
        return value


class WorkerConfig(FrozenModel):
    """Trusted deployment configuration, never accepted through the job endpoint.

    ``container_image_digest`` and Git/HF revisions are deployment attestations;
    installed versions, hardware, model/config/tokenizer bytes are checked locally.
    CUDA execution requires a delegated cgroup v2 directory for hard RAM/CPU/PID caps.
    """
    model_directory: str
    model: ModelIdentity
    assets: Annotated[tuple[FileDigest, ...], Field(min_length=1, max_length=128)]
    datasets: Annotated[tuple[DatasetAsset, ...], Field(min_length=1, max_length=1000)]
    tensor_directory: str
    output_directory: str
    backend: Literal["nnsight", "reference"] = "nnsight"
    device: Literal["cpu", "cuda:0"] = "cpu"
    code_git_commit: GitSHA
    container_image_digest: SHA256 | None = None
    environment_lock_path: str | None = None
    provider_backend: Literal["runpod", "local_cpu"]
    region: Identifier
    live_price_usd_per_hour: Annotated[float, Field(ge=0, lt=1.5, allow_inf_nan=False)]
    cgroup_directory: str | None = None
    max_request_bytes: Annotated[int, Field(strict=True, ge=1024, le=1048576)] = 262144
    max_tensor_bytes: Annotated[int, Field(strict=True, ge=1024, le=5 * 1024**3)] = 1024**3

    @model_validator(mode="after")
    def coherent_profile(self):
        if self.container_image_digest is None and self.environment_lock_path is None:
            raise ValueError("native execution needs an actual environment lock; containers need an image digest")
        if self.device == "cuda:0" and self.container_image_digest is None:
            raise ValueError("CUDA deployment requires a pinned container image digest")
        if self.device == "cpu":
            if self.provider_backend != "local_cpu" or self.live_price_usd_per_hour != 0:
                raise ValueError("CPU execution must use honest local_cpu/zero-GPU pricing")
        elif self.provider_backend != "runpod" or self.live_price_usd_per_hour <= 0 or self.cgroup_directory is None:
            raise ValueError("CUDA requires RunPod provenance, positive observed price and delegated cgroups")
        if self.model.repo == "probe/testing-tiny-qwen3" and self.device != "cpu":
            raise ValueError("the synthetic fixture is CPU calibration only")
        if len({asset.path for asset in self.assets}) != len(self.assets):
            raise ValueError("duplicate model assets")
        if len({asset.sha256 for asset in self.datasets}) != len(self.datasets):
            raise ValueError("duplicate datasets")
        return self


class ScienceMetadata(FrozenModel):
    primary_metric: Identifier = "unscored_observation"
    predicted_direction: Literal["increase", "decrease", "no_change"] = "no_change"
    falsifier: str = "This execution makes no scientific claim; inspect retained observations."
    alternative_explanations: tuple[str, ...] = ("measurement artifact",)
    controls: Controls = Controls(random_component=False, norm_matched_direction=False, unrelated_behavior_suite="not_scored")
    preregistration_hash: SHA256 | None = None
    session_id: Identifier = "trusted-dispatcher"
    replicator_blinded: bool = False


class ExecutionRequest(FrozenModel):
    schema_version: Literal[1] = 1
    job_id: Identifier
    attempt_id: Identifier
    worker_id: Identifier
    approval_id: Identifier
    deadline: UTCTimestamp
    spec: JobSpec
    science: ScienceMetadata = ScienceMetadata()


class WorkerState(StrEnum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExecutionReceipt(FrozenModel):
    job_id: Identifier
    attempt_id: Identifier
    state: WorkerState
    started_at: UTCTimestamp
    finished_at: UTCTimestamp | None = None
    failure_kind: Literal["infrastructure", "scientific", "oom", "timeout", "policy", "cancelled"] | None = None
    error_code: str | None = None
    process_stopped: bool = False
    manifest: RunManifest | None = None
    wall_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0
    generated_tokens: Annotated[int, Field(strict=True, ge=0)] = 0
    peak_rss_bytes: Annotated[int, Field(strict=True, ge=0)] = 0
    peak_vram_bytes: Annotated[int, Field(strict=True, ge=0)] = 0


    @model_validator(mode="after")
    def consistent_outcome(self):
        terminal = self.state in {WorkerState.SUCCEEDED, WorkerState.FAILED, WorkerState.CANCELLED}
        if self.process_stopped and not terminal:
            raise ValueError("running/accepted receipts cannot acknowledge process termination")
        if terminal and self.finished_at is None:
            raise ValueError("terminal receipts require an observed finish timestamp")
        if self.state == WorkerState.SUCCEEDED and (self.manifest is None or self.failure_kind is not None):
            raise ValueError("success requires a manifest and no failure classification")
        if self.state in {WorkerState.FAILED, WorkerState.CANCELLED} and self.failure_kind is None:
            raise ValueError("failure requires a classification")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finish precedes execution start")
        return self


class WorkerRequestError(ValueError):
    """An execution request violates a trusted worker contract."""


class WorkerBusyError(WorkerRequestError):
    """Only one unresolved execution may occupy this worker."""
