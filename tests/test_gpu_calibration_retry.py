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


@pytest.fixture
def prior_retry(tmp_path, replacement, monkeypatch):
    monkeypatch.setattr(r,'ROOT',tmp_path/'opt')
    previous_pin='2'*64
    prior=dict(schema_version=1, original_manifest_sha256='3'*64,
               previous_activation_manifest_sha256='4'*64, retry_id=replacement.retry_id,
               target_upgrade={'wheel_sha256':'5'*64,'release_manifest_sha256':'6'*64},
               files={name:r.digest(replacement.new[name]) for name in ('plan.json','acceptance.json',*r.CALIBRATION)})
    capsule=r.ROOT/'calibration-retries'/previous_pin
    raw=r.encoded(prior);put(capsule/'manifest.json',raw)
    failed={'job_id':'new-job','request_id':'new-request','worker_id':'new-worker','provider_id':'new-pod'}
    report=dict(schema_version=1,status='prepared',manifest_sha256=previous_pin,
                target_wheel_sha256='5'*64,old_history_preserved=True,approval_issued=False,cloud_mutations_performed=False,
                submission={**failed,'approval_consumed_by_runner':False})
    put(capsule/'prepare-report.json',r.encoded(report))
    put(capsule/'close-report.json',r.encoded(dict(status='closed',manifest_sha256=previous_pin,
                                                original_job_cancelled=True,attempts_created=False,approval_issued=False)))
    for name in prior['files']:put(capsule/name,replacement.new[name])
    manifest=dict(schema_version=2,original_manifest_sha256=prior['original_manifest_sha256'],
                  previous_activation_manifest_sha256=prior['previous_activation_manifest_sha256'],
                  previous_upgrade=prior['target_upgrade'],target_upgrade={'wheel_sha256':'7'*64,'release_manifest_sha256':'8'*64},
                  previous_retry_manifest_sha256=previous_pin,previous_retry_record_sha256=r.digest(raw),
                  expected_failure_reason='WORKER_STARTUP_DEADLINE',retry_id='b'*32,failed=failed)
    a=SimpleNamespace(Activation=lambda:SimpleNamespace(),trusted=lambda _:None)
    operation=r.Recovery(a,None,manifest,{},'9'*64)
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    return SimpleNamespace(operation=operation,original=replacement.old,old=replacement.new,
                           prior=prior,capsule=capsule,report=report,manifest=manifest)


def test_second_retry_loads_pinned_nested_inputs_and_changes_only_fresh_identity(prior_retry):
    s=prior_retry
    before={path.name:path.read_bytes() for path in s.capsule.iterdir()}
    old=s.operation.previous_inputs(s.original)
    assert old['plan.json']==s.old['plan.json'] and old['worker.json']==s.original['worker.json']
    new=dict(old);plan=json.loads(old['plan.json']);plan['label']+='-second';plan['cases'][0]['spec']['idempotency_key']+='-second'
    new['plan.json']=r.encoded(plan)
    config=json.loads(old['acceptance.json']);new_public=r.PUBLIC/('retry-'+s.manifest['retry_id'])
    old_config_path=Path(config['plan_path']).parent/'acceptance.json'
    config.update(plan_path=str(new_public/'plan.json'),plan_sha256='sha256:'+r.digest(new['plan.json']),
                  submission_state_directory=str(r.SUBMIT/('retry-'+s.manifest['retry_id'])),
                  trusted_state_directory=str(r.STATE/('retry-'+s.manifest['retry_id'])))
    new['acceptance.json']=r.encoded(config)
    for name in r.CALIBRATION:
        new[name]=old[name].replace(('--config '+str(old_config_path)).encode(),('--config '+str(new_public/'acceptance.json')).encode())
    public,after=r.validate_replacement(old,new,s.manifest['retry_id'])
    assert public==new_public and after['worker_config_path']=='/etc/probe-calibration/worker.json'
    assert after['trusted_state_directory'].count('retry-')==1
    assert before=={path.name:path.read_bytes() for path in s.capsule.iterdir()}


@pytest.mark.parametrize('fault',['record','incomplete','wrong_wheel','wrong_job','file','missing_close','wrong_chain'])
def test_second_retry_refuses_unpinned_incomplete_or_mismatched_history(prior_retry,fault):
    s=prior_retry
    if fault=='record':s.manifest['previous_retry_record_sha256']='f'*64
    elif fault=='file':put(s.capsule/'plan.json',b'changed')
    elif fault=='missing_close':(s.capsule/'close-report.json').unlink()
    elif fault=='wrong_chain':s.manifest['previous_upgrade']['wheel_sha256']='f'*64
    else:
        report=deepcopy(s.report)
        if fault=='incomplete':report['status']='failed'
        elif fault=='wrong_wheel':report['target_wheel_sha256']='f'*64
        else:report['submission']['job_id']='different'
        put(s.capsule/'prepare-report.json',r.encoded(report))
    with pytest.raises((r.RetryError,FileNotFoundError)):s.operation.previous_inputs(s.original)


