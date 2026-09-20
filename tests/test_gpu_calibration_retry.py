"""Pinned retry preparation with real ledger records and mocked host services."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from probe_core import gpu_acceptance_runner as runner
from probe_core.schemas import JobSpec
from probe_core.runpod_provider import ProviderHTTPError
from test_gpu_acceptance_runner import setup, approve_fixture, runpod, manifest_data
from test_gpu_calibration_activation import typed_inputs

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('retry_calibration', PROJECT/'deploy/retry-gpu-calibration.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def put(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)


@pytest.fixture
def replacement(typed_inputs):
    old = dict(typed_inputs.bodies)
    for name in r.CALIBRATION: old[name] = (PROJECT/'deploy'/name).read_bytes()
    new, retry_id = dict(old), 'a'*32
    plan = json.loads(old['plan.json'])
    plan['label'] += '-retry'
    plan['cases'][0]['spec']['idempotency_key'] += '-retry'
    new['plan.json'] = r.encoded(plan)
    config = json.loads(old['acceptance.json'])
    config.update(plan_path=str(r.PUBLIC/('retry-'+retry_id)/'plan.json'), plan_sha256='sha256:'+r.digest(new['plan.json']),
                  submission_state_directory=str(r.SUBMIT/('retry-'+retry_id)), trusted_state_directory=str(r.STATE/('retry-'+retry_id)))
    new['acceptance.json'] = r.encoded(config)
    for name in r.CALIBRATION:
        new[name] = old[name].replace(b'--config /etc/probe-calibration/acceptance.json',
                                     ('--config /etc/probe-calibration/retry-'+retry_id+'/acceptance.json').encode())
    return SimpleNamespace(old=old, new=new, retry_id=retry_id)


def test_only_job_identity_config_paths_and_unit_config_argument_may_change(replacement):
    s = replacement
    public, config = r.validate_replacement(s.old, s.new, s.retry_id)
    assert public == r.PUBLIC/('retry-'+s.retry_id)
    assert config['worker_config_path'] == '/etc/probe-calibration/worker.json'


@pytest.mark.parametrize('change', ['prompt', 'model', 'price', 'runtime', 'unit_user', 'unit_extra', 'same_key'])
def test_scope_changes_rejected_before_any_action(replacement, change):
    s = replacement
    if change in {'prompt', 'model', 'same_key'}:
        body = json.loads(s.new['plan.json'])
        if change == 'prompt': body['cases'][0]['spec']['inputs']['prompt_ids'][0] = 'different'
        elif change == 'model': body['model']['revision_sha'] = 'f'*40
        else: body['cases'][0]['spec']['idempotency_key'] = json.loads(s.old['plan.json'])['cases'][0]['spec']['idempotency_key']
        s.new['plan.json'] = r.encoded(body)
    elif change in {'price', 'runtime'}:
        body = json.loads(s.new['acceptance.json'])
        body['expected_worker_price_usd_per_hour' if change == 'price' else 'max_runtime_seconds'] = .79 if change == 'price' else 800
        s.new['acceptance.json'] = r.encoded(body)
    else:
        name = r.CALIBRATION[1]
        s.new[name] = s.new[name].replace(b'User=probe-trusted', b'User=root') if change == 'unit_user' else s.new[name]+b'\nOther=setting\n'
    with pytest.raises(r.RetryError): r.validate_replacement(s.old, s.new, s.retry_id)


@pytest.fixture
def real_failed(setup, monkeypatch):
    s = setup
    request = approve_fixture(s)
    failed = {'request_id': request['request_id'], 'worker_id': request['worker_id'],
              'job_id': request['job_ids'][0], 'provider_id': request['observed_provider_id']}
    s.controller.stop_gpu(failed['worker_id'])
    assert s.ledger.get_job(failed['job_id']).state == 'PENDING'
    result = {'status': 'failed', 'stage': 'verified_worker_startup', 'reason': 'ACCEPTANCE_RUNTIME_UNAVAILABLE',
              **{key: failed[key] for key in ('request_id','worker_id','provider_id')},
              'teardown': {'provider_id': failed['provider_id'], 'state': 'ABSENT', 'confirmed': True},
              'approval_consumed_by_runner': False}
    bound = {key: request[key] for key in ('request_id','worker_id','approval_id','batch_hash','deadline','observed_provider_id')}
    submitted = {**failed, 'approval_id': request['approval_id']}
    # Execute the actual fixed reader against real temporary DBs. Only the
    # production paths and network transport construction are substituted.
    from probe_core import runpod_provider
    monkeypatch.setattr(runpod_provider.RunPodConfig, 'load', lambda _: SimpleNamespace(api_key_file='not-read'))
    monkeypatch.setattr(runpod_provider, 'RunPodHTTP', lambda _: s.http)
    source = r.SNAPSHOT.replace('/var/lib/probe-core/research.sqlite', str(s.root/'research.sqlite')).replace(
        '/var/lib/probe-provider/runpod.sqlite', str(s.backend.path))
    def snapshot():
        namespace = {'FAILED_JSON': json.dumps(failed), '__name__': 'fixed_reader_test'}
        import contextlib, io
        output = io.StringIO()
        with contextlib.redirect_stdout(output): exec(source, namespace)
        return json.loads(output.getvalue())
    return SimpleNamespace(s=s, failed=failed, result=result, bound=bound, submitted=submitted, snapshot=snapshot,
                           plan=s.plan.model_dump(mode='json'))


def test_real_closed_request_keeps_undispatched_job_pending_until_audited_cancel(real_failed):
    f = real_failed
    before = f.snapshot()
    r.validate_failed(before, f.failed, f.plan, f.result, f.bound, f.submitted)
    f.s.rpc.call('cancel_job', {'job_id': f.failed['job_id']})
    after = f.snapshot()
    r.validate_failed(after, f.failed, f.plan, f.result, f.bound, f.submitted, cancelled=True)
    assert after['attempts'] == [] and after['jobs'][0]['attempt_count'] == 0
    assert before['jobs'][0]['spec_json'] == after['jobs'][0]['spec_json']
    with f.s.ledger.read_connection() as conn:
        events = [json.loads(row[0]) for row in conn.execute('SELECT record FROM audit_events')]
    assert any(event['payload'].get('tool') == 'cancel_job' for event in events)


@pytest.mark.parametrize('change', ['attempt', 'not_absent', 'other_pod', 'open_approval', 'wrong_result', 'wrong_binding', 'other_job'])
def test_known_failure_gate_rejects_any_execution_or_uncertain_scope(real_failed, change):
    f = real_failed
    snapshot, result, bound = f.snapshot(), deepcopy(f.result), deepcopy(f.bound)
    if change == 'attempt': snapshot['attempts'] = [{'attempt_id': 'unexpected'}]
    elif change == 'not_absent': snapshot['provider_absent'] = False
    elif change == 'other_pod': snapshot['pods'] = 1
    elif change == 'open_approval': snapshot['approvals'][0]['ended_at'] = None
    elif change == 'wrong_result': result['stage'] = 'approved_dispatch'
    elif change == 'wrong_binding': bound['approval_id'] = 'different'
    else: snapshot['other_unfinished_jobs'] = 1
    with pytest.raises(r.RetryError): r.validate_failed(snapshot, f.failed, f.plan, result, bound, f.submitted)


def test_close_phase_is_audited_once_and_reuses_durable_receipt(real_failed, tmp_path, monkeypatch):
    f = real_failed
    m = {'failed': f.failed}
    a = SimpleNamespace(Activation=lambda: None, write_file=lambda path, raw, **_: put(path, raw))
    operation = r.Recovery(a, None, m, {}, 'a'*64)
    operation.work = tmp_path/'recovery'; operation.work.mkdir()
    operation.old = {'plan.json': r.encoded(f.plan)}
    operation.result, operation.bound, operation.submitted = f.result, f.bound, f.submitted
    operation.failure_hash = 'b'*64
    operation.snapshot = f.snapshot
    calls = []
    def command(argv, **kwargs):
        assert argv[:6] == ['/usr/sbin/runuser','-u','probe-research','-g','probe-research','--']
        assert 'cancel_job' in argv[-1] and f.failed['job_id'] in argv[-1] and 'approve' not in argv[-1]
        calls.append(argv)
        return r.encoded(f.s.rpc.call('cancel_job', {'job_id': f.failed['job_id']}))
    operation.operation = SimpleNamespace(users=(1000,1001,1002), command=command, baseline=None,
                                         idle=lambda: {'idle':True,'history_sha256':'c'*64,'audit_tip':'d'*64})
    monkeypatch.setattr(r, 'read', lambda path, **_: Path(path).read_bytes())
    first = operation.close_failed()
    assert operation.close_failed() == first and len(calls) == 1
    assert (operation.work/'close-intent.json').exists()
    assert f.s.ledger.get_job(f.failed['job_id']).failure_kind == 'cancelled'


def test_main_requires_actual_administrator_and_never_runs_host_commands(capsys):
    if os.geteuid() == 0: pytest.skip('non-root check')
    assert r.main(['close-failed','--manifest','/not-read','--manifest-sha256','a'*64,'--human','unused']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'ADMINISTRATOR_REQUIRED'


@pytest.fixture
def prepare_case(tmp_path, replacement, monkeypatch):
    units = tmp_path/'units'; units.mkdir()
    monkeypatch.setattr(r, 'UNITS', units)
    public, submit, state = (tmp_path/name for name in ('public','submit','state'))
    for name in r.CALIBRATION: put(units/name, replacement.old[name])
    config = json.loads(replacement.new['acceptance.json'])
    config.update(plan_path=str(public/'plan.json'), submission_state_directory=str(submit), trusted_state_directory=str(state))
    files = dict(replacement.new, **{'acceptance.json':r.encoded(config)})
    events, fault = [], [None]
    a = SimpleNamespace(Activation=lambda: None, trusted=lambda _:None,
                        write_file=lambda path, raw, **_:put(path, raw))
    manifest = {'target_upgrade':{'wheel_sha256':'a'*64}, 'failed':{'job_id':'old-job','request_id':'old-request','worker_id':'old-worker'}}
    operation = r.Recovery(a, None, manifest, files, 'b'*64)
    operation.work = tmp_path/'capsule'; operation.work.mkdir()
    operation.public, operation.config, operation.failure_hash = public, config, 'c'*64
    operation.verify_failure = lambda *args, **kwargs:events.append('verified_old_failure')
    operation.snapshot = lambda:{}
    operation.record('close-report.json', {'status':'closed','manifest_sha256':operation.pin,
                                          'failed_evidence_sha256':operation.failure_hash,'history':{'old':'retained'}})
    def ctl(*args):
        events.append(args)
        if args == ('start',r.CALIBRATION[0]):
            if fault[0] == 'submit': raise RuntimeError('synthetic submit failure')
            put(submit/'submitted.json', r.encoded({'job_id':'new-job','request_id':'new-request','worker_id':'new-worker',
                                                    'approval_consumed_by_runner':False}))
        if args == ('start',r.CALIBRATION[1]):
            if fault[0] == 'runner': raise RuntimeError('synthetic runner failure')
            expected = {'config_sha256':runner.digest(runner.RunnerConfig.model_validate(config).model_dump(mode='json')),
                        'plan_sha256':config['plan_sha256']}
            put(state/'binding.json', r.encoded(expected))
    operation.operation = SimpleNamespace(users=(994,993,1000), baseline=None,
        idle=lambda:events.append('idle') or {'idle':True}, ctl=ctl)
    operation.unit_state = lambda _:{'ActiveState':'active','MainPID':'42','ControlPID':'0','DropInPaths':''}
    monkeypatch.setattr(r, 'read', lambda path, **_:Path(path).read_bytes())
    monkeypatch.setattr(r.os, 'chown', lambda *args:None)
    monkeypatch.setattr(r.grp, 'getgrnam', lambda _:SimpleNamespace(gr_gid=os.getegid()))
    original_record = operation.record
    def record(name,value):
        if name == 'prepare-report.json' and fault[0] == 'receipt': raise OSError('synthetic receipt failure')
        return original_record(name,value)
    operation.record = record
    return SimpleNamespace(operation=operation, units=units, public=public, submit=submit,state=state,events=events,fault=fault)


def test_prepare_stages_new_private_state_then_waits_for_bound_runner_without_approval(prepare_case):
    s = prepare_case
    result = s.operation.prepare()
    assert result['status'] == 'prepared' and result['approval_issued'] is False
    assert result['cloud_mutations_performed'] is False
    assert s.events.index('idle') < s.events.index(('start',r.CALIBRATION[0])) < s.events.index(('start',r.CALIBRATION[1]))
    assert not any((s.units/(name+'.d')).exists() for name in r.CALIBRATION)
    assert (s.operation.work/'close-report.json').exists() and (s.operation.work/'prepare-report.json').exists()
    assert s.submit.stat().st_mode & 0o777 == 0o700 and s.state.stat().st_mode & 0o777 == 0o700
    assert not any('approve' in str(event) or 'probe-controller' in str(event) or 'probe-research.service' in str(event) for event in s.events)


@pytest.mark.parametrize('fault',['submit','runner','receipt'])
def test_prepare_failure_guards_only_calibration_and_preserves_close_receipt(prepare_case,fault):
    s = prepare_case
    s.fault[0] = fault
    before = (s.operation.work/'close-report.json').read_bytes()
    with pytest.raises((RuntimeError,OSError)):s.operation.prepare()
    assert (s.operation.work/'close-report.json').read_bytes() == before
    assert not (s.public/'prepared').exists()
    assert all((s.units/(name+'.d')/'50-probe-calibration-retry.conf').exists() for name in r.CALIBRATION)
    assert s.events[-1] == ('stop',*r.CALIBRATION)
    assert not any('probe-controller' in str(event) or 'probe-research.service' in str(event) for event in s.events)
    assert json.loads((s.operation.work/'prepare-failure.json').read_text())['cleanup_confirmed'] is True


def test_package_pins_are_checked_before_helpers_can_execute(tmp_path, monkeypatch):
    files = {name:b'# pinned file '+name.encode() for name in r.FILES}
    for name, raw in files.items():put(tmp_path/name,raw)
    manifest = {'schema_version':1,'original_manifest_sha256':'a'*64,
                'previous_upgrade':{'wheel_sha256':'b'*64,'release_manifest_sha256':'c'*64},
                'target_upgrade':{'wheel_sha256':'d'*64,'release_manifest_sha256':'e'*64},
                'previous_activation_manifest_sha256':'f'*64,'retry_id':'1'*32,
                'failed':{'job_id':'old-job','request_id':'old-request','worker_id':'old-worker','provider_id':'old-pod'},
                'files':{name:r.digest(raw) for name,raw in files.items()}}
    path=tmp_path/'manifest.json';put(path,r.encoded(manifest));pin=r.digest(path.read_bytes())
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    assert r.inputs(path,pin)[2] == files
    put(tmp_path/'upgrade-controller.py',b'changed helper')
    with pytest.raises(r.RetryError,match='FILE_PIN_CHANGED'):r.inputs(path,pin)
    with pytest.raises(r.RetryError,match='MANIFEST_PIN_CHANGED'):r.inputs(path,'0'*64)


def test_generic_error_reports_only_type_and_helper_line(tmp_path, monkeypatch,capsys):
    monkeypatch.setattr(r.os,'geteuid',lambda:0)
    monkeypatch.setattr(r.os,'umask',lambda _:None)
    monkeypatch.setattr(r,'inputs',lambda *_:(_ for _ in ()).throw(RuntimeError('PRIVATE_CREDENTIAL_TEXT')))
    assert r.main(['prepare','--manifest',str(tmp_path/'manifest'),'--manifest-sha256','a'*64,'--human','human']) == 1
    raw=capsys.readouterr().out;report=json.loads(raw)
    assert report['reason']=='RECOVERY_REFUSED' and report['exception_type']=='RuntimeError'
    assert type(report['helper_line']) is int and 'PRIVATE_CREDENTIAL_TEXT' not in raw
