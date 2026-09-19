"""Provider contracts and a persistent simulator. This module never calls a cloud API."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Protocol
import uuid

from pydantic import BaseModel, ConfigDict, Field

from .audit import canonical_json


class DeploymentSpec(BaseModel):
    """The complete immutable configuration approved for simulator provisioning."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    gpu_model: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_. -]+$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    volume_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    region: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    volume_gb: int = Field(ge=1, le=1000)
    gpu_count: int = Field(default=1, ge=1, le=1)

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(self.model_dump()).encode()).hexdigest()


class WorkerState(StrEnum):
    ABSENT = "ABSENT"
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    STARTING = "STARTING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class WorkerStatus:
    worker_id: str
    state: WorkerState
    provider_id: str | None = None
    request_key: str | None = None
    configuration_hash: str | None = None

    def as_dict(self) -> dict:
        result = asdict(self)
        result["state"] = self.state.value
        return result


@dataclass(frozen=True)
class PriceQuote:
    usd_per_hour: float
    projected_storage_usd_per_day: float
    checked_at: datetime


class StopBackend(Protocol):
    def status(self, worker_id: str) -> WorkerStatus: ...
    def stop(self, worker_id: str) -> None: ...


class ComputeBackend(StopBackend, Protocol):
    """Quotes must include all persistent storage, including detached volumes.

    A live adapter must enforce the quoted ceilings at purchase, use bounded
    request timeouts, and support lookup of uncertain creates by logical identity.

    ``stop_confirms_execution`` may be true only when a positively observed
    provider shutdown proves that every executor on that worker has terminated.
    A simulator that only updates metadata cannot supply that evidence.
    """
    stop_confirms_execution: bool
    def quote(self, *, worker_id: str | None = None, deployment: DeploymentSpec | None = None) -> PriceQuote: ...
    def create(self, worker_id: str, deployment: DeploymentSpec, *, request_key: str,
               price_ceiling_usd_per_hour: float, storage_ceiling_usd_per_day: float) -> WorkerStatus: ...
    def start(self, worker_id: str, *, request_key: str,
              price_ceiling_usd_per_hour: float, storage_ceiling_usd_per_day: float) -> WorkerStatus: ...


class ProviderBudgetRefused(RuntimeError):
    """A definitive provider refusal: no paid operation was submitted."""


class StopOnlyBackend:
    """Restrict the watchdog's adapter interface to status and stop.

    A live implementation must additionally use a provider-enforced stop-only
    credential. This wrapper is not a substitute for that credential boundary.
    """

    def __init__(self, backend: StopBackend):
        self.__backend = backend

    def status(self, worker_id: str) -> WorkerStatus:
        return self.__backend.status(worker_id)

    def stop(self, worker_id: str) -> None:
        self.__backend.stop(worker_id)