def test_real_snapshot_deadline_failure_requires_exact_manifest_reason(real_failed):
    f=real_failed
    result=dict(f.result,reason='WORKER_STARTUP_DEADLINE')
    r.validate_failed(f.snapshot(),f.failed,f.plan,result,f.bound,f.submitted,expected_failure_reason='WORKER_STARTUP_DEADLINE')
    with pytest.raises(r.RetryError,match='FAILED_RESULT_CHANGED'):
        r.validate_failed(f.snapshot(),f.failed,f.plan,result,f.bound,f.submitted)
    f.s.rpc.call('cancel_job',{'job_id':f.failed['job_id']})
    r.validate_failed(f.snapshot(),f.failed,f.plan,result,f.bound,f.submitted,cancelled=True,
                      expected_failure_reason='WORKER_STARTUP_DEADLINE')


@pytest.mark.parametrize('fault',[None,'missing_record_pin','unknown_reason'])
def test_schema2_manifest_requires_canonical_record_pin_and_allowlisted_reason(tmp_path,monkeypatch,fault):
    files={name:b'# pinned '+name.encode() for name in r.FILES}
    for name,raw in files.items():put(tmp_path/name,raw)
    manifest=dict(schema_version=2,original_manifest_sha256='a'*64,
                  previous_upgrade={'wheel_sha256':'b'*64,'release_manifest_sha256':'c'*64},
                  target_upgrade={'wheel_sha256':'d'*64,'release_manifest_sha256':'e'*64},
                  previous_activation_manifest_sha256='f'*64,previous_retry_manifest_sha256='1'*64,
                  previous_retry_record_sha256='2'*64,expected_failure_reason='WORKER_STARTUP_DEADLINE',retry_id='3'*32,
                  failed={'job_id':'job','request_id':'request','worker_id':'worker','provider_id':'pod'},
                  files={name:r.digest(raw) for name,raw in files.items()})
    if fault=='missing_record_pin':del manifest['previous_retry_record_sha256']
    if fault=='unknown_reason':manifest['expected_failure_reason']='UNRELATED_FAILURE'
    path=tmp_path/'manifest.json';put(path,r.encoded(manifest))
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    if fault:
        with pytest.raises(r.RetryError):r.inputs(path,r.digest(path.read_bytes()))
    else:assert r.inputs(path,r.digest(path.read_bytes()))[1]==manifest


def next_inputs(old, retry_id, *, new_worker=False):
    new = dict(old)
    plan = json.loads(old['plan.json'])
    plan['label'] += '-next'
    plan['cases'][0]['spec']['idempotency_key'] += '-next'
    new['plan.json'] = r.encoded(plan)
    config = json.loads(old['acceptance.json'])
    old_path = str(Path(config['plan_path']).parent/'acceptance.json')
    public = r.PUBLIC/('retry-'+retry_id)
    config.update(plan_path=str(public/'plan.json'), plan_sha256='sha256:'+r.digest(new['plan.json']),
                  submission_state_directory=str(r.SUBMIT/('retry-'+retry_id)),
                  trusted_state_directory=str(r.STATE/('retry-'+retry_id)))
    if new_worker:
        worker = json.loads(old['worker.json'])
        worker.update(code_git_commit='d'*40, container_image_digest='sha256:'+'e'*64)
        new['worker.json'] = r.encoded(worker)
        config.update(source_commit=worker['code_git_commit'], worker_config_path=str(public/'worker.json'),
                      worker_config_sha256='sha256:'+r.digest(new['worker.json']))
        config['deployment']['image_digest'] = worker['container_image_digest']
    new['acceptance.json'] = r.encoded(config)
    for name in r.CALIBRATION:
        new[name] = old[name].replace(('--config '+old_path).encode(), ('--config '+str(public/'acceptance.json')).encode())
    return new


@pytest.fixture
def third_retry(prior_retry):
    s = prior_retry
    old = next_inputs(s.old, s.manifest['retry_id'])
    previous = deepcopy(s.manifest)
    previous['files'] = {name:r.digest(old[name]) for name in ('plan.json','acceptance.json',*r.CALIBRATION)}
    original_raw = (json.dumps(previous, indent=2)+'\n').encode()
    previous_pin = r.digest(original_raw)
    capsule = r.ROOT/'calibration-retries'/previous_pin
    put(capsule/'manifest-original.json', original_raw)
    put(capsule/'manifest.json', r.encoded(previous))
    failed = dict(job_id='third-job', request_id='third-request', worker_id='third-worker', provider_id='third-pod')
    report = dict(schema_version=1, status='prepared', manifest_sha256=previous_pin,
                  target_wheel_sha256=previous['target_upgrade']['wheel_sha256'], old_history_preserved=True,
                  approval_issued=False, cloud_mutations_performed=False,
                  submission={**failed,'approval_consumed_by_runner':False})
    put(capsule/'prepare-report.json',r.encoded(report))
    put(capsule/'close-report.json',r.encoded(dict(status='closed',manifest_sha256=previous_pin,
                                                original_job_cancelled=True,attempts_created=False,approval_issued=False)))
    for name in previous['files']:put(capsule/name,old[name])
    manifest = dict(previous, schema_version=3, previous_upgrade=previous['target_upgrade'],
                    target_upgrade={'wheel_sha256':'c'*64,'release_manifest_sha256':'d'*64},
                    previous_retry_manifest_sha256=previous_pin, previous_retry_record_sha256=r.digest(r.encoded(previous)),
                    expected_failure_reason='REQUEST_CHANGED', failed_result_canonical_sha256='e'*64,
                    retry_id='c'*32, failed=failed)
    new = next_inputs(old,manifest['retry_id'],new_worker=True)
    manifest['files'] = {name:r.digest(new[name]) for name in r.FILES|{'worker.json'} if name in new}
    operation = r.Recovery(s.operation.a,None,manifest,new,'f'*64)
    return SimpleNamespace(operation=operation, original=s.original, first=s.capsule, capsule=capsule,
                           previous=previous, manifest=manifest, old=old, new=new)


