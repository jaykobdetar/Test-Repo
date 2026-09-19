"""Trusted approval controller and independently runnable stop watchdog.

Only the Unix admin endpoint can consume approval. Research clients can propose
work, read status, or stop it. The supplied provider is a persistent simulator;
there are no RunPod credentials or live provider actions in this implementation.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
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
from .ledger import JobState, Ledger
from .provider import ComputeBackend, DeploymentSpec, ProviderBudgetRefused, SimulatedProvider, StopBackend, StopOnlyBackend, WorkerState
from .rpc import UnixRPCClient, UnixRPCServer
from .schemas import ApprovalNonce


UTC = timezone.utc
_TERMINAL = {"STOPPED", "REJECTED"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


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

    def __init__(self, ledger: Ledger, backend: ComputeBackend, *, watchdog_health_path: str | Path,
                 controller_idle_usd_per_day: float, watchdog_uid: int | None = None, clock=None):
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
                deadline REAL, observed_provider_id TEXT, last_error_code TEXT)
            """)
        ledger._submit(initialize)

    def _request(self, action, worker_id, job_ids, runtime, deployment=None, replaces_worker_id=None):
        _identifier(worker_id, "worker ID")
        _runtime(runtime)
        if type(job_ids) is not list or not job_ids or len(job_ids) > 10000:
            raise ValueError("job_ids must be a nonempty bounded list")
        for job_id in job_ids:
            _identifier(job_id, "job ID")
        if replaces_worker_id is not None:
            _identifier(replaces_worker_id, "replacement worker ID")
        request_id = "request-" + uuid.uuid4().hex
        approval_id = "approval-" + uuid.uuid4().hex

        def create(connection, now):
            batch_hash = Ledger._batch(connection, job_ids)
            for job_id in job_ids:
                if Ledger._row(connection, job_id)["state"] != JobState.PENDING:
                    raise ControllerConflict("only pending jobs may request compute")
            connection.execute("INSERT INTO compute_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (request_id, worker_id, action,
                                canonical_json(deployment.model_dump()) if deployment else None,
                                deployment.digest if deployment else None, replaces_worker_id,
                                canonical_json(job_ids), batch_hash, runtime, "PENDING", approval_id,
                                now.timestamp(), None, None, None))
            Ledger._event(connection, now, "policy_evaluation", {
                "decision": "compute_requested", "request_id": request_id,
                "worker_id": worker_id, "action": action, "batch_hash": batch_hash,
                "configuration_hash": deployment.digest if deployment else None,
            })
            return self._public(self._row(connection, request_id))
        return self.ledger._submit(create)

    def request_start(self, worker_id: str, job_ids: list[str], max_runtime_seconds: int) -> dict:
        return self._request("START", worker_id, job_ids, max_runtime_seconds)

    def request_provision(self, deployment: DeploymentSpec | dict, job_ids: list[str],
                          max_runtime_seconds: int, replaces_worker_id: str | None = None) -> dict:
        deployment = DeploymentSpec.model_validate(deployment)
        return self._request("REPLACE" if replaces_worker_id else "CREATE", "worker-" + uuid.uuid4().hex,
                             job_ids, max_runtime_seconds, deployment, replaces_worker_id)

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
        return result

    def status(self) -> list[dict]:
        with self.ledger.read_connection() as connection:
            return [self._public(row) for row in connection.execute("SELECT * FROM compute_requests ORDER BY created_at,request_id")]

    def _state(self, request_id, state, *, deadline=None, provider_id=None, error=None):
        def update(connection, now):
            connection.execute("""UPDATE compute_requests SET state=?, deadline=COALESCE(?,deadline),
                observed_provider_id=COALESCE(?,observed_provider_id),last_error_code=? WHERE request_id=?""",
                               (state, deadline, provider_id, error, request_id))
            Ledger._event(connection, now, "policy_evaluation", {
                "decision": "compute_" + state.lower(), "request_id": request_id, "error_code": error,
            })
            return self._public(self._row(connection, request_id))
        return self.ledger._submit(update)

    def _watchdog_ready(self):
        try:
            fd = os.open(self.health_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.watchdog_uid or info.st_mode & 0o022):
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
            if deployment is None:
                existing = self.backend.status(request["worker_id"])
                if existing.state != WorkerState.STOPPED:
                    raise ControllerConflict("an existing worker must be confirmed stopped")
            else:
                if self.backend.status(request["worker_id"]).state != WorkerState.ABSENT:
                    raise ControllerConflict("reserved creation identity already exists")
                if request["replaces_worker_id"] is not None:
                    if self.backend.status(request["replaces_worker_id"]).state != WorkerState.STOPPED:
                        raise ControllerConflict("replacement requires the previous worker to be confirmed stopped")
            quote = self.backend.quote(worker_id=request["worker_id"] if deployment is None else None,
                                       deployment=deployment)
            price = _finite(quote.usd_per_hour, "provider price")
            idle = _finite(quote.projected_storage_usd_per_day, "provider storage cost") + self.overhead
            age = (_now(self.clock) - quote.checked_at).total_seconds()
            if not 0 <= age <= 30 or not 0 < price <= ceiling or price >= 1.50 or idle >= 2:
                raise BudgetError("provider quote exceeds the price/idle limits or is stale")

            def claim(connection, now):
                row = self._row(connection, request_id)
                if row["state"] != "PENDING":
                    raise ControllerConflict("approval request is already consumed")
                if Ledger._batch(connection, request["job_ids"]) != request["batch_hash"]:
                    raise ControllerConflict("approved batch changed")
                connection.execute("UPDATE compute_requests SET state='PREPARING' WHERE request_id=?", (request_id,))
                Ledger._event(connection, now, "policy_evaluation", {
                    "decision": "human_approved", "request_id": request_id,
                    "configuration_hash": request["configuration_hash"], "batch_hash": request["batch_hash"],
                    "max_runtime_seconds": request["max_runtime_seconds"], "price_ceiling_usd_per_hour": ceiling,
                    "quoted_price_usd_per_hour": price, "projected_idle_usd_per_day": idle,
                })
            self.ledger._submit(claim)
            now = _now(self.clock)
            approval = ApprovalNonce(
                approval_id=request["approval_id"], token=secrets.token_urlsafe(48),
                pod_id=request["worker_id"], batch_hash=request["batch_hash"],
                max_runtime_seconds=request["max_runtime_seconds"], price_ceiling_usd_per_hour=ceiling,
                issued_at=now, expires_at=now + timedelta(minutes=5),
            )
            try:
                self.ledger.register_approval(approval)
                grant = self.ledger.consume_approval(
                    approval.approval_id, approval.token.get_secret_value(), pod_id=request["worker_id"],
                    job_ids=request["job_ids"], live_price_usd_per_hour=price,
                    requested_runtime_seconds=request["max_runtime_seconds"],
                )
                self._state(request_id, "STARTING", deadline=grant.deadline.timestamp())
                self._await_watchdog_ack(request, grant.deadline.timestamp())
            except Exception:
                self._state(request_id, "REJECTED", error="ApprovalPreparationFailed")
                self._close_core_approval(request)
                raise
            try:
                if deployment is not None:
                    self.backend.create(request["worker_id"], deployment, request_key=request_id,
                                        price_ceiling_usd_per_hour=ceiling, storage_ceiling_usd_per_day=2 - self.overhead)
                else:
                    self.backend.start(request["worker_id"], request_key=request_id,
                                       price_ceiling_usd_per_hour=ceiling, storage_ceiling_usd_per_day=2 - self.overhead)
                observed = self.backend.status(request["worker_id"])
                if observed.state != WorkerState.RUNNING or observed.provider_id is None:
                    raise StartUncertain("provider did not confirm the requested running worker")
                if deployment is not None and (observed.request_key != request_id or observed.configuration_hash != deployment.digest):
                    raise StartUncertain("provider resource does not match the approved immutable configuration")
                self._state(request_id, "RUNNING", provider_id=observed.provider_id)
                if _now(self.clock) >= grant.deadline:
                    self.stop_gpu(request["worker_id"])
                with self.ledger.read_connection() as connection:
                    return self._public(self._row(connection, request_id))
            except ProviderBudgetRefused as exc:
                self._close_core_approval(request)
                self._state(request_id, "REJECTED", error="ProviderBudgetRefused")
                raise BudgetError("provider rejected the changed price or storage offer") from exc
            except Exception as exc:
                self._state(request_id, "UNCERTAIN", error=type(exc).__name__)
                # Stop is repeatable; paid starts and creates never are.
                self.reconcile()
                raise StartUncertain("provider action was uncertain; inspect reconciliation status before a new approval") from exc

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
            unconfirmed = connection.execute("SELECT 1 FROM attempts WHERE approval_id=? AND stopped_at IS NULL",
                                             (request["approval_id"],)).fetchone()
            row = connection.execute("SELECT consumed_at,ended_at FROM approvals WHERE approval_id=?",
                                     (request["approval_id"],)).fetchone()
        if unconfirmed is not None:
            return False
        if row is not None and row["consumed_at"] is not None and row["ended_at"] is None:
            self.ledger.end_approval(request["approval_id"])
        return True

    def _stop_one(self, request):
        self._state(request["request_id"], "STOP_REQUESTED")
        try:
            self.backend.stop(request["worker_id"])
            observed = self.backend.status(request["worker_id"])
            if observed.state == WorkerState.STOPPED or (
                    observed.state == WorkerState.ABSENT and request["observed_provider_id"] is not None):
                if not self._close_core_approval(request):
                    return self._state(request["request_id"], "STOP_REQUESTED", provider_id=observed.provider_id,
                                       error="WorkerStopPending")
                return self._state(request["request_id"], "STOPPED", provider_id=observed.provider_id)
            return self._state(request["request_id"], "UNCERTAIN", error="StopNotConfirmed")
        except Exception as exc:
            return self._state(request["request_id"], "UNCERTAIN", error=type(exc).__name__)

    def stop_gpu(self, worker_id: str | None = None) -> list[dict]:
        """Research-visible fail-safe; only controller-owned requests are stopped."""
        if worker_id is not None:
            _identifier(worker_id, "worker ID")
        with self._action_lock:
            return [self._stop_one(request) for request in self.status()
                    if request["state"] not in _TERMINAL | {"PENDING"}
                    and (worker_id is None or worker_id == request["worker_id"])]

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
                    observed = self.backend.status(request["worker_id"])
                except Exception:
                    self._stop_one(request)
                    continue
                if observed.provider_id is not None and observed.provider_id != request["observed_provider_id"]:
                    request = self._state(request["request_id"], request["state"], provider_id=observed.provider_id)
                if (request["state"] != "RUNNING" or request["deadline"] is None or
                        _now(self.clock).timestamp() >= request["deadline"] or observed.state != WorkerState.RUNNING):
                    self._stop_one(request)
            return self.status()

    def research_dispatch(self, method: str, params: dict):
        handlers = {"request_start": self.request_start, "request_provision": self.request_provision,
                    "status": self.status, "stop_gpu": self.stop_gpu}
        if method not in handlers:
            raise PermissionError("method is not available to research clients")
        return handlers[method](**params)

    def admin_dispatch(self, method: str, params: dict):
        handlers = {"approve": self.approve_and_start, "status": self.status,
                    "stop_gpu": self.stop_gpu, "reconcile": self.reconcile}
        if method not in handlers:
            raise PermissionError("unknown administrative method")
        return handlers[method](**params)


