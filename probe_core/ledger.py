"""Local authoritative state with one writer thread and recoverable audit export.

SQLite owns the queue, immutable manifests, and audit chain. JSONL is a durable
projection of committed events, never a second independent transaction participant.
All worker-facing identifiers must come from the trusted dispatcher/supervisor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import queue
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, TypeVar

from .audit import AuditLog, GENESIS_HASH, canonical_json, make_record
from .schemas import ApprovalNonce, ExperimentStage, HypothesisRecord, HypothesisState, JobSpec, RunManifest

T = TypeVar("T")
UTC = timezone.utc
_APPLICATION_ID = 0x50524F42
_SCHEMA_VERSION = 1


class LedgerError(Exception):
    """Base error for rejected state-engine operations."""


class NotFoundError(LedgerError):
    """Requested identifier does not exist."""


class InvalidTransition(LedgerError):
    """A transition would violate the lifecycle."""


class LeaseError(LedgerError):
    """Execution ownership is stale, expired, or belongs to another worker."""


class IdempotencyConflict(LedgerError):
    """A submission key was reused for different content."""


class ApprovalError(LedgerError):
    """A start approval is missing, expired, consumed, or incompatible."""


class RetryNotAllowed(LedgerError):
    """Retry would exceed its scope, count, or approved interval."""


class ArtifactError(LedgerError):
    """An artifact is missing, unsafe, or does not match its manifest."""


class JobState(StrEnum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


_ACTIVE = (JobState.DISPATCHED, JobState.RUNNING, JobState.FINALIZING)
_FAILURE_KINDS = {"scientific", "infrastructure", "cancelled", "policy", "timeout", "oom"}
_HYPOTHESIS_TRANSITIONS = {
    "DRAFT": {"FROZEN"},
    "FROZEN": {"TESTING"},
    "TESTING": {"REPLICATING", "FALSIFIED"},
    "REPLICATING": {"VALIDATED", "FALSIFIED"},
    "VALIDATED": set(),
    "FALSIFIED": set(),
}


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    spec: JobSpec
    state: JobState
    created_at: datetime
    updated_at: datetime
    attempt_id: str | None
    worker_id: str | None
    lease_expires_at: datetime | None
    attempt_count: int
    retry_count: int
    approval_id: str | None
    failure_kind: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class ApprovalGrant:
    approval_id: str
    pod_id: str
    batch_hash: str
    approved_at: datetime
    deadline: datetime


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _time(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value is not None else None


def _positive_seconds(value: int, maximum: int = 86400) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"duration must be an integer in [1, {maximum}]")
    return value


def _text(value: str, label: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty and at most {maximum} characters")
    return value


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Bound initial journal-mode contention before any ledger transaction.

    SQLite may bypass its busy handler when concurrent initializers both try to
    promote their journal locks. Retry this idempotent setup step explicitly;
    disabling the handler here keeps its own wait from extending our deadline.
    """
    deadline = time.monotonic() + 5.0
    conn.execute("PRAGMA busy_timeout=0;")
    try:
        while True:
            try:
                mode = conn.execute("PRAGMA journal_mode=WAL;").fetchone()[0]
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
            else:
                if mode.lower() != "wal":
                    raise LedgerError("SQLite failed to enable WAL mode")
                return
    finally:
        conn.execute("PRAGMA busy_timeout=5000;")


def _require_local_path(path: Path) -> Path:
    """Reject known remote Linux mounts; deployments must still provision local disk."""
    if str(path) == ":memory:":
        raise ValueError("the authoritative ledger requires a persistent local file")
    if path.is_symlink():
        raise ValueError("database symlinks are not allowed")
    resolved = path.expanduser().absolute().resolve()
    mountinfo = Path("/proc/self/mountinfo")
    remote = {"nfs", "nfs4", "cifs", "smb3", "9p", "ceph", "glusterfs", "afs", "lustre"}
    if mountinfo.exists():
        best: tuple[int, str] = (-1, "")
        for line in mountinfo.read_text().splitlines():
            left, _, right = line.partition(" - ")
            if not right:
                continue
            fields = left.split()
            mount = fields[4]
            for escaped, literal in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                mount = mount.replace(escaped, literal)
            if resolved.is_relative_to(Path(mount)) and len(mount) > best[0]:
                best = (len(mount), right.split()[0])
        if best[1] in remote or best[1].startswith(("fuse.sshfs", "fuse.rclone", "fuse.s3")):
            raise ValueError("SQLite WAL requires local storage, not a network filesystem")
    return resolved


_DDL = (
    """CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
        spec_json TEXT NOT NULL, spec_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
            ('PENDING','DISPATCHED','RUNNING','FINALIZING','COMPLETED','FAILED')),
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        attempt_id TEXT, worker_id TEXT, lease_expires_at REAL,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count BETWEEN 0 AND 1),
        approval_id TEXT, failure_kind TEXT, failure_reason TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs((1))
        WHERE state IN ('DISPATCHED','RUNNING','FINALIZING')""",
    """CREATE TABLE IF NOT EXISTS attempts (
        attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
        attempt_number INTEGER NOT NULL, worker_id TEXT NOT NULL,
        approval_id TEXT NOT NULL, dispatched_at REAL NOT NULL,
        heartbeat_at REAL NOT NULL, lease_expires_at REAL NOT NULL,
        execution_deadline REAL NOT NULL, stopped_at REAL, outcome TEXT,
        UNIQUE(job_id, attempt_number)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_unreaped_attempt ON attempts((1))
        WHERE stopped_at IS NULL""",
    """CREATE TABLE IF NOT EXISTS approvals (
        approval_id TEXT PRIMARY KEY, document TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, consumed_at REAL, deadline REAL, ended_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS approval_jobs (
        approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        PRIMARY KEY(approval_id, job_id)
    )""",
    """CREATE TABLE IF NOT EXISTS hypotheses (
        hypothesis_id TEXT PRIMARY KEY, document TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS manifests (
        job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), run_id TEXT NOT NULL UNIQUE,
        document TEXT NOT NULL, digest TEXT NOT NULL, artifact_root TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_events (
        sequence INTEGER PRIMARY KEY, record TEXT NOT NULL,
        hash TEXT NOT NULL UNIQUE
    )""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS manifest_no_update BEFORE UPDATE ON manifests
        BEGIN SELECT RAISE(ABORT, 'manifests are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS manifest_no_delete BEFORE DELETE ON manifests
        BEGIN SELECT RAISE(ABORT, 'manifests are immutable'); END""",
)


