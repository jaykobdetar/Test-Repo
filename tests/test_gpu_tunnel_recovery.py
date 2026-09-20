"""Real Ledger/action transcripts and owned CPU processes; no SSH/GPU/cloud."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from probe_core import gpu_acceptance_runner as runner
from probe_core.gpu_acceptance_actions import collect_action_evidence, run_action
from probe_core.schemas import RunManifest
from probe_core.worker_contracts import ExecutionReceipt
from test_gpu_acceptance_runner import (setup, runpod, manifest_data, approve_fixture,
    select_wait_case, ActionEndpoint, Endpoint)
from test_gpu_acceptance_actions import scenario, clock, job_factory


class ReconnectEndpoint(ActionEndpoint):
    def status(self, attempt_id):
        if any((self.s.root/'runner/actions').glob('*.result.json')):
            return Endpoint.status(self, attempt_id)
        return self.worker.status(attempt_id)


@pytest.fixture
def reconnect_case(setup):
    s = setup
    select_wait_case(s, 'tunnel-reconnect')
    request = approve_fixture(s)
    client = ReconnectEndpoint(s, missing_markers=1)
    hosts = s.root/'known_hosts'
    hosts.write_text('fixed synthetic host key')
    hosts.chmod(0o600)
    settings = {'host': 'fixture.example', 'user': 'root', 'ssh_port': 40125,
        'identity_file': Path(s.config.ssh_identity_file), 'known_hosts_file': hosts}
    transports, configurations, hooks = [], [], {}

    class OwnedTransport:
        def __init__(self, **values):
            self.values = values
            self.local_port = values.get('local_port', 45678)
            self.process = None
            self.original_process = None
            transports.append(self)

        def __enter__(self):
            self.process = self.original_process = subprocess.Popen(
                [sys.executable, '-I', '-c', 'import time; time.sleep(30)'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if len(transports) == 2 and 'open' in hooks:
                hooks['open']()
            return self

        def close(self):
            if self.process is not None:
                self.process.terminate()
                self.process.wait(timeout=2)
                self.process = None
                if self is transports[0] and 'close' in hooks:
                    hooks['close']()

        def __exit__(self, *_):
            self.close()

        def ensure_alive(self):
            assert self.process is not None and self.process.poll() is None

    def lifecycle(values):
        pinned = runner.SSHActionClient(values)
        return SimpleNamespace(config_sha256=pinned.config_sha256, endpoint_identity=pinned.endpoint_identity,
                               inspect=client.inspect, restart=client.restart)

    def configure(*_args, **_kwargs):
        configurations.append(True)
        return {'configured': True, 'worker_config_sha256': client.config_sha256}

    def run(state, **extra):
        return runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)), state,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: time.sleep(.001),
            endpoint=lambda *_: settings, tunnel_factory=OwnedTransport,
            client_factory=lambda *_a, **_k: client, readiness=lambda *_a, **_k: None,
            configure=configure, lifecycle_factory=lifecycle, **extra)

    yield SimpleNamespace(s=s, request=request, client=client, transports=transports,
                          configurations=configurations, hooks=hooks, run=run, hosts=hosts)
    for transport in transports:
        transport.close()


def test_owned_tunnel_reconnect_preserves_exact_execution_and_never_replays(reconnect_case):
    f = reconnect_case
    with runner.State(f.s.config.trusted_state_directory) as state:
        result = f.run(state)
        assert result['status'] == 'passed', result
        observation = state.read('observations.json')
        assert observation['action_evidence']['complete'] is True
        assert f.run(state) == result
    assert len(f.transports) == 2 and len(f.configurations) == f.client.posts == 1
    first, second = f.transports
    assert second.values == dict(first.values, local_port=first.local_port)
    assert all(item.original_process.poll() is not None for item in f.transports)
    report = json.loads(next((f.s.root/'runner/actions').glob('*.result.json')).read_bytes())
    assert report['before']['request'] == report['after']['request'] == f.client.request.model_dump(mode='json')
    assert report['before']['child'] == report['after']['child']
    assert report['before']['supervisor'] == report['after']['supervisor']
    ack = report['action_acknowledgment']
    assert ack['previous_transport']['pid'] == first.original_process.pid
    assert ack['replacement_transport']['pid'] == second.original_process.pid
    assert ack['previous_transport']['exit_status'] is not None
    assert ack['renewed_lease'] <= ack['execution_deadline'] == f.client.request.deadline.timestamp()
    assert f.client.worker.restart_calls == f.client.worker.cancel_calls == 0
    job = f.s.ledger.get_job(f.client.request.job_id)
    assert job.attempt_count == 1 and job.retry_count == 0 and job.attempt_id == f.client.request.attempt_id
    assert job.approval_id == f.client.request.approval_id == f.request['approval_id']
    with closing(sqlite3.connect(f.s.ledger.path)) as connection:
        for table in ('jobs', 'attempts', 'approvals', 'approval_jobs', 'compute_requests'):
            assert connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
        assert connection.execute('SELECT request_id,worker_id FROM compute_requests').fetchone() == (
            f.request['request_id'], f.request['worker_id'])
    assert len(f.s.http.purchases) == 1
    assert result['teardown']['confirmed'] is True and f.s.http.pods == []


@pytest.mark.parametrize('fault', ['key', 'expired_lease', 'short_window', 'open_failure', 'lease_during_open', 'deadline_during_open', 'stop_during_open'])
def test_reconnect_failure_does_not_replay_or_leave_owned_processes(reconnect_case, monkeypatch, fault):
    f = reconnect_case
    if fault == 'key':
        f.hooks['close'] = lambda: f.hosts.write_text('changed synthetic host key')
    elif fault in {'open_failure', 'lease_during_open', 'deadline_during_open', 'stop_during_open'}:
        def fail_open():
            if fault == 'open_failure':
                raise runner.RunnerError('TEST_RECONNECT_FAILED')
            if fault in {'lease_during_open', 'deadline_during_open'}:
                f.s.clock.advance(31 if fault == 'lease_during_open' else 121)
            else:
                raise runner.RunnerError('RUNNER_INTERRUPTED')
        f.hooks['open'] = fail_open
    else:
        original = runner.reconnect_owned_tunnel
        def expired(*args, **kwargs):
            if fault == 'expired_lease':
                with closing(sqlite3.connect(f.s.ledger.path)) as connection:
                    connection.execute('UPDATE jobs SET lease_expires_at=0')
                    connection.execute('UPDATE attempts SET lease_expires_at=0')
                    connection.commit()
            else:
                # Keep the lease live, but leave less than the reconnect budget.
                f.s.clock.advance(100)
                with closing(sqlite3.connect(f.s.ledger.path)) as connection:
                    connection.execute('UPDATE jobs SET lease_expires_at=?', (f.s.clock().timestamp()+20,))
                    connection.execute('UPDATE attempts SET lease_expires_at=?', (f.s.clock().timestamp()+20,))
                    connection.commit()
            return original(*args, **kwargs)
        monkeypatch.setattr(runner, 'reconnect_owned_tunnel', expired)
    with runner.State(f.s.config.trusted_state_directory) as state:
        result = f.run(state)
        assert result['status'] == 'failed', result
        assert f.run(state) == result
    assert f.client.posts == len(f.configurations) == 1 and len(f.transports) <= 2
    assert all(item.original_process.poll() is not None for item in f.transports)
    assert result['teardown']['confirmed'] is True and f.s.http.pods == []
    assert not list((f.s.root/'runner/actions').glob('*.result.json'))


def reconnect_worker(worker):
    calls = []
    def reconnect(request, action_id, *, expected_before):
        calls.append(action_id)
        return {'schema_version': 1, 'operation': 'reconnect_tunnel', 'action_id': action_id,
            'replayed': False, 'before': deepcopy(expected_before), 'endpoint_identity': worker.endpoint_identity,
            'previous_transport': {'pid': 201, 'exit_status': -15, 'local_port': 45678},
            'replacement_transport': {'pid': 202, 'local_port': 45678},
            'renewed_at': worker.clock().isoformat(), 'previous_lease': worker.clock().timestamp()+30,
            'renewed_lease': worker.clock().timestamp()+30, 'execution_deadline': request.deadline.timestamp()}
    worker.reconnect = reconnect
    return calls


def test_completed_and_uncertain_reconnect_intents_never_repeat(scenario):
    ledger, plan, worker, kwargs = scenario('reconnect_tunnel_after_running')
    calls = reconnect_worker(worker)
    report = run_action(ledger, plan, **kwargs)
    assert report['status'] == 'passed'
    assert run_action(ledger, plan, **kwargs) == report and len(calls) == 1
    second_ledger, second_plan, second_worker, second = scenario('reconnect_tunnel_after_running')
    second_calls = []
    def lost(*_args, **_kwargs):
        second_calls.append(True)
        raise runner.TransportError('PRIVATE_UNCERTAIN_REPLY')
    second_worker.reconnect = lost
    assert run_action(second_ledger, second_plan, **second)['status'] == 'uncertain'
    assert run_action(second_ledger, second_plan, **second)['status'] == 'uncertain'
    assert second_calls == [True]


def test_completion_before_reconnect_observation_stays_inconclusive(scenario, manifest_data):
    ledger, plan, worker, kwargs = scenario('reconnect_tunnel_after_running')
    reconnect_worker(worker)
    reconnect = worker.reconnect
    def completes(*args, **kwargs):
        ack = reconnect(*args, **kwargs)
        worker.receipt = ExecutionReceipt(job_id=worker.request.job_id, attempt_id=worker.request.attempt_id,
            state='SUCCEEDED', started_at=worker.clock(), finished_at=worker.clock(),
            process_stopped=True, manifest=RunManifest.model_validate(manifest_data))
        worker.snapshot['child_alive'] = False
        return ack
    worker.reconnect = completes
    report = run_action(ledger, plan, **kwargs)
    assert report['status'] == 'inconclusive'
    assert report['reason'] == 'RECONNECT_DID_NOT_PRESERVE_LIVE_ATTEMPT'
    assert collect_action_evidence(ledger, plan, kwargs['action_directory'])['complete'] is False


@pytest.mark.parametrize('fault', ['endpoint', 'old_alive', 'same_pid', 'local_port', 'deadline', 'supervisor', 'child', 'request', 'replayed', 'missing_ack'])
def test_reconnect_collector_rejects_tampered_durable_proof(scenario, fault):
    ledger, plan, worker, kwargs = scenario('reconnect_tunnel_after_running')
    reconnect_worker(worker)
    report = run_action(ledger, plan, **kwargs)
    assert report['status'] == 'passed'
    assert collect_action_evidence(ledger, plan, kwargs['action_directory'])['complete'] is True
    root = kwargs['action_directory']
    if fault == 'missing_ack':
        next(root.glob('*.ack.json')).unlink()
    else:
        path = next(root.glob('*.result.json'))
        body = json.loads(path.read_bytes())
        ack = body['action_acknowledgment']
        if fault == 'endpoint': ack['endpoint_identity'] = 'sha256:'+'f'*64
        elif fault == 'old_alive': ack['previous_transport']['exit_status'] = None
        elif fault == 'same_pid': ack['replacement_transport']['pid'] = ack['previous_transport']['pid']
        elif fault == 'local_port': ack['replacement_transport']['local_port'] += 1
        elif fault == 'deadline': ack['execution_deadline'] += 1
        elif fault == 'supervisor': body['after']['supervisor']['pid'] += 1
        elif fault == 'child': body['after']['child']['monotonic_deadline'] += 1
        elif fault == 'replayed': ack['replayed'] = True
        else: body['after']['request']['attempt_id'] = 'different-attempt'
        path.write_text(json.dumps(body))
        # Mutate the matching acknowledgment too, so the independent proof
        # checks, not merely byte equality between records, reject it.
        ack_path = next(root.glob('*.ack.json'))
        ack_body = json.loads(ack_path.read_bytes()); ack_body['ack'] = ack
        ack_path.write_text(json.dumps(ack_body))
    assert collect_action_evidence(ledger, plan, root)['complete'] is False


def test_tunnel_case_rejects_mutated_recipe_before_submission(setup):
    s = setup
    case = select_wait_case(s, 'tunnel-reconnect')
    runner.validate_plan(s.config, s.plan)
    s.plan = s.plan.model_copy(update={'cases': (case.model_copy(update={'action': 'wait'}),)})
    with pytest.raises(runner.RunnerError, match='FIXED_CALIBRATION_CASE_REQUIRED'):
        runner.validate_plan(s.config, s.plan)
    assert s.ledger.list_jobs() == [] and s.http.purchases == []