class ControllerClient:
    """Research-side socket client: deliberately has no approval method."""

    def __init__(self, socket_path, *, expected_server_uid, timeout_seconds=30):
        self.rpc = UnixRPCClient(socket_path, expected_server_uid=expected_server_uid,
                                 timeout_seconds=timeout_seconds)

    def request_start(self, worker_id, job_ids, max_runtime_seconds):
        return self.rpc.call("request_start", dict(worker_id=worker_id, job_ids=job_ids, max_runtime_seconds=max_runtime_seconds))

    def request_provision(self, deployment, job_ids, max_runtime_seconds, replaces_worker_id=None):
        if isinstance(deployment, DeploymentSpec):
            deployment = deployment.model_dump()
        return self.rpc.call("request_provision", dict(deployment=deployment, job_ids=job_ids,
            max_runtime_seconds=max_runtime_seconds, replaces_worker_id=replaces_worker_id))

    def status(self):
        return self.rpc.call("status")

    def stop_gpu(self, worker_id=None):
        return self.rpc.call("stop_gpu", dict(worker_id=worker_id))


class StopWatchdog:
    """Read authoritative deadlines and stop through an independently owned adapter.

    This process never opens Ledger, writes its database, creates approvals or
    calls start/create. Its own SQLite file preserves idle timers across restarts.
    """

    def __init__(self, ledger_path: str | Path, backend: StopBackend, *, state_path: str | Path,
                 health_path: str | Path, clock=None, idle_timeout_seconds: int = 300):
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
                requests = [dict(row) for row in reader.execute("""SELECT r.*,a.deadline AS absolute_deadline,a.consumed_at
                    FROM compute_requests r JOIN approvals a ON a.approval_id=r.approval_id
                    WHERE a.consumed_at IS NOT NULL AND a.ended_at IS NULL
                    AND r.state NOT IN ('STOPPED','REJECTED','PENDING')""")]
                active = {row[0] for row in reader.execute("SELECT DISTINCT approval_id FROM attempts WHERE stopped_at IS NULL")}
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
                        state.execute("""INSERT INTO watch_schedule(request_id,snapshot,active) VALUES(?,?,1)
                            ON CONFLICT(request_id) DO UPDATE SET snapshot=excluded.snapshot,active=1""",
                            (request["request_id"], canonical_json(request)))
                    state.commit()
                else:
                    requests = [json.loads(row[0]) for row in state.execute("SELECT snapshot FROM watch_schedule WHERE active=1")]
                for request in requests:
                    old = state.execute("SELECT idle_since,last_stop_reason FROM watch_schedule WHERE request_id=?",
                                        (request["request_id"],)).fetchone()
                    idle_since = -1 if request["approval_id"] in active else (
                        now if old and old[0] == -1 else
                        old[0] if old and old[0] is not None else request["consumed_at"])
                    state.execute("UPDATE watch_schedule SET idle_since=? WHERE request_id=?",
                                  (idle_since, request["request_id"]))
                    reason = None
                    if not live:
                        reason = "ledger_unavailable"
                    elif now >= request["absolute_deadline"]:
                        reason = "absolute_deadline"
                    elif request["state"] in ("UNCERTAIN", "STOP_REQUESTED"):
                        reason = "uncertain_action"
                    elif old and old[1]:
                        reason = old[1]  # Once stopping is required, activity cannot renew it.
                    elif idle_since >= 0 and now - idle_since >= 300:
                        reason = "five_minute_idle"
                    if reason is None:
                        continue
                    # Commit the stop decision before invoking the external provider.
                    state.execute("UPDATE watch_schedule SET last_stop_reason=? WHERE request_id=?", (reason, request["request_id"]))
                    state.commit()
                    try:
                        self.backend.stop(request["worker_id"])
                        observed = self.backend.status(request["worker_id"])
                        off = observed.state == WorkerState.STOPPED or (observed.state == WorkerState.ABSENT and request["observed_provider_id"] is not None)
                        if not off:
                            healthy = False
                        state.execute("UPDATE watch_schedule SET confirmed_off=? WHERE request_id=?", (int(off), request["request_id"]))
                        actions.append({"worker_id": request["worker_id"], "reason": reason, "confirmed_off": off})
                    except Exception:
                        healthy = False
                        actions.append({"worker_id": request["worker_id"], "reason": reason, "confirmed_off": False})
                state.commit()
                watched = [{"approval_id": request["approval_id"], "worker_id": request["worker_id"],
                            "deadline": request["absolute_deadline"]} for request in requests]
        except Exception:
            healthy = False
            raise
        finally:
            _atomic_json(self.health_path, {"database": str(self.database), "checked_at": now,
                                           "healthy": healthy, "watched_approvals": watched})
        return actions


