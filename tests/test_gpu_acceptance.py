from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from probe_core.dispatcher import Dispatcher, TransportError
from probe_core.gpu_acceptance import AcceptanceCase, AcceptancePlan, collect, make_plan, submit
from probe_core.ledger import Ledger
from probe_core.schemas import RunManifest
from probe_core.worker_contracts import ExecutionReceipt, WorkerConfig
from test_ledger import approve, clock, job_factory
from test_schemas import manifest_data


def test_plan_requires_live_canonical_config_and_submits_no_approval(manifest_data, tmp_path):
    import hashlib

    dataset = tmp_path / "public-calibration-prompts.json"
    dataset.write_text(
        json.dumps(
            {
                "prompts": [
                    {"prompt_id": "short", "text": "The capital of France is"},
                    {"prompt_id": "long", "text": "A quiet garden has three red flowers. Describe their color."},
                ]
            }
        )
    )
    config = WorkerConfig(
        model_directory=str(tmp_path / "models"),
        model=manifest_data["model"],
        assets=[{"path": "config.json", "sha256": "sha256:" + "a" * 64}],
        datasets=[{"path": str(dataset), "sha256": "sha256:" + hashlib.sha256(dataset.read_bytes()).hexdigest()}],
        tensor_directory=str(tmp_path / "tensors"),
        output_directory=str(tmp_path / "output"),
        device="cuda:0",
        backend="nnsight",
        code_git_commit="a" * 40,
        container_image_digest="sha256:" + "b" * 64,
        provider_backend="runpod",
        region="test-fixture",
        live_price_usd_per_hour=0.74,
        cgroup_directory="/not-a-real-cgroup-test-fixture",
    )
    plan = make_plan(config, "base-acceptance")
    assert len(plan.cases) == 8
    assert plan.cases[0].spec.operation.kind == "backend_parity"
    assert all(case.spec.experiment_stage.value == "calibration" for case in plan.cases)
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        queued = submit(ledger, plan)
        assert not queued["approval_consumed"] and not queued["compute_started"]
        assert len(queued["job_ids"]) == 8
        observed = collect(ledger, plan)
        assert not observed["case_results_passed"]
        assert not observed["lifecycle_acceptance_complete"]
        assert not observed["scientific_evidence"]
        assert all(case["reason"] == "no execution attempt exists" for case in observed["cases"])


def test_worker_config_bootstrap_rejects_unsafe_ownership(tmp_path, monkeypatch):
    from probe_core.gpu_launch import _private_worker_file

    path = tmp_path / "config"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        _private_worker_file(path)


def test_path_preflight_rejects_worker_owned_model_tree_before_loading(tmp_path):
    from types import SimpleNamespace
    from probe_core.gpu_launch import check_worker_paths

    model = tmp_path / "model"
    model.mkdir()
    with pytest.raises(ValueError, match="must not own"):
        check_worker_paths(SimpleNamespace(model_directory=str(model)), token_path=tmp_path / "unused-token")


@pytest.fixture
def cancellation_execution(tmp_path, clock, job_factory):
    spec = job_factory("cancellation-acceptance")
    plan = AcceptancePlan(
        label="cancellation-acceptance",
        model=spec.model,
        cases=(
            AcceptanceCase(
                name="cancel-running",
                action="cancel_after_running",
                expected_state="FAILED",
                expected_failure_kind="cancelled",
                spec=spec,
            ),
        ),
    )
    with Ledger(tmp_path / "cancellation.sqlite", clock=clock) as ledger:
        pending = ledger.submit_job(spec)
        grant = approve(ledger, clock, [pending])
        dispatched = ledger.dispatch_next("worker-1", approval_id=grant.approval_id)
        job = ledger.start_job(dispatched.job_id, dispatched.attempt_id, "worker-1")
        yield ledger, plan, job, clock


class ReceiptEndpoint:
    """An already stopped worker returns its original outcome on late cancel."""

    def __init__(self, receipt):
        self.receipt = receipt
        self.queries = []
        self.cancellations = []

    def status(self, attempt_id):
        self.queries.append(attempt_id)
        return self.receipt

    def cancel(self, attempt_id):
        self.cancellations.append(attempt_id)
        return self.receipt


def cancelled_receipt(job, clock, **changes):
    values = dict(
        job_id=job.job_id,
        attempt_id=job.attempt_id,
        state="CANCELLED",
        started_at=clock(),
        finished_at=clock(),
        failure_kind="cancelled",
        process_stopped=True,
    )
    return ExecutionReceipt(**dict(values, **changes))