def test_third_retry_revalidates_entire_chain_and_pins_new_worker_without_mutating_prior_inputs(third_retry):
    s = third_retry
    before = {str(path):path.read_bytes() for folder in (s.first,s.capsule) for path in folder.iterdir()}
    old = s.operation.previous_inputs(s.original)
    assert old == {name:s.old[name] for name in ('plan.json','acceptance.json','worker.json',*r.CALIBRATION)}
    public, config = r.validate_replacement(old,s.new,s.manifest['retry_id'],schema_version=3)
    assert config['worker_config_path'] == str(public/'worker.json')
    assert config['worker_config_sha256'] == 'sha256:'+r.digest(s.new['worker.json'])
    assert config['source_commit'] == 'd'*40 and config['deployment']['image_digest'] == 'sha256:'+'e'*64
    assert before == {str(path):path.read_bytes() for folder in (s.first,s.capsule) for path in folder.iterdir()}
    with pytest.raises(r.RetryError,match='RUNNER_SCOPE_CHANGED'):
        r.validate_replacement(old,s.new,s.manifest['retry_id'],schema_version=2)


@pytest.mark.parametrize('fault',['original_missing','original_hash','original_content','earlier_file','earlier_report','previous_report'])
def test_third_retry_refuses_broken_original_or_intermediate_chain(third_retry,fault):
    s = third_retry
    if fault == 'original_missing':(s.capsule/'manifest-original.json').unlink()
    elif fault == 'original_hash':put(s.capsule/'manifest-original.json',b'{}\n')
    elif fault == 'original_content':
        changed=deepcopy(s.previous);changed['retry_id']='9'*32
        raw=r.encoded(changed);put(s.capsule/'manifest.json',raw)
        s.manifest['previous_retry_record_sha256']=r.digest(raw)
    elif fault == 'earlier_file':put(s.first/'plan.json',b'{}\n')
    else:
        folder=s.first if fault=='earlier_report' else s.capsule
        body=json.loads((folder/'prepare-report.json').read_bytes());body['submission']['job_id']='unrelated'
        put(folder/'prepare-report.json',r.encoded(body))
    with pytest.raises((r.RetryError,FileNotFoundError)):s.operation.previous_inputs(s.original)


@pytest.mark.parametrize('fault',['model','dataset','ram','price','region','launch','worker_price','worker_path',
                                 'worker_hash','same_image','same_source','source_mismatch'])
def test_schema3_allows_software_provenance_only(third_retry,fault):
    s=third_retry
    config=json.loads(s.new['acceptance.json']);worker=json.loads(s.new['worker.json']);plan=json.loads(s.new['plan.json'])
    if fault=='model':worker['model']['revision_sha']='1'*40
    elif fault=='dataset':worker['datasets'][0]['sha256']='sha256:'+'1'*64
    elif fault=='ram':
        plan['cases'][0]['spec']['limits']['max_ram_bytes']=JobSpec.model_validate(plan['cases'][0]['spec']).limits.max_ram_bytes+1
    elif fault=='price':config['expected_worker_price_usd_per_hour']=.79
    elif fault=='region':config['deployment']['region']='EU-RO-1'
    elif fault=='launch':config['deployment']['launch_config_hash']='sha256:'+'1'*64
    elif fault=='worker_price':worker['live_price_usd_per_hour']=.79
    elif fault=='worker_path':config['worker_config_path']='/etc/probe-calibration/worker.json'
    elif fault=='same_image':worker['container_image_digest']=json.loads(s.old['worker.json'])['container_image_digest']
    elif fault=='same_source':worker['code_git_commit']=json.loads(s.old['worker.json'])['code_git_commit']
    elif fault=='source_mismatch':config['source_commit']='1'*40
    s.new['plan.json']=r.encoded(plan);config['plan_sha256']='sha256:'+r.digest(s.new['plan.json'])
    s.new['worker.json']=r.encoded(worker);config['worker_config_sha256']='sha256:'+r.digest(s.new['worker.json'])
    if fault=='worker_hash':config['worker_config_sha256']='sha256:'+'1'*64
    s.new['acceptance.json']=r.encoded(config)
    with pytest.raises((r.RetryError,ValueError)):
        r.validate_replacement(s.old,s.new,s.manifest['retry_id'],schema_version=3)


