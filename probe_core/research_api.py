"""Trusted research facade. No direct ledger access is given to MCP clients."""

from __future__ import annotations

import hashlib
from pathlib import Path
import threading
from typing import Any, Protocol

from pydantic import Field

from .audit import canonical_json
from .ledger import JobRecord, JobState, Ledger
from .schemas import (
    ExperimentStage,
    FrozenModel,
    HypothesisRecord,
    HypothesisState,
    Identifier,
    JobSpec,
    SHA256,
)


class CloudRequests(Protocol):
    def request_start(self, worker_id: str, job_ids: list[str], max_runtime_seconds: int) -> Any: ...
    def status(self) -> Any: ...
    def stop_gpu(self, worker_id: str | None = None) -> Any: ...
    def request_provision(
        self, deployment: dict, job_ids: list[str], max_runtime_seconds: int, replaces_worker_id: str | None = None
    ) -> Any: ...


class Query(FrozenModel):
    offset: int = Field(default=0, strict=True, ge=0, le=1000000)
    limit: int = Field(default=50, strict=True, ge=1, le=100)


class JobID(FrozenModel):
    job_id: Identifier


class HypothesisID(FrozenModel):
    hypothesis_id: Identifier


class JobSubmission(FrozenModel):
    spec: JobSpec


class HypothesisSubmission(FrozenModel):
    hypothesis: HypothesisRecord


class StartRequest(FrozenModel):
    worker_id: Identifier
    job_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    max_runtime_seconds: int = Field(strict=True, ge=1, le=86400)


class StopRequest(FrozenModel):
    worker_id: Identifier | None = None


class ProvisionRequest(FrozenModel):
    deployment: dict[str, Any]
    job_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    max_runtime_seconds: int = Field(strict=True, ge=1, le=86400)
    replaces_worker_id: Identifier | None = None


class InputArtifact(FrozenModel):
    job_id: Identifier
    artifact_index: int = Field(strict=True, ge=0, le=1023)


class StoredArtifact(FrozenModel):
    artifact_id: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")


class SandboxRequest(FrozenModel):
    code: str = Field(strict=True, min_length=1, max_length=65536)
    input_artifacts: tuple[InputArtifact | StoredArtifact, ...] = Field(default=(), max_length=32)
    job_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)


class ResearchPolicy(FrozenModel):
    """Administrator-controlled corpus allowlist; an empty list permits no jobs."""

    discovery_datasets: tuple[SHA256, ...] = ()
    allow_calibration: bool = False


def job_view(job: JobRecord) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "operation": job.spec.operation.kind,
        "hypothesis_id": job.spec.hypothesis_id,
        "stage": job.spec.experiment_stage.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "attempt_count": job.attempt_count,
        "failure_kind": job.failure_kind,
    }


