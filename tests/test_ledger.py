"""Behavioral tests for durable queue, approval and scientific-ledger invariants."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from probe_core.audit import AuditLog, SecretDetectedError
import probe_core.ledger as ledger_module
from probe_core.ledger import (
    ApprovalError,
    ArtifactError,
    IdempotencyConflict,
    InvalidTransition,
    LeaseError,
    Ledger,
    LedgerError,
    NotFoundError,
    RetryNotAllowed,
)
from probe_core.schemas import ApprovalNonce, HypothesisRecord, JobSpec, RunManifest


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def manifest_data() -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / "manifest.json").read_text())


@pytest.fixture
def job_factory(manifest_data):
    def make(key: str = "job-1", *, runtime: int = 60, seed: int = 12345) -> JobSpec:
        inputs = dict(manifest_data["inputs"])
        inputs["random_seed"] = seed
        return JobSpec(
            idempotency_key=key,
            model=manifest_data["model"],
            inputs=inputs,
            operation={
                "kind": "capture",
                "modules": [{"layer": 0, "component": "residual"}],
                "positions": ["last"],
            },
            limits={"max_runtime_seconds": runtime, "max_output_bytes": 1000000},
        )

    return make


@pytest.fixture
def ledger(tmp_path, clock):
    with Ledger(tmp_path / "research.sqlite", clock=clock) as instance:
        yield instance


def approve(ledger, clock, jobs, *, approval_id="wake-1", runtime=300, token="test-nonce-" + "a" * 40):
    nonce = ApprovalNonce(
        approval_id=approval_id,
        token=token,
        pod_id="pod-1",
        batch_hash=ledger.batch_hash([job.job_id for job in jobs]),
        max_runtime_seconds=runtime,
        price_ceiling_usd_per_hour=1.20,
        issued_at=clock(),
        expires_at=clock() + timedelta(minutes=5),
    )
    ledger.register_approval(nonce)
    grant = ledger.consume_approval(
        approval_id,
        token,
        pod_id="pod-1",
        job_ids=[job.job_id for job in jobs],
        live_price_usd_per_hour=1.0,
        requested_runtime_seconds=runtime,
    )
    return grant


def dispatch(ledger, clock, job_factory, *, lease=30, runtime=60):
    submitted = ledger.submit_job(job_factory(runtime=runtime))
    grant = approve(ledger, clock, [submitted])
    dispatched = ledger.dispatch_next("worker-1", approval_id=grant.approval_id, lease_seconds=lease)
    assert dispatched is not None
    return dispatched, grant


def state(record) -> str:
    return str(record.state)


def test_initialization_sets_required_sqlite_pragmas(ledger):
    with ledger.read_connection() as reader:
        assert reader.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
        assert reader.execute("PRAGMA busy_timeout;").fetchone()[0] == 5000
        assert reader.execute("PRAGMA synchronous;").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("CREATE TABLE unauthorized_write (id INTEGER)")


def test_concurrent_submission_is_durable_and_idempotent(ledger, job_factory):
    shared = job_factory()
    start = threading.Barrier(12)

    def submit(_):
        start.wait(timeout=10)
        return ledger.submit_job(shared)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(submit, range(12)))
    assert len({result.job_id for result in results}) == 1
    assert len(ledger.list_jobs()) == 1
    assert all(state(result) == "PENDING" for result in results)
    assert ledger.get_job(results[0].job_id).spec == shared


def test_same_idempotency_key_with_different_spec_is_rejected(ledger, job_factory):
    original = ledger.submit_job(job_factory(seed=1))
    with pytest.raises(IdempotencyConflict):
        ledger.submit_job(job_factory(seed=2))
    assert ledger.get_job(original.job_id).spec.inputs.random_seed == 1
    assert len(ledger.list_jobs()) == 1


def test_distinct_replications_get_distinct_job_ids(ledger, job_factory):
    first = ledger.submit_job(job_factory("replication-1"))
    second = ledger.submit_job(job_factory("replication-2"))
    assert first.job_id != second.job_id


def test_read_snapshot_does_not_block_writer_commit(ledger, job_factory):
    ledger.submit_job(job_factory("first"))
    with ledger.read_connection() as reader:
        reader.execute("BEGIN")
        before = reader.execute("SELECT count(*) FROM jobs").fetchone()[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(ledger.submit_job, job_factory("second")).result(timeout=5)
        assert state(result) == "PENDING"
        assert reader.execute("SELECT count(*) FROM jobs").fetchone()[0] == before
        reader.execute("COMMIT")
        assert reader.execute("SELECT count(*) FROM jobs").fetchone()[0] == before + 1


def test_jobs_and_execution_leases_survive_reopen(tmp_path, clock, job_factory):
    database = tmp_path / "restart.sqlite"
    with Ledger(database, clock=clock) as first:
        record, grant = dispatch(first, clock, job_factory)
        first.start_job(record.job_id, record.attempt_id, "worker-1")
    with Ledger(database, clock=clock) as reopened:
        persisted = reopened.get_job(record.job_id)
        assert state(persisted) == "RUNNING"
        assert persisted.attempt_id == record.attempt_id
        assert reopened.submit_job(job_factory()).job_id == record.job_id
        assert reopened.dispatch_next("worker-2", approval_id=grant.approval_id) is None
        reopened.heartbeat(record.job_id, record.attempt_id, "worker-1", lease_seconds=30)


def test_two_ledger_instances_cannot_dispatch_simultaneous_executions(tmp_path, clock, job_factory):
    database = tmp_path / "shared.sqlite"
    with Ledger(database, clock=clock) as first, Ledger(database, clock=clock) as second:
        jobs = [first.submit_job(job_factory(f"job-{i}")) for i in range(2)]
        grant = approve(first, clock, jobs)
        start = threading.Barrier(2)

        def claim(pair):
            client, worker = pair
            start.wait(timeout=10)
            return client.dispatch_next(worker, approval_id=grant.approval_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, [(first, "worker-1"), (second, "worker-2")]))
        assert sum(record is not None for record in claims) == 1
        assert sorted(state(job) for job in first.list_jobs()) == ["DISPATCHED", "PENDING"]


def test_state_machine_rejects_skipped_and_backward_transitions(ledger, clock, job_factory):
    record, _ = dispatch(ledger, clock, job_factory)
    with pytest.raises(InvalidTransition):
        ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    running = ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    assert state(running) == "RUNNING"
    assert ledger.start_job(record.job_id, record.attempt_id, "worker-1") == running
    finalizing = ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    assert state(finalizing) == "FINALIZING"
    with pytest.raises(InvalidTransition):
        ledger.start_job(record.job_id, record.attempt_id, "worker-1")


@pytest.mark.parametrize("method", ["start_job", "heartbeat", "begin_finalization"])
def test_wrong_worker_and_attempt_are_fenced(ledger, clock, job_factory, method):
    record, _ = dispatch(ledger, clock, job_factory)
    action = getattr(ledger, method)
    with pytest.raises(LeaseError):
        action(record.job_id, record.attempt_id, "other-worker")
    with pytest.raises(LeaseError):
        action(record.job_id, "nonexistent-attempt", "worker-1")
    assert state(ledger.get_job(record.job_id)) == "DISPATCHED"


def test_expired_heartbeat_cannot_revive_execution(ledger, clock, job_factory):
    record, _ = dispatch(ledger, clock, job_factory, lease=10)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    clock.advance(10)
    with pytest.raises(LeaseError):
        ledger.heartbeat(record.job_id, record.attempt_id, "worker-1", lease_seconds=30)
    recovered = ledger.recover_expired()
    assert [job.job_id for job in recovered] == [record.job_id]
    assert state(ledger.get_job(record.job_id)) == "FAILED"
    assert ledger.recover_expired() == []


@pytest.mark.parametrize("execution_state", ["DISPATCHED", "RUNNING", "FINALIZING"])
def test_every_execution_state_is_subject_to_lease_expiration(ledger, clock, job_factory, execution_state):
    record, _ = dispatch(ledger, clock, job_factory, lease=10)
    if execution_state != "DISPATCHED":
        ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    if execution_state == "FINALIZING":
        ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    clock.advance(11)
    recovered = ledger.recover_expired()
    assert len(recovered) == 1
    assert state(recovered[0]) == "FAILED"


def test_heartbeat_extends_lease_but_cannot_extend_job_runtime(ledger, clock, job_factory):
    record, _ = dispatch(ledger, clock, job_factory, lease=10, runtime=20)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    clock.advance(5)
    alive = ledger.heartbeat(record.job_id, record.attempt_id, "worker-1", lease_seconds=30)
    assert alive.lease_expires_at <= datetime(2026, 9, 19, 12, 0, 20, tzinfo=timezone.utc)
    clock.advance(15)
    assert len(ledger.recover_expired()) == 1
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    with pytest.raises(RetryNotAllowed):
        ledger.retry_job(record.job_id, record.attempt_id)


def test_failed_execution_blocks_all_dispatch_until_confirmed_stopped(ledger, clock, job_factory):
    jobs = [ledger.submit_job(job_factory(f"job-{i}")) for i in range(2)]
    grant = approve(ledger, clock, jobs)
    first = ledger.dispatch_next("worker-1", approval_id=grant.approval_id, lease_seconds=10)
    clock.advance(11)
    ledger.recover_expired()
    assert ledger.dispatch_next("worker-2", approval_id=grant.approval_id) is None
    with pytest.raises(RetryNotAllowed):
        ledger.retry_job(first.job_id, first.attempt_id)
    ledger.confirm_stopped(first.job_id, first.attempt_id)
    second = ledger.dispatch_next("worker-2", approval_id=grant.approval_id)
    assert second is not None
    assert second.job_id != first.job_id


def test_one_infrastructure_retry_is_idempotent_and_fences_old_attempt(ledger, clock, job_factory):
    original, grant = dispatch(ledger, clock, job_factory, lease=10)
    clock.advance(11)
    ledger.recover_expired()
    ledger.confirm_stopped(original.job_id, original.attempt_id)
    retried = ledger.retry_job(original.job_id, original.attempt_id)
    assert state(retried) == "PENDING"
    assert retried.job_id == original.job_id
    assert retried.retry_count == 1
    assert ledger.retry_job(original.job_id, original.attempt_id).job_id == original.job_id
    assert ledger.get_job(original.job_id).retry_count == 1
    replacement = ledger.dispatch_next("worker-2", approval_id=grant.approval_id, lease_seconds=10)
    assert replacement.attempt_id != original.attempt_id
    assert replacement.attempt_count == 2
    with pytest.raises(LeaseError):
        ledger.heartbeat(original.job_id, original.attempt_id, "worker-1")
    clock.advance(11)
    ledger.recover_expired()
    ledger.confirm_stopped(replacement.job_id, replacement.attempt_id)
    with pytest.raises(RetryNotAllowed):
        ledger.retry_job(replacement.job_id, replacement.attempt_id)


@pytest.mark.parametrize("kind", ["scientific", "oom"])
def test_scientific_failure_and_oom_are_never_retried(ledger, clock, job_factory, kind):
    record, _ = dispatch(ledger, clock, job_factory)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    failed = ledger.fail_job(
        record.job_id, record.attempt_id, "worker-1", failure_kind=kind, reason="experiment did not succeed"
    )
    assert state(failed) == "FAILED"
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    with pytest.raises(RetryNotAllowed):
        ledger.retry_job(record.job_id, record.attempt_id)


def test_retry_requires_remaining_approved_time(ledger, clock, job_factory):
    record, _ = dispatch(ledger, clock, job_factory, lease=10)
    clock.advance(11)
    ledger.recover_expired()
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    clock.advance(250)
    with pytest.raises((RetryNotAllowed, ApprovalError)):
        ledger.retry_job(record.job_id, record.attempt_id)


def test_pending_cancellation_is_terminal(ledger, job_factory):
    original = ledger.submit_job(job_factory())
    canceled = ledger.cancel_job(original.job_id, "operator canceled before dispatch")
    assert state(canceled) == "FAILED"
    with pytest.raises(RetryNotAllowed):
        ledger.retry_job(original.job_id, original.attempt_id)


def test_unknown_job_fails_explicitly(ledger):
    with pytest.raises(NotFoundError):
        ledger.get_job("unknown-job")


def test_approval_nonce_is_one_time_and_secret_is_not_persisted(ledger, clock, job_factory, tmp_path):
    job = ledger.submit_job(job_factory())
    secret = "a-sensitive-one-time-value-" + "9" * 32
    approve(ledger, clock, [job], token=secret)
    with pytest.raises(ApprovalError):
        ledger.consume_approval(
            "wake-1",
            secret,
            pod_id="pod-1",
            job_ids=[job.job_id],
            live_price_usd_per_hour=1.0,
            requested_runtime_seconds=300,
        )
    ledger.sync_audit()
    for path in tmp_path.iterdir():
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def make_nonce(ledger, clock, jobs, *, expires_in=300):
    return ApprovalNonce(
        approval_id="wake-1",
        token="test-nonce-" + "a" * 40,
        pod_id="pod-1",
        batch_hash=ledger.batch_hash([job.job_id for job in jobs]),
        max_runtime_seconds=300,
        price_ceiling_usd_per_hour=1.20,
        issued_at=clock(),
        expires_at=clock() + timedelta(seconds=expires_in),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"token": "wrong-token-" + "b" * 40},
        {"pod_id": "other-pod"},
        {"live_price_usd_per_hour": 1.21},
        {"live_price_usd_per_hour": 1.5},
        {"requested_runtime_seconds": 301},
        {"job_ids": []},
    ],
)
def test_approval_boundaries_are_enforced_without_consuming_nonce(ledger, clock, job_factory, change):
    job = ledger.submit_job(job_factory())
    nonce = make_nonce(ledger, clock, [job])
    ledger.register_approval(nonce)
    arguments = dict(
        token=nonce.token.get_secret_value(),
        pod_id="pod-1",
        job_ids=[job.job_id],
        live_price_usd_per_hour=1.0,
        requested_runtime_seconds=300,
    )
    arguments.update(change)
    with pytest.raises(ApprovalError):
        ledger.consume_approval("wake-1", **arguments)
    arguments = dict(
        token=nonce.token.get_secret_value(),
        pod_id="pod-1",
        job_ids=[job.job_id],
        live_price_usd_per_hour=1.0,
        requested_runtime_seconds=300,
    )
    assert ledger.consume_approval("wake-1", **arguments).approval_id == "wake-1"


def test_expired_approval_cannot_be_consumed(ledger, clock, job_factory):
    job = ledger.submit_job(job_factory())
    nonce = make_nonce(ledger, clock, [job], expires_in=10)
    ledger.register_approval(nonce)
    clock.advance(10)
    with pytest.raises(ApprovalError):
        ledger.consume_approval(
            "wake-1",
            nonce.token.get_secret_value(),
            pod_id="pod-1",
            job_ids=[job.job_id],
            live_price_usd_per_hour=1.0,
            requested_runtime_seconds=300,
        )


def test_racing_approval_consumers_have_exactly_one_winner(ledger, clock, job_factory):
    job = ledger.submit_job(job_factory())
    nonce = make_nonce(ledger, clock, [job])
    ledger.register_approval(nonce)
    start = threading.Barrier(8)

    def consume(_):
        start.wait(timeout=10)
        try:
            return ledger.consume_approval(
                "wake-1",
                nonce.token.get_secret_value(),
                pod_id="pod-1",
                job_ids=[job.job_id],
                live_price_usd_per_hour=1.0,
                requested_runtime_seconds=300,
            )
        except ApprovalError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(consume, range(8)))
    assert sum(value is not None for value in outcomes) == 1


def test_approval_deadline_persists_and_is_not_renewed_by_activity(ledger, clock, job_factory):
    job = ledger.submit_job(job_factory(runtime=60))
    grant = approve(ledger, clock, [job], runtime=60)
    deadline = grant.deadline
    record = ledger.dispatch_next("worker-1", approval_id="wake-1", lease_seconds=60)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    clock.advance(30)
    alive = ledger.heartbeat(record.job_id, record.attempt_id, "worker-1", lease_seconds=60)
    assert alive.lease_expires_at <= deadline
    clock.advance(30)
    assert len(ledger.recover_expired()) == 1
    with pytest.raises(ApprovalError):
        ledger.dispatch_next("worker-2", approval_id="wake-1")


def test_hypothesis_freeze_is_durable_and_backward_transitions_are_rejected(ledger, clock, manifest_data, job_factory):
    draft = HypothesisRecord(
        preregistration_plan={
            "model": manifest_data["model"],
            "operation": job_factory().operation,
            "primary_metric": "target_behavior_delta",
            "minimum_effect": 0.01,
            "controls": manifest_data["controls"],
        },
        hypothesis_id="H-1",
        proposition="Layer zero mediates the measured contrast",
        alignment_relevance="Tests causal influence over the behavioral contrast",
        predicted_causal_intervention="Capture then patch residual states at layer zero",
        predicted_direction="increase",
        predictions=["Patching increases the primary score"],
        falsifier="The score remains unchanged under matched interventions",
    )
    ledger.register_hypothesis(draft)
    frozen = ledger.transition_hypothesis("H-1", "FROZEN")
    assert frozen.preregistration_hash.startswith("sha256:")
    assert frozen.frozen_at == clock()
    assert ledger.get_hypothesis("H-1") == frozen
    with pytest.raises(InvalidTransition):
        ledger.transition_hypothesis("H-1", "DRAFT")
    testing = ledger.transition_hypothesis("H-1", "TESTING")
    assert testing.preregistration_hash == frozen.preregistration_hash
    falsified = ledger.transition_hypothesis("H-1", "FALSIFIED")
    assert str(falsified.status) == "FALSIFIED"
    with pytest.raises(InvalidTransition):
        ledger.transition_hypothesis("H-1", "TESTING")


def test_hypothesis_cannot_be_validated_without_replication(ledger, manifest_data, job_factory):
    draft = HypothesisRecord(
        preregistration_plan={
            "model": manifest_data["model"],
            "operation": job_factory().operation,
            "primary_metric": "target_behavior_delta",
            "minimum_effect": 0.01,
            "controls": manifest_data["controls"],
        },
        hypothesis_id="H-2",
        proposition="An effect exists",
        alignment_relevance="Behavioral relevance",
        predicted_causal_intervention="Ablate the hypothesized component",
        predicted_direction="decrease",
        predictions=["The score decreases"],
        falsifier="The score stays the same",
    )
    ledger.register_hypothesis(draft)
    ledger.transition_hypothesis("H-2", "FROZEN")
    ledger.transition_hypothesis("H-2", "TESTING")
    with pytest.raises(LedgerError):
        ledger.transition_hypothesis("H-2", "VALIDATED")


def test_audit_records_are_transactional_and_hash_previous_record(ledger, clock, job_factory):
    job = ledger.submit_job(job_factory())
    approve(ledger, clock, [job])
    dispatched = ledger.dispatch_next("worker-1", approval_id="wake-1")
    ledger.start_job(dispatched.job_id, dispatched.attempt_id, "worker-1")
    records = ledger.audit_records()
    assert len(records) >= 4
    previous = "0" * 64
    for index, record in enumerate(records, start=1):
        assert record["sequence"] == index
        assert record["previous_hash"] == previous
        content = {key: value for key, value in record.items() if key != "hash"}
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert record["hash"] == hashlib.sha256(encoded.encode()).hexdigest()
        previous = record["hash"]


def test_audit_export_failure_does_not_lose_committed_job_or_events(tmp_path, clock, job_factory, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    with Ledger(tmp_path / "outbox.sqlite", audit_path=audit_path, clock=clock) as instance:
        original_sync = AuditLog.sync_records

        def disk_unavailable(self, records):
            raise OSError("simulated storage failure")

        with monkeypatch.context() as patch:
            patch.setattr(AuditLog, "sync_records", disk_unavailable)
            created = instance.submit_job(job_factory())
            assert instance.get_job(created.job_id).job_id == created.job_id
            committed = instance.audit_records()
            assert committed
            assert instance.audit_export_error is not None
        assert AuditLog.sync_records is original_sync
        instance.sync_audit()
        assert instance.audit_export_error is None
        exported = [json.loads(line) for line in audit_path.read_text().splitlines()]
        assert exported == instance.audit_records()
        instance.sync_audit()
        assert [json.loads(line) for line in audit_path.read_text().splitlines()] == exported


def test_reopen_reconciles_audit_export_from_authoritative_database(tmp_path, clock, job_factory, monkeypatch):
    database = tmp_path / "recover-outbox.sqlite"
    audit_path = tmp_path / "recover-audit.jsonl"
    with Ledger(database, audit_path=audit_path, clock=clock) as instance:
        with monkeypatch.context() as patch:

            def disk_unavailable(self, records):
                raise OSError("simulated storage failure")

            patch.setattr(AuditLog, "sync_records", disk_unavailable)
            created = instance.submit_job(job_factory())
            expected = instance.audit_records()
            instance.close()
    with Ledger(database, audit_path=audit_path, clock=clock) as reopened:
        assert reopened.get_job(created.job_id).job_id == created.job_id
        assert reopened.audit_export_error is None
        assert [json.loads(line) for line in audit_path.read_text().splitlines()] == expected


def prepare_manifest(manifest_data, record, artifact_root, clock, *, content=b"validated result\n"):
    """Use real bytes and provenance matching the queued operation."""
    data = json.loads(json.dumps(manifest_data))
    data["run"].update(
        run_id=record.job_id,
        parent_run_id=None,
        started_at=clock().isoformat(),
        experiment_stage="exploratory",
        hypothesis_id=None,
        preregistration_hash=None,
        approval_id=record.approval_id,
        replicator_blinded=False,
    )
    data["model"] = record.spec.model.model_dump(mode="json")
    data["inputs"] = record.spec.inputs.model_dump(mode="json")
    data["experiment"].update(tool="capture_activation", modules=["model.layers.0"], positions=["last"])
    normalized = json.dumps(
        record.spec.operation.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    data["experiment"]["intervention_hash"] = "sha256:" + hashlib.sha256(normalized.encode()).hexdigest()
    data["hardware"]["live_price_usd_per_hour"] = 1.0
    data["results"]["heldout"] = False
    data["results"]["replication_status"] = "not_applicable"
    data["cost"] = {"gpu_seconds": 0, "estimated_compute_usd": 0.0, "bytes_persisted": len(content)}
    data["artifacts"] = [
        {"path": "artifacts/result.txt", "sha256": hashlib.sha256(content).hexdigest(), "retention_class": "validated"}
    ]
    directory = artifact_root / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.txt").write_bytes(content)
    return RunManifest.model_validate(data)


def test_completed_job_atomically_persists_manifest_and_releases_gpu(
    ledger, clock, job_factory, manifest_data, tmp_path
):
    jobs = [ledger.submit_job(job_factory(f"completion-{index}")) for index in range(2)]
    grant = approve(ledger, clock, jobs)
    record = ledger.dispatch_next("worker-1", approval_id=grant.approval_id)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    artifact_root = tmp_path / "results"
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    manifest = prepare_manifest(manifest_data, record, artifact_root, clock)
    completed = ledger.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=artifact_root)
    assert state(completed) == "COMPLETED"
    assert ledger.get_manifest(record.job_id) == manifest
    assert ledger.get_job(record.job_id).spec == record.spec
    with pytest.raises(LedgerError):
        ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    with pytest.raises(RetryNotAllowed):
        ledger.retry_job(record.job_id, record.attempt_id)
    repeat = ledger.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=artifact_root)
    assert state(repeat) == "COMPLETED"
    next_job = ledger.dispatch_next("worker-2", approval_id=grant.approval_id)
    assert next_job is not None
    assert next_job.job_id != record.job_id


@pytest.mark.parametrize("corruption", ["missing", "modified", "symlink"])
def test_artifact_verification_failure_preserves_finalizing_state(
    ledger, clock, job_factory, manifest_data, tmp_path, corruption
):
    record, _ = dispatch(ledger, clock, job_factory)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    artifact_root = tmp_path / "results"
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    manifest = prepare_manifest(manifest_data, record, artifact_root, clock)
    artifact = artifact_root / "artifacts" / "result.txt"
    if corruption == "modified":
        artifact.write_bytes(b"changed after hashing")
    else:
        content = artifact.read_bytes()
        artifact.unlink()
        if corruption == "symlink":
            target = tmp_path / "unapproved.txt"
            target.write_bytes(content)
            artifact.symlink_to(target)
    with pytest.raises(ArtifactError):
        ledger.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=artifact_root)
    assert state(ledger.get_job(record.job_id)) == "FINALIZING"
    with pytest.raises(NotFoundError):
        ledger.get_manifest(record.job_id)


@pytest.mark.parametrize("mismatch", ["model", "inputs", "approval", "operation"])
def test_completion_rejects_manifest_that_does_not_match_execution(
    ledger, clock, job_factory, manifest_data, tmp_path, mismatch
):
    record, _ = dispatch(ledger, clock, job_factory)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    artifact_root = tmp_path / "results"
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    data = prepare_manifest(manifest_data, record, artifact_root, clock).model_dump(mode="json")
    if mismatch == "model":
        data["model"]["revision_sha"] = "d" * 40
    elif mismatch == "inputs":
        data["inputs"]["random_seed"] += 1
    elif mismatch == "approval":
        data["run"]["approval_id"] = "different-approval"
    else:
        data["experiment"]["intervention_hash"] = "sha256:" + "f" * 64
    manifest = RunManifest.model_validate(data)
    with pytest.raises((ArtifactError, InvalidTransition)):
        ledger.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=artifact_root)
    assert state(ledger.get_job(record.job_id)) == "FINALIZING"


def test_database_mutations_have_explicit_transaction_boundaries(tmp_path, clock, job_factory, monkeypatch):
    """Observe real SQL during initialization and a failed/retried execution."""
    original_connect = sqlite3.connect
    untransactional = []
    transaction_starts = []

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)

        def trace(statement):
            normalized = statement.lstrip().upper()
            if normalized.startswith("BEGIN"):
                transaction_starts.append(normalized)
            if (
                normalized.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "CREATE ", "ALTER ", "DROP "))
                and not connection.in_transaction
            ):
                untransactional.append(statement)

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    with Ledger(tmp_path / "transactions.sqlite", clock=clock) as instance:
        record, grant = dispatch(instance, clock, job_factory, lease=10)
        instance.start_job(record.job_id, record.attempt_id, "worker-1")
        instance.heartbeat(record.job_id, record.attempt_id, "worker-1", lease_seconds=10)
        clock.advance(11)
        instance.recover_expired()
        instance.confirm_stopped(record.job_id, record.attempt_id)
        instance.retry_job(record.job_id, record.attempt_id)
        replacement = instance.dispatch_next("worker-2", approval_id=grant.approval_id)
        instance.start_job(replacement.job_id, replacement.attempt_id, "worker-2")
        instance.fail_job(replacement.job_id, replacement.attempt_id, "worker-2", reason="scientific falsification")
        instance.confirm_stopped(replacement.job_id, replacement.attempt_id)
    assert transaction_starts
    assert all(statement.startswith("BEGIN IMMEDIATE") for statement in transaction_starts)
    assert untransactional == []


def test_tool_and_policy_events_are_audited_and_secrets_roll_back(ledger):
    tool = ledger.record_event(
        "tool_call", {"tool": "capture", "job_id": "job-1", "arguments_hash": "sha256:" + "a" * 64}
    )
    policy = ledger.record_event("policy_evaluation", {"decision": "allow", "job_id": "job-1"})
    assert tool["event_type"] == "tool_call"
    assert policy["previous_hash"] == tool["hash"]
    before = ledger.audit_records()
    with pytest.raises(SecretDetectedError):
        ledger.record_event("tool_call", {"arguments": [{"authorization": "sensitive-value"}]})
    assert ledger.audit_records() == before
    assert ledger.record_event("policy_evaluation", {"decision": "deny"})["sequence"] == len(before) + 1


def test_audit_failure_before_commit_rolls_back_job_submission(ledger, job_factory, monkeypatch):
    original = ledger_module.make_record

    def reject_audit(*args, **kwargs):
        raise ValueError("simulated audit validation failure")

    with monkeypatch.context() as patch:
        patch.setattr(ledger_module, "make_record", reject_audit)
        with pytest.raises(ValueError, match="simulated audit validation"):
            ledger.submit_job(job_factory())
    assert ledger_module.make_record is original
    assert ledger.list_jobs() == []
    assert ledger.audit_records() == []
    assert state(ledger.submit_job(job_factory())) == "PENDING"


def test_completion_commit_failure_rolls_back_manifest_and_job_together(
    ledger, clock, job_factory, manifest_data, tmp_path, monkeypatch
):
    record, _ = dispatch(ledger, clock, job_factory)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    root = tmp_path / "results"
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    manifest = prepare_manifest(manifest_data, record, root, clock)
    before = ledger.audit_records()

    def reject_audit(*args, **kwargs):
        raise ValueError("simulated audit validation failure")

    with monkeypatch.context() as patch:
        patch.setattr(ledger_module, "make_record", reject_audit)
        with pytest.raises(ValueError, match="simulated audit validation"):
            ledger.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=root)
    assert state(ledger.get_job(record.job_id)) == "FINALIZING"
    assert ledger.audit_records() == before
    with pytest.raises(NotFoundError):
        ledger.get_manifest(record.job_id)
    assert (
        state(ledger.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=root))
        == "COMPLETED"
    )


def test_completed_manifest_cannot_be_replaced(ledger, clock, job_factory, manifest_data, tmp_path):
    record, _ = dispatch(ledger, clock, job_factory)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    root = tmp_path / "results"
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    manifest = prepare_manifest(manifest_data, record, root, clock)
    ledger.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=root)
    altered = manifest.model_dump(mode="json")
    altered["results"]["effect_size"] = 0.99
    with pytest.raises(IdempotencyConflict):
        ledger.complete_job(
            record.job_id, record.attempt_id, "worker-1", RunManifest.model_validate(altered), artifact_root=root
        )
    assert ledger.get_manifest(record.job_id) == manifest


def test_closed_ledger_rejects_reads_and_mutations(tmp_path, clock, job_factory):
    instance = Ledger(tmp_path / "closed.sqlite", clock=clock)
    instance.close()
    instance.close()
    with pytest.raises(LedgerError):
        instance.submit_job(job_factory())
    with pytest.raises(LedgerError):
        instance.list_jobs()


def test_batch_digest_is_order_independent_and_bound_to_submitted_jobs(ledger, job_factory):
    first = ledger.submit_job(job_factory("batch-1"))
    second = ledger.submit_job(job_factory("batch-2"))
    assert ledger.batch_hash([first.job_id, second.job_id]) == ledger.batch_hash([second.job_id, first.job_id])
    assert ledger.batch_hash([first.job_id]) != ledger.batch_hash([second.job_id])
    with pytest.raises(ValueError):
        ledger.batch_hash([first.job_id, first.job_id])
    with pytest.raises(NotFoundError):
        ledger.batch_hash(["unknown-job"])


def test_completion_requires_positive_supervisor_stop_acknowledgement(
    ledger, clock, job_factory, manifest_data, tmp_path
):
    record, _ = dispatch(ledger, clock, job_factory)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    root = tmp_path / "results"
    manifest = prepare_manifest(manifest_data, record, root, clock)
    with pytest.raises(InvalidTransition):
        ledger.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=root)
    assert state(ledger.get_job(record.job_id)) == "FINALIZING"
    with pytest.raises(NotFoundError):
        ledger.get_manifest(record.job_id)


def test_expired_compute_interval_cannot_be_replaced_until_pod_is_confirmed_off(ledger, clock, job_factory):
    first = ledger.submit_job(job_factory("first-interval"))
    approve(ledger, clock, [first], approval_id="wake-1", runtime=60)
    clock.advance(61)
    second = ledger.submit_job(job_factory("second-interval"))
    nonce = ApprovalNonce(
        approval_id="wake-2",
        token="second-approval-" + "b" * 40,
        pod_id="pod-1",
        batch_hash=ledger.batch_hash([second.job_id]),
        max_runtime_seconds=60,
        price_ceiling_usd_per_hour=1.20,
        issued_at=clock(),
        expires_at=clock() + timedelta(minutes=5),
    )
    ledger.register_approval(nonce)
    arguments = dict(pod_id="pod-1", job_ids=[second.job_id], live_price_usd_per_hour=1.0, requested_runtime_seconds=60)
    with pytest.raises(ApprovalError):
        ledger.consume_approval("wake-2", nonce.token.get_secret_value(), **arguments)
    ledger.end_approval("wake-1")
    assert ledger.consume_approval("wake-2", nonce.token.get_secret_value(), **arguments).approval_id == "wake-2"


def test_two_writers_cannot_export_a_stale_audit_prefix(tmp_path, clock, job_factory, monkeypatch):
    database = tmp_path / "export-race.sqlite"
    first_export_waiting = threading.Event()
    newer_export_reached = threading.Event()
    release_first = threading.Event()
    original_sync = AuditLog.sync_records

    def coordinated_sync(self, records):
        records = list(records)
        if len(records) == 1 and not first_export_waiting.is_set():
            first_export_waiting.set()
            if not release_first.wait(timeout=5):
                raise RuntimeError("test failed to release the first export")
        if len(records) >= 2:
            newer_export_reached.set()
        return original_sync(self, records)

    with Ledger(database, clock=clock) as first, Ledger(database, clock=clock) as second:
        monkeypatch.setattr(AuditLog, "sync_records", coordinated_sync)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first.submit_job, job_factory("export-1"))
            assert first_export_waiting.wait(timeout=5)
            second_future = pool.submit(second.submit_job, job_factory("export-2"))
            try:
                # A safe exporter holds the SQLite transaction, so the second
                # writer cannot publish a newer prefix until the first finishes.
                newer_export_reached.wait(timeout=0.25)
            finally:
                release_first.set()
            first_future.result(timeout=5)
            second_future.result(timeout=5)
        assert first.audit_export_error is None
        assert second.audit_export_error is None
        exported = [json.loads(line) for line in first.audit_path.read_text().splitlines()]
        assert exported == first.audit_records()


def test_infrastructure_retry_cannot_be_rebound_to_a_fresh_approval(ledger, clock, job_factory):
    original, _ = dispatch(ledger, clock, job_factory, lease=10)
    clock.advance(11)
    ledger.recover_expired()
    ledger.confirm_stopped(original.job_id, original.attempt_id)
    pending = ledger.retry_job(original.job_id, original.attempt_id)
    ledger.end_approval("wake-1")
    with pytest.raises(ApprovalError):
        approve(ledger, clock, [pending], approval_id="wake-2", token="different-approval-" + "b" * 40)


def test_completed_outputs_are_sealed_and_persist_after_source_changes(tmp_path, clock, job_factory, manifest_data):
    database = tmp_path / "sealed.sqlite"
    source = tmp_path / "worker-output"
    with Ledger(database, clock=clock) as instance:
        record, _ = dispatch(instance, clock, job_factory)
        instance.start_job(record.job_id, record.attempt_id, "worker-1")
        instance.begin_finalization(record.job_id, record.attempt_id, "worker-1")
        instance.confirm_stopped(record.job_id, record.attempt_id)
        manifest = prepare_manifest(manifest_data, record, source, clock)
        instance.complete_job(record.job_id, record.attempt_id, "worker-1", manifest, artifact_root=source)
        sealed = instance.get_artifact_root(record.job_id)
        assert sealed != source
        sealed_file = sealed / "artifacts" / "result.txt"
        assert sealed_file.read_bytes() == b"validated result\n"
        assert sealed_file.stat().st_mode & 0o222 == 0
    (source / "artifacts" / "result.txt").write_bytes(b"worker modified its own result")
    with Ledger(database, clock=clock) as reopened:
        assert reopened.get_artifact_root(record.job_id) == sealed
        assert sealed_file.read_bytes() == b"validated result\n"
        assert reopened.get_manifest(record.job_id) == manifest


@pytest.mark.parametrize("failure", ["declared_bytes", "output_limit"])
def test_completed_output_must_match_accounting_and_stay_within_limit(
    ledger, clock, job_factory, manifest_data, tmp_path, failure
):
    record, _ = dispatch(ledger, clock, job_factory)
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    source = tmp_path / "output"
    content = b"x" * (1000001 if failure == "output_limit" else 10)
    data = prepare_manifest(manifest_data, record, source, clock, content=content).model_dump(mode="json")
    if failure == "declared_bytes":
        data["cost"]["bytes_persisted"] += 1
    with pytest.raises(ArtifactError):
        ledger.complete_job(
            record.job_id, record.attempt_id, "worker-1", RunManifest.model_validate(data), artifact_root=source
        )
    assert state(ledger.get_job(record.job_id)) == "FINALIZING"


def register_frozen_hypothesis(ledger, manifest_data, job_factory, hypothesis_id="H-validation"):
    record = HypothesisRecord(
        hypothesis_id=hypothesis_id,
        proposition="The target layer causally changes the primary metric",
        alignment_relevance="Measures the specified behavioral contrast",
        predicted_causal_intervention="Capture the registered residual states",
        predicted_direction=manifest_data["experiment"]["predicted_direction"],
        predictions=["The effect is positive and exceeds the registered minimum"],
        falsifier=manifest_data["experiment"]["falsifier"],
        preregistration_plan={
            "model": manifest_data["model"],
            "operation": job_factory().operation,
            "primary_metric": manifest_data["experiment"]["primary_metric"],
            "minimum_effect": 0.01,
            "controls": manifest_data["controls"],
        },
    )
    ledger.register_hypothesis(record)
    return ledger.transition_hypothesis(hypothesis_id, "FROZEN")


def test_hypothesis_can_be_validated_only_with_persisted_passed_replication(
    ledger, clock, job_factory, manifest_data, tmp_path
):
    frozen = register_frozen_hypothesis(ledger, manifest_data, job_factory)
    ledger.transition_hypothesis(frozen.hypothesis_id, "TESTING")
    ledger.transition_hypothesis(frozen.hypothesis_id, "REPLICATING")
    with pytest.raises(LedgerError):
        ledger.transition_hypothesis(frozen.hypothesis_id, "VALIDATED", replication_ids=["nonexistent-replication"])
    spec_data = job_factory("blinded-replication").model_dump(mode="json")
    spec_data.update(hypothesis_id=frozen.hypothesis_id, experiment_stage="replication")
    submitted = ledger.submit_job(JobSpec.model_validate(spec_data))
    approve(ledger, clock, [submitted])
    record = ledger.dispatch_next("replicator", approval_id="wake-1")
    ledger.start_job(record.job_id, record.attempt_id, "replicator")
    ledger.begin_finalization(record.job_id, record.attempt_id, "replicator")
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    source = tmp_path / "replication-output"
    data = prepare_manifest(manifest_data, record, source, clock).model_dump(mode="json")
    data["run"].update(
        experiment_stage="replication",
        hypothesis_id=frozen.hypothesis_id,
        preregistration_hash=frozen.preregistration_hash,
        replicator_blinded=True,
    )
    data["results"].update(heldout=True, replication_status="passed")
    manifest = RunManifest.model_validate(data)
    ledger.complete_job(record.job_id, record.attempt_id, "replicator", manifest, artifact_root=source)
    validated = ledger.transition_hypothesis(frozen.hypothesis_id, "VALIDATED", replication_ids=[manifest.run.run_id])
    assert str(validated.status) == "VALIDATED"
    assert validated.preregistration_hash == frozen.preregistration_hash
    assert validated.replication_ids == (manifest.run.run_id,)
    with pytest.raises(InvalidTransition):
        ledger.transition_hypothesis(frozen.hypothesis_id, "REPLICATING")


@pytest.mark.parametrize("changed", ["operation", "model"])
def test_confirmation_cannot_change_frozen_intervention(ledger, job_factory, manifest_data, changed):
    frozen = register_frozen_hypothesis(ledger, manifest_data, job_factory)
    ledger.transition_hypothesis(frozen.hypothesis_id, "TESTING")
    data = job_factory("changed-confirmation").model_dump(mode="json")
    data.update(hypothesis_id=frozen.hypothesis_id, experiment_stage="confirmatory")
    if changed == "operation":
        data["operation"]["modules"][0]["layer"] = 1
    else:
        data["model"]["revision_sha"] = "d" * 40
    with pytest.raises(InvalidTransition, match="frozen experiment plan"):
        ledger.submit_job(JobSpec.model_validate(data))
    assert ledger.list_jobs() == []


@pytest.mark.parametrize("changed", ["metric", "controls", "direction", "falsifier"])
def test_confirmation_manifest_is_bound_to_frozen_scientific_plan(
    ledger, clock, job_factory, manifest_data, tmp_path, changed
):
    frozen = register_frozen_hypothesis(ledger, manifest_data, job_factory)
    ledger.transition_hypothesis(frozen.hypothesis_id, "TESTING")
    spec_data = job_factory("confirmation").model_dump(mode="json")
    spec_data.update(hypothesis_id=frozen.hypothesis_id, experiment_stage="confirmatory")
    submitted = ledger.submit_job(JobSpec.model_validate(spec_data))
    approve(ledger, clock, [submitted])
    record = ledger.dispatch_next("worker-1", approval_id="wake-1")
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    source = tmp_path / "confirmation-output"
    data = prepare_manifest(manifest_data, record, source, clock).model_dump(mode="json")
    data["run"].update(
        experiment_stage="confirmatory",
        hypothesis_id=frozen.hypothesis_id,
        preregistration_hash=frozen.preregistration_hash,
    )
    data["results"]["heldout"] = True
    if changed == "metric":
        data["experiment"]["primary_metric"] = "posthoc_metric"
    elif changed == "controls":
        data["controls"]["random_component"] = False
    elif changed == "direction":
        data["experiment"]["predicted_direction"] = "decrease"
    else:
        data["experiment"]["falsifier"] = "A different posthoc falsifier"
    with pytest.raises(ArtifactError):
        ledger.complete_job(
            record.job_id, record.attempt_id, "worker-1", RunManifest.model_validate(data), artifact_root=source
        )
    assert state(ledger.get_job(record.job_id)) == "FINALIZING"
