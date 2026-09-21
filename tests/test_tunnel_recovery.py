"""Owned SSH failure restarts transport without granting new execution authority."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from probe_core.controller import Controller
from probe_core.dispatcher import Dispatcher, DispatcherService, SSHTunnel, TransportError, TunnelExited
from probe_core.ledger import Ledger
from probe_core.provider import SimulatedProvider
from probe_core.worker_contracts import ExecutionReceipt
from test_ledger import approve, clock, job_factory, manifest_data


def initialize_controller(ledger, tmp_path):
    # Constructor initializes durable controller schema only. No provider action.
    Controller(ledger, SimulatedProvider(tmp_path / "simulator.sqlite"),
               watchdog_health_path=tmp_path / "health.json", controller_idle_usd_per_day=0.0)


def make_tunnel(tmp_path):
    key, hosts = tmp_path / "private-key", tmp_path / "known-hosts"
    key.write_text("synthetic SSH fixture; never used with a real server")
    hosts.write_text("synthetic pinned-host fixture")
    key.chmod(0o600)
    hosts.chmod(0o600)
    return SSHTunnel("fixture.example", user="root", identity_file=key,
                     known_hosts_file=hosts, local_port=41000)


@contextmanager
def live_owned_child(tmp_path):
    tunnel = make_tunnel(tmp_path)
    # Poll a real owned process; no SSH/network/cloud connection is made here.
    tunnel.process = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield tunnel
    finally:
        tunnel.close()


def kill_owned_child(tunnel):
    tunnel.process.kill()
    tunnel.process.wait(timeout=5)


@pytest.fixture
def execution(tmp_path, clock, job_factory):
    clock.now = datetime.now(timezone.utc)
    with Ledger(tmp_path / "research.sqlite", clock=clock) as ledger:
        initialize_controller(ledger, tmp_path)
        pending = ledger.submit_job(job_factory())
        grant = approve(ledger, clock, [pending])
        job = ledger.dispatch_next("worker-1", approval_id=grant.approval_id)
        yield ledger, job, clock


def authority(ledger):
    with ledger.read_connection() as connection:
        return {
            "attempts": [tuple(row) for row in connection.execute("SELECT attempt_id,job_id,attempt_number,approval_id,execution_deadline FROM attempts ORDER BY attempt_id")],
            "approvals": [tuple(row) for row in connection.execute("SELECT * FROM approvals ORDER BY approval_id")],
            "jobs": connection.execute("SELECT count(*) FROM jobs").fetchone()[0],
            "requests": connection.execute("SELECT count(*) FROM compute_requests").fetchone()[0],
        }


class ObservedWorker:
    def __init__(self, job, now, *, failure=None):
        self.job, self.now, self.failure = job, now, failure
        self.queries, self.cancellations = [], []

    def status(self, attempt_id):
        self.queries.append(attempt_id)
        if self.failure:
            self.failure()
        return ExecutionReceipt(job_id=self.job.job_id, attempt_id=attempt_id, state="RUNNING", started_at=self.now)

    def submit(self, request):
        raise AssertionError("transport recovery must never resubmit or create an execution")

    def cancel(self, attempt_id):
        self.cancellations.append(attempt_id)
        return ExecutionReceipt(job_id=self.job.job_id, attempt_id=attempt_id, state="CANCELLED", started_at=self.now,
                                finished_at=self.now, failure_kind="cancelled", process_stopped=True)


def service_for(ledger, client, tmp_path, tunnel):
    dispatcher = Dispatcher(ledger, client, worker_id="worker-1", transfer_directory=tmp_path / "transfers")
    return DispatcherService(dispatcher, tunnel=tunnel)


def test_dead_owned_child_exits_pump_before_any_reconciliation_or_state_change(execution, tmp_path):
    ledger, job, clock = execution
    client = ObservedWorker(job, clock())
    before, audit = authority(ledger), ledger.audit_records()
    with live_owned_child(tmp_path) as tunnel:
        kill_owned_child(tunnel)
        with pytest.raises(TunnelExited, match="existing attempt"):
            service_for(ledger, client, tmp_path, tunnel).run(threading.Event())
    assert client.queries == client.cancellations == []
    assert ledger.get_job(job.job_id) == job
    assert authority(ledger) == before and ledger.audit_records() == audit


@pytest.mark.parametrize("started", [False, True])
def test_death_during_http_request_is_fatal_and_restart_queries_same_attempt(execution, tmp_path, started):
    ledger, job, clock = execution
    if started:
        ledger.start_job(job.job_id, job.attempt_id, "worker-1")
    before = authority(ledger)
    with live_owned_child(tmp_path) as tunnel:
        def lose_connection():
            kill_owned_child(tunnel)
            raise TransportError("connection lost after remote acceptance")
        client = ObservedWorker(job, clock(), failure=lose_connection)
        with pytest.raises(TunnelExited):
            service_for(ledger, client, tmp_path, tunnel).tick()
        assert ledger.get_job(job.job_id).state.value == ("RUNNING" if started else "DISPATCHED")
        assert authority(ledger) == before
    with live_owned_child(tmp_path) as replacement:
        healthy = ObservedWorker(job, clock())
        result = service_for(ledger, healthy, tmp_path, replacement).tick()
    assert healthy.queries == [job.attempt_id] and healthy.cancellations == []
    assert result[0].state.value == "RUNNING" and result[0].attempt_id == job.attempt_id
    assert result[0].attempt_count == 1 and result[0].retry_count == 0
    assert authority(ledger) == before


def test_live_tunnel_http_outage_remains_retryable(execution, tmp_path):
    ledger, job, clock = execution
    before = authority(ledger)
    with live_owned_child(tmp_path) as tunnel:
        def temporarily_unavailable():
            raise TransportError("worker HTTP temporarily unavailable")
        client = ObservedWorker(job, clock(), failure=temporarily_unavailable)
        service = service_for(ledger, client, tmp_path, tunnel)
        assert service.tick() == []
        assert service.last_error_code == "TransportError"
        tunnel.ensure_alive()
        client.failure = None
        assert service.tick()[0].attempt_id == job.attempt_id
        assert service.last_error_code is None
    assert client.queries == [job.attempt_id, job.attempt_id]
    assert authority(ledger) == before


def test_restart_after_deadline_cancels_original_attempt_without_renewal(execution, tmp_path):
    ledger, job, clock = execution
    before = authority(ledger)
    with live_owned_child(tmp_path) as tunnel:
        kill_owned_child(tunnel)
        with pytest.raises(TunnelExited):
            service_for(ledger, ObservedWorker(job, clock()), tmp_path, tunnel).tick()
    clock.advance(301)
    ledger.recover_expired()
    with live_owned_child(tmp_path) as replacement:
        worker = ObservedWorker(job, clock())
        result = service_for(ledger, worker, tmp_path, replacement).tick()
    assert worker.queries == worker.cancellations == [job.attempt_id]
    assert result[0].state.value == "FAILED" and result[0].attempt_id == job.attempt_id
    assert result[0].attempt_count == 1 and result[0].retry_count == 0
    assert authority(ledger) == before
    with ledger.read_connection() as connection:
        assert connection.execute("SELECT stopped_at FROM attempts WHERE attempt_id=?", (job.attempt_id,)).fetchone()[0] is not None


def test_real_dispatcher_process_exits_nonzero_when_owned_ssh_exits(tmp_path):
    # Exercise CLI ownership/wiring with a loopback-only SSH executable fixture.
    # It opens forwarding's local listener, then dies without remote execution.
    executable = tmp_path / "ssh"
    executable.write_text(f"""#!{sys.executable}
