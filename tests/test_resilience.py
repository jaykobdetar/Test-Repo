"""Regression checks at concurrency, transaction and artifact crash boundaries."""

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from probe_core.audit import AuditLog
import probe_core.ledger as ledger_module
from probe_core.ledger import ArtifactError, Ledger, LedgerError, NotFoundError
from probe_core.schemas import ApprovalNonce, JobSpec, RunManifest


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
CONTENT = b"independently preserved scientific result\n"
RELATIVE_ARTIFACT = "derived/statistics/result.txt"


def _finalizing_case(ledger, source):
    """Create one real, stopped execution and its reproducible output bundle."""
    data = json.loads((Path(__file__).parent / "fixtures" / "manifest.json").read_text())
    spec = JobSpec(
        idempotency_key="resilience-job",
        model=data["model"],
        inputs=data["inputs"],
        operation={"kind": "capture", "modules": [{"layer": 0, "component": "residual"}], "positions": ["last"]},
        limits={"max_runtime_seconds": 60, "max_output_bytes": 1000000},
    )
    submitted = ledger.submit_job(spec)
    nonce = ApprovalNonce(
        approval_id="resilience-approval", token="resilience-test-" + "a" * 40,
        pod_id="pod-1", batch_hash=ledger.batch_hash([submitted.job_id]),
        max_runtime_seconds=300, price_ceiling_usd_per_hour=1.20,
        issued_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )
    ledger.register_approval(nonce)
    ledger.consume_approval(nonce.approval_id, nonce.token.get_secret_value(), pod_id="pod-1",
                            job_ids=[submitted.job_id], live_price_usd_per_hour=1.0,
                            requested_runtime_seconds=300)
    record = ledger.dispatch_next("worker-1", approval_id=nonce.approval_id)
    assert record is not None
    ledger.start_job(record.job_id, record.attempt_id, "worker-1")
    ledger.begin_finalization(record.job_id, record.attempt_id, "worker-1")
    ledger.confirm_stopped(record.job_id, record.attempt_id)
    data["run"].update(
        run_id=record.job_id, started_at=NOW.isoformat(), experiment_stage="exploratory",
        hypothesis_id=None, preregistration_hash=None, approval_id=nonce.approval_id,
        replicator_blinded=False,
    )
    data["experiment"].update(tool="capture_activation", modules=["model.layers.0"],
                               positions=["last"], intervention_hash=Ledger.operation_hash(spec))
    data["results"].update(heldout=False, replication_status="not_applicable")
    data["cost"] = {"gpu_seconds": 0, "estimated_compute_usd": 0.0, "bytes_persisted": len(CONTENT)}
    data["artifacts"] = [{"path": RELATIVE_ARTIFACT, "sha256": hashlib.sha256(CONTENT).hexdigest(), "retention_class": "validated"}]
    artifact = source / RELATIVE_ARTIFACT
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(CONTENT)
    return record, RunManifest.model_validate(data)


def test_simultaneous_initializers_open_one_database_and_export_consistently(tmp_path):
    database = tmp_path / "simultaneous.sqlite"
    start = threading.Barrier(6)

    def initialize(number):
        start.wait(timeout=5)
        with Ledger(database) as instance:
            event = instance.record_event("tool_call", {"initializer": number})
            assert instance.audit_export_error is None
            return event

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(initialize, number) for number in range(6)]
        records = [future.result(timeout=15) for future in futures]
    assert sorted(record["sequence"] for record in records) == list(range(1, 7))
    with Ledger(database) as reopened:
        assert AuditLog(reopened.audit_path).verify() == reopened.audit_records()
        assert {record["payload"]["initializer"] for record in reopened.audit_records()} == set(range(6))


def _daemon_call(fn):
    """A regression must time out instead of hanging the complete test process."""
    result = Future()

    def invoke():
        try:
            result.set_result(fn())
        except BaseException as exc:
            result.set_exception(exc)

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    return result, thread


