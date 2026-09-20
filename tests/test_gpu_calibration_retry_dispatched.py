"""Stopped-attempt recovery preserves real history and never replays authority."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from probe_core.schemas import JobSpec
from test_gpu_calibration_retry import (
    PROJECT, r, put, setup, runpod, manifest_data, typed_inputs, replacement,
    prior_retry, third_retry, fourth_retry, fifth_retry, retarget_retry,
    completed_retry_then_next, next_inputs, prepare_case, approve_fixture,
)


@pytest.fixture
def stopped_failure(setup, monkeypatch):
    s = setup
    request = approve_fixture(s)
    job = s.ledger.dispatch_next(request['worker_id'], approval_id=request['approval_id'])
    s.ledger.start_job(job.job_id, job.attempt_id, job.worker_id)
    s.ledger.fail_job(job.job_id, job.attempt_id, job.worker_id, failure_kind='policy', reason='WorkerRequestError')
    s.ledger.confirm_stopped(job.job_id, job.attempt_id)
    s.controller.stop_gpu(job.worker_id)
    failed = dict(job_id=job.job_id, request_id=request['request_id'], worker_id=job.worker_id,
                  provider_id=request['observed_provider_id'])
    result = dict(schema_version=1, kind='single_public_gpu_calibration', scientific_evidence=False,
        status='failed', stage='artifact_collection', reason='CALIBRATION_DID_NOT_PASS',
        **{key:failed[key] for key in ('request_id','worker_id','provider_id')},
        teardown=dict(provider_id=failed['provider_id'], state='ABSENT', confirmed=True),
        approval_consumed_by_runner=False)
    bound = {key:request[key] for key in ('request_id','worker_id','approval_id','batch_hash','deadline','observed_provider_id')}
    submitted = {**failed, 'approval_id':request['approval_id']}
    from probe_core import runpod_provider
    monkeypatch.setattr(runpod_provider.RunPodConfig, 'load', lambda _:SimpleNamespace(api_key_file='not-read'))
    monkeypatch.setattr(runpod_provider, 'RunPodHTTP', lambda _:s.http)
    source = r.SNAPSHOT.replace('/var/lib/probe-core/research.sqlite', str(s.root/'research.sqlite')).replace(
        '/var/lib/probe-provider/runpod.sqlite', str(s.backend.path))
    def snapshot():
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(source, {'FAILED_JSON':json.dumps(failed), '__name__':'fixed_reader_test'})
        return json.loads(out.getvalue())
    attempt = dict(attempt_id=job.attempt_id, failure_kind='policy', failure_reason='WorkerRequestError')
    manifest = dict(schema_version=6, failed=failed, failed_attempt=attempt,
        expected_failure_stage='artifact_collection', expected_failure_reason='CALIBRATION_DID_NOT_PASS',
        failed_result_canonical_sha256=r.digest(r.encoded(result)))
    operation = r.Recovery(SimpleNamespace(Activation=lambda:None, write_file=lambda path,raw,**_:put(path,raw)),
                           None, manifest, {}, 'a'*64)
    operation.work = s.root/'recovery'; operation.work.mkdir()
    operation.old = {'plan.json':r.encoded(s.plan.model_dump(mode='json'))}
    operation.result, operation.bound, operation.submitted = result, bound, submitted
    operation.failure_hash = r.digest(r.encoded(dict(result=result,bound=bound,submitted=submitted)))
    operation.snapshot = snapshot
    checker = r.helper((PROJECT/'deploy/verify-installed-identities.py').read_bytes(),'history_reader')
    namespace = {}
    exec('import base64,hashlib,json,re,sqlite3,time\nfrom datetime import datetime,timezone\nfrom pathlib import Path\n'
         + checker.STATE_READER, namespace)
    def idle():
        value = namespace['idle_history_snapshot'](s.root/'research.sqlite',s.backend.path,
                                                  expected=operation.operation.baseline)
        if operation.operation.baseline is None: operation.operation.baseline = value
        return value
    operation.operation = SimpleNamespace(baseline=None, idle=idle,
        command=lambda *_args,**_kwargs:pytest.fail('preserving a terminal job must not call RPC or a service'))
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    return SimpleNamespace(s=s,operation=operation,manifest=manifest,snapshot=snapshot)


def test_schema6_preserves_real_failed_attempt_and_audit_without_cancellation(stopped_failure):
    f = stopped_failure
    before = f.snapshot()
    audit = f.s.ledger.audit_records()
    first = f.operation.close_failed()
    assert f.operation.close_failed() == first
    assert f.snapshot() == before and f.s.ledger.audit_records() == audit
    assert first['original_job_cancelled'] is False and first['failed_job_preserved'] is True
    assert first['attempts_created'] is first['approval_issued'] is False
    assert first['failed_attempt'] == f.manifest['failed_attempt']
    assert first['history']['attempts'] == 1 and first['history']['audit_valid'] is True


@pytest.mark.parametrize('fault', [
    'active_job','no_stop','extra_attempt','retry','count','attempt_id','job_id','worker','approval',
    'outcome','reason','kind','number','deadline','nan_stop','early_stop','open_approval','pod',
    'other_work','unconfirmed','request','result_stage','result_reason','result_attempt','result_pin',
    'bound_approval','approval_document',
])
def test_schema6_refuses_unproven_or_mismatched_execution(stopped_failure, fault):
    f = stopped_failure; snapshot = f.snapshot(); op = f.operation
    job, attempt = snapshot['jobs'][0], snapshot['attempts'][0]
    if fault == 'active_job': job['state']='RUNNING'
    elif fault == 'no_stop': attempt['stopped_at']=None
    elif fault == 'extra_attempt': snapshot['attempts'].append(dict(attempt,attempt_id='extra'))
    elif fault == 'retry': job['retry_count']=1
    elif fault == 'count': job['attempt_count']=2
    elif fault in {'attempt_id','job_id'}: attempt[fault]='changed'
    elif fault == 'worker': attempt['worker_id']='changed'
    elif fault == 'approval': attempt['approval_id']='changed'
    elif fault == 'outcome': attempt['outcome']='scientific'
    elif fault == 'reason': job['failure_reason']='different'
    elif fault == 'kind': job['failure_kind']='scientific'
    elif fault == 'number': attempt['attempt_number']=2
    elif fault == 'deadline': attempt['execution_deadline']=snapshot['requests'][0]['deadline']+1
    elif fault == 'nan_stop': attempt['stopped_at']=float('nan')
    elif fault == 'early_stop': attempt['stopped_at']=attempt['dispatched_at']-1
    elif fault == 'open_approval': snapshot['approvals'][0]['ended_at']=None
    elif fault == 'pod': snapshot['provider_absent']=False
    elif fault == 'other_work': snapshot['other_unfinished_jobs']=1
    elif fault == 'unconfirmed': snapshot['unconfirmed_attempts']=1
    elif fault == 'request': snapshot['requests'][0]['state']='RUNNING'
    elif fault.startswith('result_'):
        op.result[{'result_stage':'stage','result_reason':'reason','result_attempt':'attempt_id','result_pin':'extra'}[fault]]='changed'
        if fault != 'result_pin':op.m['failed_result_canonical_sha256']=r.digest(r.encoded(op.result))
    elif fault == 'bound_approval': op.bound['approval_id']='changed'
    else:
        value=json.loads(snapshot['approvals'][0]['document']);value['pod_id']='changed'
        snapshot['approvals'][0]['document']=json.dumps(value)
    with pytest.raises(r.RetryError):op.verify_failure(snapshot)


def test_schema6_detects_history_drift_between_preservation_reads(stopped_failure):
    f = stopped_failure; count=0
    def snapshot():
        nonlocal count
        count += 1
        value=f.snapshot()
        if count > 1:value['jobs'][0]['updated_at']+=1
        return value
    f.operation.snapshot=snapshot
    with pytest.raises(r.RetryError,match='FAILED_HISTORY_CHANGED'):f.operation.close_failed()
    assert not (f.operation.work/'close-report.json').exists()


def test_schema6_does_not_reuse_receipt_after_retained_history_changes(stopped_failure):
    f=stopped_failure;f.operation.close_failed()
    put(f.operation.work/'failed-snapshot.json',b'{}\n')
    with pytest.raises(r.RetryError,match='FAILED_HISTORY_CHANGED'):f.operation.close_failed()


@pytest.mark.parametrize('fault',[None,'missing_attempt','extra_attempt_field','stage','reason','missing_worker','equal_old_schema'])
def test_schema6_manifest_exact_fields_and_same_controller_allowed(tmp_path,monkeypatch,fault):
    files={name:b'# pinned '+name.encode() for name in r.FILES|{'worker.json'}}
    for name,raw in files.items():put(tmp_path/name,raw)
    baseline={'wheel_sha256':'b'*64,'release_manifest_sha256':'c'*64}
    manifest=dict(schema_version=6,original_manifest_sha256='a'*64,previous_upgrade=baseline,target_upgrade=baseline,
        previous_activation_manifest_sha256='d'*64,previous_retry_manifest_sha256='e'*64,previous_retry_record_sha256='f'*64,
        expected_failure_reason='CALIBRATION_DID_NOT_PASS',expected_failure_stage='artifact_collection',
        failed_result_canonical_sha256='1'*64,retry_id='2'*32,
        failed=dict(job_id='job',request_id='request',worker_id='worker',provider_id='pod'),
        failed_attempt=dict(attempt_id='attempt',failure_kind='policy',failure_reason='WorkerRequestError'),
        files={name:r.digest(raw) for name,raw in files.items()})
    if fault=='missing_attempt':del manifest['failed_attempt']
    elif fault=='extra_attempt_field':manifest['failed_attempt']['retry']=True
    elif fault=='stage':manifest['expected_failure_stage']='approved_dispatch'
    elif fault=='reason':manifest['expected_failure_reason']='REQUEST_CHANGED'
    elif fault=='missing_worker':del manifest['files']['worker.json']
    elif fault=='equal_old_schema':
        manifest['schema_version']=5;del manifest['failed_attempt'];del manifest['files']['worker.json']
        manifest['expected_failure_stage']='verified_worker_startup';manifest['expected_failure_reason']='REQUEST_CHANGED'
    path=tmp_path/'manifest.json';put(path,r.encoded(manifest))
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    if fault:
        with pytest.raises(r.RetryError):r.inputs(path,r.digest(path.read_bytes()))
    else:assert r.inputs(path,r.digest(path.read_bytes()))[2]==files


@pytest.fixture
def sixth_retry(retarget_retry):
    s=completed_retry_then_next(retarget_retry,'6'*32)
    s.manifest.pop('previous_retarget_manifest_sha256',None)
    s.manifest.update(schema_version=6,expected_failure_stage='artifact_collection',
        expected_failure_reason='CALIBRATION_DID_NOT_PASS',
        failed_attempt=dict(attempt_id='stopped-attempt',failure_kind='policy',failure_reason='WorkerRequestError'))
    s.manifest['target_upgrade']=s.manifest['previous_upgrade']
    s.new=next_inputs(s.old,s.manifest['retry_id'],new_worker=True)
    worker=json.loads(s.new['worker.json']);worker['code_git_commit']='8'*40;worker['container_image_digest']='sha256:'+'9'*64
    s.new['worker.json']=r.encoded(worker)
    config=json.loads(s.new['acceptance.json']);config['source_commit']=worker['code_git_commit']
    config['deployment']['image_digest']=worker['container_image_digest'];config['worker_config_sha256']='sha256:'+r.digest(s.new['worker.json'])
    s.new['acceptance.json']=r.encoded(config)
    s.manifest['files']={name:r.digest(s.new[name]) for name in r.FILES|{'worker.json'} if name in s.new}
    s.operation.files=s.new
    return s


def test_schema6_revalidates_schema5_and_historical_retarget_before_worker_only_change(sixth_retry):
    s=sixth_retry
    before={str(p):p.read_bytes() for folder in s.prior_capsules for p in folder.iterdir()}
    old=s.operation.previous_inputs(s.original)
    _,config=r.validate_replacement(old,s.new,s.manifest['retry_id'],schema_version=6)
    assert config['deployment']['region']=='EU-RO-1'
    assert config['source_commit']=='8'*40
    assert before=={str(p):p.read_bytes() for folder in s.prior_capsules for p in folder.iterdir()}


@pytest.mark.parametrize('field',['model','operation','runtime','region','price','worker_limit','same_image'])
def test_schema6_worker_replacement_cannot_change_other_scope(sixth_retry,field):
    s=sixth_retry;old=s.operation.previous_inputs(s.original)
    if field in {'model','operation'}:
        value=json.loads(s.new['plan.json'])
        if field=='model':value['cases'][0]['spec']['model']['revision_sha']='7'*40
        else:value['cases'][0]['spec']['operation']={'kind':'generate'}
        s.new['plan.json']=r.encoded(value)
    elif field in {'runtime','region','price'}:
        value=json.loads(s.new['acceptance.json'])
        if field=='runtime':value['max_runtime_seconds']=800
        elif field=='region':value['deployment']['region']='EU-CZ-1'
        else:value['expected_worker_price_usd_per_hour']=.75
        s.new['acceptance.json']=r.encoded(value)
    else:
        value=json.loads(s.new['worker.json'])
        if field=='worker_limit':value['max_tensor_bytes']=123456
        else:value['container_image_digest']=json.loads(old['worker.json'])['container_image_digest']
        s.new['worker.json']=r.encoded(value)
    with pytest.raises(r.RetryError):r.validate_replacement(old,s.new,s.manifest['retry_id'],schema_version=6)


@pytest.mark.parametrize('gate_failure',[False,True])
def test_no_reinstall_preparation_requires_local_gates_before_any_publication(prepare_case,gate_failure):
    s=prepare_case;op=s.operation
    op.m.update(schema_version=6,previous_upgrade=op.m['target_upgrade'])
    op.verify_preserved_receipt=lambda *_:None
    def gates():
        s.events.append('local_gates')
        if gate_failure:raise r.RetryError('BACKUP_NOT_VERIFIED')
        return {'before.tar':'a'*64,'backup.json':'b'*64,'identity-acceptance.json':'c'*64}
    op.local_gates=gates
    if gate_failure:
        with pytest.raises(r.RetryError,match='BACKUP_NOT_VERIFIED'):op.prepare()
        assert not s.public.exists() and not any((s.units/(name+'.d')).exists() for name in r.CALIBRATION)
    else:
        result=op.prepare()
        assert result['application_reinstalled'] is False
        assert result['local_gates']['before.tar']=='a'*64
        assert (s.public/'worker.json').read_bytes()==op.files['worker.json']
        assert s.events.index('local_gates')<s.events.index(('start',r.CALIBRATION[0]))


def identity_report():
    return dict(passed=True,checks={str(i):True for i in range(26)},check_count=26,passed_count=26,
                failure_codes=[],normal_research_audit_events=2,paid_actions_performed=False)


@pytest.fixture
def local_gate_case(stopped_failure,monkeypatch):
    f=stopped_failure;op=f.operation;root=f.s.root
    a=r.helper((PROJECT/'deploy/activate-gpu-calibration.py').read_bytes(),'activation_checks')
    op.a.identity_gate=a.identity_gate
    op.human='fixture-human'
    outbox=root/'outbox';receipts=root/'receipts';outbox.mkdir();receipts.mkdir()
    monkeypatch.setattr(r,'OUTBOX',outbox);monkeypatch.setattr(r,'RECEIPTS',receipts)
    monkeypatch.setattr(r.pwd,'getpwnam',lambda _:SimpleNamespace(pw_uid=1002))
    old=outbox/'probe-old.tar';put(old,b'old snapshot')
    latest=[old];events=[];failure=[None];starts=[0]
    op.checker=b'def completed_archive(**kwargs): return CURRENT()\n'
    original_helper=r.helper
    def helper(raw,name):
        module=original_helper(raw,name)
        if name=='pinned_retry_identity_checker':module.CURRENT=lambda:latest[0]
        return module
    monkeypatch.setattr(r,'helper',helper)
    wheel=b'installed pinned wheel'
    reference=dict(wheel_sha256=r.digest(wheel),release_manifest_sha256='1'*64)
    op.m.update(previous_upgrade=reference,target_upgrade=reference,original_manifest_sha256='2'*64)
    op.u=SimpleNamespace(verify_baseline=lambda *_args,**_kwargs:(None,wheel,None,None,None))
    def command(argv,**_kwargs):
        events.append(tuple(argv))
        if argv[:3]==['/usr/bin/systemctl','start','probe-backup.service']:
            starts[0]+=1
            if starts[0]==2 and failure[0]!='stale':
                latest[0]=outbox/'probe-new.tar';put(latest[0],b'new verified snapshot')
                put(receipts/'receipt.json',r.encoded(dict(archive_sha256=r.digest(latest[0].read_bytes()),
                    verified=True,readback_verified=True,restore_verified=failure[0]!='restore')))
        elif argv[:2]==['/usr/bin/python3','-I']:
            report=identity_report()
            if failure[0]=='identity':report['checks']['0']=False
            put(op.work/'identity-acceptance.json',r.encoded(report))
        else:pytest.fail('unexpected host command')
        return b''
    def ctl(*args):
        events.append(args)
        if args[1]=='probe-backup.service':return b'Result=success\nExecMainStatus=0\n'
        return b'failed\n' if failure[0]=='prune' else b'inactive\n'
    op.operation.command=command;op.operation.ctl=ctl;op.operation.users=(1000,1001,1003)
    op.operation.ready=lambda:events.append('ready')
    return SimpleNamespace(op=op,starts=starts,events=events,failure=failure)


def test_local_gates_drain_pending_then_backup_and_check_identities_without_reinstall(local_gate_case):
    s=local_gate_case;pins=s.op.local_gates()
    assert s.starts==[2] and s.events[-1]=='ready'
    r.validate_local_gates(s.op.work,pins,s.op.a)
    assert not any('pip' in str(event) or 'sandbox-acceptance' in str(event) for event in s.events)


@pytest.mark.parametrize('fault,code',[('stale','FRESH_BACKUP_REQUIRED'),('restore','BACKUP_NOT_VERIFIED'),
                                     ('prune','BACKUP_PRUNE_NOT_FINISHED'),('identity','IDENTITY_GATE_FAILED')])
def test_local_gate_failures_refuse_preparation(local_gate_case,fault,code):
    s=local_gate_case;s.failure[0]=fault
    with pytest.raises(ValueError,match=code):s.op.local_gates()
    assert 'ready' not in s.events


@pytest.mark.parametrize('fault',[None,'report','stop_proof','evidence_pin','snapshot','backup','identity'])
def test_completed_schema6_can_be_revalidated_only_with_retained_proofs(sixth_retry,stopped_failure,fault):
    s=sixth_retry;f=stopped_failure
    old=s.operation.previous_inputs(s.original)
    snapshot=f.snapshot();ids=s.manifest['failed'];approval=snapshot['approvals'][0]['approval_id']
    snapshot['jobs'][0].update(job_id=ids['job_id'],worker_id=ids['worker_id'],
        attempt_id=s.manifest['failed_attempt']['attempt_id'],
        spec_json=JobSpec.model_validate(json.loads(old['plan.json'])['cases'][0]['spec']).model_dump_json())
    snapshot['attempts'][0].update(attempt_id=s.manifest['failed_attempt']['attempt_id'],job_id=ids['job_id'],worker_id=ids['worker_id'])
    request=snapshot['requests'][0]
    request.update(request_id=ids['request_id'],worker_id=ids['worker_id'],observed_provider_id=ids['provider_id'],job_ids=json.dumps([ids['job_id']]))
    snapshot['intents'][0].update(worker_id=ids['worker_id'],request_key=ids['request_id'],provider_id=ids['provider_id'])
    document=json.loads(snapshot['approvals'][0]['document']);document['pod_id']=ids['worker_id']
    snapshot['approvals'][0]['document']=json.dumps(document);snapshot['approval_jobs']=[{'job_id':ids['job_id']}]
    result=dict(f.operation.result,**{key:ids[key] for key in ('request_id','worker_id','provider_id')},
                teardown=dict(provider_id=ids['provider_id'],state='ABSENT',confirmed=True))
    evidence=dict(result=result,bound={key:request[key] for key in f.operation.bound},submitted={**ids,'approval_id':approval})
    s.manifest['failed_result_canonical_sha256']=r.digest(r.encoded(result))
    following=completed_retry_then_next(s,'7'*32)
    following.manifest.update(schema_version=6,expected_failure_stage='artifact_collection',expected_failure_reason='CALIBRATION_DID_NOT_PASS')
    capsule=following.capsule
    put(capsule/'failed-snapshot.json',r.encoded(snapshot));put(capsule/'failed-evidence.json',r.encoded(evidence))
    closed=json.loads((capsule/'close-report.json').read_bytes())
    closed.update(original_job_cancelled=False,failed_job_preserved=True,failed_attempt=s.manifest['failed_attempt'],
        failed_snapshot_sha256=r.digest(r.encoded(snapshot)),failed_evidence_sha256=r.digest(r.encoded(evidence)))
    put(capsule/'close-report.json',r.encoded(closed))
    put(capsule/'before.tar',b'verified archive')
    put(capsule/'backup.json',r.encoded(dict(archive_sha256=r.digest(b'verified archive'),verified=True,readback_verified=True,restore_verified=True)))
    put(capsule/'identity-acceptance.json',r.encoded(identity_report()))
    report=json.loads((capsule/'prepare-report.json').read_bytes())
    report.update(application_reinstalled=False,local_gates={name:r.digest((capsule/name).read_bytes())
        for name in ('before.tar','backup.json','identity-acceptance.json')})
    put(capsule/'prepare-report.json',r.encoded(report))
    a=r.helper((PROJECT/'deploy/activate-gpu-calibration.py').read_bytes(),'actual_identity_gate')
    following.operation.a.identity_gate=a.identity_gate
    if fault=='report':report['application_reinstalled']=True;put(capsule/'prepare-report.json',r.encoded(report))
    elif fault=='stop_proof':snapshot['attempts'][0]['stopped_at']=None;put(capsule/'failed-snapshot.json',r.encoded(snapshot))
    elif fault=='evidence_pin':put(capsule/'failed-evidence.json',b'{}\n')
    elif fault=='snapshot':put(capsule/'failed-snapshot.json',b'{}\n')
    elif fault=='backup':put(capsule/'before.tar',b'changed archive')
    elif fault=='identity':put(capsule/'identity-acceptance.json',b'{}\n')
    if fault:
        with pytest.raises(r.RetryError):following.operation.previous_inputs(s.original)
    else:assert following.operation.previous_inputs(s.original)==s.new