class ResearchService:
    """The method table is the authorization boundary, not a prompt instruction.

    Trusted controller and this service may share a service UID. The model-facing
    MCP process must have a separate UID and no access to their database/config.
    """

    def __init__(
        self,
        ledger: Ledger,
        policy: ResearchPolicy,
        *,
        cloud: CloudRequests | None = None,
        sandbox: Any = None,
        cancel_execution: Any = None,
        artifact_store: Any = None,
    ):
        self.ledger = ledger
        self.policy = ResearchPolicy.model_validate_json(policy.model_dump_json())
        self.cloud = cloud
        self.sandbox = sandbox
        self.cancel_execution = cancel_execution
        self.artifact_store = artifact_store
        self._sandbox_slot = threading.BoundedSemaphore(1)
        self.methods = {
            "lab_status": self.lab_status,
            "query_runs": self.query_runs,
            "job_status": self.job_status,
            "submit_job": self.submit_job,
            "cancel_job": self.cancel_job,
            "read_manifest": self.read_manifest,
            "read_artifact_summary": self.read_artifact_summary,
            "list_hypotheses": self.list_hypotheses,
            "register_hypothesis": self.register_hypothesis,
            "freeze_hypothesis": self.freeze_hypothesis,
            "request_gpu_start": self.request_gpu_start,
            "gpu_status": self.gpu_status,
            "request_gpu_provision": self.request_gpu_provision,
            "stop_gpu": self.stop_gpu,
            "run_sandboxed_experiment": self.run_sandboxed_experiment,
            "import_run_artifact": self.import_run_artifact,
        }

    def _visible_spec(self, spec: JobSpec) -> None:
        allowed = {ExperimentStage.EXPLORATORY}
        if self.policy.allow_calibration:
            allowed.add(ExperimentStage.CALIBRATION)
        if spec.experiment_stage not in allowed or spec.inputs.dataset_revision not in self.policy.discovery_datasets:
            raise PermissionError("dataset or scientific stage is not available to research clients")

    def _job(self, job_id: str) -> JobRecord:
        job = self.ledger.get_job(job_id)
        self._visible_spec(job.spec)
        return job

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        # Audit only a digest of arguments: code, prompts and secrets never enter
        # the audit through generic logging. Failed method guesses are retained.
        arguments_hash = "sha256:" + hashlib.sha256(canonical_json(params).encode()).hexdigest()
        permitted = method in self.methods
        self.ledger.record_event(
            "tool_call",
            {
                "tool": method if permitted else "unknown_method",
                "arguments_hash": arguments_hash,
                "policy_decision": "allowed_surface" if permitted else "denied",
            },
        )
        if not permitted:
            raise PermissionError("method is not exposed to research clients")
        try:
            return self.methods[method](params)
        except Exception:
            self.ledger.record_event(
                "policy_evaluation",
                {"tool": method[:64], "arguments_hash": arguments_hash, "policy_decision": "request_failed"},
            )
            raise

    def lab_status(self, params: dict[str, Any]) -> dict[str, Any]:
        FrozenModel.model_validate(params)
        jobs = self.query_runs({"limit": 100})
        return {
            "jobs": jobs,
            "sandbox_configured": self.sandbox is not None,
            "cloud_controller_configured": self.cloud is not None,
            "gpu_start_authority": False,
            "evaluation_authority": False,
        }

    def query_runs(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        query = Query.model_validate(params)
        visible = []
        for job in self.ledger.list_jobs():
            try:
                self._visible_spec(job.spec)
            except PermissionError:
                continue
            visible.append(job_view(job))
        return visible[query.offset : query.offset + query.limit]

    def job_status(self, params: dict[str, Any]) -> dict[str, Any]:
        request = JobID.model_validate(params)
        return job_view(self._job(request.job_id))

    def submit_job(self, params: dict[str, Any]) -> dict[str, Any]:
        request = JobSubmission.model_validate(params)
        self._visible_spec(request.spec)
        return job_view(self.ledger.submit_job(request.spec))

    def cancel_job(self, params: dict[str, Any]) -> dict[str, Any]:
        request = JobID.model_validate(params)
        job = self._job(request.job_id)
        # The ledger cancellation is durable before the remote kill request.
        result = self.ledger.cancel_job(job.job_id, "research client cancellation")
        if job.attempt_id is not None and self.cancel_execution is not None:
            self.cancel_execution(job.job_id, job.attempt_id)
        return job_view(result)

    def read_manifest(self, params: dict[str, Any]) -> dict[str, Any]:
        request = JobID.model_validate(params)
        self._job(request.job_id)
        return self.ledger.get_manifest(request.job_id).model_dump(mode="json")

    def read_artifact_summary(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        manifest = self.read_manifest(params)
        # No filesystem paths or hidden evaluation artifacts are exposed.
        return [{"index": i, **artifact} for i, artifact in enumerate(manifest["artifacts"])]

    def list_hypotheses(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        query = Query.model_validate(params)
        with self.ledger.read_connection() as conn:
            rows = conn.execute(
                "SELECT document FROM hypotheses ORDER BY hypothesis_id LIMIT ? OFFSET ?", (query.limit, query.offset)
            ).fetchall()
        return [HypothesisRecord.model_validate_json(row[0]).model_dump(mode="json") for row in rows]

    def import_run_artifact(self, params: dict[str, Any]) -> dict[str, Any]:
        source = InputArtifact.model_validate(params)
        self._job(source.job_id)
        if self.artifact_store is None:
            raise PermissionError("retained input store is not configured")
        artifact = self.ledger.get_manifest(source.job_id).artifacts[source.artifact_index]
        return self.artifact_store.register(
            self.ledger.get_artifact_root(source.job_id) / artifact.path, expected_sha256=artifact.sha256
        )

    def register_hypothesis(self, params: dict[str, Any]) -> dict[str, Any]:
        hypothesis = HypothesisSubmission.model_validate(params).hypothesis
        if hypothesis.novelty_status is not None or hypothesis.replication_ids or hypothesis.nearest_prior_work:
            raise PermissionError("novelty and replication adjudication are trusted-service operations")
        return self.ledger.register_hypothesis(hypothesis).model_dump(mode="json")

    def freeze_hypothesis(self, params: dict[str, Any]) -> dict[str, Any]:
        request = HypothesisID.model_validate(params)
        return self.ledger.transition_hypothesis(request.hypothesis_id, HypothesisState.FROZEN).model_dump(mode="json")

    def request_gpu_start(self, params: dict[str, Any]) -> Any:
        request = StartRequest.model_validate(params)
        for job_id in request.job_ids:
            self._job(job_id)
        if self.cloud is None:
            raise RuntimeError("controller is not configured")
        return self.cloud.request_start(request.worker_id, list(request.job_ids), request.max_runtime_seconds)

    def gpu_status(self, params: dict[str, Any]) -> Any:
        FrozenModel.model_validate(params)
        return (
            {"configured": False, "requests": []}
            if self.cloud is None
            else {"configured": True, "requests": self.cloud.status()}
        )

    def request_gpu_provision(self, params: dict[str, Any]) -> Any:
        from .provider import DeploymentSpec

        request = ProvisionRequest.model_validate(params)
        deployment = DeploymentSpec.model_validate(request.deployment)
        for job_id in request.job_ids:
            self._job(job_id)
        if self.cloud is None:
            raise RuntimeError("controller is not configured")
        return self.cloud.request_provision(
            deployment.model_dump(mode="json"),
            list(request.job_ids),
            request.max_runtime_seconds,
            request.replaces_worker_id,
        )

    def stop_gpu(self, params: dict[str, Any]) -> Any:
        request = StopRequest.model_validate(params)
        if self.cloud is None:
            raise RuntimeError("controller is not configured")
        return {"requests": self.cloud.stop_gpu(request.worker_id)}

    def run_sandboxed_experiment(self, params: dict[str, Any]) -> dict[str, Any]:
        from .sandbox import GPURequestBroker, SandboxLimits

        request = SandboxRequest.model_validate(params)
        if self.sandbox is None:
            raise PermissionError("CPU sandbox is not configured")
        if not self._sandbox_slot.acquire(blocking=False):
            raise PermissionError("CPU sandbox is busy")
        try:
            approved = {}
            for job_id in request.job_ids:
                job = self._job(job_id)
                if job.state != JobState.PENDING:
                    raise PermissionError("only registered pending jobs can be submitted from the sandbox")
                approved[job_id] = job.spec
            inputs = {}
            total = 0
            for i, source in enumerate(request.input_artifacts):
                if isinstance(source, StoredArtifact):
                    if self.artifact_store is None:
                        raise PermissionError("retained input store is not configured")
                    record, data = self.artifact_store.read(source.artifact_id, max_bytes=8 * 1024 * 1024 - total)
                    total += len(data)
                    inputs[f"input-{i}{Path(record['path']).suffix}"] = data
                    continue
                self._job(source.job_id)
                artifact = self.ledger.get_manifest(source.job_id).artifacts[source.artifact_index]
                path = self.ledger.get_artifact_root(source.job_id) / artifact.path
                total += path.stat().st_size
                if total > 8 * 1024 * 1024:
                    raise ValueError("CPU input bundle exceeds limit")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != artifact.sha256:
                    raise ValueError("retained artifact changed")
                inputs[f"input-{i}{Path(artifact.path).suffix}"] = data
            broker = GPURequestBroker(approved, lambda spec: self.ledger.submit_job(spec).job_id)
            result = self.sandbox.run(request.code, inputs=inputs, limits=SandboxLimits(), broker=broker)
            artifacts = []
            for path in result.artifacts:
                if self.artifact_store is None:
                    raise PermissionError("retained input store is not configured")
                artifacts.append({"name": path.name, **self.artifact_store.register(path)})
            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "termination_reason": result.termination_reason,
                "artifacts": artifacts,
            }
        finally:
            self._sandbox_slot.release()
