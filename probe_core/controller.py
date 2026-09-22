"""Trusted approval controller and independently runnable stop watchdog.

Only the Unix admin endpoint can consume approval. Research clients can propose
work, read status, or stop it. Simulator and explicitly configured live provider
backends share the durable deadline and approval boundary.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid

from .audit import canonical_json
from .compute_timing import startup_dispatch_cutoff
from .ledger import JobState, Ledger
from .provider import (
    ComputeBackend,
    DeploymentSpec,
    ProviderBudgetRefused,
    ProviderLaunchRefused,
    SimulatedProvider,
    StopBackend,
    StopOnlyBackend,
    WorkerState,
)
from .rpc import UnixRPCClient, UnixRPCServer
from .schemas import ApprovalNonce, JobSpec


UTC = timezone.utc
_TERMINAL = {"STOPPED", "REJECTED"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_RECONCILE_ERROR_TYPES = frozenset(
    {
        "ProviderHTTPError",
        "ProviderResponseError",
        "ProviderUncertain",
        "TimeoutError",
        "PermissionError",
        "OSError",
        "ConnectionError",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "RuntimeError",
        "JSONDecodeError",
        "ValidationError",
    }
)
_REQUEST_STATES = frozenset(
    {"PENDING", "PREPARING", "STARTING", "RUNNING", "STOP_REQUESTED", "UNCERTAIN", "STOPPED", "REJECTED"}
)


class ControllerError(RuntimeError):
    pass


class BudgetError(ControllerError):
    pass


class ControllerConflict(ControllerError):
    pass


class WatchdogUnavailable(ControllerError):
    pass


class StartUncertain(ControllerError):
    """The paid action must be reconciled, never automatically retried."""


def _now(clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _identifier(value, name):
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _runtime(value):
    if type(value) is not int or not 1 <= value <= 86400:
        raise ValueError("runtime must be an integer in [1,86400]")
    return value


def _reconciliation_cause(reason, request, *, error=None, observed=None, identity_changed=False):
    """Fixed diagnostics only: never serialize exception text or provider data."""
    state = request["state"]
    cause = {"reason": reason, "request_state": state if state in _REQUEST_STATES else "INVALID"}
    if observed is not None:
        cause["provider_state"] = observed.state.value if isinstance(observed.state, WorkerState) else "INVALID"
        cause["provider_identity_changed"] = identity_changed
    if error is not None:
        name = type(error).__name__
        cause["exception_type"] = name if name in _RECONCILE_ERROR_TYPES else "Exception"
        from .runpod_provider import ProviderHTTPError

        if isinstance(error, ProviderHTTPError):
            metadata = error.metadata
            if (
                type(metadata) is dict
                and type(metadata.get("http_status")) is int
                and 100 <= metadata["http_status"] <= 599
                and type(metadata.get("content_type")) is str
                and metadata["content_type"] in {"json", "html", "other"}
                and (
                    metadata.get("retry_after_seconds") is None
                    or type(metadata["retry_after_seconds"]) is int
                    and 0 <= metadata["retry_after_seconds"] <= 86400
                )
                and type(metadata.get("cf_mitigated_challenge")) is bool
            ):
                cause["http"] = {
                    key: metadata.get(key)
                    for key in ("http_status", "content_type", "retry_after_seconds", "cf_mitigated_challenge")
                }
    return cause


def _finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise BudgetError(f"{name} must be finite and nonnegative")
    return float(value)


def _atomic_json(path: Path, body: dict, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError("watchdog health directory must not be a symlink")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json(body).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class Controller:
    """Trusted service object. Never instantiate this in the research agent.

    Logical worker IDs are reserved before initial creation/replacement. Their
    immutable configuration hash and provider request key bind the later physical
    Pod identity. Core approvals deliberately bind this stable logical identity.
    """

    def __init__(
        self,
        ledger: Ledger,
        backend: ComputeBackend,
        *,
        watchdog_health_path: str | Path,
        controller_idle_usd_per_day: float,
        watchdog_uid: int | None = None,
        clock=None,
    ):
        self.ledger = ledger
        self.backend = backend
        self.health_path = Path(watchdog_health_path).absolute()
        self.overhead = _finite(controller_idle_usd_per_day, "total non-storage idle cost")
        if self.overhead >= 2:
            raise BudgetError("total idle spend must be strictly below $2/day")
        self.watchdog_uid = os.geteuid() if watchdog_uid is None else watchdog_uid
        self.clock = clock or (lambda: datetime.now(UTC))
        self._action_lock = threading.RLock()

        def initialize(connection, now):
            connection.execute("""CREATE TABLE IF NOT EXISTS compute_requests (
                request_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, action TEXT NOT NULL,
                configuration TEXT, configuration_hash TEXT, replaces_worker_id TEXT,
                job_ids TEXT NOT NULL, batch_hash TEXT NOT NULL, max_runtime_seconds INTEGER NOT NULL,
                state TEXT NOT NULL, approval_id TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
                deadline REAL, observed_provider_id TEXT, last_error_code TEXT, infrastructure TEXT)
            """)
            if "infrastructure" not in {row[1] for row in connection.execute("PRAGMA table_info(compute_requests)")}:
                connection.execute("ALTER TABLE compute_requests ADD COLUMN infrastructure TEXT")

        ledger._submit(initialize)

    def _request(
        self, action, worker_id, job_ids, runtime, deployment=None, replaces_worker_id=None, infrastructure=None
    ):
        _identifier(worker_id, "worker ID")
        _runtime(runtime)
        if type(job_ids) is not list or (not job_ids and infrastructure is None) or len(job_ids) > 10000:
            raise ValueError("job_ids must be a nonempty bounded list")
        for job_id in job_ids:
            _identifier(job_id, "job ID")
        if replaces_worker_id is not None:
            _identifier(replaces_worker_id, "replacement worker ID")
        self._deployment_scope(deployment, action, infrastructure, job_ids)
        request_id = "request-" + uuid.uuid4().hex
        approval_id = "approval-" + uuid.uuid4().hex

        def create(connection, now):
            if infrastructure is None:
                # A stopped diagnostic identity cannot later acquire a research
                # allowance through the existing-worker start endpoint.
                previous = connection.execute(
                    "SELECT configuration FROM compute_requests WHERE worker_id=? AND configuration IS NOT NULL",
                    (worker_id,),
                )
                modes = {json.loads(row[0]).get("storage_mode") for row in previous}
                if "ephemeral_preflight" in modes:
                    raise ControllerConflict("ephemeral preflight workers cannot run research jobs")
                if "disposable_research" in modes and action == "START":
                    raise ControllerConflict("disposable research requires a newly approved CREATE or REPLACE")
            batch_hash = (
                self._infrastructure_hash(infrastructure, deployment.digest)
                if infrastructure
                else Ledger._batch(connection, job_ids)
            )
            for job_id in job_ids:
                if Ledger._row(connection, job_id)["state"] != JobState.PENDING:
                    raise ControllerConflict("only pending jobs may request compute")
            connection.execute(
                "INSERT INTO compute_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    worker_id,
                    action,
                    canonical_json(deployment.model_dump()) if deployment else None,
                    deployment.digest if deployment else None,
                    replaces_worker_id,
                    canonical_json(job_ids),
                    batch_hash,
                    runtime,
                    "PENDING",
                    approval_id,
                    now.timestamp(),
                    None,
                    None,
                    None,
                    canonical_json(infrastructure) if infrastructure else None,
                ),
            )
            Ledger._event(
                connection,
                now,
                "policy_evaluation",
                {
                    "decision": "compute_requested",
                    "request_id": request_id,
                    "worker_id": worker_id,
                    "action": action,
                    "batch_hash": batch_hash,
                    "configuration_hash": deployment.digest if deployment else None,
                },
            )
            return self._public(self._row(connection, request_id))

        return self.ledger._submit(create)

    @staticmethod
    def _deployment_scope(deployment, action, infrastructure, job_ids):
        if (
            deployment is not None
            and deployment.storage_mode == "ephemeral_preflight"
            and (action != "CREATE" or not infrastructure or infrastructure.get("kind") != "gpu_preflight" or job_ids)
        ):
            raise ControllerConflict(
                "ephemeral storage is restricted to an infrastructure preflight with no research jobs"
            )
        if (
            deployment is not None
            and deployment.storage_mode == "disposable_research"
            and (action not in {"CREATE", "REPLACE"} or infrastructure is not None or not job_ids)
        ):
            raise ControllerConflict("disposable research requires CREATE or REPLACE with an approved research batch")

    def request_start(self, worker_id: str, job_ids: list[str], max_runtime_seconds: int) -> dict:
        return self._request("START", worker_id, job_ids, max_runtime_seconds)

    def request_provision(
        self,
        deployment: DeploymentSpec | dict,
        job_ids: list[str],
        max_runtime_seconds: int,
        replaces_worker_id: str | None = None,
    ) -> dict:
        deployment = DeploymentSpec.model_validate(deployment)
        return self._request(
            "REPLACE" if replaces_worker_id else "CREATE",
            "worker-" + uuid.uuid4().hex,
            job_ids,
            max_runtime_seconds,
            deployment,
            replaces_worker_id,
        )

    @staticmethod
    def _infrastructure_hash(infrastructure, configuration_hash):
        return (
            "sha256:"
            + hashlib.sha256(
                canonical_json({"infrastructure": infrastructure, "configuration_hash": configuration_hash}).encode()
            ).hexdigest()
        )

    def request_infrastructure_preflight(
        self, deployment: DeploymentSpec | dict, script_sha256: str, max_runtime_seconds: int
    ) -> dict:
        """Human-only diagnostic allowance; it authorizes no scientific job."""
        if type(script_sha256) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", script_sha256):
            raise ValueError("the fixed diagnostic script must be bound by SHA256")
        _runtime(max_runtime_seconds)
        if max_runtime_seconds > 900:
            raise ValueError("infrastructure preflight must be at most 15 minutes")
        deployment = DeploymentSpec.model_validate(deployment)
        infrastructure = {"kind": "gpu_preflight", "script_sha256": script_sha256}
        return self._request(
            "CREATE", "worker-" + uuid.uuid4().hex, [], max_runtime_seconds, deployment, infrastructure=infrastructure
        )

    @staticmethod
    def _row(connection, request_id):
        row = connection.execute("SELECT * FROM compute_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise ControllerConflict("compute request does not exist")
        return row

    @staticmethod
    def _public(row):
        result = dict(row)
        result["job_ids"] = json.loads(result["job_ids"])
        result["configuration"] = json.loads(result["configuration"]) if result["configuration"] else None
        result["infrastructure"] = json.loads(result["infrastructure"]) if result["infrastructure"] else None
        return result

    def status(self) -> list[dict]:
        with self.ledger.read_connection() as connection:
            result = [
                self._public(row)
                for row in connection.execute("SELECT * FROM compute_requests ORDER BY created_at,request_id")
            ]
        capabilities = getattr(self.backend, "capabilities", None)
        if capabilities is not None:
            for request in result:
                request["provider_capabilities"] = capabilities()
        return result

    def _state(self, request_id, state, *, deadline=None, provider_id=None, error=None, reconciliation_cause=None):
        def update(connection, now):
            connection.execute(
                """UPDATE compute_requests SET state=?, deadline=COALESCE(?,deadline),
                observed_provider_id=COALESCE(?,observed_provider_id),last_error_code=? WHERE request_id=?""",
                (state, deadline, provider_id, error, request_id),
            )
            payload = {
                "decision": "compute_" + state.lower(),
                "request_id": request_id,
                "error_code": error,
            }
            if reconciliation_cause is not None:
                payload["reconciliation_cause"] = reconciliation_cause
            Ledger._event(connection, now, "policy_evaluation", payload)
            return self._public(self._row(connection, request_id))

        return self.ledger._submit(update)

    def _watchdog_ready(self):
        try:
            fd = os.open(self.health_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != self.watchdog_uid or info.st_mode & 0o022:
                    raise WatchdogUnavailable("watchdog health file ownership is unsafe")
                body = json.load(stream)
            age = _now(self.clock).timestamp() - body["checked_at"]
            if body["database"] != str(self.ledger.path) or body["healthy"] is not True or not 0 <= age <= 15:
                raise WatchdogUnavailable("independent stop watchdog is not healthy")
            return body
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, WatchdogUnavailable):
                raise
            raise WatchdogUnavailable("independent stop watchdog is not available") from exc

    def _await_watchdog_ack(self, request, deadline, *, timeout_seconds=10):
        until = time.monotonic() + min(timeout_seconds, max(0, deadline - _now(self.clock).timestamp()))
        expected = {"approval_id": request["approval_id"], "worker_id": request["worker_id"], "deadline": deadline}
        while time.monotonic() < until and _now(self.clock).timestamp() < deadline:
            body = self._watchdog_ready()
            if expected in body.get("watched_approvals", []):
                return
            time.sleep(0.025)
        raise WatchdogUnavailable("watchdog did not durably acknowledge this exact compute deadline")

    def approve_and_start(self, request_id: str, *, price_ceiling_usd_per_hour: float = 1.49) -> dict:
        """Human admin endpoint: consume approval once, then attempt one paid action."""
        _identifier(request_id, "request ID")
        ceiling = _finite(price_ceiling_usd_per_hour, "price ceiling")
        if not 0 < ceiling < 1.50:
            raise BudgetError("approved GPU ceiling must be strictly below $1.50/hour")
        with self._action_lock:
            self._watchdog_ready()
            with self.ledger.read_connection() as connection:
                request = self._public(self._row(connection, request_id))
            if request["state"] != "PENDING":
                raise ControllerConflict("request was already considered; uncertain actions cannot be repeated")
            deployment = DeploymentSpec.model_validate(request["configuration"]) if request["configuration"] else None
            self._deployment_scope(deployment, request["action"], request["infrastructure"], request["job_ids"])
            if deployment is None:
                existing = self.backend.status(request["worker_id"])
                if existing.state != WorkerState.STOPPED:
                    raise ControllerConflict("an existing worker must be confirmed stopped")
            else:
                if self.backend.status(request["worker_id"]).state != WorkerState.ABSENT:
                    raise ControllerConflict("reserved creation identity already exists")
                if request["replaces_worker_id"] is not None:
                    prior = self.backend.status(request["replaces_worker_id"])
                    deleted_disposable = False
                    if (
                        deployment.storage_mode == "disposable_research"
                        and prior.state == WorkerState.ABSENT
                        and prior.worker_id == request["replaces_worker_id"]
                        and prior.provider_id is not None
                    ):
                        with self.ledger.read_connection() as connection:
                            previous = connection.execute(
                                "SELECT state,observed_provider_id,configuration FROM compute_requests WHERE worker_id=?",
                                (request["replaces_worker_id"],),
                            ).fetchall()
                        # An empty provider lookup alone cannot close authority.
                        # A disposable identity has exactly one approved interval;
                        # its prior controller shutdown must already be complete.
                        deleted_disposable = (
                            len(previous) == 1
                            and previous[0]["state"] == "STOPPED"
                            and previous[0]["observed_provider_id"] == prior.provider_id
                            and previous[0]["configuration"] is not None
                            and json.loads(previous[0]["configuration"]).get("storage_mode") == "disposable_research"
                        )
                    if prior.state != WorkerState.STOPPED and not deleted_disposable:
                        raise ControllerConflict("replacement requires the previous worker to be confirmed stopped")
            quote = self.backend.quote(
                worker_id=request["worker_id"] if deployment is None else None, deployment=deployment
            )
            price = _finite(quote.usd_per_hour, "provider price")
            idle = _finite(quote.projected_storage_usd_per_day, "provider storage cost") + self.overhead
            age = (_now(self.clock) - quote.checked_at).total_seconds()
            if not 0 <= age <= 30 or not 0 < price <= ceiling or price >= 1.50 or idle >= 2:
                raise BudgetError("provider quote exceeds the price/idle limits or is stale")

            def claim(connection, now):
                row = self._row(connection, request_id)
                if row["state"] != "PENDING":
                    raise ControllerConflict("approval request is already consumed")
                actual_hash = (
                    self._infrastructure_hash(request["infrastructure"], request["configuration_hash"])
                    if request["infrastructure"]
                    else Ledger._batch(connection, request["job_ids"])
                )
                if actual_hash != request["batch_hash"]:
                    raise ControllerConflict("approved batch changed")
                connection.execute("UPDATE compute_requests SET state='PREPARING' WHERE request_id=?", (request_id,))
                Ledger._event(
                    connection,
                    now,
                    "policy_evaluation",
                    {
                        "decision": "human_approved",
                        "request_id": request_id,
                        "configuration_hash": request["configuration_hash"],
                        "batch_hash": request["batch_hash"],
                        "max_runtime_seconds": request["max_runtime_seconds"],
                        "price_ceiling_usd_per_hour": ceiling,
                        "quoted_price_usd_per_hour": price,
                        "projected_idle_usd_per_day": idle,
                    },
                )

            self.ledger._submit(claim)
            now = _now(self.clock)
            approval = ApprovalNonce(
                approval_id=request["approval_id"],
                token=secrets.token_urlsafe(48),
                pod_id=request["worker_id"],
                batch_hash=request["batch_hash"],
                purpose="infrastructure_preflight" if request["infrastructure"] else "research",
                max_runtime_seconds=request["max_runtime_seconds"],
                price_ceiling_usd_per_hour=ceiling,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            try:
                self.ledger.register_approval(approval)
                arguments = dict(
                    pod_id=request["worker_id"],
                    live_price_usd_per_hour=price,
                    requested_runtime_seconds=request["max_runtime_seconds"],
                )
                if request["infrastructure"]:
                    grant = self.ledger.consume_infrastructure_approval(
                        approval.approval_id,
                        approval.token.get_secret_value(),
                        infrastructure_hash=request["batch_hash"],
                        **arguments,
                    )
                else:
                    grant = self.ledger.consume_approval(
                        approval.approval_id, approval.token.get_secret_value(), job_ids=request["job_ids"], **arguments
                    )
                self._state(request_id, "STARTING", deadline=grant.deadline.timestamp())
                self._await_watchdog_ack(request, grant.deadline.timestamp())
            except Exception:
                self._state(request_id, "REJECTED", error="ApprovalPreparationFailed")
                self._close_core_approval(request)
                raise
            try:
                if deployment is not None:
                    self.backend.create(
                        request["worker_id"],
                        deployment,
                        request_key=request_id,
                        price_ceiling_usd_per_hour=ceiling,
                        storage_ceiling_usd_per_day=2 - self.overhead,
                        absolute_deadline=grant.deadline,
                    )
                else:
                    self.backend.start(
                        request["worker_id"],
                        request_key=request_id,
                        price_ceiling_usd_per_hour=ceiling,
                        storage_ceiling_usd_per_day=2 - self.overhead,
                        absolute_deadline=grant.deadline,
                    )
                observed = self.backend.status(request["worker_id"])
                if observed.state != WorkerState.RUNNING or observed.provider_id is None:
                    raise StartUncertain("provider did not confirm the requested running worker")
                if deployment is not None and (
                    observed.request_key != request_id or observed.configuration_hash != deployment.digest
                ):
                    raise StartUncertain("provider resource does not match the approved immutable configuration")
                self._state(request_id, "RUNNING", provider_id=observed.provider_id)
                if _now(self.clock) >= grant.deadline:
                    self.stop_gpu(request["worker_id"])
                with self.ledger.read_connection() as connection:
                    return self._public(self._row(connection, request_id))
            except ProviderLaunchRefused as exc:
                self._close_core_approval(request)
                self._state(request_id, "REJECTED", error=type(exc).__name__)
                if isinstance(exc, ProviderBudgetRefused):
                    raise BudgetError("provider rejected the changed price or storage offer") from exc
                raise ControllerError("provider cannot satisfy the approved launch requirements") from exc
            except Exception as exc:
                self._state(request_id, "UNCERTAIN", error=type(exc).__name__)
                # Stop is repeatable; paid starts and creates never are.
                self.reconcile()
                raise StartUncertain(
                    "provider action was uncertain; inspect reconciliation status before a new approval"
                ) from exc

    def _close_core_approval(self, request):
        for job in self.ledger.list_jobs():
            if job.approval_id != request["approval_id"] or job.attempt_id is None:
                continue
            if job.state in (JobState.DISPATCHED, JobState.RUNNING):
                self.ledger.cancel_job(job.job_id, "controller revoked execution after provider shutdown")
            # A simulated resource state cannot prove a real local worker's
            # process exited. Keep its stop acknowledgement for the dispatcher,
            # which obtains an authenticated, positively stopped worker receipt.
            if getattr(self.backend, "stop_confirms_execution", False) is True:
                self.ledger.confirm_stopped(job.job_id, job.attempt_id)
        with self.ledger.read_connection() as connection:
            unconfirmed = connection.execute(
                "SELECT 1 FROM attempts WHERE approval_id=? AND stopped_at IS NULL", (request["approval_id"],)
            ).fetchone()
            row = connection.execute(
                "SELECT consumed_at,ended_at FROM approvals WHERE approval_id=?", (request["approval_id"],)
            ).fetchone()
        if unconfirmed is not None:
            return False
        if row is not None and row["consumed_at"] is not None and row["ended_at"] is None:
            self.ledger.end_approval(request["approval_id"])
        return True

    def _stop_one(self, request, *, reconciliation_cause=None):
        # The initiating observation commits with STOP_REQUESTED before DELETE;
        # a later shutdown failure cannot replace its append-only evidence.
        self._state(request["request_id"], "STOP_REQUESTED", reconciliation_cause=reconciliation_cause)
        try:
            self.backend.stop(request["worker_id"])
            observed = self.backend.status(request["worker_id"])
            if observed.state == WorkerState.STOPPED or (
                observed.state == WorkerState.ABSENT and request["observed_provider_id"] is not None
            ):
                if not self._close_core_approval(request):
                    return self._state(
                        request["request_id"],
                        "STOP_REQUESTED",
                        provider_id=observed.provider_id,
                        error="WorkerStopPending",
                    )
                return self._state(request["request_id"], "STOPPED", provider_id=observed.provider_id)
            return self._state(request["request_id"], "UNCERTAIN", error="StopNotConfirmed")
        except Exception as exc:
            return self._state(request["request_id"], "UNCERTAIN", error=type(exc).__name__)

    def stop_gpu(self, worker_id: str | None = None) -> list[dict]:
        """Research-visible fail-safe; only controller-owned requests are stopped."""
        if worker_id is not None:
            _identifier(worker_id, "worker ID")
        with self._action_lock:
            return [
                self._stop_one(request)
                for request in self.status()
                if request["state"] not in _TERMINAL | {"PENDING"}
                and (worker_id is None or worker_id == request["worker_id"])
            ]

    def reconcile(self) -> list[dict]:
        """Recover without replaying a start/create, including timeout-after-create."""
        with self._action_lock:
            for request in self.status():
                if request["state"] in _TERMINAL | {"PENDING"}:
                    continue
                if request["state"] == "PREPARING":
                    # STARTING is durably committed before any provider action.
                    self._close_core_approval(request)
                    self._state(request["request_id"], "REJECTED", error="InterruptedBeforeProviderAction")
                    continue
                try:
                    read = getattr(self.backend, "reconcile_status", None)
                    deadline = request["deadline"]
                    if (
                        callable(read)
                        and request["state"] == "RUNNING"
                        and request["observed_provider_id"] is not None
                        and type(deadline) in (int, float)
                        and math.isfinite(deadline)
                        and _now(self.clock).timestamp() < deadline
                    ):
                        observed = read(
                            request["worker_id"], provider_id=request["observed_provider_id"], deadline=deadline
                        )
                    else:
                        observed = self.backend.status(request["worker_id"])
                except Exception as exc:
                    self._stop_one(
                        request, reconciliation_cause=_reconciliation_cause("provider_status_error", request, error=exc)
                    )
                    continue
                identity_changed = (
                    observed.provider_id is not None and observed.provider_id != request["observed_provider_id"]
                )
                if observed.provider_id is not None and observed.provider_id != request["observed_provider_id"]:
                    request = self._state(request["request_id"], request["state"], provider_id=observed.provider_id)
                reason = (
                    "request_not_running"
                    if request["state"] != "RUNNING"
                    else "deadline_missing"
                    if request["deadline"] is None
                    else "approval_deadline"
                    if _now(self.clock).timestamp() >= request["deadline"]
                    else "provider_not_running"
                    if observed.state != WorkerState.RUNNING
                    else None
                )
                if reason is not None:
                    self._stop_one(
                        request,
                        reconciliation_cause=_reconciliation_cause(
                            reason, request, observed=observed, identity_changed=identity_changed
                        ),
                    )
            return self.status()

    def research_dispatch(self, method: str, params: dict):
        handlers = {
            "request_start": self.request_start,
            "request_provision": self.request_provision,
            "status": self.status,
            "stop_gpu": self.stop_gpu,
        }
        if method not in handlers:
            raise PermissionError("method is not available to research clients")
        return handlers[method](**params)

    def admin_dispatch(self, method: str, params: dict):
        handlers = {
            "approve": self.approve_and_start,
            "status": self.status,
            "stop_gpu": self.stop_gpu,
            "reconcile": self.reconcile,
            "request_preflight": self.request_infrastructure_preflight,
        }
        if method not in handlers:
            raise PermissionError("unknown administrative method")
        return handlers[method](**params)


class ControllerClient:
    """Research-side socket client: deliberately has no approval method."""

    def __init__(self, socket_path, *, expected_server_uid, timeout_seconds=30):
        self.rpc = UnixRPCClient(socket_path, expected_server_uid=expected_server_uid, timeout_seconds=timeout_seconds)

    def request_start(self, worker_id, job_ids, max_runtime_seconds):
        return self.rpc.call(
            "request_start", dict(worker_id=worker_id, job_ids=job_ids, max_runtime_seconds=max_runtime_seconds)
        )

    def request_provision(self, deployment, job_ids, max_runtime_seconds, replaces_worker_id=None):
        if isinstance(deployment, DeploymentSpec):
            deployment = deployment.model_dump()
        return self.rpc.call(
            "request_provision",
            dict(
                deployment=deployment,
                job_ids=job_ids,
                max_runtime_seconds=max_runtime_seconds,
                replaces_worker_id=replaces_worker_id,
            ),
        )

    def status(self):
        return self.rpc.call("status")

    def stop_gpu(self, worker_id=None):
        return self.rpc.call("stop_gpu", dict(worker_id=worker_id))


def _calibration_startup_deadline(reader, request, *, now):
    """Recognize only the immutable single-job runner's initial startup.

    Ordinary workers, infrastructure and multi-job approvals keep their existing
    idle policy. Malformed eligibility never grants a startup exception. SQL
    failures propagate so the watchdog stops its cached workers fail-closed.
    """
    try:
        if (
            request["state"] not in {"STARTING", "RUNNING"}
            or request["action"] not in {"CREATE", "REPLACE"}
            or request["infrastructure"] is not None
        ):
            return None
        consumed, deadline, runtime = (
            request["consumed_at"],
            request["absolute_deadline"],
            request["max_runtime_seconds"],
        )
        if (
            type(runtime) is not int
            or not 1 <= runtime <= 900
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in (consumed, deadline))
            or not 0 <= consumed <= now
            or not consumed < deadline <= consumed + runtime
            or deadline != request["deadline"]
        ):
            return None
        deployment = DeploymentSpec.model_validate_json(request["configuration"])
        if deployment.storage_mode != "disposable_research" or deployment.digest != request["configuration_hash"]:
            return None
        job_ids = json.loads(request["job_ids"])
        if type(job_ids) is not list or len(job_ids) != 1 or type(job_ids[0]) is not str:
            return None
        approved = [
            row[0]
            for row in reader.execute("SELECT job_id FROM approval_jobs WHERE approval_id=?", (request["approval_id"],))
        ]
        if approved != job_ids:
            return None
        document = json.loads(request["approval_document"])
        if (
            type(document) is not dict
            or document.get("purpose", "research") != "research"
            or document["approval_id"] != request["approval_id"]
            or document["pod_id"] != request["worker_id"]
            or document["max_runtime_seconds"] != runtime
            or document["batch_hash"] != request["batch_hash"]
        ):
            return None
        job = reader.execute("SELECT * FROM jobs WHERE job_id=?", (job_ids[0],)).fetchone()
        if (
            job is None
            or job["state"] != "PENDING"
            or job["attempt_count"] != 0
            or job["retry_count"] != 0
            or any(job[key] is not None for key in ("attempt_id", "approval_id", "worker_id", "lease_expires_at"))
            or reader.execute(
                "SELECT 1 FROM attempts WHERE approval_id=? OR job_id=? LIMIT 1", (request["approval_id"], job_ids[0])
            ).fetchone()
            is not None
        ):
            return None
        spec = JobSpec.model_validate_json(job["spec_json"])
        spec_hash = "sha256:" + hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest()
        batch = (
            "sha256:"
            + hashlib.sha256(canonical_json([{"job_id": job_ids[0], "spec_hash": spec_hash}]).encode()).hexdigest()
        )
        if (
            spec.experiment_stage.value != "calibration"
            or spec_hash != job["spec_hash"]
            or batch != request["batch_hash"]
            or spec.limits.max_runtime_seconds > runtime
        ):
            return None
        return startup_dispatch_cutoff(deadline, spec.limits.max_runtime_seconds)
    except (ValueError, TypeError, KeyError):
        return None


class StopWatchdog:
    """Read authoritative deadlines and stop through an independently owned adapter.

    This process never opens Ledger, writes its database, creates approvals or
    calls start/create. Its own SQLite file preserves idle timers across restarts.
    """

    def __init__(
        self,
        ledger_path: str | Path,
        backend: StopBackend,
        *,
        state_path: str | Path,
        health_path: str | Path,
        clock=None,
        idle_timeout_seconds: int = 300,
    ):
        if idle_timeout_seconds != 300:
            raise ValueError("the idle stop policy is fixed at five minutes")
        self.database = Path(ledger_path).absolute().resolve()
        self.backend = backend
        self.state_path = Path(state_path).absolute()
        self.health_path = Path(health_path).absolute()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state_path.is_symlink():
            raise ValueError("watchdog state must not be a symlink")
        fd = os.open(self.state_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.state_path)) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS watch_schedule(
                request_id TEXT PRIMARY KEY,snapshot TEXT NOT NULL,idle_since REAL,
                last_stop_reason TEXT,confirmed_off INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1)""")
            connection.commit()

    def tick(self) -> list[dict]:
        now = _now(self.clock).timestamp()
        actions = []
        healthy = True
        live = True
        watched = []
        try:
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5)) as reader:
                reader.row_factory = sqlite3.Row
                reader.execute("PRAGMA query_only=ON")
                reader.execute("BEGIN")
                requests = [
                    dict(row)
                    for row in reader.execute("""SELECT r.*,a.deadline AS absolute_deadline,a.consumed_at,
                    a.document AS approval_document
                    FROM compute_requests r JOIN approvals a ON a.approval_id=r.approval_id
                    WHERE a.consumed_at IS NOT NULL AND a.ended_at IS NULL
                    AND r.state NOT IN ('STOPPED','REJECTED','PENDING')""")
                ]
                active = {
                    row[0]
                    for row in reader.execute("SELECT DISTINCT approval_id FROM attempts WHERE stopped_at IS NULL")
                }
                for request in requests:
                    request["startup_deadline"] = _calibration_startup_deadline(reader, request, now=now)
                    request.pop("approval_document")
                    # A whole attempt can run and stop between watchdog polls.
                    # Its persisted stop time still starts ordinary idle time;
                    # absence of a currently active attempt cannot revive startup.
                    stopped = [
                        row[0]
                        for row in reader.execute(
                            "SELECT stopped_at FROM attempts WHERE approval_id=?", (request["approval_id"],)
                        )
                    ]
                    valid = [
                        value
                        for value in stopped
                        if type(value) in (int, float)
                        and math.isfinite(value)
                        and request["consumed_at"] <= value <= now
                    ]
                    request["latest_stopped_at"] = max(valid) if valid else None
        except Exception:
            # A DB outage must not erase deadlines already acknowledged to the
            # starter. Stop cached workers immediately and keep retrying.
            live = False
            healthy = False
            requests = []
            active = set()
        try:
            with closing(sqlite3.connect(self.state_path)) as state:
                state.execute("PRAGMA synchronous=FULL")
                if live:
                    state.execute("UPDATE watch_schedule SET active=0")
                    for request in requests:
                        state.execute(
                            """INSERT INTO watch_schedule(request_id,snapshot,active) VALUES(?,?,1)
                            ON CONFLICT(request_id) DO UPDATE SET snapshot=excluded.snapshot,active=1""",
                            (request["request_id"], canonical_json(request)),
                        )
                    state.commit()
                else:
                    requests = [
                        json.loads(row[0])
                        for row in state.execute("SELECT snapshot FROM watch_schedule WHERE active=1")
                    ]
                for request in requests:
                    old = state.execute(
                        "SELECT idle_since,last_stop_reason FROM watch_schedule WHERE request_id=?",
                        (request["request_id"],),
                    ).fetchone()
                    idle_since = (
                        -1
                        if request["approval_id"] in active
                        else (
                            request.get("latest_stopped_at")
                            if request.get("latest_stopped_at") is not None
                            else now
                            if old and old[0] == -1
                            else old[0]
                            if old and old[0] is not None
                            else request["consumed_at"]
                        )
                    )
                    state.execute(
                        "UPDATE watch_schedule SET idle_since=? WHERE request_id=?", (idle_since, request["request_id"])
                    )
                    reason = None
                    if not live:
                        reason = "ledger_unavailable"
                    elif now >= request["absolute_deadline"]:
                        reason = "absolute_deadline"
                    elif request["state"] in ("UNCERTAIN", "STOP_REQUESTED"):
                        reason = "uncertain_action"
                    elif old and old[1]:
                        reason = old[1]  # Once stopping is required, activity cannot renew it.
                    elif request.get("startup_deadline") is not None:
                        if now >= request["startup_deadline"]:
                            reason = "startup_deadline"
                    elif idle_since >= 0 and now - idle_since >= 300:
                        reason = "five_minute_idle"
                    if reason is None:
                        continue
                    # Commit the stop decision before invoking the external provider.
                    state.execute(
                        "UPDATE watch_schedule SET last_stop_reason=? WHERE request_id=?",
                        (reason, request["request_id"]),
                    )
                    state.commit()
                    try:
                        self.backend.stop(request["worker_id"])
                        observed = self.backend.status(request["worker_id"])
                        off = observed.state == WorkerState.STOPPED or (
                            observed.state == WorkerState.ABSENT and request["observed_provider_id"] is not None
                        )
                        if not off:
                            healthy = False
                        state.execute(
                            "UPDATE watch_schedule SET confirmed_off=? WHERE request_id=?",
                            (int(off), request["request_id"]),
                        )
                        actions.append({"worker_id": request["worker_id"], "reason": reason, "confirmed_off": off})
                    except Exception:
                        healthy = False
                        actions.append({"worker_id": request["worker_id"], "reason": reason, "confirmed_off": False})
                state.commit()
                watched = [
                    {
                        "approval_id": request["approval_id"],
                        "worker_id": request["worker_id"],
                        "deadline": request["absolute_deadline"],
                    }
                    for request in requests
                ]
        except Exception:
            healthy = False
            raise
        finally:
            _atomic_json(
                self.health_path,
                {"database": str(self.database), "checked_at": now, "healthy": healthy, "watched_approvals": watched},
            )
        return actions


