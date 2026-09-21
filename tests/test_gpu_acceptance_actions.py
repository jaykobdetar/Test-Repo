"""Controller action authority, evidence races, and durable no-replay behavior."""
from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from probe_core.dispatcher import Dispatcher, TransportError
from probe_core.gpu_acceptance import AcceptanceCase, AcceptancePlan, collect
from probe_core.gpu_acceptance_actions import ActionError, ActionStore, _bounded_command, collect_action_evidence, digest, run_action
from probe_core.ledger import Ledger
from probe_core.schemas import JobSpec, RunManifest
from probe_core.worker_contracts import ExecutionReceipt, WorkerState
from test_ledger import approve, clock, job_factory, manifest_data


class ObservedWorker:
    """Fault-injectable observation boundary; actual signals have separate tests."""
    config_sha256 = "sha256:" + "a" * 64
    endpoint_identity = "sha256:" + "b" * 64

    def __init__(self, request, clock):
        self.request, self.clock = request, clock
        self.restart_calls = self.cancel_calls = self.inspections = 0
        self.on_inspect = self.on_restart = self.on_cancel = None
        self.receipt = ExecutionReceipt(job_id=request.job_id, attempt_id=request.attempt_id,
            state="RUNNING", started_at=clock())
        self.snapshot = {"request": request.model_dump(mode="json"),
            "request_sha256": digest(request.model_dump(mode="json")),
            "process_sha256": "sha256:" + "c" * 64,
            "execution_started_sha256": "sha256:" + "d" * 64,
            "loaded_config_sha256": "sha256:" + "e" * 64,
            "child": {"pid": 42, "identity": "101", "boot_id": "fixture-boot",
                      "deadline": request.deadline.timestamp(), "monotonic_deadline": 70.0, "cgroup": None},
            "child_alive": True, "receipt": self.receipt.model_dump(mode="json"),
            "supervisor": {"pid": 41, "identity": "100", "boot_id": "fixture-boot"},
            "bootstrap": {"pid": 1, "identity": "1", "boot_id": "fixture-boot"},
            "observed_at": clock().isoformat(), "observed_monotonic": 10.0}

    def inspect(self, request):
        assert request == self.request
        self.inspections += 1
        if self.on_inspect:
            self.on_inspect(self)
        self.snapshot["receipt"] = self.receipt.model_dump(mode="json")
        return deepcopy(self.snapshot)

    def status(self, attempt_id):
        assert attempt_id == self.request.attempt_id
        return self.receipt

    def restart(self, request, action_id, *, expected_before):
        assert request == self.request
        assert expected_before == self.snapshot
        self.restart_calls += 1
        before = deepcopy(self.snapshot)
        if self.on_restart:
            self.on_restart(self)
        else:
            self.snapshot["supervisor"] = {"pid": 43, "identity": "102", "boot_id": "fixture-boot"}
        return {"schema_version": 1, "operation": "restart", "action_id": action_id,
                "signal_sent": True, "replayed": False, "before": before, "outcome": "signal_sent"}

    def cancel(self, attempt_id):
        assert attempt_id == self.request.attempt_id
        self.cancel_calls += 1
        if self.on_cancel:
            return self.on_cancel(self)
        self.snapshot["child_alive"] = False
        self.receipt = self.receipt.model_copy(update={"state": WorkerState.CANCELLED, "finished_at": self.clock(),
            "process_stopped": True, "failure_kind": "cancelled"})
        return self.receipt


@pytest.fixture
def scenario(tmp_path, clock, job_factory):
    created = []
    def make(action="restart_supervisor_after_running"):
        spec = JobSpec.model_validate({**job_factory("case-" + str(len(created))).model_dump(mode="json"),
                                       "experiment_stage": "calibration"})
        case = AcceptanceCase(name="lifecycle", action=action,
            expected_state="FAILED" if action == "cancel_after_running" else "COMPLETED",
            expected_failure_kind="cancelled" if action == "cancel_after_running" else None, spec=spec)
        plan = AcceptancePlan(label="action-plan", model=spec.model, cases=(case,))
        ledger = Ledger(tmp_path / (str(len(created)) + ".sqlite"), clock=clock)
        created.append(ledger)
        pending = ledger.submit_job(spec)
        grant = approve(ledger, clock, [pending])
        dispatched = ledger.dispatch_next("worker-1", approval_id=grant.approval_id)
        job = ledger.start_job(dispatched.job_id, dispatched.attempt_id, "worker-1")
        request = Dispatcher(ledger, None, worker_id="worker-1", transfer_directory=tmp_path)._request(job)
        worker = ObservedWorker(request, clock)
        kwargs = dict(case_name=case.name, job_id=job.job_id, attempt_id=job.attempt_id,
            approval_id=grant.approval_id, client=worker, lifecycle=worker,
            action_directory=tmp_path / (str(len(created)) + "-actions"), clock=clock, observe_seconds=0.05)
        return ledger, plan, worker, kwargs
    yield make
    for ledger in created:
        ledger.close()