def test_initialization_retries_real_wal_lock_then_preserves_busy_timeout(tmp_path, monkeypatch):
    database = tmp_path / "locked-startup.sqlite"
    original_connect = sqlite3.connect
    contended = threading.Event()
    attempts = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA journal_mode=WAL;":
                attempts.append(statement)
                try:
                    return super().execute(statement, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY:
                        contended.set()
                    raise
            return super().execute(statement, *args, **kwargs)

    def observed_connect(*args, **kwargs):
        return original_connect(*args, factory=ObservedConnection, **kwargs)

    blocker = original_connect(database, isolation_level=None)
    try:
        blocker.execute("BEGIN;")
        blocker.execute("SELECT name FROM sqlite_schema;").fetchall()
        with monkeypatch.context() as patch:
            patch.setattr(ledger_module.sqlite3, "connect", observed_connect)
            opening, thread = _daemon_call(lambda: Ledger(database))
            try:
                assert contended.wait(timeout=5)
            finally:
                blocker.execute("ROLLBACK;")
            with opening.result(timeout=5) as instance:
                instance.record_event("tool_call", {"after_contention": True})
                settings = instance._submit(lambda connection, now: (
                    connection.execute("PRAGMA journal_mode;").fetchone()[0],
                    connection.execute("PRAGMA busy_timeout;").fetchone()[0],
                ))
                assert settings == ("wal", 5000)
                assert instance.audit_export_error is None
            thread.join(timeout=1)
            assert not thread.is_alive()
        assert len(attempts) >= 2
    finally:
        blocker.close()


def test_initialization_wal_contention_has_a_five_second_deadline(tmp_path, monkeypatch):
    database = tmp_path / "permanently-locked-startup.sqlite"
    elapsed = [0.0]
    waits = []

    def advance(seconds):
        waits.append(seconds)
        elapsed[0] += seconds

    blocker = sqlite3.connect(database, isolation_level=None)
    try:
        blocker.execute("BEGIN;")
        blocker.execute("SELECT name FROM sqlite_schema;").fetchall()
        monkeypatch.setattr(ledger_module, "time", SimpleNamespace(
            monotonic=lambda: elapsed[0], sleep=advance,
        ))
        with pytest.raises(sqlite3.OperationalError) as rejected:
            Ledger(database)
        assert rejected.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert elapsed[0] == pytest.approx(5.0)
        assert waits and all(0 < seconds <= 0.01 for seconds in waits)
    finally:
        blocker.close()
    # Failed initialization must close its connection and leave no partial schema.
    with Ledger(database) as reopened:
        assert reopened.audit_records() == []


@pytest.mark.parametrize("error_code", [None, sqlite3.SQLITE_IOERR])
def test_initialization_does_not_retry_non_contention_errors(tmp_path, monkeypatch, error_code):
    original_connect = sqlite3.connect
    attempts = []
    failure = sqlite3.OperationalError("database is locked" if error_code is None else "disk I/O error")
    if error_code is not None:
        failure.sqlite_errorcode = error_code

    class FailedConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA journal_mode=WAL;":
                attempts.append(statement)
                raise failure
            return super().execute(statement, *args, **kwargs)

    def failing_connect(*args, **kwargs):
        return original_connect(*args, factory=FailedConnection, **kwargs)

    def unexpected_wait(seconds):
        pytest.fail("non-contention failure must propagate without waiting")

    monkeypatch.setattr(ledger_module.sqlite3, "connect", failing_connect)
    monkeypatch.setattr(ledger_module, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=unexpected_wait))
    with pytest.raises(sqlite3.OperationalError) as rejected:
        Ledger(tmp_path / "failed-startup.sqlite")
    assert rejected.value is failure
    assert len(attempts) == 1