def serve_controller(
    controller,
    research_socket,
    admin_socket,
    *,
    research_uid,
    admin_uid,
    agent_uid,
    socket_gid=None,
    allow_service_uid=False,
):
    for identity in (research_uid, admin_uid, agent_uid):
        if type(identity) is not int or identity < 0:
            raise ValueError("explicit nonnegative OS identities are required")
    if agent_uid in {os.geteuid(), research_uid, admin_uid} or research_uid == admin_uid:
        raise PermissionError("human admin, untrusted agent and trusted services must use different OS identities")
    if admin_uid == os.geteuid() and not allow_service_uid:
        raise PermissionError("human admin and controller must use different OS identities")
    research = UnixRPCServer(
        research_socket,
        controller.research_dispatch,
        allowed_uids={research_uid},
        socket_gid=socket_gid,
        allow_service_uid=True,
    )
    try:
        admin = UnixRPCServer(
            admin_socket,
            controller.admin_dispatch,
            allowed_uids={admin_uid},
            socket_gid=socket_gid,
            allow_service_uid=allow_service_uid,
        )
    except BaseException:
        research.server_close()
        raise
    return research, admin


def main():
    parser = argparse.ArgumentParser(description="Probe trusted control services")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "watchdog"):
        command = sub.add_parser(name)
        command.add_argument("--ledger", required=True)
        providers = command.add_mutually_exclusive_group(required=True)
        providers.add_argument("--provider-state", help="Persistent simulator state")
        if name == "serve":
            providers.add_argument("--provider-config", help="Root-owned live RunPod configuration")
        else:
            providers.add_argument("--stop-socket", help="Independent OS-authenticated stop broker")
        command.add_argument("--watchdog-health", required=True)
    serve = sub.choices["serve"]
    serve.add_argument("--research-socket", required=True)
    serve.add_argument("--admin-socket", required=True)
    serve.add_argument(
        "--research-uid", type=int, required=True, help="UID of the trusted research facade, not the untrusted agent"
    )
    serve.add_argument(
        "--agent-uid",
        type=int,
        required=True,
        help="UID of the untrusted research agent and MCP client; must differ from human/admin services",
    )
    serve.add_argument("--admin-uid", type=int, required=True)
    serve.add_argument("--watchdog-uid", type=int, required=True)
    serve.add_argument("--socket-gid", type=int, required=True)
    serve.add_argument("--idle-overhead", type=float, required=True)
    watchdog = sub.choices["watchdog"]
    watchdog.add_argument("--state", required=True)
    watchdog.add_argument("--stop-server-uid", type=int)
    admin = sub.add_parser("admin", help="Human-only client for the authenticated administrative socket")
    admin.add_argument("--socket", required=True)
    admin.add_argument("--expected-server-uid", type=int, required=True)
    admin.add_argument(
        "--method", choices=("status", "approve", "stop_gpu", "reconcile", "request_preflight"), required=True
    )
    admin.add_argument("--request-id")
    admin.add_argument("--price-ceiling", type=float, default=1.49)
    admin.add_argument("--preflight-file", help="JSON containing deployment, script_sha256 and max_runtime_seconds")
    args = parser.parse_args()
    if args.command == "admin":
        params = {}
        if args.method == "request_preflight":
            if args.preflight_file is None:
                parser.error("--preflight-file is required for an infrastructure request")
            params = json.loads(Path(args.preflight_file).read_text())
        if args.method == "approve":
            if args.request_id is None:
                parser.error("--request-id is required for approval")
            params = {"request_id": args.request_id, "price_ceiling_usd_per_hour": args.price_ceiling}
        print(
            canonical_json(
                UnixRPCClient(args.socket, expected_server_uid=args.expected_server_uid).call(args.method, params)
            )
        )
        return
    if args.provider_state:
        backend = SimulatedProvider(args.provider_state)
    elif args.command == "serve":
        from .runpod_provider import RunPodConfig, RunPodProvider

        backend = RunPodProvider(RunPodConfig.load(args.provider_config))
    else:
        from .runpod_provider import StopBrokerClient

        if args.stop_server_uid is None:
            parser.error("--stop-server-uid is required with --stop-socket")
        backend = StopBrokerClient(args.stop_socket, expected_server_uid=args.stop_server_uid)
    if args.command == "watchdog":
        watcher = StopWatchdog(
            args.ledger, StopOnlyBackend(backend), state_path=args.state, health_path=args.watchdog_health
        )
        while True:
            try:
                watcher.tick()
            except Exception:
                pass  # Health is false and the next tick retries every stop.
            time.sleep(2)
    with Ledger(args.ledger) as ledger:
        controller = Controller(
            ledger,
            backend,
            watchdog_health_path=args.watchdog_health,
            watchdog_uid=args.watchdog_uid,
            controller_idle_usd_per_day=args.idle_overhead,
        )
        controller.reconcile()
        research, admin = serve_controller(
            controller,
            args.research_socket,
            args.admin_socket,
            research_uid=args.research_uid,
            admin_uid=args.admin_uid,
            agent_uid=args.agent_uid,
            socket_gid=args.socket_gid,
        )
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (research, admin)]
        for thread in threads:
            thread.start()
        try:
            while True:
                controller.reconcile()
                time.sleep(2)
        finally:
            research.shutdown()
            admin.shutdown()
            research.server_close()
            admin.server_close()


if __name__ == "__main__":
    main()