def test_schema3_operator_stop_requires_exact_complete_result_and_real_closed_zero_attempt_case(real_failed):
    f=real_failed
    result=dict(f.result,reason='REQUEST_CHANGED',startup={'endpoint_attempts':187,'dispatch_cutoff':12345.0})
    m={'schema_version':3,'failed':f.failed,'expected_failure_reason':'REQUEST_CHANGED',
       'failed_result_canonical_sha256':r.digest(r.encoded(result))}
    operation=r.Recovery(SimpleNamespace(Activation=lambda:None),None,m,{},'a'*64)
    operation.old={'plan.json':r.encoded(f.plan)}
    operation.result,operation.bound,operation.submitted=result,f.bound,f.submitted
    operation.verify_failure(f.snapshot())
    # Exact parsed result is pinned; formatting does not matter, observations do.
    operation.result=json.loads(json.dumps(result,indent=4))
    operation.verify_result_pin()
    operation.result['startup']['endpoint_attempts']+=1
    with pytest.raises(r.RetryError,match='FAILED_RESULT_PIN_CHANGED'):operation.verify_failure(f.snapshot())
    operation.result=result
    altered=f.snapshot();altered['attempts']=[{'attempt_id':'unexpected'}]
    with pytest.raises(r.RetryError,match='FAILED_CASE_EXECUTED_OR_CHANGED'):operation.verify_failure(altered)
    altered=f.snapshot();altered['provider_absent']=False
    with pytest.raises(r.RetryError,match='PROVIDER_DELETION_UNCONFIRMED'):operation.verify_failure(altered)
    f.s.rpc.call('cancel_job',{'job_id':f.failed['job_id']})
    operation.verify_failure(f.snapshot(),cancelled=True)


@pytest.mark.parametrize('fault',[None,'missing_worker','bad_worker_pin','missing_result_pin','old_reason'])
def test_schema3_manifest_requires_seven_pins_and_exact_reason(tmp_path,monkeypatch,fault):
    files={name:b'# pinned '+name.encode() for name in r.FILES|{'worker.json'}}
    for name,raw in files.items():put(tmp_path/name,raw)
    m=dict(schema_version=3,original_manifest_sha256='a'*64,
           previous_upgrade={'wheel_sha256':'b'*64,'release_manifest_sha256':'c'*64},
           target_upgrade={'wheel_sha256':'d'*64,'release_manifest_sha256':'e'*64},
           previous_activation_manifest_sha256='f'*64,previous_retry_manifest_sha256='1'*64,
           previous_retry_record_sha256='2'*64,expected_failure_reason='REQUEST_CHANGED',retry_id='3'*32,
           failed_result_canonical_sha256='4'*64,
           failed={'job_id':'job','request_id':'request','worker_id':'worker','provider_id':'pod'},
           files={name:r.digest(raw) for name,raw in files.items()})
    if fault=='missing_worker':del m['files']['worker.json']
    elif fault=='bad_worker_pin':m['files']['worker.json']='0'*64
    elif fault=='missing_result_pin':del m['failed_result_canonical_sha256']
    elif fault=='old_reason':m['expected_failure_reason']='WORKER_STARTUP_DEADLINE'
    path=tmp_path/'manifest.json';put(path,r.encoded(m))
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    if fault:
        with pytest.raises(r.RetryError):r.inputs(path,r.digest(path.read_bytes()))
    else:assert r.inputs(path,r.digest(path.read_bytes()))[2]==files


def test_schema3_prepare_publishes_separate_public_worker_before_submission(prepare_case):
    s=prepare_case
    s.operation.m['schema_version']=3
    expected=s.operation.files['worker.json']
    original=s.operation.a.write_file
    writes=[]
    def write(path,raw,**kwargs):
        writes.append((path,raw,kwargs));original(path,raw,**kwargs)
    s.operation.a.write_file=write
    original_ctl=s.operation.operation.ctl
    def ctl(*args):
        if args==('start',r.CALIBRATION[0]):assert (s.public/'worker.json').read_bytes()==expected
        return original_ctl(*args)
    s.operation.operation.ctl=ctl
    s.operation.prepare()
    assert (s.public/'worker.json',expected,{'uid':0,'gid':0,'mode':0o444}) in writes


@pytest.fixture
def fourth_retry(third_retry):
    s = third_retry
    previous = deepcopy(s.manifest)
    original_raw = (json.dumps(previous, indent=2)+'\n').encode()
    previous_pin = r.digest(original_raw)
    capsule = r.ROOT/'calibration-retries'/previous_pin
    put(capsule/'manifest-original.json', original_raw)
    put(capsule/'manifest.json', r.encoded(previous))
    failed = dict(job_id='fourth-job', request_id='fourth-request', worker_id='fourth-worker', provider_id='fourth-pod')
    report = dict(schema_version=1, status='prepared', manifest_sha256=previous_pin,
                  target_wheel_sha256=previous['target_upgrade']['wheel_sha256'], old_history_preserved=True,
                  approval_issued=False, cloud_mutations_performed=False,
                  submission={**failed, 'approval_consumed_by_runner':False})
    put(capsule/'prepare-report.json', r.encoded(report))
    put(capsule/'close-report.json', r.encoded(dict(status='closed', manifest_sha256=previous_pin,
                                                  original_job_cancelled=True, attempts_created=False, approval_issued=False)))
    for name in previous['files']: put(capsule/name, s.new[name])
    manifest = dict(previous, schema_version=4, previous_upgrade=previous['target_upgrade'],
                    target_upgrade={'wheel_sha256':'1'*64, 'release_manifest_sha256':'2'*64},
                    previous_retry_manifest_sha256=previous_pin, previous_retry_record_sha256=r.digest(r.encoded(previous)),
                    expected_failure_reason='ACCEPTANCE_RUNTIME_UNAVAILABLE', retry_id='d'*32, failed=failed)
    new = next_inputs(s.new, manifest['retry_id'])
    del new['worker.json']
    manifest['files'] = {name:r.digest(new[name]) for name in r.FILES if name in new}
    operation = r.Recovery(s.operation.a, None, manifest, new, '3'*64)
    return SimpleNamespace(operation=operation, original=s.original, old=s.new, new=new,
                           manifest=manifest, previous=previous, capsule=capsule,
                           prior_capsules=(s.first, s.capsule, capsule))