def test_fatal_rollback_failure_releases_current_and_queued_callers(tmp_path, monkeypatch):
    database = tmp_path / "fatal-writer.sqlite"
    original_connect = sqlite3.connect
    in_transaction = threading.Event()
    release_failure = threading.Event()
    queued = threading.Event()

    class BrokenRollback(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement.strip().upper().startswith("ROLLBACK") and release_failure.is_set():
                raise sqlite3.OperationalError("simulated rollback storage fault")
            return super().execute(statement, *args, **kwargs)

    def fault_connect(*args, **kwargs):
        kwargs["factory"] = BrokenRollback
        return original_connect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ledger_module.sqlite3, "connect", fault_connect)
        with Ledger(database) as instance:
            def failing_operation(connection, now):
                instance._event(connection, now, "tool_call", {"must_rollback": True})
                in_transaction.set()
                if not release_failure.wait(timeout=5):
                    raise RuntimeError("test did not release the writer")
                raise RuntimeError("simulated operation failure")

            def pending_operation(connection, now):
                return instance._event(connection, now, "tool_call", {"must_not_execute": True})

            original_put = instance._tasks.put

            def observed_put(item, *args, **kwargs):
                original_put(item, *args, **kwargs)
                if item is not None and item[0] is pending_operation:
                    queued.set()

            patch.setattr(instance._tasks, "put", observed_put)
            current, current_thread = _daemon_call(lambda: instance._submit(failing_operation))
            assert in_transaction.wait(timeout=5)
            waiting, waiting_thread = _daemon_call(lambda: instance._submit(pending_operation))
            try:
                assert queued.wait(timeout=5)
            finally:
                release_failure.set()
            for caller in (current, waiting):
                with pytest.raises(LedgerError, match="writer terminated"):
                    caller.result(timeout=5)
            current_thread.join(timeout=1)
            waiting_thread.join(timeout=1)
            assert not current_thread.is_alive()
            assert not waiting_thread.is_alive()
            with pytest.raises(LedgerError, match="closed"):
                instance.record_event("tool_call", {"later": True})
    with Ledger(database) as reopened:
        assert reopened.audit_records() == []


@pytest.mark.parametrize("location", ["source_ancestor", "nested_artifact_parent"])
def test_sealing_rejects_symlink_parent_directories(tmp_path, location):
    source = tmp_path / "real-parent" / "worker-output"
    with Ledger(tmp_path / "symlink.sqlite", clock=lambda: NOW) as instance:
        record, manifest = _finalizing_case(instance, source)
        if location == "source_ancestor":
            alias = tmp_path / "alias-parent"
            alias.symlink_to(source.parent, target_is_directory=True)
            presented_source = alias / source.name
        else:
            original = source / "derived" / "statistics"
            external = tmp_path / "external-statistics"
            original.rename(external)
            original.symlink_to(external, target_is_directory=True)
            presented_source = source
        with pytest.raises(ArtifactError):
            instance.complete_job(record.job_id, record.attempt_id, "worker-1", manifest,
                                  artifact_root=presented_source)
        assert str(instance.get_job(record.job_id).state) == "FINALIZING"
        with pytest.raises(NotFoundError):
            instance.get_artifact_root(record.job_id)


def test_orphan_bundle_survives_reopen_and_postcommit_audit_failure(tmp_path, monkeypatch):
    database = tmp_path / "orphan.sqlite"
    source = tmp_path / "worker-output"
    with Ledger(database, clock=lambda: NOW) as instance:
        record, manifest = _finalizing_case(instance, source)
        original_make_record = ledger_module.make_record

        def fail_completion_event(sequence, previous_hash, event_type, payload, timestamp):
            if event_type == "state_change" and payload.get("to") == "COMPLETED":
                raise ValueError("simulated transactional audit failure")
            return original_make_record(sequence, previous_hash, event_type, payload, timestamp)

        with monkeypatch.context() as patch:
            patch.setattr(ledger_module, "make_record", fail_completion_event)
            with pytest.raises(ValueError, match="transactional audit failure"):
                instance.complete_job(record.job_id, record.attempt_id, "worker-1", manifest,
                                      artifact_root=source)
        assert str(instance.get_job(record.job_id).state) == "FINALIZING"
        with pytest.raises(NotFoundError):
            instance.get_manifest(record.job_id)
        bundles = list((tmp_path / "orphan.artifacts" / record.job_id).iterdir())
        assert len(bundles) == 1
        orphan = bundles[0]
        assert (orphan / RELATIVE_ARTIFACT).read_bytes() == CONTENT

    # The durable sealed copy, not the worker's original directory, must be used
    # when replaying the exact attempt after the transaction rolled back.
    shutil.rmtree(source)
    with Ledger(database, clock=lambda: NOW) as reopened:
        def offline_export(self, records):
            raise OSError("simulated audit projection outage")

        with monkeypatch.context() as patch:
            patch.setattr(AuditLog, "sync_records", offline_export)
            completed = reopened.complete_job(record.job_id, record.attempt_id, "worker-1", manifest,
                                              artifact_root=source)
            assert str(completed.state) == "COMPLETED"
            assert isinstance(reopened.audit_export_error, OSError)
            assert reopened.get_artifact_root(record.job_id) == orphan
            assert reopened.get_manifest(record.job_id) == manifest
        committed_records = reopened.audit_records()

    with Ledger(database, clock=lambda: NOW) as recovered:
        assert recovered.audit_export_error is None
        assert recovered.get_artifact_root(record.job_id) == orphan
        assert recovered.get_manifest(record.job_id) == manifest
        assert (orphan / RELATIVE_ARTIFACT).read_bytes() == CONTENT
        assert (orphan / RELATIVE_ARTIFACT).stat().st_mode & 0o222 == 0
        assert orphan.stat().st_mode & 0o222 == 0
        assert AuditLog(recovered.audit_path).verify() == committed_records
        repeated = recovered.complete_job(record.job_id, record.attempt_id, "worker-1", manifest,
                                          artifact_root=source)
        assert str(repeated.state) == "COMPLETED"
        assert recovered.audit_records() == committed_records