def test_restart_preserves_attempt_and_replay_never_signals(scenario):
    ledger, plan, worker, kwargs = scenario()
    audit = ledger.audit_records()
    result = run_action(ledger, plan, **kwargs)
    assert result["status"] == "passed"
    assert result["before"]["child"] == result["after"]["child"]
    assert result["before"]["supervisor"] != result["after"]["supervisor"]
    assert worker.restart_calls == 1 and worker.cancel_calls == 0
    assert run_action(ledger, plan, **kwargs) == result
    assert worker.restart_calls == 1 and ledger.audit_records() == audit
    assert not result["compute_started"] and not result["approval_consumed"]


@pytest.mark.parametrize("sql", [
    "UPDATE jobs SET lease_expires_at=0",
    "UPDATE attempts SET lease_expires_at=0",
    "UPDATE approvals SET ended_at=1",
    "UPDATE approvals SET consumed_at=NULL",
    "UPDATE approvals SET deadline=0",
    "UPDATE attempts SET stopped_at=1",
    "DELETE FROM approval_jobs",
])
def test_stale_or_unapproved_authority_refuses_before_any_mutation(scenario, sql):
    ledger, plan, worker, kwargs = scenario()
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute(sql)
        connection.commit()
    with pytest.raises(ActionError, match="RUNNING_ATTEMPT"):
        run_action(ledger, plan, **kwargs)
    assert worker.restart_calls == worker.cancel_calls == worker.inspections == 0


def test_approval_ended_during_preflight_does_not_signal(scenario):
    ledger, plan, worker, kwargs = scenario()
    def end_after_observation(item):
        if item.inspections == 2:
            # Inject inconsistent durable authority directly; the normal Ledger
            # correctly refuses ending an approval while a child is unresolved.
            with closing(sqlite3.connect(ledger.path)) as connection:
                connection.execute("UPDATE approvals SET ended_at=1")
                connection.commit()
    worker.on_inspect = end_after_observation
    result = run_action(ledger, plan, **kwargs)
    assert result["status"] == "inconclusive" and worker.restart_calls == 0
    replay = run_action(ledger, plan, **kwargs)
    assert replay["status"] == "uncertain" and worker.restart_calls == 0


def test_remote_monotonic_expiry_refuses_even_with_valid_controller_clock(scenario):
    ledger, plan, worker, kwargs = scenario()
    worker.snapshot["observed_monotonic"] = 71.0
    with pytest.raises(ActionError, match="RUNNING_EVIDENCE_INCONCLUSIVE"):
        run_action(ledger, plan, **kwargs)
    assert worker.restart_calls == 0


def test_lost_restart_response_is_never_reissued(scenario):
    ledger, plan, worker, kwargs = scenario()
    def lose_response(item):
        item.snapshot["supervisor"] = {"pid": 43, "identity": "102", "boot_id": "fixture-boot"}
        raise TransportError("private transport details must not escape")
    worker.on_restart = lose_response
    result = run_action(ledger, plan, **kwargs)
    assert result["status"] == "uncertain" and "private transport" not in json.dumps(result)
    assert run_action(ledger, plan, **kwargs)["status"] == "uncertain"
    assert worker.restart_calls == 1


def test_changed_process_deadline_cannot_pass_adoption(scenario):
    ledger, plan, worker, kwargs = scenario()
    def changed(item):
        item.snapshot["supervisor"]["pid"] = 43
        item.snapshot["child"]["monotonic_deadline"] += 1
    worker.on_restart = changed
    result = run_action(ledger, plan, **kwargs)
    assert result["status"] == "inconclusive"
    assert result["reason"] == "EXECUTION_AUTHORITY_CHANGED"