class Ledger:
    """Thread-safe synchronous API backed by a connection-owning writer thread.

    Readers get independent read-only connections. Separate Ledger instances are
    serialized by SQLite's write lock; database constraints retain single execution.
    ``clock`` is injectable for deterministic recovery/deadline tests.
    """

    def __init__(self, path: str | Path, *, audit_path: str | Path | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.path = _require_local_path(Path(path))
        with self._directory(self.path.parent, create=True):
            pass
        self.audit_path = Path(audit_path) if audit_path is not None else self.path.with_suffix(".audit.jsonl")
        if self.audit_path.absolute().resolve() == self.path:
            raise ValueError("database and JSONL audit must be different files")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._gate = threading.Lock()
        self._closed = False
        self._tasks: queue.Queue[Any] = queue.Queue()
        self._ready: Future[None] = Future()
        self._audit_export_error: Exception | None = None
        self._thread = threading.Thread(target=self._writer, name="probe-core-ledger", daemon=True)
        self._thread.start()
        try:
            self._ready.result()
        except BaseException:
            self._closed = True
            self._thread.join()
            raise

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._gate:
            if not self._closed:
                self._closed = True
                self._tasks.put(None)
        if threading.current_thread() is not self._thread:
            self._thread.join()

    @property
    def audit_export_error(self) -> Exception | None:
        """Last JSONL projection failure; committed SQLite state remains authoritative."""
        return self._audit_export_error

    def _writer(self) -> None:
        conn: sqlite3.Connection | None = None
        current_future: Future[Any] | None = None
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
            conn.row_factory = sqlite3.Row
            # Connection configuration precedes transactions (WAL cannot be set in one).
            _enable_wal(conn)
            conn.execute("PRAGMA synchronous=FULL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("BEGIN IMMEDIATE;")
            try:
                version = conn.execute("PRAGMA user_version;").fetchone()[0]
                app_id = conn.execute("PRAGMA application_id;").fetchone()[0]
                if version not in (0, _SCHEMA_VERSION) or app_id not in (0, _APPLICATION_ID):
                    raise LedgerError("incompatible database schema/application")
                if app_id == 0 and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise LedgerError("refusing to adopt a non-Probe database")
                for statement in _DDL:
                    conn.execute(statement)
                conn.execute(f"PRAGMA application_id={_APPLICATION_ID};")
                conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION};")
                conn.execute("COMMIT;")
            except BaseException:
                conn.execute("ROLLBACK;")
                raise
            audit = AuditLog(self.audit_path)
            self._export(conn, audit, strict=True)
            self._ready.set_result(None)
            while True:
                task = self._tasks.get()
                if task is None:
                    break
                fn, future = task
                current_future = future
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    result = fn(conn, _utc(self._clock()))
                    conn.execute("COMMIT;")
                except BaseException as exc:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK;")
                    future.set_exception(exc)
                else:
                    # Export errors cannot roll back an already committed mutation.
                    self._export(conn, audit, strict=False)
                    future.set_result(result)
                current_future = None
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            else:
                with self._gate:
                    self._closed = True
                    if current_future is not None and not current_future.done():
                        current_future.set_exception(LedgerError("writer terminated; inspect durable state before retrying"))
                    while not self._tasks.empty():
                        pending = self._tasks.get_nowait()
                        if pending is not None:
                            pending[1].set_exception(LedgerError("writer terminated"))
        finally:
            if conn is not None:
                conn.close()

    def _submit(self, fn: Callable[[sqlite3.Connection, datetime], T]) -> T:
        future: Future[T] = Future()
        with self._gate:
            if self._closed or not self._thread.is_alive():
                raise LedgerError("ledger is closed")
            self._tasks.put((fn, future))
        return future.result()

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """An independent connection supporting explicit, concurrent WAL snapshots."""
        if self._closed:
            raise LedgerError("ledger is closed")
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True,
                               isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA query_only=ON;")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in conn.execute("SELECT record FROM audit_events ORDER BY sequence")]

    def _export(self, conn: sqlite3.Connection, audit: AuditLog, *, strict: bool) -> None:
        owns_transaction = not conn.in_transaction
        try:
            # Serialize snapshot acquisition AND file sync across Ledger instances.
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE;")
            audit.sync_records(self._records(conn))
            if owns_transaction:
                conn.execute("COMMIT;")
        except Exception as exc:
            if owns_transaction and conn.in_transaction:
                conn.execute("ROLLBACK;")
            self._audit_export_error = exc
            if strict:
                raise
        else:
            self._audit_export_error = None

    def sync_audit(self) -> None:
        # Serialize with writers; the outer transaction contains no state mutations.
        def sync(conn: sqlite3.Connection, now: datetime) -> None:
            self._export(conn, AuditLog(self.audit_path), strict=True)
        self._submit(sync)

    def audit_records(self) -> list[dict[str, Any]]:
        with self.read_connection() as conn:
            return self._records(conn)

    @staticmethod
    def _event(conn: sqlite3.Connection, now: datetime, event_type: str,
               payload: dict[str, Any]) -> dict[str, Any]:
        if not conn.in_transaction:
            raise LedgerError("audit append requires a transaction")
        last = conn.execute("SELECT sequence, hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
        payload = {key: value.value if isinstance(value, StrEnum) else value
                   for key, value in payload.items()}
        record = make_record(last[0] + 1 if last else 1, last[1] if last else GENESIS_HASH,
                             event_type, payload, now)
        conn.execute("INSERT INTO audit_events(sequence, record, hash) VALUES (?,?,?)",
                     (record["sequence"], canonical_json(record), record["hash"]))
        return record

    def record_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Record tool_call/policy_evaluation metadata; recursive secret checks apply."""
        if event_type not in {"tool_call", "policy_evaluation"}:
            raise ValueError("external events must be tool_call or policy_evaluation")
        # Snapshot caller data before crossing the writer-thread boundary.
        copied = json.loads(canonical_json(payload))
        return self._submit(lambda conn, now: self._event(conn, now, event_type, copied))

    @staticmethod
    def _row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError("job does not exist")
        return row

    @staticmethod
    def _job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(row["job_id"], JobSpec.model_validate_json(row["spec_json"]),
                         JobState(row["state"]), _time(row["created_at"]), _time(row["updated_at"]),
                         row["attempt_id"], row["worker_id"], _time(row["lease_expires_at"]),
                         row["attempt_count"], row["retry_count"], row["approval_id"],
                         row["failure_kind"], row["failure_reason"])

    def get_job(self, job_id: str) -> JobRecord:
        with self.read_connection() as conn:
            return self._job(self._row(conn, job_id))

    def list_jobs(self) -> list[JobRecord]:
        with self.read_connection() as conn:
            return [self._job(row) for row in conn.execute("SELECT * FROM jobs ORDER BY created_at, job_id")]

    def submit_job(self, spec: JobSpec) -> JobRecord:
        spec = JobSpec.model_validate_json(spec.model_dump_json())
        document = canonical_json(spec.model_dump(mode="json"))
        digest = _digest(spec.model_dump(mode="json"))

        def submit(conn: sqlite3.Connection, now: datetime) -> JobRecord:
            old = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (spec.idempotency_key,)).fetchone()
            if old is not None:
                if old["spec_hash"] != digest:
                    raise IdempotencyConflict("idempotency key is already bound to another job specification")
                return self._job(old)
            if spec.hypothesis_id is not None:
                hypothesis = self._hypothesis(conn, spec.hypothesis_id)
                if spec.experiment_stage in (ExperimentStage.CONFIRMATORY, ExperimentStage.REPLICATION):
                    required = (HypothesisState.TESTING if spec.experiment_stage == ExperimentStage.CONFIRMATORY
                                else HypothesisState.REPLICATING)
                    if hypothesis.status != required:
                        raise InvalidTransition("hypothesis is not in the required scientific phase")
                    plan = hypothesis.preregistration_plan
                    if plan is None or plan.model != spec.model or plan.operation != spec.operation:
                        raise InvalidTransition("job does not match the frozen experiment plan")
            job_id = uuid.uuid4().hex
            conn.execute("""INSERT INTO jobs(job_id,idempotency_key,spec_json,spec_hash,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""", (job_id, spec.idempotency_key, document, digest,
                                              JobState.PENDING, now.timestamp(), now.timestamp()))
            self._event(conn, now, "state_change", {"job_id": job_id, "from": None,
                        "to": JobState.PENDING, "spec_hash": digest})
            return self._job(self._row(conn, job_id))
        return self._submit(submit)

    @staticmethod
    def _batch(conn: sqlite3.Connection, job_ids: Sequence[str]) -> str:
        ids = list(job_ids)
        if not ids or len(ids) != len(set(ids)) or len(ids) > 10000:
            raise ValueError("a batch requires 1..10000 distinct job identifiers")
        records = []
        for job_id in sorted(ids):
            row = Ledger._row(conn, job_id)
            records.append({"job_id": job_id, "spec_hash": row["spec_hash"]})
        return _digest(records)

    def batch_hash(self, job_ids: Sequence[str]) -> str:
        with self.read_connection() as conn:
            return self._batch(conn, job_ids)

    def register_approval(self, nonce: ApprovalNonce) -> None:
        nonce = ApprovalNonce.model_validate({**nonce.model_dump(), "token": nonce.token})
        # Existing research approvals predate the purpose field. Keep their
        # canonical documents stable while giving infrastructure a disjoint scope.
        public = canonical_json(nonce.model_dump(mode="json", exclude={"token", "purpose"}
                                                if nonce.purpose == "research" else {"token"}))
        token_hash = hashlib.sha256(nonce.token.get_secret_value().encode()).hexdigest()

        def register(conn: sqlite3.Connection, now: datetime) -> None:
            if not nonce.issued_at <= now < nonce.expires_at:
                raise ApprovalError("approval is not currently valid")
            old = conn.execute("SELECT document, token_hash FROM approvals WHERE approval_id=?",
                               (nonce.approval_id,)).fetchone()
            if old is not None:
                if old[0] == public and hmac.compare_digest(old[1], token_hash):
                    return
                raise ApprovalError("approval identifier already exists")
            try:
                conn.execute("INSERT INTO approvals(approval_id,document,token_hash) VALUES (?,?,?)",
                             (nonce.approval_id, public, token_hash))
            except sqlite3.IntegrityError as exc:
                raise ApprovalError("approval secret has already been registered") from exc
            self._event(conn, now, "policy_evaluation", {"decision": "approval_registered",
                        "approval_id": nonce.approval_id, "batch_hash": nonce.batch_hash})
        self._submit(register)

    def consume_approval(self, approval_id: str, token: str, *, pod_id: str,
                         job_ids: Sequence[str], live_price_usd_per_hour: float,
                         requested_runtime_seconds: int) -> ApprovalGrant:
        return self._consume_approval(approval_id, token, pod_id=pod_id, job_ids=job_ids,
                                     live_price_usd_per_hour=live_price_usd_per_hour,
                                     requested_runtime_seconds=requested_runtime_seconds)

    def consume_infrastructure_approval(self, approval_id: str, token: str, *, pod_id: str,
                                        infrastructure_hash: str, live_price_usd_per_hour: float,
                                        requested_runtime_seconds: int) -> ApprovalGrant:
        """Consume a human infrastructure allowance with no dispatchable jobs."""
        if (not isinstance(infrastructure_hash, str) or len(infrastructure_hash) != 71 or
                not infrastructure_hash.startswith("sha256:") or
                any(character not in "0123456789abcdef" for character in infrastructure_hash[7:])):
            raise ApprovalError("infrastructure scope must be an immutable SHA256 hash")
        return self._consume_approval(approval_id, token, pod_id=pod_id, job_ids=(),
                                     infrastructure_hash=infrastructure_hash,
                                     live_price_usd_per_hour=live_price_usd_per_hour,
                                     requested_runtime_seconds=requested_runtime_seconds)

    def _consume_approval(self, approval_id: str, token: str, *, pod_id: str,
                          job_ids: Sequence[str], live_price_usd_per_hour: float,
                          requested_runtime_seconds: int, infrastructure_hash: str | None = None) -> ApprovalGrant:
        _positive_seconds(requested_runtime_seconds)
        if isinstance(live_price_usd_per_hour, bool) or not isinstance(live_price_usd_per_hour, (int, float)):
            raise ApprovalError("a finite live price is required")
        if not math.isfinite(live_price_usd_per_hour) or live_price_usd_per_hour < 0:
            raise ApprovalError("a finite nonnegative live price is required")
        if not isinstance(token, str):
            raise ApprovalError("invalid approval secret")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        ids = tuple(job_ids)

        def consume(conn: sqlite3.Connection, now: datetime) -> ApprovalGrant | ApprovalError:
            # A denial is itself durable but never contains the submitted credential.
            try:
                row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
                if row is None or not hmac.compare_digest(row["token_hash"], token_hash):
                    raise ApprovalError("approval authentication failed")
                public = json.loads(row["document"])
                if row["consumed_at"] is not None:
                    raise ApprovalError("approval was already consumed")
                issued = datetime.fromisoformat(public["issued_at"].replace("Z", "+00:00"))
                expires = datetime.fromisoformat(public["expires_at"].replace("Z", "+00:00"))
                if not issued <= now < expires:
                    raise ApprovalError("approval is expired or not yet valid")
                expected_purpose = "infrastructure_preflight" if infrastructure_hash is not None else "research"
                if public.get("purpose", "research") != expected_purpose:
                    raise ApprovalError("approval is for a different purpose")
                actual_hash = infrastructure_hash if infrastructure_hash is not None else self._batch(conn, ids)
                if pod_id != public["pod_id"] or actual_hash != public["batch_hash"]:
                    raise ApprovalError("approval does not match this Pod and batch")
                if any(self._row(conn, jid)["state"] != JobState.PENDING for jid in ids):
                    raise ApprovalError("approved jobs must be pending")
                if any(self._row(conn, jid)["retry_count"] for jid in ids):
                    raise ApprovalError("infrastructure retries cannot receive a new compute interval")
                if requested_runtime_seconds > public["max_runtime_seconds"]:
                    raise ApprovalError("requested runtime exceeds approval")
                if live_price_usd_per_hour >= 1.50 or live_price_usd_per_hour > public["price_ceiling_usd_per_hour"]:
                    raise ApprovalError("live price exceeds the allowed ceiling")
                if conn.execute("SELECT 1 FROM attempts WHERE stopped_at IS NULL").fetchone():
                    raise ApprovalError("an earlier execution has not been confirmed stopped")
                if conn.execute("SELECT 1 FROM jobs WHERE state IN ('DISPATCHED','RUNNING','FINALIZING')").fetchone():
                    raise ApprovalError("previous job finalization remains unresolved")
                if conn.execute("SELECT 1 FROM approvals WHERE consumed_at IS NOT NULL AND ended_at IS NULL").fetchone():
                    raise ApprovalError("previous Pod shutdown has not been confirmed")
            except (ApprovalError, NotFoundError, ValueError) as exc:
                self._event(conn, now, "policy_evaluation", {"decision": "deny_start",
                            "approval_id": approval_id, "reason_code": type(exc).__name__})
                return ApprovalError(str(exc))
            deadline = now + timedelta(seconds=requested_runtime_seconds)
            conn.execute("UPDATE approvals SET consumed_at=?,deadline=? WHERE approval_id=?",
                         (now.timestamp(), deadline.timestamp(), approval_id))
            conn.executemany("INSERT INTO approval_jobs(approval_id,job_id) VALUES (?,?)",
                             [(approval_id, jid) for jid in ids])
            self._event(conn, now, "policy_evaluation", {"decision": "allow_start",
                        "approval_id": approval_id, "pod_id": pod_id,
                        "purpose": expected_purpose,
                        "batch_hash": public["batch_hash"], "deadline": deadline.isoformat(),
                        "live_price_usd_per_hour": live_price_usd_per_hour})
            return ApprovalGrant(approval_id, pod_id, public["batch_hash"], now, deadline)
        result = self._submit(consume)
        if isinstance(result, ApprovalError):
            raise result
        return result

    def end_approval(self, approval_id: str) -> None:
        """Close an interval after the trusted controller confirms the Pod is off."""
        def end(conn: sqlite3.Connection, now: datetime) -> None:
            if conn.execute("SELECT 1 FROM attempts WHERE stopped_at IS NULL").fetchone():
                raise ApprovalError("execution must be confirmed stopped before closing approval")
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None or row["consumed_at"] is None:
                raise ApprovalError("approval has not been consumed")
            if row["ended_at"] is not None:
                return
            conn.execute("UPDATE approvals SET deadline=MIN(deadline,?),ended_at=? WHERE approval_id=?",
                         (now.timestamp(), now.timestamp(), approval_id))
            self._event(conn, now, "policy_evaluation", {"decision": "interval_closed", "approval_id": approval_id})
        self._submit(end)

    @staticmethod
    def _grant_deadline(conn: sqlite3.Connection, approval_id: str, now: datetime) -> float:
        row = conn.execute("SELECT consumed_at,deadline,ended_at FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None or row[0] is None or row[1] <= now.timestamp() or row[2] is not None:
            raise ApprovalError("no active consumed approval")
        return row[1]

    def dispatch_next(self, worker_id: str, *, approval_id: str, lease_seconds: int = 30) -> JobRecord | None:
        _text(worker_id, "worker_id", 128)
        _positive_seconds(lease_seconds, 300)

        def dispatch(conn: sqlite3.Connection, now: datetime) -> JobRecord | None:
            deadline = self._grant_deadline(conn, approval_id, now)
            if conn.execute("SELECT 1 FROM attempts WHERE stopped_at IS NULL").fetchone() or conn.execute(
                    "SELECT 1 FROM jobs WHERE state IN ('DISPATCHED','RUNNING','FINALIZING')").fetchone():
                return None
            rows = conn.execute("""SELECT j.* FROM jobs j JOIN approval_jobs a ON a.job_id=j.job_id
                WHERE a.approval_id=? AND j.state='PENDING' ORDER BY j.created_at,j.job_id""", (approval_id,)).fetchall()
            for row in rows:
                if row["retry_count"] and row["approval_id"] != approval_id:
                    continue
                spec = JobSpec.model_validate_json(row["spec_json"])
                if spec.hypothesis_id and spec.experiment_stage in (ExperimentStage.CONFIRMATORY, ExperimentStage.REPLICATION):
                    required = (HypothesisState.TESTING if spec.experiment_stage == ExperimentStage.CONFIRMATORY
                                else HypothesisState.REPLICATING)
                    if self._hypothesis(conn, spec.hypothesis_id).status != required:
                        continue
                execution_deadline = now.timestamp() + spec.limits.max_runtime_seconds
                if execution_deadline > deadline:
                    continue
                attempt_id = uuid.uuid4().hex
                expiry = min(now.timestamp() + lease_seconds, execution_deadline)
                conn.execute("""INSERT INTO attempts(attempt_id,job_id,attempt_number,worker_id,approval_id,
                    dispatched_at,heartbeat_at,lease_expires_at,execution_deadline) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (attempt_id, row["job_id"], row["attempt_count"] + 1, worker_id, approval_id,
                     now.timestamp(), now.timestamp(), expiry, execution_deadline))
                conn.execute("""UPDATE jobs SET state='DISPATCHED',attempt_id=?,worker_id=?,lease_expires_at=?,
                    attempt_count=attempt_count+1,approval_id=?,updated_at=?,failure_kind=NULL,failure_reason=NULL
                    WHERE job_id=?""", (attempt_id, worker_id, expiry, approval_id, now.timestamp(), row["job_id"]))
                self._event(conn, now, "state_change", {"job_id": row["job_id"], "attempt_id": attempt_id,
                            "from": "PENDING", "to": "DISPATCHED", "worker_id": worker_id,
                            "approval_id": approval_id, "execution_deadline": _time(execution_deadline).isoformat()})
                return self._job(self._row(conn, row["job_id"]))
            return None
        return self._submit(dispatch)

    @staticmethod
    def _owned(conn: sqlite3.Connection, now: datetime, job_id: str, attempt_id: str,
               worker_id: str) -> sqlite3.Row:
        row = Ledger._row(conn, job_id)
        if row["attempt_id"] != attempt_id or row["worker_id"] != worker_id:
            raise LeaseError("attempt does not own this job")
        if row["state"] not in _ACTIVE or row["lease_expires_at"] <= now.timestamp():
            raise LeaseError("execution lease is not active")
        Ledger._grant_deadline(conn, row["approval_id"], now)
        return row

    def _advance(self, job_id: str, attempt_id: str, worker_id: str,
                 expected: JobState, target: JobState) -> JobRecord:
        def advance(conn: sqlite3.Connection, now: datetime) -> JobRecord:
            row = self._owned(conn, now, job_id, attempt_id, worker_id)
            if row["state"] == target:
                return self._job(row)
            if row["state"] != expected:
                raise InvalidTransition(f"{row['state']} cannot transition to {target}")
            conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=?", (target, now.timestamp(), job_id))
            self._event(conn, now, "state_change", {"job_id": job_id, "attempt_id": attempt_id,
                        "from": expected, "to": target})
            return self._job(self._row(conn, job_id))
        return self._submit(advance)

    def start_job(self, job_id: str, attempt_id: str, worker_id: str) -> JobRecord:
        return self._advance(job_id, attempt_id, worker_id, JobState.DISPATCHED, JobState.RUNNING)

    def begin_finalization(self, job_id: str, attempt_id: str, worker_id: str) -> JobRecord:
        return self._advance(job_id, attempt_id, worker_id, JobState.RUNNING, JobState.FINALIZING)

    def heartbeat(self, job_id: str, attempt_id: str, worker_id: str, *, lease_seconds: int = 30) -> JobRecord:
        _positive_seconds(lease_seconds, 300)

        def beat(conn: sqlite3.Connection, now: datetime) -> JobRecord:
            self._owned(conn, now, job_id, attempt_id, worker_id)
            attempt = conn.execute("SELECT execution_deadline FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            expiry = min(now.timestamp() + lease_seconds, attempt[0])
            conn.execute("UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE job_id=?", (expiry, now.timestamp(), job_id))
            conn.execute("UPDATE attempts SET lease_expires_at=?,heartbeat_at=? WHERE attempt_id=?",
                         (expiry, now.timestamp(), attempt_id))
            self._event(conn, now, "heartbeat", {"job_id": job_id, "attempt_id": attempt_id,
                        "lease_expires_at": _time(expiry).isoformat()})
            return self._job(self._row(conn, job_id))
        return self._submit(beat)

    def _fail(self, conn: sqlite3.Connection, now: datetime, row: sqlite3.Row,
              kind: str, reason: str) -> JobRecord:
        conn.execute("UPDATE jobs SET state='FAILED',updated_at=?,failure_kind=?,failure_reason=? WHERE job_id=?",
                     (now.timestamp(), kind, reason, row["job_id"]))
        if row["attempt_id"]:
            conn.execute("UPDATE attempts SET outcome=? WHERE attempt_id=?", (kind, row["attempt_id"]))
        self._event(conn, now, "state_change", {"job_id": row["job_id"], "attempt_id": row["attempt_id"],
                    "from": row["state"], "to": "FAILED", "failure_kind": kind,
                    "reason_hash": _digest(reason)})
        return self._job(self._row(conn, row["job_id"]))

    def fail_job(self, job_id: str, attempt_id: str, worker_id: str, *, failure_kind: str = "scientific",
                 reason: str) -> JobRecord:
        if failure_kind not in _FAILURE_KINDS:
            raise ValueError("unknown failure kind")
        _text(reason, "reason")
        return self._submit(lambda conn, now: self._fail(conn, now,
            self._owned(conn, now, job_id, attempt_id, worker_id), failure_kind, reason))

    def recover_expired(self) -> list[JobRecord]:
        """Mark lost attempts failed; never infer that a remote process has stopped."""
        def recover(conn: sqlite3.Connection, now: datetime) -> list[JobRecord]:
            rows = conn.execute("""SELECT j.*,a.execution_deadline FROM jobs j
                JOIN attempts a ON a.attempt_id=j.attempt_id
                WHERE j.state IN ('DISPATCHED','RUNNING','FINALIZING') AND j.lease_expires_at<=?
                AND NOT (j.state='FINALIZING' AND a.stopped_at IS NOT NULL)""",
                (now.timestamp(),)).fetchall()
            return [self._fail(conn, now, row,
                              "timeout" if row["execution_deadline"] <= now.timestamp() else "infrastructure",
                              "execution deadline elapsed" if row["execution_deadline"] <= now.timestamp()
                              else "execution lease expired") for row in rows]
        return self._submit(recover)

    def confirm_stopped(self, job_id: str, attempt_id: str) -> None:
        """Persist the trusted supervisor's positive termination acknowledgement."""
        def stopped(conn: sqlite3.Connection, now: datetime) -> None:
            row = self._row(conn, job_id)
            if row["attempt_id"] != attempt_id:
                raise LeaseError("stale termination acknowledgement")
            if row["state"] not in (JobState.FINALIZING, JobState.FAILED, JobState.COMPLETED):
                raise InvalidTransition("finish execution before confirming termination")
            attempt = conn.execute("SELECT stopped_at FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise LeaseError("attempt does not exist")
            if attempt[0] is None:
                conn.execute("UPDATE attempts SET stopped_at=? WHERE attempt_id=?", (now.timestamp(), attempt_id))
                self._event(conn, now, "execution_stopped", {"job_id": job_id, "attempt_id": attempt_id})
        self._submit(stopped)

    def retry_job(self, job_id: str, attempt_id: str) -> JobRecord:
        def retry(conn: sqlite3.Connection, now: datetime) -> JobRecord:
            row = self._row(conn, job_id)
            if row["state"] == JobState.PENDING and row["retry_count"] == 1:
                old = conn.execute("SELECT 1 FROM attempts WHERE job_id=? AND attempt_id=? AND stopped_at IS NOT NULL",
                                   (job_id, attempt_id)).fetchone()
                if old:
                    return self._job(row)
            if row["attempt_id"] != attempt_id:
                raise LeaseError("retry references a stale attempt")
            if row["state"] != JobState.FAILED or row["failure_kind"] != "infrastructure" or row["retry_count"] >= 1:
                raise RetryNotAllowed("only one infrastructure retry is permitted")
            attempt = conn.execute("SELECT stopped_at FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None or attempt[0] is None:
                raise RetryNotAllowed("old execution has not been confirmed stopped")
            try:
                deadline = self._grant_deadline(conn, row["approval_id"], now)
            except ApprovalError as exc:
                raise RetryNotAllowed("retry has no remaining approved interval") from exc
            spec = JobSpec.model_validate_json(row["spec_json"])
            if now.timestamp() + spec.limits.max_runtime_seconds > deadline:
                raise RetryNotAllowed("remaining approved interval cannot fit the retry")
            conn.execute("""UPDATE jobs SET state='PENDING',updated_at=?,retry_count=retry_count+1,
                attempt_id=NULL,worker_id=NULL,lease_expires_at=NULL WHERE job_id=?""", (now.timestamp(), job_id))
            self._event(conn, now, "state_change", {"job_id": job_id, "previous_attempt_id": attempt_id,
                        "from": "FAILED", "to": "PENDING", "reason": "infrastructure_retry"})
            return self._job(self._row(conn, job_id))
        return self._submit(retry)

    def cancel_job(self, job_id: str, reason: str = "operator cancelled") -> JobRecord:
        _text(reason, "reason")
        def cancel(conn: sqlite3.Connection, now: datetime) -> JobRecord:
            row = self._row(conn, job_id)
            if row["state"] == JobState.FAILED and row["failure_kind"] == "cancelled":
                return self._job(row)
            if row["state"] in (JobState.COMPLETED, JobState.FAILED):
                raise InvalidTransition("terminal jobs cannot be cancelled")
            return self._fail(conn, now, row, "cancelled", reason)
        return self._submit(cancel)

    @staticmethod
    def operation_hash(spec: JobSpec) -> str:
        """Canonical intervention hash required by completed manifests."""
        return _digest(spec.operation.model_dump(mode="json"))

    @staticmethod
    @contextmanager
    def _directory(path: Path, *, create: bool = False) -> Iterator[int]:
        """Walk every directory component by descriptor, refusing symlinks."""
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.absolute().parts[1:]:
                if part in (".", ".."):
                    raise ArtifactError("artifact directory traversal is forbidden")
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    os.fsync(current)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = child
            yield current
        except OSError as exc:
            raise ArtifactError("artifact directories must exist and cannot be symlinks") from exc
        finally:
            os.close(current)

    @staticmethod
    def _copy_artifacts(manifest: RunManifest, source: Path, destination: Path | None,
                        max_bytes: int) -> int:
        total = 0
        with Ledger._directory(source) as source_fd:
            for artifact in manifest.artifacts:
                parts = Path(artifact.path).parts
                parent = os.dup(source_fd)
                try:
                    for part in parts[:-1]:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        os.close(parent)
                        parent = child
                    fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    with os.fdopen(fd, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                            raise ArtifactError("artifacts must be unshared regular files")
                        if total + before.st_size > max_bytes:
                            raise ArtifactError("artifact bundle exceeds declared output limit")
                        output = None
                        if destination is not None:
                            target = destination / artifact.path
                            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            output = target.open("xb")
                        digest = hashlib.sha256()
                        try:
                            while chunk := stream.read(1024 * 1024):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise ArtifactError("artifact bundle exceeds declared output limit")
                                digest.update(chunk)
                                if output:
                                    output.write(chunk)
                            if output:
                                output.flush()
                                os.fchmod(output.fileno(), 0o400)
                                os.fsync(output.fileno())
                        finally:
                            if output:
                                output.close()
                        after = os.fstat(stream.fileno())
                        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                            raise ArtifactError("artifact changed during verification")
                        if digest.hexdigest() != artifact.sha256:
                            raise ArtifactError("artifact checksum does not match")
                except OSError as exc:
                    raise ArtifactError("artifact is missing or unsafe") from exc
                finally:
                    os.close(parent)
        if total != manifest.cost.bytes_persisted:
            raise ArtifactError("bytes_persisted must equal the retained artifact byte count")
        return total

    def _seal_bundle(self, manifest: RunManifest, source: Path, job_id: str,
                     attempt_id: str, max_bytes: int) -> Path:
        """Copy verified outputs into a private, read-only, attempt-bound bundle.

        Filesystem publication precedes the database commit. A crash can leave an
        orphan bundle; the same attempt/manifest can safely reconcile it later.
        Only this trusted service may write to its artifact-store parent.
        """
        parent = self.path.parent / (self.path.stem + ".artifacts") / job_id
        with self._directory(parent, create=True):
            pass
        final = parent / attempt_id
        seal = canonical_json({"job_id": job_id, "attempt_id": attempt_id,
                               "manifest_hash": _digest(manifest.model_dump(mode="json"))})
        marker = ".probe-bundle.json"
        if any(artifact.path == marker for artifact in manifest.artifacts):
            raise ArtifactError("reserved artifact filename")
        if final.exists() or final.is_symlink():
            with self._directory(final) as fd:
                try:
                    marker_fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                    with os.fdopen(marker_fd, "r") as existing:
                        if existing.read(4096) != seal:
                            raise ArtifactError("existing attempt bundle has different provenance")
                except OSError as exc:
                    raise ArtifactError("existing attempt bundle is unsealed") from exc
            self._copy_artifacts(manifest, final, None, max_bytes)
            return final
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=parent))
        try:
            self._copy_artifacts(manifest, source, staging, max_bytes)
            with (staging / marker).open("x") as stream:
                stream.write(seal)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
            for directory, _, _ in os.walk(staging, topdown=False):
                with self._directory(Path(directory)) as fd:
                    os.fchmod(fd, 0o500)
                    os.fsync(fd)
            os.rename(staging, final)
            with self._directory(parent) as fd:
                os.fsync(fd)
        finally:
            if staging.exists():
                for directory, _, _ in os.walk(staging):
                    os.chmod(directory, 0o700)
                shutil.rmtree(staging)
        return final

    def complete_job(self, job_id: str, attempt_id: str, worker_id: str,
                     manifest: RunManifest, *, artifact_root: str | Path) -> JobRecord:
        manifest = RunManifest.model_validate_json(manifest.model_dump_json())
        document = canonical_json(manifest.model_dump(mode="json"))
        digest = _digest(manifest.model_dump(mode="json"))
        source = Path(artifact_root).absolute()

        def complete(conn: sqlite3.Connection, now: datetime) -> JobRecord:
            row = self._row(conn, job_id)
            if row["attempt_id"] != attempt_id or row["worker_id"] != worker_id:
                raise LeaseError("completion references a stale execution")
            if row["state"] == JobState.COMPLETED:
                old = conn.execute("SELECT digest FROM manifests WHERE job_id=?", (job_id,)).fetchone()
                if old[0] == digest:
                    return self._job(row)
                raise IdempotencyConflict("completed manifest cannot be replaced")
            if row["state"] != JobState.FINALIZING:
                raise InvalidTransition("only FINALIZING jobs can complete")
            attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt["stopped_at"] is None:
                raise InvalidTransition("supervisor must confirm execution stopped before publication")
            spec = JobSpec.model_validate_json(row["spec_json"])
            if manifest.model != spec.model or manifest.inputs != spec.inputs:
                raise ArtifactError("manifest model/inputs do not match the dispatched specification")
            if manifest.run.hypothesis_id != spec.hypothesis_id or manifest.run.approval_id != row["approval_id"]:
                raise ArtifactError("manifest is not bound to this hypothesis and approval")
            if manifest.run.experiment_stage != spec.experiment_stage:
                raise ArtifactError("manifest scientific stage does not match the job")
            if manifest.experiment.intervention_hash != self.operation_hash(spec):
                raise ArtifactError("manifest intervention does not match the dispatched specification")
            tools = {"capture": "capture_activation", "patch": "activation_patch", "ablate": "ablate_component",
                     "steer": "steer_direction", "fit_probe": "fit_probe", "generate": "generate_batch",
                     "weight_stats": "weight_stats", "tensor_slice": "tensor_slice", "module_manifest": "module_manifest", "backend_parity": "backend_parity"}
            if manifest.experiment.tool != tools[spec.operation.kind]:
                raise ArtifactError("manifest tool does not match the dispatched primitive")
            if not attempt["dispatched_at"] <= manifest.run.started_at.timestamp() <= min(now.timestamp(), attempt["execution_deadline"]):
                raise ArtifactError("run start must be inside the execution interval")
            if manifest.cost.gpu_seconds > spec.limits.max_runtime_seconds:
                raise ArtifactError("reported compute exceeds the declared runtime")
            if spec.hypothesis_id:
                hypothesis = self._hypothesis(conn, spec.hypothesis_id)
                if manifest.run.preregistration_hash != hypothesis.preregistration_hash:
                    raise ArtifactError("manifest preregistration does not match the hypothesis")
                if spec.experiment_stage in (ExperimentStage.CONFIRMATORY, ExperimentStage.REPLICATION):
                    plan = hypothesis.preregistration_plan
                    if (plan is None or manifest.experiment.primary_metric != plan.primary_metric
                            or manifest.controls != plan.controls
                            or manifest.experiment.predicted_direction != hypothesis.predicted_direction
                            or manifest.experiment.falsifier != hypothesis.falsifier
                            or manifest.run.started_at < hypothesis.frozen_at):
                        raise ArtifactError("manifest changed a preregistered prediction, metric or control")
            sealed = self._seal_bundle(manifest, source, job_id, attempt_id, spec.limits.max_output_bytes)
            # Execution is positively stopped. CPU publication is recoverable after
            # compute expiry and cannot authorize more GPU work or a replacement attempt.
            finished_at = _utc(self._clock())
            try:
                conn.execute("INSERT INTO manifests(job_id,run_id,document,digest,artifact_root) VALUES (?,?,?,?,?)",
                             (job_id, manifest.run.run_id, document, digest, str(sealed)))
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict("run identifier already has a manifest") from exc
            conn.execute("UPDATE jobs SET state='COMPLETED',updated_at=? WHERE job_id=?", (finished_at.timestamp(), job_id))
            conn.execute("UPDATE attempts SET outcome='completed' WHERE attempt_id=?", (attempt_id,))
            self._event(conn, finished_at, "state_change", {"job_id": job_id, "attempt_id": attempt_id,
                        "from": "FINALIZING", "to": "COMPLETED", "run_id": manifest.run.run_id,
                        "manifest_hash": digest})
            return self._job(self._row(conn, job_id))
        return self._submit(complete)

    def get_artifact_root(self, job_id: str) -> Path:
        """Return the persisted, sealed bundle location of an accepted run."""
        with self.read_connection() as conn:
            row = conn.execute("SELECT artifact_root FROM manifests WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("no accepted artifact bundle for this job")
            return Path(row[0])

    def get_manifest(self, job_id: str) -> RunManifest:
        with self.read_connection() as conn:
            row = conn.execute("SELECT document FROM manifests WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("no completed manifest for this job")
            return RunManifest.model_validate_json(row[0])

    @staticmethod
    def _hypothesis(conn: sqlite3.Connection, hypothesis_id: str) -> HypothesisRecord:
        row = conn.execute("SELECT document FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        if row is None:
            raise NotFoundError("hypothesis does not exist")
        return HypothesisRecord.model_validate_json(row[0])

    def register_hypothesis(self, hypothesis: HypothesisRecord) -> HypothesisRecord:
        hypothesis = HypothesisRecord.model_validate_json(hypothesis.model_dump_json())
        if hypothesis.status != HypothesisState.DRAFT:
            raise InvalidTransition("new hypotheses must start in DRAFT")
        document = canonical_json(hypothesis.model_dump(mode="json"))
        def register(conn: sqlite3.Connection, now: datetime) -> HypothesisRecord:
            old = conn.execute("SELECT document FROM hypotheses WHERE hypothesis_id=?", (hypothesis.hypothesis_id,)).fetchone()
            if old:
                if old[0] == document:
                    return hypothesis
                raise IdempotencyConflict("hypothesis identifier already exists")
            conn.execute("INSERT INTO hypotheses(hypothesis_id,document) VALUES (?,?)", (hypothesis.hypothesis_id, document))
            self._event(conn, now, "state_change", {"hypothesis_id": hypothesis.hypothesis_id,
                        "from": None, "to": "DRAFT", "definition_hash": _digest(hypothesis.model_dump(mode="json"))})
            return hypothesis
        return self._submit(register)

    def get_hypothesis(self, hypothesis_id: str) -> HypothesisRecord:
        with self.read_connection() as conn:
            return self._hypothesis(conn, hypothesis_id)

    def transition_hypothesis(self, hypothesis_id: str, target: HypothesisState | str, *,
                              replication_ids: Sequence[str] | None = None) -> HypothesisRecord:
        target = HypothesisState(target)
        evidence = tuple(replication_ids) if replication_ids is not None else None
        def transition(conn: sqlite3.Connection, now: datetime) -> HypothesisRecord:
            old = self._hypothesis(conn, hypothesis_id)
            if old.status == target:
                if evidence is not None and tuple(old.replication_ids) != evidence:
                    raise InvalidTransition("an idempotent transition cannot replace replication evidence")
                return old
            if target not in _HYPOTHESIS_TRANSITIONS[old.status]:
                raise InvalidTransition(f"{old.status} cannot transition to {target}")
            body = old.model_dump(mode="json")
            if evidence is not None:
                if target != HypothesisState.VALIDATED:
                    raise InvalidTransition("attach replication evidence when validating a hypothesis")
                body["replication_ids"] = list(evidence)
            if target == HypothesisState.VALIDATED:
                ids = body["replication_ids"]
                if not ids or len(ids) != len(set(ids)):
                    raise InvalidTransition("validation requires distinct completed replication evidence")
                for run_id in ids:
                    row = conn.execute("SELECT document FROM manifests WHERE run_id=?", (run_id,)).fetchone()
                    if row is None:
                        raise InvalidTransition("replication evidence is not a completed run")
                    manifest = RunManifest.model_validate_json(row[0])
                    if (manifest.run.hypothesis_id != hypothesis_id
                            or manifest.run.preregistration_hash != old.preregistration_hash
                            or manifest.run.experiment_stage != ExperimentStage.REPLICATION
                            or manifest.results.replication_status != "passed"
                            or manifest.results.effect_size is None):
                        raise InvalidTransition("replication evidence does not support this hypothesis")
            if target == HypothesisState.FROZEN:
                body["preregistration_hash"] = _digest(body)
                body["frozen_at"] = now.isoformat()
            body["status"] = target.value
            new = HypothesisRecord.model_validate(body)
            conn.execute("UPDATE hypotheses SET document=? WHERE hypothesis_id=?",
                         (canonical_json(new.model_dump(mode="json")), hypothesis_id))
            self._event(conn, now, "state_change", {"hypothesis_id": hypothesis_id,
                        "from": old.status, "to": target, "preregistration_hash": new.preregistration_hash})
            return new
        return self._submit(transition)

    def backup(self, destination: str | Path) -> Path:
        """Create a consistent SQLite snapshot; never copy a live database file alone."""
        destination = _require_local_path(Path(destination))
        if destination == self.path or destination.exists():
            raise ValueError("backup destination must be a new, separate local file")
        with self._directory(destination.parent, create=True):
            pass
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            with self.read_connection() as source:
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                finally:
                    target.close()
            fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            with self._directory(destination.parent) as fd:
                os.fsync(fd)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
