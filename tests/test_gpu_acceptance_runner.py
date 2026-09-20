"""No live resources: real Ledger/facade/dispatcher with a simulated endpoint."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from probe_core import gpu_acceptance_runner as runner
from probe_core.audit import canonical_json
from probe_core.controller import Controller
from probe_core.gpu_acceptance import AcceptanceCase, AcceptancePlan
from probe_core.ledger import Ledger
from probe_core.provider import DeploymentSpec, WorkerState
from probe_core.research_api import ResearchPolicy, ResearchService
from probe_core.schemas import ApprovalNonce, JobSpec, RunManifest
from probe_core.worker_contracts import ExecutionReceipt
from probe_core.worker import WorkerHTTPServer
from probe_core.dispatcher import WorkerClient
from test_runpod_provider import runpod
from test_schemas import manifest_data


@pytest.fixture
def setup(tmp_path, runpod, manifest_data):
    backend, http, clock, deployment = runpod
    backend.config = backend.config.model_copy(update={'max_runtime_seconds': 900})
    deployment = DeploymentSpec.model_validate(dict(deployment.model_dump(),
        storage_mode='disposable_research', volume_id=None, volume_gb=0))
    clock.value = datetime.now(timezone.utc)
    data = deepcopy(manifest_data)
    data['model']['revision_sha'] = 'ea980cb0a6c2ae4b936e82123acc929f1cec04c1'
    data['inputs']['generation']['max_new_tokens'] = 4
    spec = JobSpec(idempotency_key='one-public-calibration', experiment_stage='calibration',
                   model=data['model'], inputs=data['inputs'], operation={'kind': 'backend_parity'},
                   limits={'max_runtime_seconds': 240, 'max_output_bytes': 1024 * 1024})
    plan = AcceptancePlan(label='one-public', model=spec.model, cases=(AcceptanceCase(
        name='backend-parity', action='wait', expected_state='COMPLETED', spec=spec),))
    plan_path = tmp_path / 'plan.json'
    plan_path.write_text(plan.model_dump_json())
    plan_path.chmod(0o644)
    for name in ('submit', 'runner'):
        (tmp_path / name).mkdir(mode=0o700)
    key, token = tmp_path / 'key', tmp_path / 'token'
    key.write_text('synthetic fixture private key')
    token.write_text('fixture-bearer-' + 'z' * 40)
    key.chmod(0o600)
    token.chmod(0o600)
    worker_path = tmp_path / 'worker.json'
    worker_path.write_text(canonical_json({
        'model_directory': '/opt/probe-assets/models/qwen3-base', 'model': spec.model.model_dump(mode='json'),
        'assets': [{'path': 'config.json', 'sha256': 'sha256:'+'e'*64}],
        'datasets': [{'path': '/opt/probe-assets/datasets/public.json', 'sha256': spec.inputs.dataset_revision}],
        'tensor_directory': '/workspace/probe/tensors', 'output_directory': '/workspace/probe/attempts',
        'backend': 'nnsight', 'device': 'cuda:0', 'code_git_commit': 'c'*40,
        'container_image_digest': deployment.image_digest, 'provider_backend': 'runpod',
        'region': deployment.region, 'live_price_usd_per_hour': .74,
        'cgroup_directory': '/sys/fs/cgroup/probe-jobs'}))
    worker_path.chmod(0o644)
    config = runner.RunnerConfig(service_uid=os.geteuid(), research_uid=os.geteuid()+10,
        admin_uid=os.geteuid()+20, plan_path=str(plan_path),
        plan_sha256='sha256:' + hashlib.sha256(plan_path.read_bytes()).hexdigest(), deployment=deployment,
        worker_config_path=str(worker_path), worker_config_sha256='sha256:'+hashlib.sha256(worker_path.read_bytes()).hexdigest(),
        source_commit='c' * 40, expected_worker_price_usd_per_hour=0.74, max_runtime_seconds=900,
        submission_state_directory=str(tmp_path/'submit'), trusted_state_directory=str(tmp_path/'runner'),
        ssh_identity_file=str(key), bearer_secret_file=str(token))
    ledger = Ledger(tmp_path / 'research.sqlite', clock=clock)
    controller = Controller(ledger, backend, watchdog_health_path=tmp_path/'not-used', controller_idle_usd_per_day=0)
    service = ResearchService(ledger, ResearchPolicy(discovery_datasets=(spec.inputs.dataset_revision,),
                              allow_calibration=True), cloud=controller)
    rpc = SimpleNamespace(call=lambda method, params=None: service.dispatch(method, params or {}))
    yield SimpleNamespace(config=config, plan=plan, ledger=ledger, controller=controller,
                          rpc=rpc, backend=backend, http=http, clock=clock, data=data, root=tmp_path)
    ledger.close()


def queue(s):
    with runner.State(s.config.submission_state_directory) as state:
        return runner.submit(s.config, s.plan, s.rpc, state)


def approve_fixture(s):
    queued = queue(s)
    request = runner.find_request(s.ledger, s.config, s.plan)
    nonce = ApprovalNonce(approval_id=request['approval_id'], token='test-'+'s'*48,
        pod_id=request['worker_id'], batch_hash=request['batch_hash'], max_runtime_seconds=s.config.max_runtime_seconds,
        price_ceiling_usd_per_hour=0.8, issued_at=s.clock(), expires_at=s.clock()+timedelta(minutes=5))
    s.ledger.register_approval(nonce)
    grant = s.ledger.consume_approval(nonce.approval_id, nonce.token.get_secret_value(),
        pod_id=request['worker_id'], job_ids=request['job_ids'], live_price_usd_per_hour=0.74,
        requested_runtime_seconds=s.config.max_runtime_seconds)
    s.backend.create(request['worker_id'], s.config.deployment, request_key=request['request_id'],
        price_ceiling_usd_per_hour=0.8, storage_ceiling_usd_per_day=2, absolute_deadline=grant.deadline)
    observed = s.backend.status(request['worker_id'])
    s.controller._state(request['request_id'], 'RUNNING', deadline=grant.deadline.timestamp(), provider_id=observed.provider_id)
    return runner.find_request(s.ledger, s.config, s.plan)


def test_research_submission_is_audited_without_consuming_approval(setup):
    s = setup
    first = queue(s)
    assert queue(s) == first
    assert len(s.ledger.list_jobs()) == len(s.controller.status()) == 1
    assert not s.http.purchases
    with s.ledger.read_connection() as conn:
        assert conn.execute('SELECT count(*) FROM approvals').fetchone()[0] == 0
        events = [json.loads(row[0]) for row in conn.execute('SELECT record FROM audit_events')]
    assert any(row['payload'].get('tool') == 'submit_job' for row in events)
    assert first['approval_consumed_by_runner'] is False


def test_lost_provision_reply_reconciles_without_second_proposal(setup):
    s = setup
    calls = []
    def call(method, params=None):
        calls.append(method)
        result = s.rpc.call(method, params or {})
        if method == 'request_gpu_provision':
            raise TimeoutError('private response')
        return result
    with runner.State(s.config.submission_state_directory) as state:
        first = runner.submit(s.config, s.plan, SimpleNamespace(call=call), state)
        assert runner.submit(s.config, s.plan, SimpleNamespace(call=call), state) == first
    assert calls.count('request_gpu_provision') == 1
    assert len(s.controller.status()) == 1


def test_uncertain_intent_without_response_is_never_replayed(setup):
    s = setup
    attempts = []
    def call(method, params=None):
        if method == 'request_gpu_provision':
            attempts.append(1)
            raise TimeoutError('never committed')
        return s.rpc.call(method, params or {})
    for _ in range(2):
        with runner.State(s.config.submission_state_directory) as state:
            with pytest.raises(runner.RunnerError, match='PROVISION_RESPONSE_UNCERTAIN'):
                runner.submit(s.config, s.plan, SimpleNamespace(call=call), state)
    assert len(attempts) == 1


def test_duplicate_exact_requests_refused(setup):
    s = setup
    queued = queue(s)
    s.controller.request_provision(s.config.deployment, [queued['job_id']], s.config.max_runtime_seconds)
    with pytest.raises(runner.RunnerError, match='DUPLICATE_PROVISION_REQUESTS'):
        runner.find_request(s.ledger, s.config, s.plan)


@pytest.mark.parametrize('change,code', [
    ({'state': 'PENDING'}, 'REQUEST_CHANGED'),
    ({'observed_provider_id': 'other'}, 'REQUEST_CHANGED'),
    ({'deadline': 0}, 'REQUEST_CHANGED'),
])
def test_authority_requires_exact_current_request(setup, change, code):
    s = setup
    request = approve_fixture(s)
    assert runner.validate_authority(s.ledger, s.config, s.plan, request, now=s.clock().timestamp()) == request['deadline']
    with pytest.raises(runner.RunnerError, match=code):
        runner.validate_authority(s.ledger, s.config, s.plan, dict(request, **change), now=s.clock().timestamp())


def test_expired_or_ended_approval_refused(setup):
    s = setup
    request = approve_fixture(s)
    with pytest.raises(runner.RunnerError, match='APPROVAL_BINDING_INVALID'):
        runner.validate_authority(s.ledger, s.config, s.plan, request, now=request['deadline'])
    s.ledger.end_approval(request['approval_id'])
    with pytest.raises(runner.RunnerError, match='APPROVAL_BINDING_INVALID'):
        runner.validate_authority(s.ledger, s.config, s.plan, request, now=s.clock().timestamp())


def public_key(fill):
    raw = b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + bytes([fill]) * 32
    return 'ssh-ed25519 ' + base64.b64encode(raw).decode()


def endpoint_fixtures(s, *, host='213.192.2.71'):
    request = approve_fixture(s)
    s.http.pods[0]['runtime'] = {'ports': [{'ip': host, 'private': 22, 'public': 40125, 'type': 'tcp'}]}
    client, server = public_key(1), public_key(2)
    events = [{'source': 'container', 'line': f'256 {runner.fingerprint(key)} {label} (ED25519)'}
              for key, label in ((client, 'client'), (server, 'root@worker'))]
    def command(argv):
        return client if argv[0].endswith('ssh-keygen') else f'[213.192.2.71]:40125 {server}\n'
    return request, events, command


def test_actual_v2_endpoint_shape_requires_authenticated_host_fingerprint(setup):
    s = setup
    request, events, command = endpoint_fixtures(s)
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.verified_endpoint(s.config, request, s.backend, state,
                                         logs=lambda *_: events, command=command)
        assert result['host'] == '213.192.2.71' and result['ssh_port'] == 40125
        assert result['known_hosts_file'].read_text().endswith(public_key(2)+'\n')
        assert state.read('endpoint.json')['provider_id'] == request['observed_provider_id']


@pytest.mark.parametrize('shape', ['runtime_missing', 'runtime_null', 'ports_missing', 'ports_null', 'ports_empty'])
def test_not_yet_published_endpoint_is_unavailable_without_ssh_or_log_reads(setup, shape):
    s = setup
    request, _, _ = endpoint_fixtures(s)
    if shape == 'runtime_missing':
        s.http.pods[0].pop('runtime')
    else:
        s.http.pods[0]['runtime'] = {'runtime_null': None, 'ports_missing': {},
                                   'ports_null': {'ports': None}, 'ports_empty': {'ports': []}}[shape]
    def forbidden(*_):
        raise AssertionError('SSH and log reads require an actual endpoint')
    with runner.State(s.config.trusted_state_directory) as state:
        with pytest.raises(runner.RunnerError, match='^DIRECT_SSH_ENDPOINT_UNAVAILABLE$'):
            runner.verified_endpoint(s.config, request, s.backend, state, logs=forbidden, command=forbidden)
        assert state.read('endpoint.json') is None
    assert not (s.root/'runner'/'known_hosts').exists()


@pytest.mark.parametrize('runtime', [False, 'invalid', [], {'ports': False}, {'ports': {}},
                                    {'ports': [None]}, {'ports': [{'private': '22', 'type': 'tcp'}]},
                                    {'ports': [{'private': 22, 'type': None}]}])
def test_malformed_endpoint_metadata_fails_closed_with_fixed_code(setup, runtime):
    s = setup
    request, events, command = endpoint_fixtures(s)
    s.http.pods[0]['runtime'] = runtime
    with runner.State(s.config.trusted_state_directory) as state:
        with pytest.raises(runner.RunnerError, match='^PROVIDER_RUNTIME_INVALID$'):
            runner.verified_endpoint(s.config, request, s.backend, state, logs=lambda *_: events, command=command)
        assert state.read('endpoint.json') is None


@pytest.mark.parametrize('status,retryable', [(408, True), (429, True), (500, True), (503, True),
                                           (401, False), (403, False), (404, False)])
def test_endpoint_get_retries_only_transient_http_statuses(setup, monkeypatch, status, retryable):
    s = setup
    request, _, _ = endpoint_fixtures(s)
    calls = []
    def response(method, path, body=None):
        calls.append((method, path, body))
        raise runner.ProviderHTTPError(status)
    monkeypatch.setattr(s.backend.transport, 'request', response)
    with runner.State(s.config.trusted_state_directory) as state:
        code = 'PROVIDER_ENDPOINT_UNAVAILABLE' if retryable else 'PROVIDER_ENDPOINT_REFUSED'
        with pytest.raises(runner.RunnerError, match='^'+code+'$'):
            runner.verified_endpoint(s.config, request, s.backend, state)
    assert calls == [('GET', '/v2/pods/'+request['observed_provider_id'], None)]


@pytest.mark.parametrize('network', [True, False])
def test_wrapped_network_failure_and_invalid_json_are_distinguished(setup, monkeypatch, network):
    s = setup
    request, _, _ = endpoint_fixtures(s)
    def response(*_):
        try:
            raise runner.URLError('private-url') if network else ValueError('private-response')
        except Exception:
            raise runner.ProviderResponseError('redacted transport failure') from None
    monkeypatch.setattr(s.backend.transport, 'request', response)
    with runner.State(s.config.trusted_state_directory) as state:
        code = 'PROVIDER_ENDPOINT_UNAVAILABLE' if network else 'PROVIDER_ENDPOINT_RESPONSE_INVALID'
        with pytest.raises(runner.RunnerError, match='^'+code+'$'):
            runner.verified_endpoint(s.config, request, s.backend, state)


@pytest.mark.parametrize('problem', ['wrong_key', 'only_client', 'wrong_price', 'private_ip', 'wrong_region'])
def test_endpoint_or_host_provenance_mismatch_never_starts_tunnel(setup, problem):
    s = setup
    request, events, command = endpoint_fixtures(s)
    if problem == 'wrong_key':
        events[1]['line'] = f'256 {runner.fingerprint(public_key(3))} root@worker (ED25519)'
    elif problem == 'only_client':
        events = events[:1]
    elif problem == 'wrong_price':
        s.http.pods[0]['cost'] = 0.75  # within approval ceiling; still false WorkerConfig provenance
    elif problem == 'private_ip':
        s.http.pods[0]['runtime']['ports'][0]['ip'] = '127.0.0.1'
    else:
        s.http.pods[0]['dataCenterId'] = 'OTHER'
    with runner.State(s.config.trusted_state_directory) as state:
        with pytest.raises(Exception):
            runner.verified_endpoint(s.config, request, s.backend, state, logs=lambda *_: events, command=command)
    assert not (s.root/'runner'/'known_hosts').exists()


def test_state_is_exclusive_private_and_does_not_rewrite_partial_intent(tmp_path):
    directory = tmp_path/'state'
    directory.mkdir(mode=0o700)
    with runner.State(directory) as first:
        with pytest.raises(runner.RunnerError, match='RUNNER_ALREADY_ACTIVE'):
            runner.State(directory)
        first.publish('one.json', {'bound': 1})
        with pytest.raises(runner.RunnerError, match='STATE_BINDING_CONFLICT'):
            first.publish('one.json', {'bound': 2})
    (directory/'partial.json').write_text('{')
    (directory/'partial.json').chmod(0o600)
    with runner.State(directory) as state:
        with pytest.raises(ValueError):
            state.publish('partial.json', {'new': True})
    assert (directory/'partial.json').read_text() == '{'


def test_input_identity_hash_and_symlink_checks(setup):
    s = setup
    config = s.root/'config.json'
    config.write_text(s.config.model_dump_json())
    config.chmod(0o644)
    assert runner.load_inputs(config, mode='run', owner=os.geteuid())[1] == s.plan
    with pytest.raises(runner.RunnerError, match='WRONG_PROCESS_IDENTITY'):
        runner.load_inputs(config, mode='submit', owner=os.geteuid())
    Path(s.config.plan_path).write_text(s.plan.model_dump_json()+' ')
    with pytest.raises(runner.RunnerError, match='PLAN_HASH_MISMATCH'):
        runner.load_inputs(config, mode='run', owner=os.geteuid())
    link = s.root/'link'
    link.symlink_to(config)
    with pytest.raises(OSError):
        runner.read_file(link, owner=os.geteuid())


class Tunnel:
    local_port = 45678
    def __init__(self, **kwargs):
        self.open = False
    def __enter__(self):
        self.open = True
        return self
    def __exit__(self, *_):
        self.open = False
    def ensure_alive(self):
        assert self.open


class Endpoint:
    def __init__(self, s, *, bad_source=False):
        self.s, self.request, self.posts, self.bad_source = s, None, 0, bad_source
    def submit(self, request):
        self.request = request
        self.posts += 1
        return ExecutionReceipt(job_id=request.job_id, attempt_id=request.attempt_id, state='RUNNING', started_at=self.s.clock())
    def status(self, attempt_id):
        s, request = self.s, self.request
        assert request.attempt_id == attempt_id
        summary = canonical_json({'suite': 'backend_parity_v1', 'passed': True, 'scientific_evidence': False}).encode()
        data = deepcopy(s.data)
        data['run'].update(run_id=request.job_id, parent_run_id=None, started_at=s.clock().isoformat(),
            experiment_stage='calibration', hypothesis_id=None, preregistration_hash=None,
            approval_id=request.approval_id, replicator_blinded=False)
        data['model'] = request.spec.model.model_dump(mode='json')
        data['inputs'] = request.spec.inputs.model_dump(mode='json')
        data['experiment'].update(tool='backend_parity', intervention_hash=runner.digest(request.spec.operation.model_dump(mode='json')))
        data['results'].update(heldout=False, replication_status='not_applicable')
        data['software'].update(container_image_digest=s.config.deployment.image_digest,
            probe_mcp_git_commit=('d'*40 if self.bad_source else s.config.source_commit))
        data['hardware'].update(region=s.config.deployment.region, live_price_usd_per_hour=0.74)
        data['cost'] = {'gpu_seconds': 0, 'estimated_compute_usd': 0.0, 'bytes_persisted': len(summary)}
        data['artifacts'] = [{'path': 'summary.json', 'sha256': hashlib.sha256(summary).hexdigest(), 'retention_class': 'validated'}]
        self.summary = summary
        return ExecutionReceipt(job_id=request.job_id, attempt_id=attempt_id, state='SUCCEEDED',
            started_at=s.clock(), finished_at=s.clock(), process_stopped=True, manifest=RunManifest.model_validate(data))
    def download(self, receipt, destination, *, max_bytes):
        destination.mkdir(parents=True)
        (destination/'summary.json').write_bytes(self.summary)
        return destination


@pytest.mark.parametrize('bad_source', [False, True])
def test_real_ledger_dispatcher_collector_flow_and_scoped_deletion(setup, bad_source):
    s = setup
    request = approve_fixture(s)
    client = Endpoint(s, bad_source=bad_source)
    cloud = SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker))
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend, cloud, state,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: None,
            endpoint=lambda *_: {}, tunnel_factory=Tunnel, client_factory=lambda *_a, **_k: client,
            readiness=lambda *_a, **_k: None, configure=lambda *_a, **_k: {'configured': True})
        assert result['status'] == ('failed' if bad_source else 'passed'), result
        assert result['teardown']['confirmed'] is True
        if bad_source:
            assert result['reason'] == 'MANIFEST_PROVENANCE_MISMATCH'
        assert runner.run(s.config, s.plan, s.ledger, s.backend, cloud, state) == result
    assert client.posts == 1
    assert s.ledger.get_job(request['job_ids'][0]).state == 'COMPLETED'
    assert s.backend.status(request['worker_id']).state == WorkerState.ABSENT
    assert len(s.http.purchases) == 1  # only the fixture's already approved creation
    assert (s.root/'runner'/'observations.json').is_file()


@pytest.mark.parametrize('first_observation', ['runtime_null', 'http_503'])
@pytest.mark.parametrize('startup_delay', [1, 200])
def test_startup_recovers_original_endpoint_without_new_create_or_extended_approval(setup, first_observation, startup_delay):
    s = setup
    request, events, command = endpoint_fixtures(s)
    initial_deadline = request['deadline']
    initial_time = s.clock().timestamp()
    valid_runtime = deepcopy(s.http.pods[0]['runtime'])
    transport = s.backend.transport.request
    gets = []
    def response(method, path, body=None):
        if method == 'GET' and path == '/v2/pods/'+request['observed_provider_id']:
            gets.append(1)
            if len(gets) == 1:
                if first_observation == 'http_503':
                    raise runner.ProviderHTTPError(503)
                s.http.pods[0]['runtime'] = None
            elif len(gets) == 2:
                s.http.pods[0]['runtime'] = valid_runtime
        return transport(method, path, body)
    s.backend.transport.request = response
    client = Endpoint(s)
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        s.clock.advance(startup_delay if seconds == 1 else seconds)
    def endpoint(*args):
        return runner.verified_endpoint(*args, logs=lambda *_: events, command=command)
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)), state,
            clock=lambda: s.clock().timestamp(), sleep=sleep, endpoint=endpoint, tunnel_factory=Tunnel,
            client_factory=lambda *_a, **_k: client, readiness=lambda *_a, **_k: None,
            configure=lambda *_a, **_k: {'configured': True})
        assert result['status'] == 'passed', result
        assert state.read('bound-request.json')['deadline'] == initial_deadline
        assert state.read('startup-window.json')['dispatch_cutoff'] == initial_deadline - 240 - 120
        assert result['startup']['endpoint_ready_at'] == initial_time + startup_delay
    assert sleeps == [1, 0.25] and len(gets) >= 2  # One startup retry, then one normal dispatcher tick.
    assert client.posts == 1 and len(s.http.purchases) == 1
    assert client.request.approval_id == request['approval_id']
    assert client.request.deadline.timestamp() <= initial_deadline
    assert result['teardown']['confirmed'] is True


def test_runtime_missing_forever_reaches_original_startup_deadline_then_deletes(setup):
    s = setup
    request, _, _ = endpoint_fixtures(s)
    s.http.pods[0].pop('runtime')
    started = s.clock().timestamp()
    tries = []
    def endpoint(*args):
        tries.append(s.clock().timestamp())
        return runner.verified_endpoint(*args)
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)), state,
            clock=lambda: s.clock().timestamp(), sleep=lambda seconds: s.clock.advance(seconds), endpoint=endpoint)
    assert result['reason'] == 'WORKER_STARTUP_DEADLINE' and result['teardown']['confirmed'] is True
    window = s.config.max_runtime_seconds - s.plan.cases[0].spec.limits.max_runtime_seconds - 120
    assert len(tries) == window and s.clock().timestamp() == started + window
    assert result['startup']['last_endpoint_reason'] == 'DIRECT_SSH_ENDPOINT_UNAVAILABLE'
    assert s.ledger.get_job(request['job_ids'][0]).attempt_id is None
    assert len(s.http.purchases) == 1 and s.http.pods == []


def test_unknown_startup_failure_retains_only_safe_type_and_local_location(setup):
    s = setup
    approve_fixture(s)
    def unknown(*args):
        raise AttributeError('token=RAW_PRIVATE_SECRET')
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)), state,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: None, endpoint=unknown)
    assert result['reason'] == 'ACCEPTANCE_RUNTIME_UNAVAILABLE'
    assert result['diagnostic']['exception_type'] == 'AttributeError'
    assert result['diagnostic']['location'].startswith('gpu_acceptance_runner.py:')
    assert result['diagnostic']['location'].split(':')[1].isdigit()
    assert 'RAW_PRIVATE_SECRET' not in canonical_json(result)
    assert result['teardown']['confirmed'] is True


def test_failed_endpoint_binding_still_deletes_only_bound_pod(setup):
    s = setup
    request = approve_fixture(s)
    def unavailable(*_):
        raise runner.RunnerError('SSH_HOST_KEY_MISMATCH')
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda _: (_ for _ in ()).throw(ConnectionError())), state,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: None, endpoint=unavailable)
    assert result['status'] == 'failed' and result['reason'] == 'SSH_HOST_KEY_MISMATCH'
    assert result['teardown']['confirmed'] is True
    assert s.backend.status(request['worker_id']).state == WorkerState.ABSENT


def test_units_preserve_identity_boundary_and_have_no_approval_command():
    directory = Path(__file__).parents[1]/'deploy'
    submission = (directory/'probe-calibration-submit.service').read_text()
    trusted = (directory/'probe-calibration-run.service').read_text()
    assert 'User=probe-research\nGroup=probe-research' in submission
    assert 'SupplementaryGroups=' not in submission
    assert 'User=probe-trusted\nGroup=probe-trusted' in trusted
    assert '-I -m probe_core.gpu_acceptance_runner submit' in submission
    assert '-I -m probe_core.gpu_acceptance_runner run' in trusted
    assert '--method approve' not in submission+trusted
    assert '/etc/probe-calibration/acceptance.json' in submission and '/etc/probe-calibration/acceptance.json' in trusted


def test_provider_log_reader_uses_authenticated_fixed_origin_and_bounds(setup, monkeypatch):
    s = setup
    secret = s.root/'provider-key'
    secret.write_text('never-public-provider-secret')
    secret.chmod(0o600)
    line = {'source': 'container', 'line': f'256 {runner.fingerprint(public_key(2))} root@worker (ED25519)', 'ts': '2026-09-20T01:00:00Z'}
    # A large unrelated structured diagnostic is allowed within the total bound.
    data = b'data: '+json.dumps({'line': 'x'*70000}).encode()+b'\n\ndata: '+json.dumps(line).encode()+b'\n\n'
    class Stream(io.BytesIO):
        headers = SimpleNamespace(get_content_type=lambda: 'text/event-stream')
    observed = []
    def open_response(request, timeout):
        observed.append(request)
        assert request.get_header('Authorization') == 'Bearer never-public-provider-secret'
        assert request.full_url == 'https://api.runpod.io/v2/pods/owned-pod/logs?source=container&tail=1000'
        assert request.get_method() == 'GET'
        return Stream(data)
    monkeypatch.setattr(runner, 'build_opener', lambda *handlers: SimpleNamespace(open=open_response))
    result = runner.read_provider_logs(SimpleNamespace(api_key_file=str(secret)), 'owned-pod')
    assert result == [line] and len(observed) == 1
    data = b'data: '+b'x'*(runner.BOUND+1)
    with pytest.raises(runner.RunnerError, match='PROVIDER_LOG_BOUND_EXCEEDED'):
        runner.read_provider_logs(SimpleNamespace(api_key_file=str(secret)), 'owned-pod')


def test_unconfirmed_deletion_remains_failure(setup):
    s = setup
    request = approve_fixture(s)
    def no_stop(_):
        return None
    backend = SimpleNamespace(status=lambda _: s.backend.status(request['worker_id']), stop=no_stop)
    result = runner.stop_and_observe(SimpleNamespace(stop_gpu=no_stop), backend, request, sleep=lambda _: None)
    assert result['confirmed'] is False
    assert s.backend.status(request['worker_id']).state == WorkerState.RUNNING


def test_public_cli_never_prints_unexpected_exception_or_secret(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['gpu_acceptance_runner', 'run', '--config', '/not-read'])
    def refusal(*_args, **_kwargs):
        raise RuntimeError('token=RAW_PRIVATE_SECRET')
    monkeypatch.setattr(runner, 'load_inputs', refusal)
    monkeypatch.setattr(runner.signal, 'signal', lambda *_: None)
    with pytest.raises(SystemExit):
        runner.main()
    output = capsys.readouterr().out
    assert 'RAW_PRIVATE_SECRET' not in output and 'ACCEPTANCE_RUNNER_REFUSED' in output


def test_changed_plan_or_config_never_reuses_an_existing_submission(setup):
    s = setup
    queue(s)
    changed = s.config.model_copy(update={'expected_worker_price_usd_per_hour': 0.75})
    with runner.State(s.config.submission_state_directory) as state:
        with pytest.raises(runner.RunnerError, match='STATE_BINDING_CONFLICT'):
            runner.submit(changed, s.plan, s.rpc, state)
    assert len(s.controller.status()) == 1


def test_state_record_fsync_precedes_provision_rpc(setup, monkeypatch):
    s = setup
    syncs = []
    original = runner.os.fsync
    monkeypatch.setattr(runner.os, 'fsync', lambda fd: (syncs.append(fd), original(fd))[1])
    def call(method, params=None):
        if method == 'request_gpu_provision':
            assert (s.root/'submit/provision-intent.json').is_file()
            assert len(syncs) >= 4  # binding file+directory, then intent file+directory
        return s.rpc.call(method, params or {})
    with runner.State(s.config.submission_state_directory) as state:
        runner.submit(s.config, s.plan, SimpleNamespace(call=call), state)


def test_readiness_authenticates_real_worker_handler_without_submitting():
    class NoExecution:
        def submit(self, *_):
            raise AssertionError('readiness must not submit')
        def status(self, *_):
            raise AssertionError('readiness must not inspect or allocate an attempt')
    secret = 'fixture-bearer-'+'x'*40
    with WorkerHTTPServer(NoExecution(), secret) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:'+str(server.server_port)
            runner.wait_for_worker(WorkerClient(url, secret), deadline=time.time()+2)
            with pytest.raises(runner.RunnerError, match='WORKER_AUTHENTICATION_FAILED'):
                runner.wait_for_worker(WorkerClient(url, 'wrong-'+'y'*40), deadline=time.time()+2)
        finally:
            server.shutdown()
            thread.join(2)


def test_unavailable_startup_retries_without_dispatch_then_stops_at_deadline(setup):
    s = setup
    request = approve_fixture(s)
    tries = []
    def missing(*_):
        tries.append(1)
        raise runner.RunnerError('HOST_FINGERPRINT_UNAVAILABLE')
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)), state,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: s.clock.advance(30), endpoint=missing)
    window = s.config.max_runtime_seconds - s.plan.cases[0].spec.limits.max_runtime_seconds - 120
    assert len(tries) == window // 30
    assert result['reason'] == 'WORKER_STARTUP_DEADLINE'
    assert result['teardown']['confirmed'] is True
    assert s.ledger.get_job(request['job_ids'][0]).attempt_id is None


def test_approval_expiring_during_connection_never_dispatches(setup):
    s = setup
    request = approve_fixture(s)
    client = Endpoint(s)
    def readiness(*_a, **_k):
        s.clock.advance(s.config.max_runtime_seconds + 1)
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)), state,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: None, endpoint=lambda *_: {},
            tunnel_factory=Tunnel, client_factory=lambda *_a, **_k: client, readiness=readiness,
            configure=lambda *_a, **_k: {'configured': True})
    assert client.posts == 0
    assert result['reason'] == 'APPROVED_DEADLINE_REACHED'
    assert result['teardown']['confirmed'] is True


def test_insufficient_remaining_job_and_cleanup_window_refuses_startup(setup):
    s = setup
    request = approve_fixture(s)
    remaining = s.plan.cases[0].spec.limits.max_runtime_seconds + 120
    s.clock.advance(s.config.max_runtime_seconds - remaining)
    called = []
    def endpoint(*_):
        called.append(True)
        raise AssertionError('no startup work is allowed without a full execution window')
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)), state,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: None, endpoint=endpoint)
        assert state.read('startup-window.json')['approval_deadline'] == request['deadline']
    assert result['reason'] == 'INSUFFICIENT_EXECUTION_WINDOW'
    assert result['startup']['endpoint_attempts'] == 0 and called == []
    assert result['teardown']['confirmed'] is True
    assert s.ledger.get_job(request['job_ids'][0]).attempt_id is None
    assert len(s.http.purchases) == 1


@pytest.mark.parametrize('late_phase', ['endpoint', 'configuration', 'readiness', 'dispatch'])
def test_every_startup_stage_shares_cutoff_and_late_ready_worker_never_dispatches(setup, late_phase):
    s = setup
    request = approve_fixture(s)
    cutoff = request['deadline'] - s.plan.cases[0].spec.limits.max_runtime_seconds - 120
    client = Endpoint(s)
    phases = []
    def reach_cutoff():
        s.clock.advance(cutoff - s.clock().timestamp())
    def endpoint(*_):
        phases.append('endpoint')
        if late_phase == 'endpoint':
            reach_cutoff()
        return {}
    def configure(*_args, deadline, **_kwargs):
        phases.append('configuration')
        assert deadline == cutoff
        if late_phase == 'configuration':
            reach_cutoff()
        return {'configured': True}
    def readiness(*_args, deadline, **_kwargs):
        phases.append('readiness')
        assert deadline == cutoff
        if late_phase == 'readiness':
            reach_cutoff()
    def progress(stage):
        if stage == 'approved_dispatch' and late_phase == 'dispatch':
            reach_cutoff()
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(s.config, s.plan, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=lambda worker: s.controller.stop_gpu(worker)), state,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: None, endpoint=endpoint,
            tunnel_factory=Tunnel, client_factory=lambda *_a, **_k: client,
            readiness=readiness, configure=configure, progress=progress)
    assert result['reason'] == 'WORKER_STARTUP_DEADLINE'
    assert phases == ['endpoint', 'configuration', 'readiness'][:min(3, ['endpoint', 'configuration', 'readiness', 'dispatch'].index(late_phase)+1)]
    assert s.clock().timestamp() == cutoff < request['deadline']
    assert client.posts == 0 and s.ledger.get_job(request['job_ids'][0]).attempt_id is None
    assert result['teardown']['confirmed'] is True and len(s.http.purchases) == 1


def configure_settings(s, state):
    state.publish('endpoint.json', {'provider_id': 'fixture', 'host': '213.192.2.71', 'ssh_port': 40125})
    return {'host': '213.192.2.71', 'ssh_port': 40125, 'identity_file': Path(s.config.ssh_identity_file),
            'known_hosts_file': state.directory/'known_hosts'}


def configure_receipt(bundle):
    return {'schema_version': 1, 'configured': True, 'worker_config_sha256': runner.digest(bundle['worker_config']),
            'bundle_sha256': runner.digest(bundle)}


def test_private_configuration_upload_is_fixed_pinned_and_not_replayed(setup):
    s = setup
    calls, bundles = [], []
    with runner.State(s.config.trusted_state_directory) as state:
        settings = configure_settings(s, state)
        def command(argv, *, timeout):
            calls.append(argv)
            assert 0 < timeout <= 15
            assert state.read('configure-intent.json') is not None
            if argv[0] == '/usr/bin/scp':
                path = Path(argv[-2])
                assert path.stat().st_mode & 0o777 == 0o600
                bundle = json.loads(path.read_text())
                assert set(bundle) == {'schema_version', 'worker_config', 'bearer_token'}
                assert bundle['bearer_token'] == Path(s.config.bearer_secret_file).read_text()
                bundles.append(bundle)
                return ''
            return canonical_json(configure_receipt(bundles[0]))
        result = runner.configure_worker(s.config, s.plan, settings, state, deadline=time.time()+60,
                                         command=command, owner=os.geteuid())
        assert result == configure_receipt(bundles[0])
        assert runner.configure_worker(s.config, s.plan, settings, state, deadline=time.time()+60,
                                        command=command, owner=os.geteuid()) == result
        assert len(calls) == 2
        assert calls[0][-1] == 'root@213.192.2.71:/run/probe-worker-bootstrap.json'
        assert calls[1][-7:] == ['root@213.192.2.71', '/opt/probe-core/venv/bin/python', '-I', '-m',
                                'probe_core.gpu_launch', '--configure', '/run/probe-worker-bootstrap.json']
        assert 'StrictHostKeyChecking=yes' in calls[0] and 'IdentitiesOnly=yes' in calls[0]
        assert '-p' in calls[0]  # Preserve the source's 0600 mode in SCP/SFTP.
        assert not (state.directory/'configuration-bundle.json').exists()
        assert bundles[0]['bearer_token'] not in canonical_json(result)
        for path in state.directory.glob('*.json'):
            assert bundles[0]['bearer_token'] not in path.read_text()


@pytest.mark.parametrize('failure_after_upload', [False, True])
def test_configuration_transport_uncertainty_never_replays_and_removes_private_file(setup, failure_after_upload):
    s = setup
    calls = []
    with runner.State(s.config.trusted_state_directory) as state:
        settings = configure_settings(s, state)
        def command(argv, *, timeout):
            calls.append(argv)
            if failure_after_upload and len(calls) == 1:
                return ''
            raise runner.RunnerError('SSH_VERIFICATION_COMMAND_FAILED')
        first = runner.configure_worker(s.config, s.plan, settings, state, deadline=time.time()+60,
                                        command=command, owner=os.geteuid())
        assert first == {'configured': None, 'reason': 'CONFIGURATION_RESPONSE_UNCERTAIN'}
        assert not (state.directory/'configuration-bundle.json').exists()
        count = len(calls)
        assert runner.configure_worker(s.config, s.plan, settings, state, deadline=time.time()+60,
                                        command=command, owner=os.geteuid()) == first
        assert len(calls) == count


def test_configuration_receipt_mismatch_is_refused_and_source_secret_removed(setup):
    s = setup
    with runner.State(s.config.trusted_state_directory) as state:
        settings = configure_settings(s, state)
        def command(argv, *, timeout):
            return '' if argv[0] == '/usr/bin/scp' else '{"configured":true}'
        with pytest.raises(runner.RunnerError, match='CONFIGURATION_RECEIPT_MISMATCH'):
            runner.configure_worker(s.config, s.plan, settings, state, deadline=time.time()+60,
                                    command=command, owner=os.geteuid())
        assert not (state.directory/'configuration-bundle.json').exists()
        assert state.read('configured.json') is None


def test_configuration_deadline_and_worker_hash_checked_before_remote_execution(setup):
    s = setup
    calls, now = [], [100.0]
    with runner.State(s.config.trusted_state_directory) as state:
        settings = configure_settings(s, state)
        def command(argv, *, timeout):
            calls.append(argv)
            now[0] = 160
            return ''
        with pytest.raises(runner.RunnerError, match='WORKER_STARTUP_DEADLINE'):
            runner.configure_worker(s.config, s.plan, settings, state, deadline=100,
                                    clock=lambda: now[0], command=command, owner=os.geteuid())
        assert calls == [] and state.read('configure-intent.json') is None
        with pytest.raises(runner.RunnerError, match='WORKER_STARTUP_DEADLINE'):
            runner.configure_worker(s.config, s.plan, settings, state, deadline=150,
                                    clock=lambda: now[0], command=command, owner=os.geteuid())
        assert len(calls) == 1 and calls[0][0] == '/usr/bin/scp'
        assert not (state.directory/'configuration-bundle.json').exists()
    Path(s.config.worker_config_path).write_text('{}')
    with pytest.raises(runner.RunnerError, match='WORKER_CONFIG_HASH_MISMATCH'):
        runner.load_worker_config(s.config, s.plan, owner=os.geteuid())


@pytest.mark.parametrize('field,value', [('region', 'another-region'),
    ('live_price_usd_per_hour', .75), ('output_directory', '/tmp/output'),
    ('model_directory', '/opt/probe-assets/models/../private')])
def test_worker_configuration_provenance_refuses_even_freshly_pinned_mismatch(setup, field, value):
    s = setup
    path = Path(s.config.worker_config_path)
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(canonical_json(data))
    config = s.config.model_copy(update={'worker_config_sha256': 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()})
    with pytest.raises(runner.RunnerError):
        runner.load_worker_config(config, s.plan, owner=os.geteuid())


def test_command_output_and_runtime_are_bounded_without_exposing_stderr():
    with pytest.raises(runner.RunnerError, match='^SSH_VERIFICATION_COMMAND_FAILED$'):
        runner.command_bytes([sys.executable, '-I', '-c', 'import sys;sys.stderr.write("PRIVATE");print("x"*70000)'])
    started = time.monotonic()
    with pytest.raises(runner.RunnerError, match='^SSH_VERIFICATION_COMMAND_FAILED$'):
        runner.command_bytes([sys.executable, '-I', '-c', 'import time;time.sleep(20)'], timeout=.1)
    assert time.monotonic()-started < 3