def test_fourth_retry_retains_schema3_worker_and_revalidates_full_unmodified_chain(fourth_retry):
    s = fourth_retry
    before = {str(path):path.read_bytes() for folder in s.prior_capsules for path in folder.iterdir()}
    old = s.operation.previous_inputs(s.original)
    assert old['worker.json'] == s.old['worker.json'] != s.original['worker.json']
    public, config = r.validate_replacement(old, s.new, s.manifest['retry_id'], schema_version=4)
    previous = json.loads(s.old['acceptance.json'])
    for field in ('worker_config_path', 'worker_config_sha256', 'source_commit', 'deployment'):
        assert config[field] == previous[field]
    assert config['worker_config_path'] != str(public/'worker.json')
    assert config['worker_config_sha256'] == 'sha256:'+r.digest(old['worker.json'])
    assert before == {str(path):path.read_bytes() for folder in s.prior_capsules for path in folder.iterdir()}


@pytest.mark.parametrize('fault', ['worker_missing', 'worker_bytes', 'original_missing', 'original_bytes',
                                  'report', 'earlier_worker_binding'])
def test_fourth_retry_refuses_damaged_schema3_capsule_or_chain(fourth_retry, fault):
    s = fourth_retry
    if fault == 'worker_missing': (s.capsule/'worker.json').unlink()
    elif fault == 'worker_bytes': put(s.capsule/'worker.json', s.original['worker.json'])
    elif fault == 'original_missing': (s.capsule/'manifest-original.json').unlink()
    elif fault == 'original_bytes': put(s.capsule/'manifest-original.json', b'{}\n')
    elif fault == 'report':
        report = json.loads((s.capsule/'prepare-report.json').read_bytes())
        report['submission']['job_id'] = 'different'
        put(s.capsule/'prepare-report.json', r.encoded(report))
    else:
        s.original['worker.json'] = s.old['worker.json']
    with pytest.raises((r.RetryError, FileNotFoundError)):
        s.operation.previous_inputs(s.original)


@pytest.mark.parametrize('field', ['worker_config_path', 'worker_config_sha256', 'source_commit', 'image',
                                  'price', 'region', 'runtime', 'model'])
def test_schema4_cannot_change_worker_or_scientific_scope(fourth_retry, field):
    s = fourth_retry
    old = s.operation.previous_inputs(s.original)
    config = json.loads(s.new['acceptance.json'])
    if field == 'worker_config_path': config[field] = str(r.PUBLIC/'worker.json')
    elif field == 'worker_config_sha256': config[field] = 'sha256:'+'a'*64
    elif field == 'source_commit': config[field] = 'a'*40
    elif field == 'image': config['deployment']['image_digest'] = 'sha256:'+'a'*64
    elif field == 'price': config['expected_worker_price_usd_per_hour'] = .79
    elif field == 'region': config['deployment']['region'] = 'unapproved-region'
    elif field == 'runtime': config['max_runtime_seconds'] -= 1
    else:
        plan = json.loads(s.new['plan.json'])
        plan['model']['revision_sha'] = 'a'*40
        s.new['plan.json'] = r.encoded(plan)
        config['plan_sha256'] = 'sha256:'+r.digest(s.new['plan.json'])
    s.new['acceptance.json'] = r.encoded(config)
    with pytest.raises(r.RetryError):
        r.validate_replacement(old, s.new, s.manifest['retry_id'], schema_version=4)


def configuration_failure(f):
    return dict(f.result, stage='worker_configuration',
                configuration={'configured':None, 'reason':'CONFIGURATION_RESPONSE_UNCERTAIN'},
                diagnostic={'exception_type':'TransportError', 'location':'gpu_acceptance_runner.py:684'})


def test_schema4_real_closed_zero_attempt_configuration_failure_remains_audited(real_failed):
    f = real_failed
    result = configuration_failure(f)
    manifest = {'schema_version':4, 'failed':f.failed, 'expected_failure_reason':'ACCEPTANCE_RUNTIME_UNAVAILABLE',
                'failed_result_canonical_sha256':r.digest(r.encoded(result))}
    operation = r.Recovery(SimpleNamespace(Activation=lambda:None), None, manifest, {}, 'a'*64)
    operation.old = {'plan.json':r.encoded(f.plan)}
    operation.result, operation.bound, operation.submitted = result, f.bound, f.submitted
    operation.verify_failure(f.snapshot())
    operation.result = dict(result, configuration={'configured':False, 'reason':'CONFIGURATION_RESPONSE_UNCERTAIN'})
    with pytest.raises(r.RetryError, match='FAILED_RESULT_PIN_CHANGED'): operation.verify_failure(f.snapshot())
    operation.result = result
    changed = f.snapshot(); changed['attempts'] = [{'attempt_id':'unexpected'}]
    with pytest.raises(r.RetryError, match='FAILED_CASE_EXECUTED_OR_CHANGED'): operation.verify_failure(changed)
    changed = f.snapshot(); changed['approvals'][0]['ended_at'] = None
    with pytest.raises(r.RetryError, match='FAILED_APPROVAL_NOT_CLOSED'): operation.verify_failure(changed)
    changed = f.snapshot(); changed['provider_absent'] = False
    with pytest.raises(r.RetryError, match='PROVIDER_DELETION_UNCONFIRMED'): operation.verify_failure(changed)
    f.s.rpc.call('cancel_job', {'job_id':f.failed['job_id']})
    operation.verify_failure(f.snapshot(), cancelled=True)
    assert f.s.ledger.get_job(f.failed['job_id']).failure_kind == 'cancelled'
    assert f.snapshot()['attempts'] == []


