"""Real ledger/dispatcher integration; simulated provider and worker only."""
from types import SimpleNamespace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

from probe_core import supervised_runner as runner
from probe_core.dispatcher import Dispatcher
from probe_core.gpu_acceptance import AcceptancePlan, fixed_plan
from probe_core.worker_contracts import ExecutionReceipt
from test_gpu_acceptance_runner import setup, approve_fixture, Endpoint
from test_runpod_provider import runpod
from test_schemas import manifest_data


def configuration(s):
    return runner.SupervisedRunnerConfig(**s.config.model_dump(mode='json'),
        helper_path='/pinned/public-job.py', helper_sha256='sha256:'+'e'*64)


class SSHFixture(Endpoint):
    def stage_helper(self, path, sha):
        return {'sha256': sha, 'path': '/opt/probe/public-job.py'}


def execute(s, client):
    with runner.State(s.config.trusted_state_directory) as state:
        return runner.run(configuration(s), s.plan, {}, s.ledger, s.backend,
            SimpleNamespace(stop_gpu=s.controller.stop_gpu), state,
            endpoint=lambda *_: {}, client_factory=lambda *_a, **_k: client,
            clock=lambda: s.clock().timestamp(), sleep=lambda _: None)


@pytest.mark.parametrize('bad_source', [False, True])
def test_existing_approved_ledger_dispatch_and_sealed_collection(setup, bad_source):
    s = setup
    request = approve_fixture(s)
    client = SSHFixture(s, bad_source=bad_source)
    result = execute(s, client)
    assert result['status'] == ('failed' if bad_source else 'passed'), result
    assert result['teardown']['confirmed']
    assert client.posts == 1 and len(s.http.purchases) == 1
    assert client.request.approval_id == request['approval_id']
    assert client.request.deadline.timestamp() <= request['deadline']
    assert s.ledger.get_job(request['job_ids'][0]).attempt_count == 1
    assert s.backend.status(request['worker_id']).state.value == 'ABSENT'


def test_coordinator_reattaches_existing_attempt_without_resubmission(setup):
    s = setup
    request = approve_fixture(s)
    client = SSHFixture(s)
    original = Dispatcher(s.ledger, client, worker_id=request['worker_id'],
        transfer_directory=s.root/'initial-transfers').dispatch_next(approval_id=request['approval_id'])
    execution = client.request
    result = execute(s, client)
    assert result['status'] == 'passed', result
    assert result['attempt_id'] == original.attempt_id
    assert client.posts == 1 and client.request == execution
    assert result['attempt_count'] == 1


def test_changed_current_authority_never_dispatches_and_still_deletes(setup):
    s = setup
    request = approve_fixture(s)
    s.ledger.end_approval(request['approval_id'])
    client = SSHFixture(s)
    result = execute(s, client)
    assert result['status'] == 'failed' and result['reason'] == 'APPROVAL_BINDING_INVALID'
    assert client.posts == 0 and result['teardown']['confirmed']


class CancellableSSH(SSHFixture):
    cancelled = False
    proof = True
    child = {'pid': 111, 'identity': '42', 'boot_id': 'fixture'}

    def status(self, attempt_id):
        r = self.request
        assert attempt_id == r.attempt_id
        if self.cancelled:
            return ExecutionReceipt(job_id=r.job_id, attempt_id=r.attempt_id, state='CANCELLED',
                started_at=self.s.clock(), finished_at=self.s.clock(), failure_kind='cancelled',
                error_code='OperatorCancelled', process_stopped=True)
        return ExecutionReceipt(job_id=r.job_id, attempt_id=r.attempt_id, state='RUNNING',
                                started_at=self.s.clock())

    def inspect(self, attempt_id):
        request = runner.find_request(self.s.ledger, configuration(self.s), self.s.plan)
        body = {'receipt': self.status(attempt_id).model_dump(mode='json'), 'execution_started': True,
                'request_sha256': runner.digest(self.request.model_dump(mode='json')),
                'config_sha256': runner.digest({}), 'child_identity': self.child,
                'original_deadline': datetime.fromtimestamp(request['deadline'], timezone.utc).isoformat(),
                'cancellation': None}
        if self.cancelled and self.proof:
            body['cancellation'] = {key: body[key] for key in
                ('request_sha256', 'config_sha256', 'child_identity', 'original_deadline')}
            body['cancellation'].update(attempt_id=attempt_id, process_stopped=True,
                descendants_stopped=True, result_present=False, signal='SIGKILL',
                signal_scope='fenced_monitor_descendants', signalled=[self.child])
        return body

    def cancel(self, attempt_id):
        self.cancelled = True
        return self.status(attempt_id)