def test_stopped_finalization_survives_expired_lease_and_approval_on_reopen(tmp_path):
    database = tmp_path / "expired-finalization.sqlite"
    source = tmp_path / "worker-output"
    clock = [NOW]
    with Ledger(database, clock=lambda: clock[0]) as instance:
        record, manifest = _finalizing_case(instance, source)
        assert record.lease_expires_at < NOW + timedelta(seconds=600)

    # Inference was positively acknowledged as stopped before the crash. CPU
    # publication must remain recoverable after both execution allowances expire.
    clock[0] = NOW + timedelta(seconds=600)
    with Ledger(database, clock=lambda: clock[0]) as reopened:
        assert reopened.recover_expired() == []
        assert str(reopened.get_job(record.job_id).state) == "FINALIZING"
        reopened.end_approval(record.approval_id)
        completed = reopened.complete_job(record.job_id, record.attempt_id, "worker-1", manifest,
                                          artifact_root=source)
        assert str(completed.state) == "COMPLETED"
        assert (reopened.get_artifact_root(record.job_id) / RELATIVE_ARTIFACT).read_bytes() == CONTENT
        with reopened.read_connection() as reader:
            approval = reader.execute("SELECT deadline,ended_at FROM approvals WHERE approval_id=?",
                                      (record.approval_id,)).fetchone()
            assert approval["deadline"] == (NOW + timedelta(seconds=300)).timestamp()
            assert approval["ended_at"] == clock[0].timestamp()


def test_slow_sealing_after_stop_does_not_require_or_extend_compute_lease(tmp_path, monkeypatch):
    clock = [NOW]
    source = tmp_path / "worker-output"
    with Ledger(tmp_path / "slow-seal.sqlite", clock=lambda: clock[0]) as instance:
        record, manifest = _finalizing_case(instance, source)
        original_copy = Ledger._copy_artifacts

        def slow_copy(manifest, source, destination, max_bytes):
            copied = original_copy(manifest, source, destination, max_bytes)
            clock[0] = NOW + timedelta(seconds=600)
            return copied

        monkeypatch.setattr(Ledger, "_copy_artifacts", staticmethod(slow_copy))
        completed = instance.complete_job(record.job_id, record.attempt_id, "worker-1", manifest,
                                          artifact_root=source)
        assert str(completed.state) == "COMPLETED"
        assert (instance.get_artifact_root(record.job_id) / RELATIVE_ARTIFACT).read_bytes() == CONTENT
        with instance.read_connection() as reader:
            attempt = reader.execute("SELECT execution_deadline,lease_expires_at FROM attempts WHERE attempt_id=?",
                                     (record.attempt_id,)).fetchone()
            assert attempt["execution_deadline"] == (NOW + timedelta(seconds=60)).timestamp()
            assert attempt["lease_expires_at"] == record.lease_expires_at.timestamp()
            approval = reader.execute("SELECT deadline,ended_at FROM approvals WHERE approval_id=?",
                                      (record.approval_id,)).fetchone()
            assert approval["deadline"] == (NOW + timedelta(seconds=300)).timestamp()
            assert approval["ended_at"] is None
