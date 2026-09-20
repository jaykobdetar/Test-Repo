"""Provider contract tests. Fake HTTP only: no cloud resource is created."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.message import Message
import hashlib
import io
import json
import os
import sqlite3
import threading
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from probe_core.audit import canonical_json
from probe_core.provider import DeploymentSpec, ProviderBudgetRefused, WorkerState
from probe_core.runpod_provider import (
    ProviderCapabilityError, ProviderHTTPError, ProviderResponseError, ProviderUncertain,
    RunPodConfig, RunPodHTTP, RunPodLaunchConfig, RunPodProvider, StorageRates,
    provider_http_metadata, serve_stop_broker,
)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class HTTP:
    def __init__(self):
        self.calls = []
        self.pods = []
        self.volumes = [{"id": "volume1", "size": 100, "dataCenter": "US-IL-1", "type": "STANDARD"}]
        self.price = 0.74
        self.create_hook = None
        self.delete_hook = None
        self.fail_after_create = False
        self.fail_after_delete = False
        self.initial_state = "RUNNING"
        self.page_size = 1000

    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if path.startswith("/v2/catalog/gpus/"):
            return {"id": "NVIDIA GeForce RTX 4090", "price": {"secure": self.price},
                    "dataCenters": [{"id": "US-IL-1", "availability": "LOW"}]}
        if path == "/v2/network-volumes":
            return {"networkVolumes": deepcopy(self.volumes)}
        if path.startswith("/v2/pods?"):
            from urllib.parse import parse_qs, urlsplit
            cursor = int(parse_qs(urlsplit(path).query).get("cursor", [0])[0])
            end = cursor + self.page_size
            return {"pods": deepcopy(self.pods[cursor:end]), "pagination": {
                "hasNextPage": end < len(self.pods), "nextCursor": str(end) if end < len(self.pods) else None}}
        if method == "POST" and path == "/graphql":
            if self.create_hook:
                self.create_hook(body)
            config = body["variables"]["input"]
            pod = {"id": "pod" + str(len(self.pods) + 1), "name": config["name"],
                   "env": {v["key"]: v["value"] for v in config["env"]},
                   "gpu": {"id": config["gpuTypeId"], "count": config["gpuCount"]},
                   "dataCenterId": config["dataCenterId"], "cudaVersion": config["minCudaVersion"],
                   "image": config["imageName"], "args": config["dockerArgs"], "disk": config["containerDiskInGb"],
                   "ports": config["ports"].split(',') if config["ports"] else [],
                   "mounts": {"network": ([{"volumeId": config["networkVolumeId"], "path": "/workspace"}]
                                            if "networkVolumeId" in config else [])},
                   "locked": False, "status": self.initial_state, "cost": self.price}
            self.pods.append(pod)
            if self.fail_after_create:
                raise TimeoutError("a secret must not appear in the public exception")
            return {"data": {"podFindAndDeployOnDemand": {"id": pod["id"]}}}
        if path.startswith("/v2/pods/"):
            pod_id = path.split('/')[3]
            pods = [pod for pod in self.pods if pod["id"] == pod_id]
            if not pods:
                raise ProviderHTTPError(404)
            if method == "DELETE":
                assert path == "/v2/pods/" + pod_id and body is None
                if self.delete_hook:
                    self.delete_hook(pod_id)
                self.pods.remove(pods[0])
                if self.fail_after_delete:
                    raise TimeoutError("synthetic private transport diagnostic")
                return None
            assert method == "GET"
            return deepcopy(pods[0])
        raise AssertionError((method, path, body))

    @property
    def purchases(self):
        return [call for call in self.calls if call[:2] == ("POST", "/graphql")]


@pytest.fixture
def runpod(tmp_path):
    clock, http = Clock(), HTTP()
    launch = RunPodLaunchConfig(image_repository="ghcr.io/test/worker", ports=("22/tcp",))
    config = RunPodConfig(state_path=str(tmp_path / "provider" / "runpod.sqlite"),
                          api_key_file=str(tmp_path / "never-read"), launch=launch,
                          storage_rates=StorageRates(checked_at=clock()), mode="supervised_acceptance")
    backend = RunPodProvider(config, transport=http, clock=clock, sleep=clock.advance)
    spec = DeploymentSpec(gpu_model="NVIDIA GeForce RTX 4090", image_digest="sha256:" + "a" * 64,
                          image_repository=launch.image_repository, launch_config_hash=launch.digest,
                          volume_id="volume1", volume_gb=100, region="US-IL-1")
    return backend, http, clock, spec


def create(runpod, **overrides):
    backend, _, clock, spec = runpod
    args = dict(request_key="request1", price_ceiling_usd_per_hour=0.80,
                storage_ceiling_usd_per_day=1.90, absolute_deadline=clock() + timedelta(seconds=300))
    args.update(overrides)
    return backend.create("worker1", spec, **args)


def test_create_binds_image_approval_deadline_and_atomic_provider_price_ceiling(runpod):
    backend, http, clock, spec = runpod

    def before_submit(body):
        with closing(sqlite3.connect(backend.path)) as connection:
            row = connection.execute("SELECT request_key,configuration_hash,deadline FROM runpod_intents").fetchone()
        assert row == ("request1", spec.digest, (clock() + timedelta(seconds=300)).timestamp())
    http.create_hook = before_submit
    result = create(runpod)
    assert result.state == WorkerState.RUNNING
    body = http.purchases[0][2]["variables"]["input"]
    assert body["imageName"] == spec.image_repository + "@" + spec.image_digest
    assert body["stopAfter"] == (clock() + timedelta(seconds=300)).isoformat()
    assert 0.74 < body["deployCost"] < 0.80
    assert body["startSsh"] is False
    assert {v["key"] for v in body["env"]} == {"PROBE_WORKER_ID", "PROBE_REQUEST_ID", "PROBE_CONFIGURATION_HASH", "PROBE_ABSOLUTE_DEADLINE"}
    assert backend.capabilities()["host_loss_guarantee"] == "unverified"


def ephemeral(runpod, mode="ephemeral_preflight"):
    backend, http, clock, spec = runpod
    values = spec.model_dump()
    values.update(storage_mode=mode, volume_id=None, volume_gb=0)
    return backend, http, clock, DeploymentSpec.model_validate(values)


@pytest.mark.parametrize("mode", ["ephemeral_preflight", "disposable_research"])
def test_disposable_create_has_no_persistent_storage_and_reconciles_after_restart(runpod, mode):
    runpod = ephemeral(runpod, mode)
    backend, http, clock, spec = runpod
    http.volumes.clear()
    quote = backend.quote(deployment=spec)
    assert quote.projected_storage_usd_per_day == 0
    assert quote.usd_per_hour == pytest.approx(.74 + 20 * .10 / (28 * 24))
    assert create(runpod).state == WorkerState.RUNNING
    body = http.purchases[0][2]["variables"]["input"]
    assert "networkVolumeId" not in body and "volumeMountPath" not in body
    assert body["volumeInGb"] == 0 and body["containerDiskInGb"] == 20
    assert body["stopAfter"] == (clock() + timedelta(seconds=300)).isoformat()
    assert .74 < body["deployCost"] < .80
    assert {entry["key"]: entry["value"] for entry in body["env"]}["PROBE_CONFIGURATION_HASH"] == spec.digest
    reopened = RunPodProvider(backend.config, transport=http, clock=clock)
    assert reopened.status("worker1").state == WorkerState.RUNNING
    with pytest.raises(ProviderUncertain, match="already consumed"):
        create((reopened, http, clock, spec))
    reopened.stop("worker1")
    assert reopened.status("worker1").state == WorkerState.ABSENT
    assert http.pods == []
    assert len(http.purchases) == 1
    with pytest.raises(ProviderCapabilityError, match="resumes are disabled"):
        reopened.start("worker1", request_key="never-resume", price_ceiling_usd_per_hour=.8,
                       storage_ceiling_usd_per_day=1.9, absolute_deadline=clock() + timedelta(seconds=60))
    assert len(http.purchases) == 1


@pytest.mark.parametrize("mode", ["ephemeral_preflight", "disposable_research"])
def test_disposable_still_counts_and_limits_all_account_storage(runpod, mode):
    runpod = ephemeral(runpod, mode)
    backend, http, _, spec = runpod
    http.volumes.append({"id": "detached-old", "size": 1000, "dataCenter": "US-TX-3", "type": "STANDARD"})
    assert backend.quote(deployment=spec).projected_storage_usd_per_day == pytest.approx(1100 * .07 / 28)
    with pytest.raises(ProviderBudgetRefused):
        create(runpod)
    assert http.purchases == []


@pytest.mark.parametrize("reported", [{}, {"network": []}, {"network": [], "persistent": None},
                                     {"network": [], "persistent": {"size": 0, "path": "/workspace"}}])
def test_disposable_research_requires_explicit_empty_or_zero_mount_inventory(runpod, reported):
    runpod = ephemeral(runpod, "disposable_research")
    backend, http, _, _ = runpod
    create(runpod)
    http.pods[0]["mounts"] = reported
    assert backend.status("worker1").state == WorkerState.RUNNING


@pytest.mark.parametrize("reported", [None, [], "missing", {"network": None},
    {"network": [{"volumeId": "unexpected", "path": "/workspace"}]},
    {"persistent": {}}, {"persistent": {"size": 1}}, {"persistent": {"size": False}},
    {"network": [], "global": [{"volumeId": "unapproved"}]}])
def test_disposable_research_rejects_ambiguous_or_unapproved_storage(runpod, reported):
    runpod = ephemeral(runpod, "disposable_research")
    backend, http, _, _ = runpod
    create(runpod)
    if reported == "missing":
        http.pods[0].pop("mounts")
    else:
        http.pods[0]["mounts"] = reported
    with pytest.raises(ProviderUncertain, match="configuration differs"):
        backend.status("worker1")
    backend.stop("worker1")
    assert http.pods == [] and len(http.purchases) == 1


def test_disposable_research_loss_is_absent_without_replay_or_storage_recovery(runpod):
    runpod = ephemeral(runpod, "disposable_research")
    backend, http, clock, spec = runpod
    initial = create(runpod)
    http.pods.clear()  # Provider has lost the Pod and its disposable filesystem.
    reopened = RunPodProvider(backend.config, transport=http, clock=clock)
    result = reopened.status("worker1")
    assert result.state == WorkerState.ABSENT and result.provider_id == initial.provider_id
    with pytest.raises(ProviderUncertain, match="already consumed"):
        create((reopened, http, clock, spec))
    assert len(http.purchases) == 1


@pytest.mark.parametrize("unexpected", [
    {"network": [{"volumeId": "unexpected", "path": "/workspace"}]},
    {"network": [], "persistent": {"size": 1}},
])
def test_ephemeral_preflight_rejects_unapproved_persistent_storage_readback(runpod, unexpected):
    runpod = ephemeral(runpod)
    backend, http, _, _ = runpod
    create(runpod)
    http.pods[0]["mounts"] = unexpected
    with pytest.raises(ProviderUncertain, match="configuration differs"):
        backend.status("worker1")
    backend.stop("worker1")
    assert http.pods == []


@pytest.mark.parametrize("overrides", [
    {"volume_id": None, "volume_gb": 0},
    {"volume_gb": 0},
    {"storage_mode": "ephemeral_preflight"},
    {"storage_mode": "ephemeral_preflight", "volume_id": None, "volume_gb": 1},
    {"storage_mode": "ephemeral_preflight", "volume_id": None, "volume_gb": 0, "launch_config_hash": None},
    {"storage_mode": "ephemeral_research", "volume_id": None, "volume_gb": 0},
    {"storage_mode": "disposable_research"},
    {"storage_mode": "disposable_research", "volume_id": None, "volume_gb": 1},
    {"storage_mode": "disposable_research", "volume_id": None, "volume_gb": 0, "launch_config_hash": None},
    {"storage_mode": "disposable_research", "volume_id": None, "volume_gb": 0, "image_repository": None},
])
def test_only_explicit_image_bound_disposable_modes_can_omit_a_volume(runpod, overrides):
    values = runpod[3].model_dump()
    values.update(overrides)
    with pytest.raises(ValueError):
        DeploymentSpec.model_validate(values)


def test_research_deployment_still_requires_existing_matching_volume(runpod):
    backend, http, _, spec = runpod
    http.volumes.clear()
    with pytest.raises(ProviderCapabilityError, match="persistent volume"):
        backend.quote(deployment=spec)
    assert http.purchases == []


def test_disabled_mode_and_resume_never_submit_a_paid_operation(runpod):
    backend, http, clock, _ = runpod
    disabled = RunPodProvider(backend.config.model_copy(update={"mode": "disabled"}), transport=http, clock=clock)
    with pytest.raises(ProviderCapabilityError, match="unattended"):
        create((disabled, *runpod[1:]))
    with pytest.raises(ProviderCapabilityError, match="resumes"):
        backend.start("worker1", request_key="resume1")
    assert http.purchases == []


@pytest.mark.parametrize("price", [1.5, 0.81, float('nan')])
def test_changed_live_price_is_refused_before_submission(runpod, price):
    _, http, _, _ = runpod
    http.price = price
    with pytest.raises((ProviderBudgetRefused, ProviderResponseError)):
        create(runpod)
    assert http.purchases == []


@pytest.mark.parametrize("seconds", [-1, 0, 301])
def test_runtime_must_be_positive_and_bounded_before_purchase(runpod, seconds):
    _, http, clock, _ = runpod
    with pytest.raises(ProviderCapabilityError):
        create(runpod, absolute_deadline=clock() + timedelta(seconds=seconds))
    assert http.purchases == []


def test_quote_counts_detached_volumes_and_all_pages_of_retained_disks(runpod):
    backend, http, _, spec = runpod
    http.volumes.append({"id": "retained-old-volume", "size": 1000, "dataCenter": "US-TX-3", "type": "STANDARD"})
    http.pods.extend([{"mounts": {"persistent": {"size": 10}}}, {"mounts": {"persistent": {"size": 20}}}])
    http.page_size = 1
    quoted = backend.quote(deployment=spec)
    assert quoted.projected_storage_usd_per_day == pytest.approx((1100 * .07 + 30 * .20) / 28)
    with pytest.raises(ProviderBudgetRefused):
        create(runpod)
    assert http.purchases == []


def test_old_rate_evidence_wrong_volume_and_changed_launch_fail_closed(runpod):
    backend, http, clock, spec = runpod
    clock.advance(86401)
    with pytest.raises(ProviderCapabilityError, match="one day"):
        backend.quote(deployment=spec)
    clock.advance(-86401)
    http.volumes[0]["dataCenter"] = "other-region"
    with pytest.raises(ProviderCapabilityError, match="persistent volume"):
        backend.quote(deployment=spec)
    with pytest.raises(ProviderCapabilityError, match="exact trusted launch"):
        backend.quote(deployment=spec.model_copy(update={"launch_config_hash": "sha256:" + "b" * 64}))


def test_high_performance_storage_anywhere_in_account_requires_separate_rates(runpod):
    backend, http, _, spec = runpod
    http.volumes.append({"id": "old-high-performance", "size": 100, "dataCenter": "US-TX-3", "type": "HIGH_PERFORMANCE"})
    with pytest.raises(ProviderCapabilityError, match="non-Standard"):
        backend.quote(deployment=spec)
    assert http.purchases == []


@pytest.mark.parametrize("state,cost,runtime", [("ERROR", 0, None), ("EXITED", .74, None),
                                               ("EXITED", 0, {"uptime": 100})])
def test_inconsistent_or_crashed_container_state_does_not_prove_compute_off(runpod, state, cost, runtime):
    backend, http, _, _ = runpod
    create(runpod)
    http.pods[0].update(status=state, cost=cost, runtime=runtime)
    assert backend.status("worker1").state == WorkerState.UNKNOWN


def test_timeout_after_create_recovers_by_inventory_and_never_retries_purchase(runpod):
    backend, http, clock, spec = runpod
    http.fail_after_create = True
    with pytest.raises(ProviderUncertain) as error:
        create(runpod)
    assert "secret" not in str(error.value)
    reopened = RunPodProvider(backend.config, transport=http, clock=clock)
    assert reopened.status("worker1").state == WorkerState.RUNNING
    with pytest.raises(ProviderUncertain, match="already consumed"):
        create((reopened, http, clock, spec))
    assert len(http.purchases) == 1
    reopened.stop("worker1")
    assert reopened.status("worker1").state == WorkerState.ABSENT


def test_stop_binds_verified_uncertain_create_before_deleting_its_inventory_evidence(runpod):
    backend, http, clock, spec = runpod
    http.fail_after_create = True
    with pytest.raises(ProviderUncertain):
        create(runpod)
    assert backend._intent("worker1")["provider_id"] is None

    def before_delete(pod_id):
        with closing(sqlite3.connect(backend.path)) as connection:
            assert connection.execute(
                "SELECT provider_id,provider_seen FROM runpod_intents").fetchone() == (pod_id, 1)
    http.delete_hook = before_delete
    volumes = deepcopy(http.volumes)
    backend.stop("worker1")
    assert http.pods == [] and http.volumes == volumes
    reopened = RunPodProvider(backend.config, transport=http, clock=clock)
    observed = reopened.status("worker1")
    assert observed.state == WorkerState.ABSENT and observed.provider_id == "pod1"
    reopened.stop("worker1")  # A repeated deletion receives 404; it never buys a replacement.
    with pytest.raises(ProviderUncertain, match="already consumed"):
        create((reopened, http, clock, spec))
    assert len(http.purchases) == 1
    assert [(method, path) for method, path, _ in http.calls if method == "DELETE"] == [
        ("DELETE", "/v2/pods/pod1"), ("DELETE", "/v2/pods/pod1")]


def test_delete_timeout_reconciles_known_pod_absence_without_replaying_create(runpod):
    backend, http, clock, _ = runpod
    create(runpod)
    http.fail_after_delete = True
    with pytest.raises(ProviderUncertain, match="acknowledge termination") as error:
        backend.stop("worker1")
    assert "private" not in str(error.value)
    assert http.pods == []
    reopened = RunPodProvider(backend.config, transport=http, clock=clock)
    assert reopened.status("worker1").state == WorkerState.ABSENT
    reopened.stop("worker1")
    assert len(http.purchases) == 1


@pytest.mark.parametrize("status", [403, 409, 500])
def test_delete_errors_do_not_acknowledge_release(runpod, status):
    backend, http, _, _ = runpod
    create(runpod)

    def refuse(_):
        raise ProviderHTTPError(status)
    http.delete_hook = refuse
    with pytest.raises(ProviderUncertain, match="acknowledge termination"):
        backend.stop("worker1")
    assert backend.status("worker1").state == WorkerState.RUNNING
    assert len(http.purchases) == 1


def test_delete_acknowledgement_alone_is_not_positive_release(runpod, monkeypatch):
    backend, http, _, _ = runpod
    create(runpod)
    original = http.request

    def pending(method, path, body=None):
        if method == "DELETE":
            return {"id": "pod1"}
        return original(method, path, body)
    monkeypatch.setattr(http, "request", pending)
    backend.stop("worker1")
    assert backend.status("worker1").state == WorkerState.RUNNING
    monkeypatch.setattr(http, "request", original)
    backend.stop("worker1")
    assert backend.status("worker1").state == WorkerState.ABSENT


def test_delete_only_targets_durable_owned_pod_and_preserves_network_volume(runpod):
    backend, http, _, _ = runpod
    create(runpod)
    unrelated = deepcopy(http.pods[0])
    unrelated.update(id="unrelated", name="another-users-pod")
    http.pods.append(unrelated)
    volumes = deepcopy(http.volumes)
    with pytest.raises(ProviderCapabilityError, match="durable owned"):
        backend.stop("unrelated")
    backend.stop("worker1")
    assert http.pods == [unrelated] and http.volumes == volumes
    assert [path for method, path, _ in http.calls if method == "DELETE"] == ["/v2/pods/pod1"]


def test_uncertain_create_not_yet_visible_is_unknown_not_safely_absent(runpod):
    backend, http, _, _ = runpod
    http.create_hook = lambda _: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(ProviderUncertain):
        create(runpod)
    assert backend.status("worker1").state == WorkerState.UNKNOWN
    with pytest.raises(ProviderUncertain, match="unbound"):
        backend.stop("worker1")
    assert backend.status("never-created").state == WorkerState.ABSENT


def test_concurrent_duplicate_creates_make_exactly_one_provider_request(runpod):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(create, runpod) for _ in range(2)]
    assert sum(result.exception() is None for result in results) == 1
    assert sum(isinstance(result.exception(), ProviderUncertain) for result in results) == 1
    assert len(runpod[1].purchases) == 1


def test_unexpected_configuration_and_price_can_still_be_stopped(runpod):
    backend, http, _, _ = runpod
    create(runpod)
    http.pods[0]["image"] = "unexpected/image:latest"
    http.pods[0]["cost"] = 2
    with pytest.raises(ProviderUncertain):
        backend.status("worker1")
    backend.stop("worker1")
    assert http.pods == []


def test_duplicate_owned_resources_are_all_stopped_but_never_misreported(runpod):
    backend, http, _, _ = runpod
    http.fail_after_create = True
    with pytest.raises(ProviderUncertain):
        create(runpod)
    duplicate = deepcopy(http.pods[0])
    duplicate["id"] = "podduplicate"
    http.pods.append(duplicate)
    with pytest.raises(ProviderUncertain, match="multiple"):
        backend.status("worker1")
    backend.stop("worker1")
    assert http.pods == []
    assert backend.status("worker1").state == WorkerState.UNKNOWN
    assert {path for method, path, _ in http.calls if method == "DELETE"} == {
        "/v2/pods/pod1", "/v2/pods/podduplicate"}


def test_positive_bound_404_distinguished_from_unbound_empty_inventory(runpod):
    backend, http, _, _ = runpod
    create(runpod)
    http.pods.clear()
    observed = backend.status("worker1")
    assert observed.state == WorkerState.ABSENT and observed.provider_id == "pod1"


def test_first_read_404_after_create_receipt_is_not_a_stop_confirmation(runpod, monkeypatch):
    backend, http, _, _ = runpod
    original = http.request

    def delayed_visibility(method, path, body=None):
        if method == "GET" and path == "/v2/pods/pod1":
            raise ProviderHTTPError(404)
        return original(method, path, body)
    monkeypatch.setattr(http, "request", delayed_visibility)
    with pytest.raises(ProviderUncertain):
        create(runpod)
    assert backend.status("worker1").state == WorkerState.UNKNOWN
    assert http.pods[0]["status"] == "RUNNING"
    backend.stop("worker1")
    assert http.pods == []
    # Deletion does not retroactively verify an unobserved create response.
    # No automatic retry or new purchase is allowed to resolve this uncertainty.
    assert backend.status("worker1").state == WorkerState.UNKNOWN
    monkeypatch.setattr(http, "request", original)
    assert backend.status("worker1").state == WorkerState.UNKNOWN


def test_pending_start_deadline_and_readiness_timeout_do_not_create_again(runpod):
    backend, http, _, _ = runpod
    http.initial_state = "STARTING"
    with pytest.raises(ProviderUncertain, match="readiness"):
        create(runpod)
    backend.stop("worker1")
    assert backend.status("worker1").state == WorkerState.ABSENT
    assert len(http.purchases) == 1


def test_legacy_deployment_digest_does_not_change_for_omitted_live_fields():
    old = dict(gpu_model="RTX-A5000", image_digest="sha256:" + "a" * 64,
               volume_id="research-volume", volume_gb=100, region="test-region", gpu_count=1)
    assert DeploymentSpec(**old).digest == "sha256:" + hashlib.sha256(canonical_json(old).encode()).hexdigest()


def test_config_and_credentials_reject_symlinks_public_permissions_and_wrong_owner(runpod, tmp_path):
    backend, _, _, _ = runpod
    config = tmp_path / "config.json"
    config.write_text(backend.config.model_dump_json())
    config.chmod(0o640)
    assert RunPodConfig.load(config, owner_uid=os.geteuid()) == backend.config
    config.chmod(0o666)
    with pytest.raises(PermissionError):
        RunPodConfig.load(config, owner_uid=os.geteuid())
    config.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(config)
    with pytest.raises(OSError):
        RunPodConfig.load(link, owner_uid=os.geteuid())
    with pytest.raises(PermissionError):
        RunPodConfig.load(config, owner_uid=os.geteuid() + 1)
    key = tmp_path / "key"
    key.write_text("synthetic-private-test-key")
    key.chmod(0o644)
    with pytest.raises(PermissionError):
        RunPodHTTP(key).request("GET", "/v2/pods")


@pytest.mark.parametrize('media,kind', [('application/json; charset=utf-8', 'json'),
    ('application/problem+json', 'json'), ('TEXT/HTML; charset=UTF-8', 'html'),
    ('application/xhtml+xml', 'html'), ('private-unknown', 'other'), ('x'*300, 'other')])
@pytest.mark.parametrize('retry,seconds', [('0', 0), ('120', 120), ('86400', 86400),
    ('86401', None), ('-1', None), ('1.5', None), ('Wed, 21 Oct 2015 07:28:00 GMT', None)])
def test_http_error_headers_reduce_to_bounded_fixed_metadata(media, kind, retry, seconds):
    headers = Message()
    headers['Content-Type'] = media
    headers['Retry-After'] = retry
    headers['cf-mitigated'] = 'challenge'
    headers['PRIVATE'] = 'never-retained'
    result = provider_http_metadata(403, headers)
    assert result == {'http_status': 403, 'content_type': kind, 'retry_after_seconds': seconds,
                      'cf_mitigated_challenge': True}
    assert 'never-retained' not in canonical_json(result)


@pytest.mark.parametrize('status', [True, None, '403', 99, 600])
def test_http_error_status_requires_an_actual_valid_integer(status):
    with pytest.raises(ProviderResponseError, match='provider HTTP status is invalid'):
        ProviderHTTPError(status)


@pytest.mark.parametrize('mitigated', ['Challenge', 'anything-else', '', 'challenge'+'x'*300])
def test_challenge_flag_requires_exact_header_value(mitigated):
    assert provider_http_metadata(403, {'cf-mitigated': mitigated})['cf_mitigated_challenge'] is False


@pytest.mark.parametrize('method,path', [('GET', '/v2/pods'), ('POST', '/graphql')])
def test_transport_http_failure_keeps_safe_metadata_without_reading_body_or_retry(tmp_path, method, path):
    key = tmp_path/'key'
    key.write_text('PRIVATE_PROVIDER_KEY')
    key.chmod(0o600)
    headers = Message()
    headers['content-type'] = 'text/html'
    headers['retry-after'] = '60'
    headers['cf-mitigated'] = 'challenge'
    headers['x-private'] = 'PRIVATE_HEADER'
    class UnreadBody(io.BytesIO):
        def read(self, *_args):
            raise AssertionError('provider error body must never be read')
    body = UnreadBody(b'PRIVATE_BODY')
    calls = []
    def open_request(request, *, timeout):
        calls.append((request.method, timeout))
        raise HTTPError('https://private.invalid/PRIVATE_URL', 403, 'PRIVATE_MESSAGE', headers, body)
    transport = RunPodHTTP(key)
    transport.opener = SimpleNamespace(open=open_request)
    with pytest.raises(ProviderHTTPError) as caught:
        transport.request(method, path, {} if method == 'POST' else None)
    assert caught.value.metadata == {'http_status': 403, 'content_type': 'html',
                                    'retry_after_seconds': 60, 'cf_mitigated_challenge': True}
    assert calls == [(method, 15)] and body.closed
    assert 'PRIVATE' not in str(caught.value) + canonical_json(vars(caught.value))


def test_stop_broker_exposes_only_status_and_stop_with_distinct_uid(runpod, tmp_path, monkeypatch):
    backend, _, _, _ = runpod
    create(runpod)
    captured = {}

    def server(path, dispatch, **kwargs):
        captured.update(dispatch=dispatch, **kwargs)
        return object()
    monkeypatch.setattr("probe_core.runpod_provider.UnixRPCServer", server)
    with pytest.raises(PermissionError, match="distinct"):
        serve_stop_broker(backend, tmp_path / "stop.sock", watchdog_uid=os.geteuid())
    serve_stop_broker(backend, tmp_path / "stop.sock", watchdog_uid=os.geteuid() + 1)
    assert captured["allowed_uids"] == {os.geteuid() + 1}
    for method in ("start", "create", "approve", "request", "credentials"):
        with pytest.raises(PermissionError):
            captured["dispatch"](method, {"worker_id": "worker1"})
    with pytest.raises(ValueError):
        captured["dispatch"]("stop", {"worker_id": "worker1", "provider_id": "not-owned"})
    assert captured["dispatch"]("stop", {"worker_id": "worker1"})["state"] == "ABSENT"


@pytest.mark.parametrize("environment", [{"RUNPOD_API_KEY": "not-allowed"}, {"PUBLIC_KEY": "two\nkeys"},
                                         {"PROBE_CGROUP_ROOT": "/sys/fs/cgroup/../etc"}])
def test_launch_environment_is_small_nonsecret_allowlist(environment):
    with pytest.raises(ValueError):
        RunPodLaunchConfig(image_repository="ghcr.io/test/worker", environment=environment)