import socket, sys, time
forward = sys.argv[sys.argv.index('-L') + 1].split(':')
with socket.socket() as server:
    server.bind(('127.0.0.1', int(forward[1])))
    server.listen()
    connection, _ = server.accept()
    connection.close()
    time.sleep(0.2)
sys.exit(255)
""")
    executable.chmod(0o700)
    tunnel = make_tunnel(tmp_path)
    secret = tmp_path / "bearer"
    secret.write_text("fixture-" + "a" * 40)
    secret.chmod(0o600)
    database = tmp_path / "research.sqlite"
    with Ledger(database) as ledger:
        initialize_controller(ledger, tmp_path)
        before = authority(ledger)
    config = tmp_path / "dispatcher.json"
    config.write_text(json.dumps({"ledger_path": str(database), "worker_id": "worker-1",
        "transfer_directory": str(tmp_path / "transfers"), "input_artifact_root": str(tmp_path / "inputs"),
        "bearer_secret_file": str(secret), "poll_seconds": 0.05,
        "ssh": {"host": "fixture.example", "user": "root", "identity_file": str(tunnel.identity_file),
                "known_hosts_file": str(tunnel.known_hosts_file)}}))
    config.chmod(0o600)
    result = subprocess.run([sys.executable, "-m", "probe_core.dispatcher", "--config", str(config)],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    assert result.returncode != 0 and b"TunnelExited" in result.stderr
    assert secret.read_bytes() not in result.stderr
    with Ledger(database) as ledger:
        assert authority(ledger) == before
    command = tunnel.command()
    assert "StrictHostKeyChecking=yes" in command and "ExitOnForwardFailure=yes" in command
    assert "UserKnownHostsFile=" + str(tunnel.known_hosts_file) in command