def record_ledger_cancellation(ledger, job):
    ledger.cancel_job(job.job_id)
    ledger.confirm_stopped(job.job_id, job.attempt_id)


def test_completed_before_cancel_receipt_cannot_pass(cancellation_execution, manifest_data, tmp_path):
    ledger, plan, job, clock = cancellation_execution
    endpoint = ReceiptEndpoint(
        cancelled_receipt(
            job, clock, state="SUCCEEDED", failure_kind=None, manifest=RunManifest.model_validate(manifest_data)
        )
    )
    # Reproduce the actual dispatcher race: a successful, stopped worker receipt
    # is enough for the current dispatcher to record local FAILED/cancelled.
    dispatcher = Dispatcher(ledger, endpoint, worker_id="worker-1", transfer_directory=tmp_path / "transfers")
    result = dispatcher.cancel(job.job_id)
    assert result.state.value == "FAILED" and result.failure_kind == "cancelled"
    assert endpoint.cancellations == [job.attempt_id]

    report = collect(ledger, plan, endpoint)
    row = report["cases"][0]
    assert not report["case_results_passed"] and not row["passed"]
    assert row["cancellation_observation"] == "inconclusive"
    assert row["observed_receipt"]["state"] == "SUCCEEDED"
    assert row["reason"] == "worker receipt does not confirm cancellation of the exact attempt"
    assert endpoint.queries == [job.attempt_id]


def test_ledger_only_cancellation_is_inconclusive(cancellation_execution):
    ledger, plan, job, _ = cancellation_execution
    record_ledger_cancellation(ledger, job)
    report = collect(ledger, plan)
    row = report["cases"][0]
    assert row["state"] == "FAILED" and row["failure_kind"] == "cancelled"
    assert row["process_stopped_at"] is not None
    assert not row["passed"] and not report["case_results_passed"]
    assert row["cancellation_observation"] == "inconclusive"
    assert "actual worker receipt" in row["reason"]


def test_worker_confirmed_cancellation_passes_outcome_only(cancellation_execution, tmp_path):
    ledger, plan, job, clock = cancellation_execution
    endpoint = ReceiptEndpoint(cancelled_receipt(job, clock))
    Dispatcher(ledger, endpoint, worker_id="worker-1", transfer_directory=tmp_path / "transfers").cancel(job.job_id)
    audit_before = ledger.audit_records()
    report = collect(ledger, plan, endpoint)
    row = report["cases"][0]
    assert row["passed"] and report["case_results_passed"]
    assert row["cancellation_observation"] == "confirmed"
    assert row["observed_receipt"] == endpoint.receipt.model_dump(mode="json")
    assert endpoint.queries == [job.attempt_id]
    assert endpoint.cancellations == [job.attempt_id]
    assert ledger.audit_records() == audit_before
    assert not report["scientific_evidence"]
    assert not report["lifecycle_acceptance_complete"]
    assert "cancellation requested after the exact attempt was observed running" in report["remaining_evidence"]


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": "another-job"},
        {"attempt_id": "another-attempt"},
        {"process_stopped": False},
        {"state": "FAILED"},
        {"failure_kind": "timeout"},
    ],
)
def test_cancellation_requires_exact_worker_outcome(cancellation_execution, changes):
    ledger, plan, job, clock = cancellation_execution
    record_ledger_cancellation(ledger, job)
    report = collect(ledger, plan, ReceiptEndpoint(cancelled_receipt(job, clock, **changes)))
    assert not report["case_results_passed"]
    assert not report["cases"][0]["passed"]
    assert report["cases"][0]["cancellation_observation"] == "inconclusive"


def test_unavailable_worker_cancellation_receipt_is_inconclusive(cancellation_execution):
    ledger, plan, job, _ = cancellation_execution
    record_ledger_cancellation(ledger, job)

    class UnavailableEndpoint:
        def status(self, attempt_id):
            assert attempt_id == job.attempt_id
            raise TransportError("synthetic unavailable worker; never print this raw exception")

    report = collect(ledger, plan, UnavailableEndpoint())
    row = report["cases"][0]
    assert not row["passed"] and not report["case_results_passed"]
    assert row["cancellation_observation"] == "inconclusive"
    assert row["reason"] == "worker receipt is unavailable or invalid"
    assert "synthetic" not in json.dumps(report)