class SimulatedProvider:
    """Persistent, crash-observable provider model shared by separate processes.

    A logical worker ID is reserved locally before approval and maps uniquely to
    one provider resource. Creation is tagged with the durable request key. Real
    adapters must preserve that lookup contract, including ambiguous timeouts.
    """

    stop_confirms_execution = False

    def __init__(self, path: str | Path, *, price_usd_per_hour: float = 0.50, clock=None):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("simulator database must not be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS simulator_settings (id INTEGER PRIMARY KEY CHECK(id=1), price REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS simulator_workers (
                    worker_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL UNIQUE,
                    request_key TEXT NOT NULL UNIQUE, configuration TEXT NOT NULL,
                    configuration_hash TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS simulator_calls (
                    sequence INTEGER PRIMARY KEY, operation TEXT NOT NULL,
                    worker_id TEXT NOT NULL, request_key TEXT);
            """)
            connection.execute("INSERT OR IGNORE INTO simulator_settings VALUES(1,?)", (price_usd_per_hour,))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _status(worker_id: str, row) -> WorkerStatus:
        if row is None:
            return WorkerStatus(worker_id, WorkerState.ABSENT)
        return WorkerStatus(worker_id, WorkerState(row["state"]), row["provider_id"],
                            row["request_key"], row["configuration_hash"])

    def status(self, worker_id: str) -> WorkerStatus:
        with self._connect() as connection:
            return self._status(worker_id, connection.execute(
                "SELECT * FROM simulator_workers WHERE worker_id=?", (worker_id,)).fetchone())

    def quote(self, *, worker_id=None, deployment=None) -> PriceQuote:
        if (worker_id is None) == (deployment is None):
            raise ValueError("quote exactly one existing worker or deployment")
        with self._connect() as connection:
            if worker_id is not None and connection.execute(
                    "SELECT 1 FROM simulator_workers WHERE worker_id=?", (worker_id,)).fetchone() is None:
                raise ValueError("worker does not exist")
            volumes = {}
            for row in connection.execute("SELECT configuration FROM simulator_workers"):
                config = json.loads(row[0])
                volumes[config["volume_id"]] = max(volumes.get(config["volume_id"], 0), config["volume_gb"])
            if deployment is not None:
                volumes[deployment.volume_id] = max(volumes.get(deployment.volume_id, 0), deployment.volume_gb)
            price = connection.execute("SELECT price FROM simulator_settings WHERE id=1").fetchone()[0]
        # A simulator fixture, not a claim about current RunPod pricing.
        storage = sum(volumes.values()) * 0.07 / 30.44
        return PriceQuote(price, storage, self.clock())

    def set_price(self, price: float) -> None:
        """Administrator-only simulator control; never an input to controller.start."""
        with self._connect() as connection:
            connection.execute("UPDATE simulator_settings SET price=? WHERE id=1", (price,))

    @staticmethod
    def _check_budget(connection, deployment, price_ceiling, storage_ceiling):
        price = connection.execute("SELECT price FROM simulator_settings WHERE id=1").fetchone()[0]
        if (not math.isfinite(price) or not 0 < price <= price_ceiling or price >= 1.50):
            raise ProviderBudgetRefused("provider price no longer satisfies approval")
        volumes = {}
        for row in connection.execute("SELECT configuration FROM simulator_workers"):
            config = json.loads(row[0])
            volumes[config["volume_id"]] = max(volumes.get(config["volume_id"], 0), config["volume_gb"])
        if deployment:
            volumes[deployment.volume_id] = max(volumes.get(deployment.volume_id, 0), deployment.volume_gb)
        if sum(volumes.values()) * 0.07 / 30.44 >= storage_ceiling:
            raise ProviderBudgetRefused("provider storage no longer satisfies idle budget")

    def create(self, worker_id: str, deployment: DeploymentSpec, *, request_key: str,
               price_ceiling_usd_per_hour: float = 1.49, storage_ceiling_usd_per_day: float = 2.0) -> WorkerStatus:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._check_budget(connection, deployment, price_ceiling_usd_per_hour, storage_ceiling_usd_per_day)
                old = connection.execute("SELECT * FROM simulator_workers WHERE worker_id=? OR request_key=?",
                                         (worker_id, request_key)).fetchone()
                if old is not None:
                    if old["worker_id"] != worker_id or old["configuration_hash"] != deployment.digest:
                        raise ValueError("provider creation identity conflict")
                    connection.execute("COMMIT")
                    return self._status(worker_id, old)
                provider_id = "sim-" + uuid.uuid4().hex
                connection.execute("INSERT INTO simulator_workers VALUES(?,?,?,?,?,?)",
                                   (worker_id, provider_id, request_key, canonical_json(deployment.model_dump()),
                                    deployment.digest, WorkerState.RUNNING.value))
                connection.execute("INSERT INTO simulator_calls(operation,worker_id,request_key) VALUES('create',?,?)",
                                   (worker_id, request_key))
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return self.status(worker_id)

    def start(self, worker_id: str, *, request_key: str,
              price_ceiling_usd_per_hour: float = 1.49, storage_ceiling_usd_per_day: float = 2.0) -> WorkerStatus:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._check_budget(connection, None, price_ceiling_usd_per_hour, storage_ceiling_usd_per_day)
                row = connection.execute("SELECT state FROM simulator_workers WHERE worker_id=?", (worker_id,)).fetchone()
                if row is None or row[0] != WorkerState.STOPPED.value:
                    raise ValueError("only an existing stopped worker may start")
                connection.execute("UPDATE simulator_workers SET state='RUNNING' WHERE worker_id=?", (worker_id,))
                connection.execute("INSERT INTO simulator_calls(operation,worker_id,request_key) VALUES('start',?,?)",
                                   (worker_id, request_key))
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return self.status(worker_id)

    def stop(self, worker_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("UPDATE simulator_workers SET state='STOPPED' WHERE worker_id=?", (worker_id,))
                connection.execute("INSERT INTO simulator_calls(operation,worker_id) VALUES('stop',?)", (worker_id,))
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def calls(self) -> list[dict]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM simulator_calls ORDER BY sequence")]