@pytest.mark.parametrize('fault', ['stage', 'reason', 'configured', 'configuration_reason', 'exception', 'location'])
def test_schema4_rejects_other_configuration_failures_even_with_a_new_canonical_pin(real_failed, fault):
    f = real_failed
    result = configuration_failure(f)
    if fault == 'stage': result['stage'] = 'verified_worker_startup'
    elif fault == 'reason': result['reason'] = 'REQUEST_CHANGED'
    elif fault == 'configured': result['configuration']['configured'] = False
    elif fault == 'configuration_reason': result['configuration']['reason'] = 'UNRELATED'
    elif fault == 'exception': result['diagnostic']['exception_type'] = 'ValueError'
    else: result['diagnostic']['location'] = 'gpu_acceptance_runner.py:1'
    manifest = {'schema_version':4, 'failed':f.failed, 'expected_failure_reason':'ACCEPTANCE_RUNTIME_UNAVAILABLE',
                'failed_result_canonical_sha256':r.digest(r.encoded(result))}
    operation = r.Recovery(SimpleNamespace(Activation=lambda:None), None, manifest, {}, 'a'*64)
    operation.old = {'plan.json':r.encoded(f.plan)}
    operation.result, operation.bound, operation.submitted = result, f.bound, f.submitted
    with pytest.raises(r.RetryError, match='FAILED_(CONFIGURATION_)?RESULT_CHANGED'):
        operation.verify_failure(f.snapshot())


@pytest.mark.parametrize('schema_version', [1, 2, 3])
def test_legacy_recovery_does_not_accept_new_configuration_stage(real_failed, schema_version):
    f = real_failed
    with pytest.raises(r.RetryError, match='FAILED_RESULT_CHANGED'):
        r.validate_failed(f.snapshot(), f.failed, f.plan, configuration_failure(f), f.bound, f.submitted,
                          schema_version=schema_version)


@pytest.mark.parametrize('fault', [None, 'extra_worker', 'missing_result_pin', 'missing_record_pin', 'old_reason'])
def test_schema4_manifest_has_six_pins_and_exact_configuration_reason(tmp_path, monkeypatch, fault):
    files = {name:b'# pinned '+name.encode() for name in r.FILES}
    for name, raw in files.items(): put(tmp_path/name, raw)
    manifest = dict(schema_version=4, original_manifest_sha256='a'*64,
                    previous_upgrade={'wheel_sha256':'b'*64, 'release_manifest_sha256':'c'*64},
                    target_upgrade={'wheel_sha256':'d'*64, 'release_manifest_sha256':'e'*64},
                    previous_activation_manifest_sha256='f'*64, previous_retry_manifest_sha256='1'*64,
                    previous_retry_record_sha256='2'*64, expected_failure_reason='ACCEPTANCE_RUNTIME_UNAVAILABLE',
                    failed_result_canonical_sha256='3'*64, retry_id='4'*32,
                    failed={'job_id':'job', 'request_id':'request', 'worker_id':'worker', 'provider_id':'pod'},
                    files={name:r.digest(raw) for name, raw in files.items()})
    if fault == 'extra_worker': manifest['files']['worker.json'] = '5'*64
    elif fault == 'missing_result_pin': del manifest['failed_result_canonical_sha256']
    elif fault == 'missing_record_pin': del manifest['previous_retry_record_sha256']
    elif fault == 'old_reason': manifest['expected_failure_reason'] = 'REQUEST_CHANGED'
    path = tmp_path/'manifest.json'; put(path, r.encoded(manifest))
    monkeypatch.setattr(r, 'read', lambda path, **_:Path(path).read_bytes())
    if fault:
        with pytest.raises(r.RetryError): r.inputs(path, r.digest(path.read_bytes()))
    else:
        assert r.inputs(path, r.digest(path.read_bytes()))[2] == files
        assert len(files) == 6


def test_schema4_prepare_does_not_copy_or_replace_existing_worker(prepare_case, tmp_path):
    s = prepare_case
    s.operation.m['schema_version'] = 4
    worker = tmp_path/'previous-public'/'worker.json'
    put(worker, s.operation.files.pop('worker.json'))
    before = worker.read_bytes()
    s.operation.config['worker_config_path'] = str(worker)
    s.operation.files['acceptance.json'] = r.encoded(s.operation.config)
    result = s.operation.prepare()
    assert result['status'] == 'prepared'
    assert worker.read_bytes() == before and not (s.public/'worker.json').exists()
    assert result['approval_issued'] is False and result['cloud_mutations_performed'] is False