def test_slow_observation_cannot_report_pass_after_its_budget(scenario):
    ledger, plan, worker, kwargs = scenario()
    def slow(item):
        if item.inspections == 3:
            time.sleep(0.03)
    worker.on_inspect = slow
    result = run_action(ledger, plan, **dict(kwargs, observe_seconds=0.01))
    assert result["status"] == "uncertain" and worker.inspections == 3


def test_changed_supervisor_before_helper_action_must_be_refused(scenario):
    ledger, plan, worker, kwargs = scenario()
    def reject_expected(request, action_id, *, expected_before):
        assert expected_before["supervisor"]["pid"] == 41
        raise TransportError("helper refused a naturally replaced supervisor")
    worker.restart = reject_expected
    result = run_action(ledger, plan, **kwargs)
    assert result["status"] == "uncertain" and worker.restart_calls == 0


def test_late_cancellation_cannot_accept_a_successful_receipt(scenario, manifest_data):
    ledger, plan, worker, kwargs = scenario("cancel_after_running")
    def finish(item):
        item.snapshot["child_alive"] = False
        item.receipt = ExecutionReceipt(job_id=item.request.job_id, attempt_id=item.request.attempt_id,
            state="SUCCEEDED", started_at=item.clock(), finished_at=item.clock(),
            process_stopped=True, manifest=RunManifest.model_validate(manifest_data))
        return item.receipt
    worker.on_cancel = finish
    result = run_action(ledger, plan, **kwargs)
    assert result["status"] == "inconclusive" and worker.cancel_calls == 1
    assert run_action(ledger, plan, **kwargs)["status"] == "inconclusive"
    assert worker.cancel_calls == 1


def test_cancelled_receipt_without_actual_signal_proof_is_inconclusive(scenario):
    ledger, plan, worker, kwargs = scenario("cancel_after_running")
    result = run_action(ledger, plan, **kwargs)
    assert result["status"] == "inconclusive"
    assert result["reason"] == "CANCELLATION_SIGNAL_EVIDENCE_MISSING"


def add_cancel_proof(item):
    item.on_cancel = None
    receipt = item.cancel(item.request.attempt_id)
    item.cancel_calls -= 1  # The fixture creates evidence; only the outer RPC was issued.
    proof = {"schema_version": 1,
        **{key: getattr(item.request, key) for key in ("job_id", "attempt_id", "worker_id", "approval_id")},
        "request_sha256": digest(item.request.model_dump(mode="json")),
        "config_sha256": item.snapshot["loaded_config_sha256"], **item.snapshot["child"],
        "signal": "SIGKILL", "signal_scope": "process_group",
        "signal_sent_at": item.clock().isoformat(), "stopped_at": item.clock().isoformat(),
        "process_stopped": True, "job_scope_stopped": True, "result_present": False}
    item.snapshot.update(cancellation=proof, cancellation_sha256=digest(proof))
    return receipt


def test_actual_signal_evidence_passes_and_collects_without_new_actions(scenario):
    ledger, plan, worker, kwargs = scenario("cancel_after_running")
    worker.on_cancel = add_cancel_proof
    result = run_action(ledger, plan, **kwargs)
    assert result["status"] == "passed" and worker.cancel_calls == 1
    before_calls = worker.inspections
    evidence = collect_action_evidence(ledger, plan, kwargs["action_directory"])
    assert evidence["complete"] and evidence["cases"][0]["action_id"] == result["action_id"]
    assert worker.cancel_calls == 1 and worker.inspections == before_calls
    report = collect(ledger, plan, action_directory=kwargs["action_directory"])
    assert report["action_evidence"]["complete"]
    assert not report["lifecycle_acceptance_complete"]
    assert "cancellation requested after the exact attempt was observed running" not in report["remaining_evidence"]
    assert "provider-confirmed stop and separately approved restart/replacement" in report["remaining_evidence"]