def cancel_plan(s):
    inputs = s.plan.cases[0].spec.inputs
    fixed = fixed_plan(s.plan.model, 'cancel-proof', inputs.dataset_revision,
                       inputs.prompt_set_hash, inputs.prompt_ids)
    s.plan = AcceptancePlan(label=fixed.label, model=fixed.model, cases=(fixed.cases[2],))


def test_cancellation_is_bound_to_running_approved_attempt(setup):
    s = setup
    cancel_plan(s)
    request = approve_fixture(s)
    client = CancellableSSH(s)
    result = execute(s, client)
    assert result['status'] == 'passed' and result['cancellation_proven'], result
    assert result['observed_failure_kind'] == 'cancelled' and client.posts == 1
    assert client.request.approval_id == request['approval_id']
    assert result['teardown']['confirmed']


@pytest.mark.parametrize('remote_already_cancelled', [False, True])
def test_resumes_saved_cancellation_intent_without_resubmission(setup, remote_already_cancelled):
    s = setup
    cancel_plan(s)
    request = approve_fixture(s)
    client = CancellableSSH(s)
    dispatcher = Dispatcher(s.ledger, client, worker_id=request['worker_id'],
        transfer_directory=s.root/'initial-transfers')
    job = dispatcher.dispatch_next(approval_id=request['approval_id'])
    with runner.State(s.config.trusted_state_directory) as state:
        state.publish('cancellation-before.json', client.inspect(job.attempt_id))
    client.cancelled = remote_already_cancelled
    result = execute(s, client)
    assert result['status'] == 'passed' and result['cancellation_proven'], result
    assert client.posts == 1 and result['attempt_count'] == 1
    assert result['teardown']['confirmed']


def test_terminal_cancelled_receipt_without_signal_evidence_is_insufficient(setup):
    s = setup
    cancel_plan(s)
    approve_fixture(s)
    client = CancellableSSH(s)
    client.proof = False
    result = execute(s, client)
    assert result['status'] == 'failed' and result['reason'] == 'CANCELLATION_STOP_UNCONFIRMED'
    assert result['teardown']['confirmed']


def test_suite_rejects_duplicate_or_external_paths(monkeypatch):
    for paths in [['../foreign-acceptance.json'], ['a-acceptance.json'] * 2, []]:
        monkeypatch.setattr(runner, 'read_file', lambda *_a, **_k:
            json.dumps({'schema_version': 1, 'configurations': paths}).encode())
        with pytest.raises(runner.RunnerError):
            runner.configuration_paths(None, Path('/etc/probe-supervised/suite.json'))


def test_whole_suite_is_validated_before_any_proposal(monkeypatch):
    paths = [Path('/one'), Path('/two')]
    monkeypatch.setattr(sys, 'argv', ['runner', 'submit', '--suite', '/suite.json'])
    monkeypatch.setattr(runner, 'configuration_paths', lambda *_: paths)
    checked = []
    def load(path, **_):
        checked.append(path)
        if path == paths[1]:
            raise runner.RunnerError('INVALID_SECOND_CASE')
        return (None, None, None)
    monkeypatch.setattr(runner, 'load_inputs', load)
    monkeypatch.setattr(runner, 'submit', lambda *_: pytest.fail('invalid suite must not queue anything'))
    with pytest.raises(runner.RunnerError, match='INVALID_SECOND_CASE'):
        runner.main()
    assert checked == paths