def completed_retry_then_next(previous_case, retry_id):
    s = previous_case
    previous = deepcopy(s.manifest)
    raw = (json.dumps(previous, indent=2)+'\n').encode()
    pin = r.digest(raw)
    capsule = r.ROOT/'calibration-retries'/pin
    put(capsule/'manifest-original.json', raw)
    put(capsule/'manifest.json', r.encoded(previous))
    failed = {name:name+'-'+retry_id for name in ('job_id','request_id','worker_id','provider_id')}
    put(capsule/'prepare-report.json', r.encoded(dict(schema_version=1,status='prepared',manifest_sha256=pin,
        target_wheel_sha256=previous['target_upgrade']['wheel_sha256'],old_history_preserved=True,
        approval_issued=False,cloud_mutations_performed=False,submission={**failed,'approval_consumed_by_runner':False})))
    put(capsule/'close-report.json', r.encoded(dict(status='closed',manifest_sha256=pin,
        original_job_cancelled=True,attempts_created=False,approval_issued=False)))
    for name in previous['files']: put(capsule/name,s.new[name])
    manifest = dict(previous,schema_version=5,previous_upgrade=previous['target_upgrade'],
        target_upgrade={'wheel_sha256':retry_id*2,'release_manifest_sha256':r.digest(retry_id.encode())},
        previous_retry_manifest_sha256=pin,previous_retry_record_sha256=r.digest(r.encoded(previous)),
        expected_failure_reason='PROVIDER_ENDPOINT_REFUSED',expected_failure_stage='verified_worker_startup',
        failed_result_canonical_sha256='9'*64,retry_id=retry_id,failed=failed)
    new = next_inputs(s.new,retry_id)
    manifest['files'] = {name:r.digest(new[name]) for name in r.FILES if name in new}
    operation = r.Recovery(s.operation.a,None,manifest,new,'a'*64)
    return SimpleNamespace(operation=operation,original=s.original,old=s.new,new=new,manifest=manifest,
                           previous=previous,capsule=capsule,prior_capsules=(*s.prior_capsules,capsule))


@pytest.fixture
def fifth_retry(fourth_retry):
    return completed_retry_then_next(fourth_retry,'e'*32)


def test_schema5_follows_schema4_and_carries_original_schema3_worker(fifth_retry):
    s = fifth_retry
    before = {str(p):p.read_bytes() for folder in s.prior_capsules for p in folder.iterdir()}
    old = s.operation.previous_inputs(s.original)
    config_before = json.loads(old['acceptance.json'])
    public, config = r.validate_replacement(old,s.new,s.manifest['retry_id'],schema_version=5)
    assert old['worker.json'] != s.original['worker.json']
    assert config['worker_config_path'] == config_before['worker_config_path'] != str(public/'worker.json')
    assert config['worker_config_sha256'] == 'sha256:'+r.digest(old['worker.json'])
    assert config['source_commit'] == config_before['source_commit']
    assert config['deployment'] == config_before['deployment']
    assert before == {str(p):p.read_bytes() for folder in s.prior_capsules for p in folder.iterdir()}


def test_schema5_can_follow_completed_schema5_without_new_incident_schema(fifth_retry):
    s = completed_retry_then_next(fifth_retry,'f'*32)
    old = s.operation.previous_inputs(s.original)
    _, config = r.validate_replacement(old,s.new,s.manifest['retry_id'],schema_version=5)
    assert s.previous['schema_version'] == s.manifest['schema_version'] == 5
    assert config['worker_config_sha256'] == 'sha256:'+r.digest(old['worker.json'])
    assert config['worker_config_path'] == json.loads(fifth_retry.old['acceptance.json'])['worker_config_path']


@pytest.mark.parametrize('fault',['repeat','limit','skip_schema4','worker_tamper','missing_original'])
def test_schema5_chain_refuses_repeated_excessive_or_broken_history(fifth_retry,monkeypatch,fault):
    s = fifth_retry
    kwargs = {}
    if fault in {'repeat','limit'}:
        kwargs['_seen'] = ((s.manifest['previous_retry_manifest_sha256'],) if fault=='repeat'
                           else tuple(f'{index:064x}' for index in range(32)))
        monkeypatch.setattr(r,'read',lambda *_a,**_k:pytest.fail('refuse before reading a repeated/excessive capsule'))
    elif fault=='skip_schema4':
        earlier = json.loads((s.prior_capsules[-2]/'manifest.json').read_bytes())
        s.manifest['previous_retry_manifest_sha256'] = s.prior_capsules[-2].name
        s.manifest['previous_retry_record_sha256'] = r.digest(r.encoded(earlier))
    elif fault=='worker_tamper':
        worker_capsule = next(p for p in s.prior_capsules if (p/'worker.json').exists())
        put(worker_capsule/'worker.json',s.original['worker.json'])
    else: (s.capsule/'manifest-original.json').unlink()
    with pytest.raises((r.RetryError,FileNotFoundError)):
        s.operation.previous_inputs(s.original,**kwargs)


def startup_failure(f, *, stage='verified_worker_startup', reason='PROVIDER_ENDPOINT_REFUSED'):
    return dict(f.result,schema_version=1,kind='single_public_gpu_calibration',scientific_evidence=False,
                stage=stage,reason=reason,startup={'endpoint_attempts':201},
                bootstrap_diagnostic={'status':'unavailable','records':[]})