@pytest.mark.parametrize("field,value", [
    ("pid", 99), ("request_sha256", "sha256:" + "f" * 64),
    ("config_sha256", "sha256:" + "f" * 64), ("job_scope_stopped", False),
    ("result_present", True), ("signal_scope", "child_only"),
])
def test_wrong_or_incomplete_cancellation_proof_cannot_pass(scenario, field, value):
    ledger, plan, worker, kwargs = scenario("cancel_after_running")
    def altered(item):
        receipt = add_cancel_proof(item)
        item.snapshot["cancellation"][field] = value
        return receipt
    worker.on_cancel = altered
    assert run_action(ledger, plan, **kwargs)["status"] == "inconclusive"


def test_collector_rejects_tampered_completed_action_transcript(scenario):
    ledger, plan, worker, kwargs = scenario()
    result = run_action(ledger, plan, **kwargs)
    assert collect_action_evidence(ledger, plan, kwargs["action_directory"])["complete"]
    path = kwargs["action_directory"] / (result["action_id"] + ".result.json")
    result["after"]["supervisor"] = result["before"]["supervisor"]
    path.write_text(json.dumps(result))
    assert not collect_action_evidence(ledger, plan, kwargs["action_directory"])["complete"]


def test_collector_does_not_create_a_missing_action_directory(scenario, tmp_path):
    ledger, plan, _, _ = scenario()
    missing = tmp_path / "absent-actions"
    assert not collect_action_evidence(ledger, plan, missing)["complete"]
    assert not missing.exists()


def test_reused_job_with_different_attempt_is_never_cancelled(scenario):
    ledger, plan, worker, kwargs = scenario("cancel_after_running")
    with pytest.raises(ActionError, match="ATTEMPT_MISMATCH"):
        run_action(ledger, plan, **dict(kwargs, attempt_id="different-attempt"))
    assert worker.cancel_calls == worker.inspections == 0


def test_partial_intent_is_never_overwritten_or_replayed(scenario):
    ledger, plan, worker, kwargs = scenario()
    worker.on_restart = lambda _: (_ for _ in ()).throw(TransportError("lost"))
    first = run_action(ledger, plan, **kwargs)
    path = kwargs["action_directory"] / (first["action_id"] + ".intent.json")
    path.write_bytes(b'{"interrupted":')
    with pytest.raises(ValueError):
        run_action(ledger, plan, **kwargs)
    assert worker.restart_calls == 1 and path.read_bytes() == b'{"interrupted":'


def test_nonobject_result_refuses_replay_and_collection(scenario):
    ledger, plan, worker, kwargs = scenario()
    first = run_action(ledger, plan, **kwargs)
    path = kwargs["action_directory"] / (first["action_id"] + ".result.json")
    path.write_text("[]")
    with pytest.raises(ActionError, match="RECORD_INVALID"):
        run_action(ledger, plan, **kwargs)
    assert worker.restart_calls == 1
    assert not collect_action_evidence(ledger, plan, kwargs["action_directory"])["complete"]


def test_new_store_fsyncs_parent_before_first_intent(tmp_path, monkeypatch):
    import probe_core.gpu_acceptance_actions as actions
    calls = []
    original = os.fsync
    def observe(fd):
        calls.append(Path(os.readlink('/proc/self/fd/' + str(fd))))
        return original(fd)
    monkeypatch.setattr(actions.os, "fsync", observe)
    store = ActionStore(tmp_path / "actions")
    try:
        store.publish("a" * 64, "intent", {"value": 1})
    finally:
        store.close()
    assert calls[0] == tmp_path
    assert calls[-1] == tmp_path / "actions"


def test_action_store_rejects_symlink_directory_and_shared_file(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    with pytest.raises(OSError):
        ActionStore(link)
    store = ActionStore(private)
    try:
        store.publish("a" * 64, "intent", {"x": 1})
        os.link(private / ("a" * 64 + ".intent.json"), tmp_path / "second-link")
        with pytest.raises(ActionError, match="UNSAFE"):
            store.read("a" * 64, "intent")
    finally:
        store.close()


@pytest.mark.parametrize("program,code", [
    ("import sys; sys.stdout.buffer.write(b'x' * 300000)", "TOO_LARGE"),
    ("import time; time.sleep(5)", "UNCERTAIN"),
    ("print('not JSON')", "INVALID"),
])
def test_helper_transport_bounds_output_and_lifetime(program, code):
    with pytest.raises(TransportError, match=code):
        _bounded_command([sys.executable, "-I", "-c", program], {}, timeout=0.3)