def serve_controller(controller, research_socket, admin_socket, *, research_uid, admin_uid, agent_uid,
                     socket_gid=None, allow_service_uid=False):
    for identity in (research_uid, admin_uid, agent_uid):
        if type(identity) is not int or identity < 0:
            raise ValueError("explicit nonnegative OS identities are required")
    if agent_uid in {os.geteuid(), research_uid, admin_uid} or research_uid == admin_uid:
        raise PermissionError("human admin, untrusted agent and trusted services must use different OS identities")
    if admin_uid == os.geteuid() and not allow_service_uid:
        raise PermissionError("human admin and controller must use different OS identities")
    research = UnixRPCServer(research_socket, controller.research_dispatch, allowed_uids={research_uid},
                            socket_gid=socket_gid, allow_service_uid=True)
    try:
        admin = UnixRPCServer(admin_socket, controller.admin_dispatch, allowed_uids={admin_uid},
                             socket_gid=socket_gid, allow_service_uid=allow_service_uid)
    except BaseException:
        research.server_close()
        raise
    return research, admin


def main():
    parser = argparse.ArgumentParser(description="Probe trusted control services (simulator only)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "watchdog"):
        command = sub.add_parser(name)
        command.add_argument("--ledger", required=True)
        command.add_argument("--provider-state", required=True)
        command.add_argument("--watchdog-health", required=True)
    serve = sub.choices["serve"]
    serve.add_argument("--research-socket", required=True)
    serve.add_argument("--admin-socket", required=True)
    serve.add_argument("--research-uid", type=int, required=True,
                       help="UID of the trusted research facade, not the untrusted agent")
    serve.add_argument("--agent-uid", type=int, required=True,
                       help="UID of the untrusted research agent and MCP client; must differ from human/admin services")
    serve.add_argument("--admin-uid", type=int, required=True)
    serve.add_argument("--watchdog-uid", type=int, required=True)
    serve.add_argument("--socket-gid", type=int, required=True)
    serve.add_argument("--idle-overhead", type=float, required=True)
    watchdog = sub.choices["watchdog"]
    watchdog.add_argument("--state", required=True)
    admin = sub.add_parser("admin", help="Human-only client for the authenticated administrative socket")
    admin.add_argument("--socket", required=True)
    admin.add_argument("--expected-server-uid", type=int, required=True)
    admin.add_argument("--method", choices=("status", "approve", "stop_gpu", "reconcile"), required=True)
    admin.add_argument("--request-id")
    admin.add_argument("--price-ceiling", type=float, default=1.49)
    args = parser.parse_args()
    if args.command == "admin":
        params = {}
        if args.method == "approve":
            if args.request_id is None:
                parser.error("--request-id is required for approval")
            params = {"request_id": args.request_id, "price_ceiling_usd_per_hour": args.price_ceiling}
        print(canonical_json(UnixRPCClient(args.socket, expected_server_uid=args.expected_server_uid).call(args.method, params)))
        return
    backend = SimulatedProvider(args.provider_state)
    if args.command == "watchdog":
        watcher = StopWatchdog(args.ledger, StopOnlyBackend(backend), state_path=args.state,
                               health_path=args.watchdog_health)
        while True:
            try:
                watcher.tick()
            except Exception:
                pass  # Health is false and the next tick retries every stop.
            time.sleep(2)
    with Ledger(args.ledger) as ledger:
        controller = Controller(ledger, backend, watchdog_health_path=args.watchdog_health,
                                watchdog_uid=args.watchdog_uid, controller_idle_usd_per_day=args.idle_overhead)
        controller.reconcile()
        research, admin = serve_controller(controller, args.research_socket, args.admin_socket,
                                           research_uid=args.research_uid, admin_uid=args.admin_uid,
                                           agent_uid=args.agent_uid,
                                           socket_gid=args.socket_gid)
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
