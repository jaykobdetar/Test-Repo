import json
from pathlib import Path

import pytest

from probe_core.ledger import Ledger
from probe_core.research_api import ResearchPolicy, ResearchService
from probe_core.schemas import JobSpec


@pytest.fixture
def research(tmp_path):
    data = json.loads((Path(__file__).parent / "fixtures/manifest.json").read_text())
    spec = JobSpec(
        idempotency_key="research-1",
        model=data["model"],
        inputs=data["inputs"],
        operation={"kind": "generate"},
        limits={"max_runtime_seconds": 10, "max_output_bytes": 1024},
    )
    with Ledger(tmp_path / "research.sqlite") as ledger:
        yield ResearchService(ledger, ResearchPolicy(discovery_datasets=(spec.inputs.dataset_revision,))), spec


def test_submission_disconnect_safe_and_cancel(research):
    service, spec = research
    first = service.dispatch("submit_job", {"spec": spec.model_dump(mode="json")})
    second = service.dispatch("submit_job", {"spec": spec.model_dump(mode="json")})
    assert first == second
    assert service.dispatch("job_status", {"job_id": first["job_id"]})["state"] == "PENDING"
    assert service.dispatch("cancel_job", {"job_id": first["job_id"]})["state"] == "FAILED"
    assert service.dispatch("query_runs", {})[0]["failure_kind"] == "cancelled"


@pytest.mark.parametrize(
    "method",
    [
        "approve",
        "approve_gpu_start",
        "consume_approval",
        "end_approval",
        "confirm_stopped",
        "complete_job",
        "transition_hypothesis",
        "evaluate",
        "read_file",
        "execute_python",
    ],
)
def test_trusted_methods_absent_and_denials_audited(research, method):
    service, _ = research
    with pytest.raises(PermissionError):
        service.dispatch(method, {"token": "a-secret-not-to-log"})
    encoded = json.dumps(service.ledger.audit_records())
    assert "a-secret-not-to-log" not in encoded
    assert '"policy_decision": "denied"' in encoded


def test_private_corpus_is_not_reachable_or_listed(research):
    service, spec = research
    data = spec.model_dump(mode="json")
    data["inputs"]["dataset_revision"] = "sha256:" + "f" * 64
    with pytest.raises(PermissionError):
        service.dispatch("submit_job", {"spec": data})
    hidden = service.ledger.submit_job(JobSpec.model_validate(data))
    assert service.dispatch("query_runs", {}) == []
    for method in ("job_status", "cancel_job", "read_manifest", "read_artifact_summary"):
        with pytest.raises(PermissionError):
            service.dispatch(method, {"job_id": hidden.job_id})


def test_scientific_stage_cannot_be_self_certified(research):
    service, spec = research
    data = spec.model_dump(mode="json")
    data.update(experiment_stage="confirmatory", hypothesis_id="H-1")
    data["model"]["dtype"] = "bfloat16"
    with pytest.raises(PermissionError):
        service.dispatch("submit_job", {"spec": data})


def test_unknown_arguments_rejected(research):
    service, _ = research
    with pytest.raises(ValueError):
        service.dispatch("lab_status", {"admin": True})
    assert service.dispatch("lab_status", {})["gpu_start_authority"] is False


def test_empty_allowlist_fails_closed(research):
    service, spec = research
    service.policy = ResearchPolicy()
    with pytest.raises(PermissionError):
        service.dispatch("submit_job", {"spec": spec.model_dump(mode="json")})


def test_provision_request_validates_entire_batch_before_contacting_controller(research):
    service, spec = research
    from probe_core.provider import DeploymentSpec

    class ControllerRequests:
        calls = []

        def request_provision(self, deployment, job_ids, max_runtime_seconds, replaces_worker_id=None):
            self.calls.append((deployment, job_ids, max_runtime_seconds, replaces_worker_id))
            return {"request_id": "pending-human-approval"}

    cloud = ControllerRequests()
    service.cloud = cloud
    visible = service.ledger.submit_job(spec)
    data = spec.model_dump(mode="json")
    data["idempotency_key"] = "hidden-provision"
    data["inputs"]["dataset_revision"] = "sha256:" + "f" * 64
    hidden = service.ledger.submit_job(JobSpec.model_validate(data))
    deployment = DeploymentSpec(
        gpu_model="SIMULATED",
        image_digest="sha256:" + "c" * 64,
        volume_id="simulated-volume",
        volume_gb=100,
        region="simulation",
    )
    arguments = {
        "deployment": deployment.model_dump(mode="json"),
        "job_ids": [visible.job_id, hidden.job_id],
        "max_runtime_seconds": 60,
    }
    with pytest.raises(PermissionError):
        service.dispatch("request_gpu_provision", arguments)
    assert cloud.calls == []
    arguments["job_ids"] = [visible.job_id]
    assert service.dispatch("request_gpu_provision", arguments) == {"request_id": "pending-human-approval"}
    assert len(cloud.calls) == 1
    assert service.ledger.get_job(visible.job_id).state.value == "PENDING"
