"""Simulator-only tests for human approval, deadlines and independent stopping."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import multiprocessing
import sqlite3
import threading

import pytest

from probe_core.controller import (
    BudgetError, Controller, ControllerClient, ControllerConflict, StartUncertain,
    StopWatchdog, WatchdogUnavailable, serve_controller,
)
from probe_core.ledger import Ledger
from probe_core.ledger import ApprovalError
from probe_core.provider import DeploymentSpec, PriceQuote, SimulatedProvider, StopOnlyBackend, WorkerState
from probe_core.rpc import RPCError, UnixRPCClient
from probe_core.schemas import JobSpec
from probe_core.schemas import ApprovalNonce


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def deployment(**overrides):
    fields = dict(gpu_model="RTX-A5000", image_digest="sha256:" + "a" * 64,
                  volume_id="research-volume", volume_gb=100, region="test-region")
    fields.update(overrides)
    return DeploymentSpec(**fields)


@pytest.fixture
def harness(tmp_path):
    clock = Clock()
    ledger = Ledger(tmp_path / "research.sqlite", clock=clock)
    backend = SimulatedProvider(tmp_path / "provider.sqlite", clock=clock)
    health = tmp_path / "watchdog" / "health.json"
    controller = Controller(ledger, backend, watchdog_health_path=health,
                            controller_idle_usd_per_day=0.20, clock=clock)
    watcher = StopWatchdog(ledger.path, StopOnlyBackend(backend),
                           state_path=tmp_path / "watchdog" / "state.sqlite", health_path=health, clock=clock)
    watcher.tick()
    original_ack = controller._await_watchdog_ack

    def acknowledge(request, deadline):
        # Unit tests drive the separate watchdog deterministically. A subprocess
        # regression below covers the independently running process boundary.
        watcher.tick()
        return original_ack(request, deadline)

    controller._await_watchdog_ack = acknowledge
    data = json.loads((Path(__file__).parent / "fixtures" / "manifest.json").read_text())
    spec = JobSpec(idempotency_key="controller-job", model=data["model"], inputs=data["inputs"],
                   operation={"kind": "capture", "modules": [{"layer": 0, "component": "residual"}], "positions": ["last"]},
                   limits={"max_runtime_seconds": 60, "max_output_bytes": 1000000})
    job = ledger.submit_job(spec)
    yield dict(clock=clock, ledger=ledger, backend=backend, controller=controller, watcher=watcher,
               health=health, job=job, tmp_path=tmp_path)
    ledger.close()


def provision(harness, runtime=900):
    request = harness["controller"].request_provision(deployment(), [harness["job"].job_id], runtime)
    started = harness["controller"].approve_and_start(request["request_id"])
    return request, started


def test_human_infrastructure_allowance_creates_no_dispatchable_research_jobs(harness):
    controller = harness["controller"]
    params = {"deployment": deployment().model_dump(), "script_sha256": "sha256:" + "c" * 64,
              "max_runtime_seconds": 300}
    with pytest.raises(PermissionError):
        controller.research_dispatch("request_preflight", params)
    request = controller.admin_dispatch("request_preflight", params)
    assert request["job_ids"] == []
    assert request["infrastructure"] == {"kind": "gpu_preflight", "script_sha256": "sha256:" + "c" * 64}
    started = controller.approve_and_start(request["request_id"])
    assert started["state"] == "RUNNING"
    with harness["ledger"].read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM approval_jobs WHERE approval_id=?", (request["approval_id"],)).fetchone()[0] == 0
        body = json.loads(connection.execute("SELECT document FROM approvals WHERE approval_id=?", (request["approval_id"],)).fetchone()[0])
    assert body["purpose"] == "infrastructure_preflight"
    assert harness["ledger"].dispatch_next(request["worker_id"], approval_id=request["approval_id"]) is None
    harness["clock"].advance(300)
    assert harness["watcher"].tick()[0]["reason"] == "absolute_deadline"
    controller.reconcile()
    assert controller.status()[0]["state"] == "STOPPED"


def test_ephemeral_preflight_cannot_be_provisioned_replaced_or_reused_for_research(harness):
    controller = harness["controller"]
    spec = deployment(storage_mode="ephemeral_preflight", volume_id=None, volume_gb=0,
                      image_repository="ghcr.io/test/diagnostic", launch_config_hash="sha256:" + "d" * 64)
    for replaces in (None, "old-worker"):
        with pytest.raises(ControllerConflict, match="infrastructure preflight"):
            controller.request_provision(spec, [harness["job"].job_id], 300, replaces_worker_id=replaces)
    params = {"deployment": spec.model_dump(), "script_sha256": "sha256:" + "c" * 64,
              "max_runtime_seconds": 300}
    with pytest.raises(PermissionError):
        controller.research_dispatch("request_preflight", params)
    request = controller.admin_dispatch("request_preflight", params)
    assert request["job_ids"] == [] and request["configuration_hash"] == spec.digest
    controller.approve_and_start(request["request_id"], price_ceiling_usd_per_hour=.80)
    assert harness["ledger"].dispatch_next(request["worker_id"], approval_id=request["approval_id"]) is None
    harness["clock"].advance(300)
    harness["watcher"].tick()
    controller.reconcile()
    assert controller.status()[0]["state"] == "STOPPED"
    with pytest.raises(ControllerConflict, match="cannot run research"):
        controller.request_start(request["worker_id"], [harness["job"].job_id], 300)
    assert len(calls(harness, "create")) == 1 and calls(harness, "start") == []


@pytest.mark.parametrize("purpose", ["research", "infrastructure_preflight"])
def test_research_and_infrastructure_approval_scopes_cannot_cross(harness, purpose):
    ledger, clock = harness["ledger"], harness["clock"]
    digest = ledger.batch_hash([harness["job"].job_id])
    approval = ApprovalNonce(approval_id="scope-test", token="s" * 48, pod_id="worker-scope", batch_hash=digest,
                             purpose=purpose, max_runtime_seconds=300, price_ceiling_usd_per_hour=1.0,
                             issued_at=clock(), expires_at=clock() + timedelta(seconds=300))
    ledger.register_approval(approval)
    common = dict(pod_id="worker-scope", live_price_usd_per_hour=0.50, requested_runtime_seconds=300)
    with pytest.raises(ApprovalError, match="different purpose"):
        if purpose == "research":
            ledger.consume_infrastructure_approval("scope-test", "s" * 48, infrastructure_hash=digest, **common)
        else:
            ledger.consume_approval("scope-test", "s" * 48, job_ids=[harness["job"].job_id], **common)
    with ledger.read_connection() as connection:
        assert connection.execute("SELECT consumed_at FROM approvals WHERE approval_id='scope-test'").fetchone()[0] is None


def test_infrastructure_cannot_overlap_an_unclosed_research_allowance(harness):
    provision(harness, runtime=300)
    request = harness["controller"].request_infrastructure_preflight(deployment(), "sha256:" + "d" * 64, 300)
    with pytest.raises(ApprovalError, match="shutdown"):
        harness["controller"].approve_and_start(request["request_id"])
    assert len(calls(harness, "create")) == 1


def calls(harness, operation):
    return [call for call in harness["backend"].calls() if call["operation"] == operation]


def test_provisioning_requires_admin_action_and_binds_exact_configuration(harness):
    controller = harness["controller"]
    spec = deployment()
    request = controller.request_provision(spec, [harness["job"].job_id], 300)
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.ABSENT
    assert calls(harness, "create") == []
    assert request["configuration_hash"] == spec.digest
    started = controller.approve_and_start(request["request_id"])
    assert started["state"] == "RUNNING"
    observed = harness["backend"].status(request["worker_id"])
    assert observed.request_key == request["request_id"]
    assert observed.configuration_hash == spec.digest
    assert started["observed_provider_id"] == observed.provider_id
    with harness["ledger"].read_connection() as connection:
        row = connection.execute("SELECT document,deadline FROM approvals WHERE approval_id=?", (request["approval_id"],)).fetchone()
    assert json.loads(row["document"])["pod_id"] == request["worker_id"]
    assert row["deadline"] == started["deadline"]
    with pytest.raises(ControllerConflict):
        controller.approve_and_start(request["request_id"])
    assert len(calls(harness, "create")) == 1


def test_paid_action_observes_committed_absolute_deadline(harness, monkeypatch):
    original = harness["backend"].create

    def checked_create(worker_id, config, *, request_key, **limits):
        with harness["ledger"].read_connection() as connection:
            row = connection.execute("""SELECT r.state,r.deadline,a.consumed_at,a.deadline AS grant_deadline
                FROM compute_requests r JOIN approvals a USING(approval_id) WHERE r.request_id=?""", (request_key,)).fetchone()
        assert row["state"] == "STARTING"
        assert row["consumed_at"] is not None
        assert row["deadline"] == row["grant_deadline"] == (harness["clock"]() + timedelta(seconds=300)).timestamp()
        return original(worker_id, config, request_key=request_key, **limits)

    monkeypatch.setattr(harness["backend"], "create", checked_create)
    provision(harness, runtime=300)


@pytest.mark.parametrize("price", [1.50, 2.0, -1.0, 0.0])
def test_provider_price_gate_is_strict_and_never_accepts_caller_prices(harness, price):
    harness["backend"].set_price(price)
    request = harness["controller"].request_provision(deployment(), [harness["job"].job_id], 300)
    with pytest.raises(BudgetError):
        harness["controller"].approve_and_start(request["request_id"])
    assert calls(harness, "create") == []
    with pytest.raises(TypeError):
        harness["controller"].approve_and_start(request["request_id"], live_price_usd_per_hour=0.1)


def test_total_idle_cost_includes_storage_and_controller(harness):
    controller = Controller(harness["ledger"], harness["backend"], watchdog_health_path=harness["health"],
                            controller_idle_usd_per_day=1.95, clock=harness["clock"])
    request = controller.request_provision(deployment(), [harness["job"].job_id], 300)
    with pytest.raises(BudgetError):
        controller.approve_and_start(request["request_id"])
    assert calls(harness, "create") == []


@pytest.mark.parametrize("quote", ["nan", "stale", "future"])
def test_unknown_or_stale_provider_quote_fails_closed(harness, monkeypatch, quote):
    now = harness["clock"]()
    supplied = PriceQuote(float("nan") if quote == "nan" else 0.50, 0.25,
                          now + timedelta(seconds=-31 if quote == "stale" else 1 if quote == "future" else 0))
    monkeypatch.setattr(harness["backend"], "quote", lambda **kwargs: supplied)
    request = harness["controller"].request_provision(deployment(), [harness["job"].job_id], 300)
    with pytest.raises(BudgetError):
        harness["controller"].approve_and_start(request["request_id"])
    assert calls(harness, "create") == []


def test_start_refuses_stale_independent_watchdog(harness):
    request = harness["controller"].request_provision(deployment(), [harness["job"].job_id], 300)
    harness["clock"].advance(16)
    with pytest.raises(WatchdogUnavailable):
        harness["controller"].approve_and_start(request["request_id"])
    assert calls(harness, "create") == []
    harness["watcher"].tick()
    assert harness["controller"].approve_and_start(request["request_id"])["state"] == "RUNNING"


def test_watchdog_stops_after_five_minutes_idle_and_reads_back(harness):
    request, _ = provision(harness)
    harness["watcher"].tick()
    harness["clock"].advance(299)
    assert harness["watcher"].tick() == []
    harness["clock"].advance(1)
    assert harness["watcher"].tick() == [{"worker_id": request["worker_id"], "reason": "five_minute_idle", "confirmed_off": True}]
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED
    harness["controller"].reconcile()
    assert harness["controller"].status()[0]["state"] == "STOPPED"


def test_watchdog_idle_deadline_survives_its_own_restart(harness):
    request, _ = provision(harness)
    harness["watcher"].tick()
    harness["clock"].advance(200)
    restarted = StopWatchdog(harness["ledger"].path, StopOnlyBackend(harness["backend"]),
                             state_path=harness["watcher"].state_path, health_path=harness["health"], clock=harness["clock"])
    assert restarted.tick() == []
    harness["clock"].advance(100)
    assert restarted.tick()[0]["reason"] == "five_minute_idle"
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED


def test_absolute_deadline_stops_even_an_active_job_without_controller(harness):
    request, started = provision(harness, runtime=120)
    active = harness["ledger"].dispatch_next("worker-1", approval_id=started["approval_id"])
    harness["ledger"].start_job(active.job_id, active.attempt_id, "worker-1")
    harness["watcher"].tick()
    harness["clock"].advance(120)
    result = harness["watcher"].tick()
    assert result == [{"worker_id": request["worker_id"], "reason": "absolute_deadline", "confirmed_off": True}]
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED
    harness["controller"].reconcile()
    assert str(harness["ledger"].get_job(active.job_id).state) == "FAILED"


def test_stop_failure_is_retried_and_cannot_renew_deadline(harness, monkeypatch):
    request, started = provision(harness, runtime=60)
    original = harness["backend"].stop
    harness["clock"].advance(60)
    with monkeypatch.context() as patch:
        patch.setattr(harness["backend"], "stop", lambda _: (_ for _ in ()).throw(OSError("stop unavailable")))
        assert harness["watcher"].tick()[0]["confirmed_off"] is False
    harness["clock"].advance(2)
    assert harness["watcher"].tick()[0]["confirmed_off"] is True
    assert harness["backend"].stop == original
    assert harness["controller"].status()[0]["deadline"] == started["deadline"]
    assert len(calls(harness, "create")) == 1


def test_create_accepted_then_timeout_is_stopped_and_never_replayed(harness, monkeypatch):
    original = harness["backend"].create

    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("provider response lost")

    monkeypatch.setattr(harness["backend"], "create", uncertain)
    request = harness["controller"].request_provision(deployment(), [harness["job"].job_id], 300)
    with pytest.raises(StartUncertain):
        harness["controller"].approve_and_start(request["request_id"])
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED
    assert len(calls(harness, "create")) == 1
    harness["controller"].reconcile()
    with pytest.raises(ControllerConflict):
        harness["controller"].approve_and_start(request["request_id"])
    assert len(calls(harness, "create")) == 1


def test_absent_uncertain_creation_stays_blocked_until_resource_can_be_stopped(harness, monkeypatch):
    original = harness["backend"].create
    request = harness["controller"].request_provision(deployment(), [harness["job"].job_id], 300)
    with monkeypatch.context() as patch:
        patch.setattr(harness["backend"], "create", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("ambiguous submission")))
        with pytest.raises(StartUncertain):
            harness["controller"].approve_and_start(request["request_id"])
    assert harness["controller"].status()[0]["state"] == "UNCERTAIN"
    assert len(calls(harness, "create")) == 0
    # Simulate the provider completing the original delayed request, never a
    # second controller-issued create. Reconciliation must find and stop it.
    original(request["worker_id"], deployment(), request_key=request["request_id"])
    harness["controller"].reconcile()
    assert harness["controller"].status()[0]["state"] == "STOPPED"
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED


@pytest.mark.parametrize("crash_at", ["STARTING", "RUNNING"])
def test_restart_reconciles_crash_boundaries_without_repeating_paid_action(harness, monkeypatch, crash_at):
    controller = harness["controller"]
    request = controller.request_provision(deployment(), [harness["job"].job_id], 300)
    original_state = controller._state

    def interrupted(request_id, state, **kwargs):
        if state == crash_at:
            raise SystemExit("simulated process death")
        return original_state(request_id, state, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(controller, "_state", interrupted)
        with pytest.raises(SystemExit):
            controller.approve_and_start(request["request_id"])
    restarted = Controller(harness["ledger"], harness["backend"], watchdog_health_path=harness["health"],
                           controller_idle_usd_per_day=0.2, clock=harness["clock"])
    restarted.reconcile()
    assert restarted.status()[0]["state"] == ("REJECTED" if crash_at == "STARTING" else "STOPPED")
    assert len(calls(harness, "create")) == (0 if crash_at == "STARTING" else 1)


def test_replacement_and_restart_each_require_new_human_approval(harness):
    request, _ = provision(harness)
    harness["controller"].stop_gpu(request["worker_id"])
    replacement = harness["controller"].request_provision(deployment(image_digest="sha256:" + "b" * 64),
        [harness["job"].job_id], 300, replaces_worker_id=request["worker_id"])
    assert len(calls(harness, "create")) == 1
    assert replacement["worker_id"] != request["worker_id"]
    harness["controller"].approve_and_start(replacement["request_id"])
    assert len(calls(harness, "create")) == 2
    harness["controller"].stop_gpu(replacement["worker_id"])
    restart = harness["controller"].request_start(replacement["worker_id"], [harness["job"].job_id], 300)
    assert calls(harness, "start") == []
    harness["controller"].approve_and_start(restart["request_id"])
    assert len(calls(harness, "start")) == 1


def test_simulated_shutdown_waits_for_positive_executor_stop_receipt(harness):
    request, started = provision(harness)
    ledger = harness["ledger"]
    active = ledger.dispatch_next(request["worker_id"], approval_id=started["approval_id"])
    ledger.start_job(active.job_id, active.attempt_id, request["worker_id"])

    stopped = harness["controller"].stop_gpu(request["worker_id"])[0]
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED
    assert stopped["state"] == "STOP_REQUESTED"
    assert stopped["last_error_code"] == "WorkerStopPending"
    assert ledger.get_job(active.job_id).state.value == "FAILED"
    with ledger.read_connection() as reader:
        assert reader.execute("SELECT stopped_at FROM attempts WHERE attempt_id=?", (active.attempt_id,)).fetchone()[0] is None
        assert reader.execute("SELECT ended_at FROM approvals WHERE approval_id=?", (started["approval_id"],)).fetchone()[0] is None

    # The trusted dispatcher supplies this only after checking the worker's
    # authenticated process-stop receipt. Controller restart must preserve it.
    restarted = Controller(ledger, harness["backend"], watchdog_health_path=harness["health"],
                           controller_idle_usd_per_day=0.2, clock=harness["clock"])
    assert restarted.reconcile()[0]["state"] == "STOP_REQUESTED"
    ledger.confirm_stopped(active.job_id, active.attempt_id)
    assert restarted.reconcile()[0]["state"] == "STOPPED"
    with ledger.read_connection() as reader:
        assert reader.execute("SELECT ended_at FROM approvals WHERE approval_id=?", (started["approval_id"],)).fetchone()[0] is not None
    assert len(calls(harness, "create")) == 1


def test_research_dispatch_has_no_approval_or_arbitrary_provider_route(harness):
    for method in ("approve", "approve_and_start", "start", "create", "provider"):
        with pytest.raises(PermissionError):
            harness["controller"].research_dispatch(method, {})
    assert harness["controller"].research_dispatch("status", {}) == []


@pytest.mark.parametrize("facade_offset,admin_offset,agent_offset", [
    (0, 0, 2),   # Human and trusted service are one identity.
    (0, 1, 1),   # Human and untrusted MCP client are one identity.
    (0, 1, 0),   # Agent and controller/facade are one identity.
    (1, 2, 1),   # Agent and a separate trusted facade are one identity.
    (1, 1, 2),   # Human and a separate trusted facade are one identity.
])
def test_trusted_human_and_agent_identities_are_distinct(harness, facade_offset, admin_offset, agent_offset):
    with pytest.raises(PermissionError):
        serve_controller(harness["controller"], harness["tmp_path"] / "research.sock",
                         harness["tmp_path"] / "admin.sock", research_uid=os.geteuid() + facade_offset,
                         admin_uid=os.geteuid() + admin_offset, agent_uid=os.geteuid() + agent_offset)
    assert not (harness["tmp_path"] / "research.sock").exists()


def test_agent_identity_is_mandatory(harness):
    with pytest.raises(TypeError, match="agent_uid"):
        serve_controller(harness["controller"], harness["tmp_path"] / "research.sock",
                         harness["tmp_path"] / "admin.sock", research_uid=os.geteuid(), admin_uid=os.geteuid() + 1)


def test_real_research_socket_can_request_but_cannot_approve(harness):
    research, admin = serve_controller(harness["controller"], harness["tmp_path"] / "research.sock",
        harness["tmp_path"] / "admin.sock", research_uid=os.geteuid(), admin_uid=os.geteuid() + 1,
        agent_uid=os.geteuid() + 2,
        allow_service_uid=True)  # Test-only same-process peer identity.
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (research, admin)]
    for thread in threads:
        thread.start()
    try:
        client = ControllerClient(research.path, expected_server_uid=os.geteuid())
        request = client.request_provision(deployment(), [harness["job"].job_id], 300)
        assert request["state"] == "PENDING"
        with pytest.raises(RPCError):
            client.rpc.call("approve", {"request_id": request["request_id"]})
        with pytest.raises(RPCError):
            UnixRPCClient(admin.path, expected_server_uid=os.geteuid()).call("approve", {"request_id": request["request_id"]})
        assert calls(harness, "create") == []
    finally:
        for server in (research, admin):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_watchdog_ack_must_match_exact_worker_approval_and_deadline(harness):
    request, started = provision(harness)
    controller = Controller(harness["ledger"], harness["backend"], watchdog_health_path=harness["health"],
                            controller_idle_usd_per_day=0.2, clock=harness["clock"])
    controller._await_watchdog_ack(request, started["deadline"], timeout_seconds=0.03)
    with pytest.raises(WatchdogUnavailable, match="exact compute deadline"):
        controller._await_watchdog_ack(request, started["deadline"] + 1, timeout_seconds=0.03)


def test_cached_watchdog_schedule_stops_after_ledger_becomes_unavailable(harness, monkeypatch):
    request, _ = provision(harness)
    restarted = StopWatchdog(harness["ledger"].path, StopOnlyBackend(harness["backend"]),
                             state_path=harness["watcher"].state_path, health_path=harness["health"], clock=harness["clock"])
    original_connect = sqlite3.connect

    def unavailable(database, *args, **kwargs):
        if str(database) == harness["ledger"].path.as_uri() + "?mode=ro":
            raise sqlite3.OperationalError("controller disk unavailable")
        return original_connect(database, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", unavailable)
        actions = restarted.tick()
    assert actions == [{"worker_id": request["worker_id"], "reason": "ledger_unavailable", "confirmed_off": True}]
    assert json.loads(harness["health"].read_text())["healthy"] is False
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED


def test_storage_gate_keeps_old_replacement_volumes_in_total(harness):
    old, _ = provision(harness)
    harness["controller"].stop_gpu(old["worker_id"])
    request = harness["controller"].request_provision(deployment(volume_id="another-volume", volume_gb=900),
        [harness["job"].job_id], 300, replaces_worker_id=old["worker_id"])
    with pytest.raises(BudgetError):
        harness["controller"].approve_and_start(request["request_id"])
    assert len(calls(harness, "create")) == 1


def _watchdog_process(database, provider, state, health, clock_value, stopped, replies):
    clock = lambda: datetime.fromtimestamp(clock_value.value, timezone.utc)
    backend = SimulatedProvider(provider, clock=clock)
    watcher = StopWatchdog(database, StopOnlyBackend(backend), state_path=state, health_path=health, clock=clock)
    while not stopped.is_set():
        try:
            for action in watcher.tick():
                replies.put(action)
        except Exception as exc:
            replies.put({"error": type(exc).__name__})
        stopped.wait(0.02)


def _approve_process(database, provider, health, clock_value, request_id, replies):
    clock = lambda: datetime.fromtimestamp(clock_value.value, timezone.utc)
    try:
        with Ledger(database, clock=clock) as ledger:
            controller = Controller(ledger, SimulatedProvider(provider, clock=clock),
                                    watchdog_health_path=health, controller_idle_usd_per_day=0.2, clock=clock)
            replies.put(controller.approve_and_start(request_id))
    except Exception as exc:
        replies.put({"error": type(exc).__name__})


def test_separate_watchdog_process_enforces_deadline_after_starting_process_exits(harness):
    context = multiprocessing.get_context("spawn")
    clock_value = context.Value("d", harness["clock"]().timestamp())
    stopped = context.Event()
    stops = context.Queue()
    starts = context.Queue()
    request = harness["controller"].request_provision(deployment(), [harness["job"].job_id], 60)
    watcher = context.Process(target=_watchdog_process, args=(str(harness["ledger"].path), str(harness["backend"].path),
        str(harness["watcher"].state_path), str(harness["health"]), clock_value, stopped, stops))
    starter = context.Process(target=_approve_process, args=(str(harness["ledger"].path), str(harness["backend"].path),
        str(harness["health"]), clock_value, request["request_id"], starts))
    watcher.start()
    starter.start()
    try:
        started = starts.get(timeout=10)
        assert started.get("state") == "RUNNING", started
        starter.join(timeout=5)
        assert starter.exitcode == 0
        assert watcher.is_alive()
        with clock_value.get_lock():
            clock_value.value += 60
        action = stops.get(timeout=10)
        assert action == {"worker_id": request["worker_id"], "reason": "absolute_deadline", "confirmed_off": True}
        assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED
    finally:
        stopped.set()
        for process in (starter, watcher):
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        stops.close()
        starts.close()


def test_provider_rechecks_budget_if_price_changes_after_approval(harness, monkeypatch):
    original_create = harness["backend"].create

    def increased_price(*args, **kwargs):
        harness["backend"].set_price(1.50)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(harness["backend"], "create", increased_price)
    request = harness["controller"].request_provision(deployment(), [harness["job"].job_id], 300)
    with pytest.raises(BudgetError, match="changed price"):
        harness["controller"].approve_and_start(request["request_id"])
    assert calls(harness, "create") == []
    assert harness["controller"].status()[0]["state"] == "REJECTED"


def test_idle_timer_starts_after_active_execution_finishes(harness):
    request, started = provision(harness, runtime=900)
    active = harness["ledger"].dispatch_next("worker", approval_id=started["approval_id"])
    harness["watcher"].tick()
    harness["clock"].advance(400)
    harness["ledger"].cancel_job(active.job_id)
    harness["ledger"].confirm_stopped(active.job_id, active.attempt_id)
    assert harness["watcher"].tick() == []
    harness["clock"].advance(299)
    assert harness["watcher"].tick() == []
    harness["clock"].advance(1)
    assert harness["watcher"].tick()[0]["reason"] == "five_minute_idle"
    assert harness["backend"].status(request["worker_id"]).state == WorkerState.STOPPED
