"""Price-capped RunPod creation, inventory reconciliation and an OS stop broker.

RunPod REST v2 has no atomic purchase ceiling or documented shutdown deadline.
The legacy GraphQL create supports ``deployCost`` and ``stopAfter``. The former
is documented as a ceiling by RunPod's CLI; the latter has no verified delivery
guarantee. Consequently this adapter permits only explicitly configured, short,
supervised acceptance runs. It refuses unattended operation and all resumes.
No account credential is sent to the worker or the stop watchdog.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .audit import canonical_json, validate_audit_payload
from .provider import DeploymentSpec, PriceQuote, ProviderBudgetRefused, ProviderLaunchRefused, WorkerState, WorkerStatus
from .rpc import UnixRPCClient, UnixRPCServer


UTC = timezone.utc
_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_CREATE = """mutation ProbeCreate($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) { id }
}"""


class ProviderCapabilityError(ProviderLaunchRefused):
    """A required provider guarantee is unsupported; no purchase was submitted."""


class ProviderUncertain(RuntimeError):
    """The request may have reached RunPod. Reconcile; never replay a create."""


class ProviderResponseError(RuntimeError):
    pass


def provider_http_metadata(status, headers=None):
    """Fixed HTTP diagnostics only; never retain headers, bodies or addresses."""
    if type(status) is not int or not 100 <= status <= 599:
        raise ProviderResponseError("provider HTTP status is invalid")

    def header(name):
        value = headers.get(name, "") if headers is not None else ""
        return value.strip() if type(value) is str and len(value) <= 256 else ""

    media = header("Content-Type").split(";", 1)[0].lower()
    content_type = ("json" if media == "application/json" or media.startswith("application/") and media.endswith("+json")
                    else "html" if media in {"text/html", "application/xhtml+xml"} else "other")
    retry = header("Retry-After")
    retry_seconds = int(retry) if re.fullmatch(r"[0-9]{1,5}", retry) and int(retry) <= 86400 else None
    return {"http_status": status, "content_type": content_type,
            "retry_after_seconds": retry_seconds, "cf_mitigated_challenge": header("cf-mitigated") == "challenge"}


class ProviderHTTPError(ProviderResponseError):
    def __init__(self, status: int, *, headers=None):
        self.metadata = provider_http_metadata(status, headers)
        self.status = status
        super().__init__(f"RunPod HTTP status {status}")


class RunPodLaunchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    image_repository: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
    args: str = Field(default="", max_length=16000)
    container_disk_gb: int = Field(default=20, ge=1, le=200)
    min_cuda_version: str = Field(default="13.0", pattern=r"^\d+\.\d+$")
    start_ssh: bool = False
    ports: tuple[str, ...] = ()
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("ports")
    @classmethod
    def supported_ports(cls, ports):
        if any(not re.fullmatch(r"[0-9]{1,5}/tcp", port) or not 1 <= int(port.split('/')[0]) <= 65535 for port in ports):
            raise ValueError("only explicit valid TCP ports are supported")
        return ports

    @field_validator("environment")
    @classmethod
    def allowed_environment(cls, environment):
        if set(environment) - {"PUBLIC_KEY", "PROBE_CGROUP_ROOT"}:
            raise ValueError("only the dedicated SSH public key and delegated cgroup path may be supplied")
        public_key = environment.get("PUBLIC_KEY")
        if public_key is not None and not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]{68}={0,2}(?: [^\r\n]{1,100})?", public_key):
            raise ValueError("PUBLIC_KEY must be one Ed25519 public key")
        cgroup = environment.get("PROBE_CGROUP_ROOT")
        if cgroup is not None and (not cgroup.startswith("/sys/fs/cgroup/") or ".." in cgroup.split('/') or '\n' in cgroup):
            raise ValueError("cgroup delegation must use an absolute cgroup filesystem path")
        return environment

    @property
    def digest(self):
        return "sha256:" + hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode()).hexdigest()


class StorageRates(BaseModel):
    """Operator-pinned official published rates, with a bounded validity period.

    The inventory API reports sizes and tiers, not storage prices. These rates
    must be reviewed against RunPod's pricing page. A 28-day denominator gives a
    conservative daily bound, including February. We deliberately do not apply
    large-volume discounts.
    """
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    checked_at: datetime
    source: Literal["https://docs.runpod.io/pods/pricing"] = "https://docs.runpod.io/pods/pricing"
    network_usd_per_gb_month: float = Field(default=0.07, ge=0.07, le=1)
    volume_idle_usd_per_gb_month: float = Field(default=0.20, ge=0.20, le=1)
    container_usd_per_gb_month: float = Field(default=0.10, ge=0.10, le=1)


class RunPodConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    state_path: str
    api_key_file: str
    launch: RunPodLaunchConfig
    storage_rates: StorageRates
    mode: Literal["disabled", "supervised_acceptance"] = "disabled"
    max_runtime_seconds: int = Field(default=300, ge=1, le=900)
    request_timeout_seconds: int = Field(default=15, ge=1, le=30)
    ready_timeout_seconds: int = Field(default=120, ge=1, le=600)

    @classmethod
    def load(cls, path, *, owner_uid=0):
        return cls.model_validate_json(_read_owned_file(path, owner_uid, private=False))


def _read_owned_file(path, owner_uid, *, private):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        forbidden = 0o077 if private else 0o022
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or
                info.st_nlink != 1 or info.st_mode & forbidden or info.st_size > 131072):
            raise PermissionError("provider configuration or credential file has unsafe ownership, type or permissions")
        return stream.read(131073)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RunPodHTTP:
    """Bounded, non-retrying HTTPS transport; provider errors never echo secrets."""

    def __init__(self, api_key_file, *, timeout_seconds=15):
        self.api_key_file = api_key_file
        self.timeout = timeout_seconds
        self.opener = build_opener(_NoRedirect())

    def request(self, method, path, body=None):
        if not (path == "/graphql" or path.startswith("/v2/")) or "#" in path:
            raise ValueError("unsupported RunPod API path")
        key = _read_owned_file(self.api_key_file, os.geteuid(), private=True).decode().strip()
        if not key or any(c.isspace() for c in key):
            raise PermissionError("invalid provider credential")
        request = Request("https://api.runpod.io" + path, method=method,
                          data=canonical_json(body).encode() if body is not None else None,
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                   "User-Agent": "Mozilla/5.0 Probe-MCP/0.2"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ProviderResponseError("provider response exceeds limit")
                return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if raw else None
        except HTTPError as exc:
            with exc:
                raise ProviderHTTPError(exc.code, headers=exc.headers) from None
        except (URLError, OSError, ValueError) as exc:
            raise ProviderResponseError("provider request failed or returned invalid JSON") from None


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError("invalid provider identity")
    return value


def _decimal(value):
    if type(value) not in (float, int) or not Decimal(str(value)).is_finite() or value < 0:
        raise ProviderResponseError("provider price or size is invalid")
    return Decimal(str(value))


class RunPodProvider:
    # A positive provider termination releases compute. ERROR/404 of an unbound create
    # never counts as a stop; the independent process receipt remains useful too.
    stop_confirms_execution = True

    def __init__(self, config: RunPodConfig, *, transport=None, clock=None, sleep=None):
        self.config = config
        self.transport = transport or RunPodHTTP(config.api_key_file, timeout_seconds=config.request_timeout_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or time.sleep
        self.path = Path(config.state_path).absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for parent in (self.path.parent, *self.path.parent.parents):
            if parent.is_symlink():
                raise PermissionError("provider state path must not traverse a symlink")
        directory = self.path.parent.stat()
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o022:
            raise PermissionError("provider state directory must be private to the service")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                raise PermissionError("provider state must be a private owned regular file")
            os.fsync(fd)
        finally:
            os.close(fd)
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS runpod_intents(
                worker_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
                configuration TEXT NOT NULL, configuration_hash TEXT NOT NULL,
                deadline REAL NOT NULL, provider_id TEXT UNIQUE, submitted_at REAL NOT NULL,
                launch_configuration TEXT NOT NULL, price_ceiling REAL NOT NULL,
                provider_seen INTEGER NOT NULL DEFAULT 0)""")
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    def capabilities(self):
        return {"provider": "runpod", "mode": self.config.mode, "create_price_ceiling": True,
                "resume_supported": False, "native_stop_only_credential": False,
                "host_loss_guarantee": "unverified", "provider_deadline": "stopAfter sent; delivery unverified",
                "max_runtime_seconds": self.config.max_runtime_seconds,
                "launch_config_hash": self.config.launch.digest,
                "launch": self.config.launch.model_dump(mode="json")}

    def _intent(self, worker_id):
        _identifier(worker_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runpod_intents WHERE worker_id=?", (worker_id,)).fetchone()
            return dict(row) if row else None

    def _pods(self):
        result, cursor, seen = [], None, set()
        for _ in range(1000):
            params = {"limit": 1000, "includeClusterPods": "true"}
            if cursor is not None:
                params["cursor"] = cursor
            body = self.transport.request("GET", "/v2/pods?" + urlencode(params))
            if type(body) is not dict or type(body.get("pods")) is not list or type(body.get("pagination")) is not dict:
                raise ProviderResponseError("malformed paginated pod inventory")
            result.extend(body["pods"])
            page = body["pagination"]
            if page.get("hasNextPage") is False:
                return result
            cursor = page.get("nextCursor")
            if type(cursor) is not str or not cursor or cursor in seen:
                raise ProviderResponseError("pod pagination is incomplete or cyclic")
            seen.add(cursor)
        raise ProviderResponseError("pod inventory exceeds page limit")

    @staticmethod
    def _matches(pod, intent):
        env = pod.get("env", {})
        return (pod.get("name") == "probe-" + intent["worker_id"] and type(env) is dict and
                env.get("PROBE_WORKER_ID") == intent["worker_id"] and
                env.get("PROBE_REQUEST_ID") == intent["request_key"] and
                env.get("PROBE_CONFIGURATION_HASH") == intent["configuration_hash"])

    def _observe(self, pod, intent):
        if not self._matches(pod, intent):
            raise ProviderUncertain("provider identity metadata conflicts with durable approval")
        provider_id = _identifier(pod.get("id"))
        spec = DeploymentSpec.model_validate_json(intent["configuration"])
        launch = RunPodLaunchConfig.model_validate_json(intent["launch_configuration"])
        reported = pod.get("mounts")
        mounts = reported.get("network", []) if type(reported) is dict else []
        if spec.storage_mode == "disposable_research":
            # Absence of a complete mounts field is not proof of zero storage.
            # Reject unknown mount kinds and malformed/ambiguous persistent
            # entries rather than treating falsy values as an empty inventory.
            persistent = reported.get("persistent") if type(reported) is dict else None
            storage_matches = (type(reported) is dict and not set(reported) - {"network", "persistent"}
                and reported.get("network", []) == []
                and (persistent is None or (type(persistent) is dict
                    and not set(persistent) - {"size", "path"}
                    and type(persistent.get("size")) is int and persistent["size"] == 0)))
        elif spec.storage_mode == "ephemeral_preflight":
            persistent = pod.get("mounts", {}).get("persistent")
            storage_matches = not mounts and (persistent is None or persistent == {} or
                                              (type(persistent) is dict and persistent.get("size") == 0))
        else:
            storage_matches = any(v.get("volumeId") == spec.volume_id and v.get("path") == "/workspace" for v in mounts)
        if (pod.get("image") != spec.image_repository + "@" + spec.image_digest or
                pod.get("gpu", {}).get("id") != spec.gpu_model or pod.get("gpu", {}).get("count") != 1 or
                pod.get("dataCenterId") not in (None, spec.region) or
                not storage_matches or
                pod.get("locked") is not False or pod.get("disk") != launch.container_disk_gb or
                pod.get("args") != launch.args or set(pod.get("ports", [])) != set(launch.ports) or
                any(pod.get("env", {}).get(key) != value for key, value in launch.environment.items())):
            raise ProviderUncertain("provider configuration differs from approved deployment")
        if pod.get("status") == "RUNNING":
            cuda = pod.get("cudaVersion")
            if (type(cuda) is not str or not re.fullmatch(r"\d+\.\d+", cuda) or
                    tuple(map(int, cuda.split('.'))) < tuple(map(int, launch.min_cuda_version.split('.'))) or
                    not 0 < _decimal(pod.get("cost")) <= _decimal(intent["price_ceiling"])):
                raise ProviderUncertain("running provider CUDA or price does not satisfy approval")
        if intent["provider_id"] not in (None, provider_id):
            raise ProviderUncertain("logical worker maps to multiple physical pods")
        with self._connect() as connection:
            connection.execute("UPDATE runpod_intents SET provider_id=?,provider_seen=1 WHERE worker_id=? AND (provider_id IS NULL OR provider_id=?)",
                               (provider_id, intent["worker_id"], provider_id))
        state = {"RUNNING": WorkerState.RUNNING, "PROVISIONING": WorkerState.STARTING,
                 "STARTING": WorkerState.STARTING}.get(pod.get("status"), WorkerState.UNKNOWN)
        if pod.get("status") in ("EXITED", "TERMINATED"):
            # Never treat a crashed-but-billable or inconsistent observation as
            # proof that the provider released compute.
            if _decimal(pod.get("cost")) == 0 and pod.get("runtime") in (None, {}):
                state = WorkerState.STOPPED
        return WorkerStatus(intent["worker_id"], state, provider_id, intent["request_key"], intent["configuration_hash"])

    def status(self, worker_id):
        intent = self._intent(worker_id)
        if intent is None:
            return WorkerStatus(worker_id, WorkerState.ABSENT)
        if intent["provider_id"] is not None:
            try:
                pod = self.transport.request("GET", "/v2/pods/" + quote(intent["provider_id"], safe=""))
            except ProviderHTTPError as exc:
                if exc.status == 404:
                    # A create response can precede inventory visibility. Its
                    # first 404 is uncertainty, not evidence of termination.
                    return WorkerStatus(worker_id, WorkerState.ABSENT if intent["provider_seen"] else WorkerState.UNKNOWN,
                                        intent["provider_id"], intent["request_key"], intent["configuration_hash"])
                raise
            return self._observe(pod, intent)
        matches = [pod for pod in self._pods() if self._matches(pod, intent)]
        if len(matches) > 1:
            raise ProviderUncertain("multiple pods match a single immutable create intent")
        if not matches:
            # A timed-out create can become visible later. This cannot close an
            # approval or authorize a replacement merely because list is empty.
            return WorkerStatus(worker_id, WorkerState.UNKNOWN, request_key=intent["request_key"],
                                configuration_hash=intent["configuration_hash"])
        return self._observe(matches[0], intent)

    def _check_spec(self, deployment):
        if (deployment.image_repository != self.config.launch.image_repository or
                deployment.launch_config_hash != self.config.launch.digest):
            raise ProviderCapabilityError("deployment must bind the exact trusted launch configuration")

    def _storage(self, deployment):
        rates = self.config.storage_rates
        now = self.clock()
        if rates.checked_at.tzinfo is None or not 0 <= (now - rates.checked_at).total_seconds() <= 86400:
            raise ProviderCapabilityError("official storage price evidence is absent or more than one day old")
        body = self.transport.request("GET", "/v2/network-volumes")
        if type(body) is not dict or type(body.get("networkVolumes")) is not list:
            raise ProviderResponseError("invalid network volume inventory")
        volumes = body["networkVolumes"]
        if deployment.storage_mode not in {"ephemeral_preflight", "disposable_research"}:
            target = [v for v in volumes if v.get("id") == deployment.volume_id]
            if len(target) != 1 or target[0].get("size") != deployment.volume_gb or target[0].get("dataCenter") != deployment.region:
                raise ProviderCapabilityError("approved persistent volume must already exist in the exact size and data center")
        total = Decimal(0)
        for volume in volumes:
            if volume.get("type") != "STANDARD":
                raise ProviderCapabilityError("non-Standard storage tier requires a separate official price review")
            total += _decimal(volume.get("size")) * _decimal(rates.network_usd_per_gb_month) / 28
        for pod in self._pods():
            total += _decimal(pod.get("mounts", {}).get("persistent", {}).get("size", 0)) * _decimal(rates.volume_idle_usd_per_gb_month) / 28
        return total

    def quote(self, *, worker_id=None, deployment=None):
        if worker_id is not None:
            raise ProviderCapabilityError("RunPod resume lacks an atomic price ceiling; request a newly approved replacement")
        if deployment is None:
            raise ValueError("deployment is required")
        self._check_spec(deployment)
        gpu = self.transport.request("GET", "/v2/catalog/gpus/" + quote(deployment.gpu_model, safe="") + "?" +
                                     urlencode({"include": "AVAILABILITY", "product": "POD", "cloud": "SECURE",
                                                "count": 1, "minCudaVersion": self.config.launch.min_cuda_version}))
        if (gpu.get("id") != deployment.gpu_model or not any(dc.get("id") == deployment.region and
                dc.get("availability") in ("LOW", "MEDIUM", "HIGH") for dc in gpu.get("dataCenters", []))):
            raise ProviderCapabilityError("approved GPU and CUDA floor have no current capacity in this data center")
        storage = self._storage(deployment)
        disk_hourly = self.config.launch.container_disk_gb * _decimal(self.config.storage_rates.container_usd_per_gb_month) / (28 * 24)
        compute_price = _decimal(gpu.get("price", {}).get("secure"))
        if compute_price <= 0:
            raise ProviderResponseError("provider GPU price must be positive")
        price = compute_price + disk_hourly
        return PriceQuote(float(price), float(storage), self.clock())

    def start(self, worker_id, **kwargs):
        raise ProviderCapabilityError("RunPod resumes are disabled; create a newly approved replacement")

    def create(self, worker_id, deployment, *, request_key, price_ceiling_usd_per_hour,
               storage_ceiling_usd_per_day, absolute_deadline=None):
        _identifier(worker_id)
        _identifier(request_key)
        self._check_spec(deployment)
        now = self.clock()
        if self.config.mode != "supervised_acceptance":
            raise ProviderCapabilityError("unattended RunPod launches are disabled: host-loss shutdown guarantee is unverified")
        if (absolute_deadline is None or absolute_deadline.tzinfo is None or
                not 0 < (absolute_deadline - now).total_seconds() <= self.config.max_runtime_seconds):
            raise ProviderCapabilityError("supervised acceptance requires a short, already committed absolute deadline")
        supplied = self.quote(deployment=deployment)
        ceiling = _decimal(price_ceiling_usd_per_hour)
        if (not 0 < ceiling < Decimal("1.50") or _decimal(supplied.usd_per_hour) > ceiling or
                _decimal(supplied.projected_storage_usd_per_day) >= _decimal(storage_ceiling_usd_per_day)):
            raise ProviderBudgetRefused("live quote exceeds the approved compute or total storage ceiling")
        launch = self.config.launch
        disk_hourly = launch.container_disk_gb * _decimal(self.config.storage_rates.container_usd_per_gb_month) / (28 * 24)
        env = {**launch.environment, "PROBE_WORKER_ID": worker_id, "PROBE_REQUEST_ID": request_key,
               "PROBE_CONFIGURATION_HASH": deployment.digest,
               "PROBE_ABSOLUTE_DEADLINE": absolute_deadline.astimezone(UTC).isoformat()}
        body = {"cloudType": "SECURE", "name": "probe-" + worker_id,
                "imageName": deployment.image_repository + "@" + deployment.image_digest,
                "gpuTypeId": deployment.gpu_model, "gpuCount": 1, "dataCenterId": deployment.region,
                "volumeInGb": 0,
                "containerDiskInGb": launch.container_disk_gb, "dockerArgs": launch.args,
                "minCudaVersion": launch.min_cuda_version, "deployCost": float(ceiling - disk_hourly),
                "startSsh": launch.start_ssh, "startJupyter": False, "ports": ",".join(launch.ports),
                "stopAfter": absolute_deadline.astimezone(UTC).isoformat(),
                "env": [{"key": key, "value": value} for key, value in env.items()]}
        if deployment.storage_mode not in {"ephemeral_preflight", "disposable_research"}:
            body.update(networkVolumeId=deployment.volume_id, volumeMountPath="/workspace")
        validate_audit_payload(body)
        if self.clock() >= absolute_deadline:
            raise ProviderCapabilityError("approval expired during provider preflight")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("INSERT INTO runpod_intents VALUES(?,?,?,?,?,?,?,?,?,0)",
                                   (worker_id, request_key, canonical_json(deployment.model_dump(exclude_none=True)),
                                    deployment.digest, absolute_deadline.timestamp(), None, now.timestamp(),
                                    canonical_json(launch.model_dump(mode="json")), float(ceiling)))
                connection.execute("COMMIT")
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                raise ProviderUncertain("create intent is already consumed; reconcile it without resubmitting") from None
        # The intent is committed before the first network write. No exception,
        # HTTP status, or process restart reopens it for a second submission.
        try:
            if self.clock() >= absolute_deadline:
                raise ProviderUncertain("approval expired after durable submission reservation")
            reply = self.transport.request("POST", "/graphql", {"query": _CREATE, "variables": {"input": body}})
            if type(reply) is not dict or reply.get("errors"):
                raise ProviderUncertain("GraphQL create did not confirm one resource")
            provider_id = _identifier(reply.get("data", {}).get("podFindAndDeployOnDemand", {}).get("id"))
            with self._connect() as connection:
                connection.execute("UPDATE runpod_intents SET provider_id=? WHERE worker_id=?", (provider_id, worker_id))
        except Exception:
            raise ProviderUncertain("RunPod create outcome requires inventory reconciliation") from None
        until = min(absolute_deadline.timestamp(), self.clock().timestamp() + self.config.ready_timeout_seconds)
        wall_until = time.monotonic() + self.config.ready_timeout_seconds
        while self.clock().timestamp() < until and time.monotonic() < wall_until:
            observed = self.status(worker_id)
            if observed.state == WorkerState.RUNNING:
                return observed
            if observed.state != WorkerState.STARTING:
                raise ProviderUncertain("created worker did not reach a usable running state")
            self.sleep(min(2, max(0, until - self.clock().timestamp())))
        raise ProviderUncertain("created worker readiness was not confirmed before its deadline")

    def stop(self, worker_id):
        """Terminate owned Pods; retained network volumes are separate resources.

        A container's EXITED state can still be billed. Since this adapter never
        resumes Pods, deletion is the reliable lifecycle operation for stopping
        compute and removing disposable Pod storage. Mutation acknowledgement
        alone is not proof of release: callers must separately read back status.
        """
        intent = self._intent(worker_id)
        if intent is None:
            raise ProviderCapabilityError("stop is restricted to a durable owned create intent")
        # Once an ID was returned by our create, a changed image/metadata or a
        # failed readiness check must never prevent its emergency stop.
        if intent["provider_id"] is not None:
            ids = [intent["provider_id"]]
        else:
            matches = [pod for pod in self._pods() if self._matches(pod, intent)]
            ids = [_identifier(pod.get("id")) for pod in matches]
            if not ids:
                raise ProviderUncertain("create remains unbound; retry inventory reconciliation, never create")
            if len(matches) == 1:
                try:
                    # Preserve a verified physical identity before deletion can
                    # remove the inventory evidence for a timed-out create.
                    self._observe(matches[0], intent)
                except (ProviderUncertain, ProviderResponseError):
                    # Conflicting configuration must not block emergency cleanup,
                    # but cannot establish a trustworthy terminal identity either.
                    pass
        errors = []
        for provider_id in ids:
            try:
                self.transport.request("DELETE", "/v2/pods/" + quote(provider_id, safe=""))
            except ProviderHTTPError as exc:
                if exc.status != 404:
                    errors.append(exc)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ProviderUncertain("one or more owned pods did not acknowledge termination") from None
        # The caller must observe a positive terminal state separately. A success
        # or 404 from the mutation alone never supplies termination evidence.


class StopBrokerClient:
    def __init__(self, path, *, expected_server_uid):
        self.client = UnixRPCClient(path, expected_server_uid=expected_server_uid)

    def status(self, worker_id):
        body = self.client.call("status", {"worker_id": worker_id})
        return WorkerStatus(body["worker_id"], WorkerState(body["state"]), body.get("provider_id"),
                            body.get("request_key"), body.get("configuration_hash"))

    def stop(self, worker_id):
        self.client.call("stop", {"worker_id": worker_id})


def serve_stop_broker(backend, path, *, watchdog_uid, socket_gid=None):
    if type(watchdog_uid) is not int or watchdog_uid < 0 or watchdog_uid == os.geteuid():
        raise PermissionError("stop watchdog requires a distinct OS identity")

    def dispatch(method, params):
        if set(params) != {"worker_id"}:
            raise ValueError("stop broker accepts only a logical worker identity")
        worker_id = _identifier(params["worker_id"])
        if method == "status":
            return backend.status(worker_id).as_dict()
        if method == "stop":
            backend.stop(worker_id)
            return backend.status(worker_id).as_dict()
        raise PermissionError("stop broker does not expose provisioning, start, credentials or arbitrary requests")
    return UnixRPCServer(path, dispatch, allowed_uids={watchdog_uid}, socket_gid=socket_gid)


def main():
    parser = argparse.ArgumentParser(description="RunPod provider inspection and independently supervised stop-only broker")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect-config")
    inspect.add_argument("--config", required=True)
    broker = sub.add_parser("serve-stop")
    broker.add_argument("--config", required=True)
    broker.add_argument("--socket", required=True)
    broker.add_argument("--watchdog-uid", type=int, required=True)
    broker.add_argument("--socket-gid", type=int, required=True)
    args = parser.parse_args()
    backend = RunPodProvider(RunPodConfig.load(args.config))
    if args.command == "inspect-config":
        print(canonical_json(backend.capabilities()))
        return
    server = serve_stop_broker(backend, args.socket, watchdog_uid=args.watchdog_uid, socket_gid=args.socket_gid)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