def startup_recovery(f,result):
    manifest = dict(schema_version=5,failed=f.failed,expected_failure_reason=result['reason'],
                    expected_failure_stage=result['stage'],failed_result_canonical_sha256=r.digest(r.encoded(result)))
    operation = r.Recovery(SimpleNamespace(Activation=lambda:None),None,manifest,{},'a'*64)
    operation.old = {'plan.json':r.encoded(f.plan)}
    operation.result,operation.bound,operation.submitted = result,f.bound,f.submitted
    return operation


@pytest.mark.parametrize('stage',sorted(r.STARTUP_STAGES))
def test_schema5_pinned_startup_stages_require_closed_real_zero_attempt_case(real_failed,stage):
    f = real_failed
    operation = startup_recovery(f,startup_failure(f,stage=stage))
    operation.verify_failure(f.snapshot())
    f.s.rpc.call('cancel_job',{'job_id':f.failed['job_id']})
    operation.verify_failure(f.snapshot(),cancelled=True)
    assert f.snapshot()['attempts'] == []


@pytest.mark.parametrize('fault',['attempt','attempt_count','attempt_identity','spec','approval','request','absence',
                                 'scientific','success','result_schema','result_kind','result_job','result_stage','result_reason'])
def test_schema5_cannot_recover_executed_unclosed_or_reclassified_cases(real_failed,fault):
    f = real_failed
    operation = startup_recovery(f,startup_failure(f))
    snapshot = f.snapshot()
    if fault=='attempt': snapshot['attempts']=[{'attempt_id':'executed'}]
    elif fault=='attempt_count': snapshot['jobs'][0]['attempt_count']=1
    elif fault=='attempt_identity': snapshot['jobs'][0]['attempt_id']='executed'
    elif fault=='spec':
        value=json.loads(snapshot['jobs'][0]['spec_json']);value['limits']['max_output_bytes']+=1
        snapshot['jobs'][0]['spec_json']=json.dumps(value)
    elif fault=='approval': snapshot['approvals'][0]['ended_at']=None
    elif fault=='request': snapshot['requests'][0]['state']='RUNNING'
    elif fault=='absence': snapshot['provider_absent']=False
    elif fault=='scientific': operation.result['scientific_evidence']=True
    elif fault=='success': operation.result['status']='passed'
    elif fault=='result_schema': operation.result['schema_version']=True
    elif fault=='result_kind': operation.result['kind']='different'
    elif fault=='result_job': operation.result['request_id']='different'
    elif fault=='result_stage': operation.result['stage']='approved_dispatch'
    else: operation.result['reason']='ANOTHER_REASON'
    # Even a freshly pinned result cannot waive independent stage/identity/job gates.
    operation.m['failed_result_canonical_sha256']=r.digest(r.encoded(operation.result))
    with pytest.raises(r.RetryError): operation.verify_failure(snapshot)


def test_schema5_rejects_canonical_evidence_drift_before_any_snapshot_gate(real_failed):
    f = real_failed
    operation = startup_recovery(f,startup_failure(f))
    operation.result['startup']['endpoint_attempts']+=1
    with pytest.raises(r.RetryError,match='FAILED_RESULT_PIN_CHANGED'):
        operation.verify_failure({})


@pytest.mark.parametrize('fault',[None,'stage_missing','stage_execution','reason_private','reason_empty','extra_worker','pin_missing'])
def test_schema5_manifest_requires_six_files_exact_stage_reason_and_result_pin(tmp_path,monkeypatch,fault):
    files={name:b'# pinned '+name.encode() for name in r.FILES}
    for name,raw in files.items():put(tmp_path/name,raw)
    manifest=dict(schema_version=5,original_manifest_sha256='a'*64,
        previous_upgrade={'wheel_sha256':'b'*64,'release_manifest_sha256':'c'*64},
        target_upgrade={'wheel_sha256':'d'*64,'release_manifest_sha256':'e'*64},
        previous_activation_manifest_sha256='f'*64,previous_retry_manifest_sha256='1'*64,
        previous_retry_record_sha256='2'*64,expected_failure_reason='PROVIDER_ENDPOINT_REFUSED',
        expected_failure_stage='verified_worker_startup',failed_result_canonical_sha256='3'*64,retry_id='4'*32,
        failed={'job_id':'job','request_id':'request','worker_id':'worker','provider_id':'pod'},
        files={name:r.digest(raw) for name,raw in files.items()})
    if fault=='stage_missing':del manifest['expected_failure_stage']
    elif fault=='stage_execution':manifest['expected_failure_stage']='approved_dispatch'
    elif fault=='reason_private':manifest['expected_failure_reason']='Untrusted message /private/path'
    elif fault=='reason_empty':manifest['expected_failure_reason']=''
    elif fault=='extra_worker':manifest['files']['worker.json']='5'*64
    elif fault=='pin_missing':del manifest['failed_result_canonical_sha256']
    path=tmp_path/'manifest.json';put(path,r.encoded(manifest))
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    if fault:
        with pytest.raises(r.RetryError):r.inputs(path,r.digest(path.read_bytes()))
    else:assert r.inputs(path,r.digest(path.read_bytes()))[2]==files
