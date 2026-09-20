"""Region-only configuration preparation; real Ledger/RPC, no host/cloud actions."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from probe_core import gpu_acceptance_runner as runner
from probe_core.ledger import ApprovalError
from probe_core.schemas import ApprovalNonce
from test_gpu_acceptance_runner import setup, queue, runpod, manifest_data
from test_gpu_calibration_activation import typed_inputs

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('retarget_calibration', PROJECT / 'deploy/retarget-gpu-calibration.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def put(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)


def changed(old, identity='a'*32):
    new = dict(old)
    plan = json.loads(old['plan.json'])
    plan['label'] += '-romania'
    plan['cases'][0]['spec']['idempotency_key'] += '-romania'
    new['plan.json'] = r.encoded(plan)
    worker = dict(json.loads(old['worker.json']), region='EU-RO-1')
    new['worker.json'] = r.encoded(worker)
    before = json.loads(old['acceptance.json'])
    public = r.PUBLIC / ('retarget-' + identity)
    config = dict(before, deployment=dict(before['deployment'], region='EU-RO-1'),
        plan_path=str(public/'plan.json'), plan_sha256='sha256:'+r.digest(new['plan.json']),
        worker_config_path=str(public/'worker.json'), worker_config_sha256='sha256:'+r.digest(new['worker.json']),
        submission_state_directory=str(r.SUBMIT/('retarget-'+identity)),
        trusted_state_directory=str(r.STATE/('retarget-'+identity)))
    new['acceptance.json'] = r.encoded(config)
    for name in r.CALIBRATION:
        new[name] = old[name].replace(('--config '+str(Path(before['plan_path']).parent/'acceptance.json')).encode(),
                                    ('--config '+str(public/'acceptance.json')).encode())
    return new


@pytest.fixture
def replacement(typed_inputs):
    old = {name: typed_inputs.bodies[name] for name in ('plan.json','acceptance.json','worker.json')}
    old['acceptance.json'] = r.encoded(runner.RunnerConfig.model_validate_json(old['acceptance.json']).model_dump(mode='json'))
    old.update({name:(PROJECT/'deploy'/name).read_bytes() for name in r.CALIBRATION})
    return SimpleNamespace(old=old,new=changed(old),identity='a'*32)


def test_region_and_fresh_identity_are_the_only_permitted_changes(replacement):
    f = replacement
    public, config = r.validate_retarget(f.old,f.new,f.identity,'EU-CZ-1','EU-RO-1')
    assert config['deployment']['region'] == json.loads(f.new['worker.json'])['region'] == 'EU-RO-1'
    assert config['worker_config_path'] == str(public/'worker.json')
    assert config['worker_config_sha256'] == 'sha256:'+r.digest(f.new['worker.json'])
    assert json.loads(f.old['worker.json'])['region'] == 'EU-CZ-1'


@pytest.mark.parametrize('fault',['worker_region','worker_model','worker_source','worker_image','worker_assets','runtime','price',
    'runner_region','token','identity','approval_wait','prompt','model','same_key','same_label','unit_user','unit_extra'])
def test_scope_expansion_refused_before_actions(replacement,fault):
    f = replacement
    if fault.startswith('worker_'):
        worker = json.loads(f.new['worker.json'])
        key = {'worker_region':'region','worker_model':'model','worker_source':'code_git_commit',
               'worker_image':'container_image_digest','worker_assets':'assets'}[fault]
        if fault == 'worker_region': worker[key] = 'EU-CZ-1'
        elif fault == 'worker_source': worker[key] = 'f'*40
        elif fault == 'worker_image': worker[key] = 'sha256:'+'f'*64
        elif fault == 'worker_model': worker[key]['revision_sha'] = 'f'*40
        else: worker[key][0]['sha256'] = 'sha256:'+'f'*64
        f.new['worker.json'] = r.encoded(worker)
        config = json.loads(f.new['acceptance.json']);config['worker_config_sha256']='sha256:'+r.digest(f.new['worker.json'])
        f.new['acceptance.json']=r.encoded(config)
    elif fault in {'runtime','price','runner_region','token','identity','approval_wait'}:
        config=json.loads(f.new['acceptance.json'])
        if fault=='runner_region':config['deployment']['region']='EU-CZ-1'
        else:
            key={'runtime':'max_runtime_seconds','price':'expected_worker_price_usd_per_hour',
                 'token':'bearer_secret_file','identity':'ssh_identity_file','approval_wait':'approval_wait_seconds'}[fault]
            config[key] = '/different-secret' if fault in {'token','identity'} else 360 if fault!='price' else .79
        f.new['acceptance.json']=r.encoded(config)
    elif fault in {'prompt','model','same_key','same_label'}:
        plan=json.loads(f.new['plan.json'])
        if fault=='prompt':plan['cases'][0]['spec']['inputs']['prompt_ids'][0]='different'
        elif fault=='model':plan['model']['revision_sha']='f'*40
        elif fault=='same_key':plan['cases'][0]['spec']['idempotency_key']=json.loads(f.old['plan.json'])['cases'][0]['spec']['idempotency_key']
        else:plan['label']=json.loads(f.old['plan.json'])['label']
        f.new['plan.json']=r.encoded(plan)
    else:
        name=r.CALIBRATION[1]
        f.new[name]=f.new[name].replace(b'User=probe-trusted',b'User=root') if fault=='unit_user' else f.new[name]+b'\nOther=value\n'
    with pytest.raises((r.RetargetError,ValueError)):
        r.validate_retarget(f.old,f.new,f.identity,'EU-CZ-1','EU-RO-1')


@pytest.fixture
def pending(setup,monkeypatch):
    s=setup
    worker_path=Path(s.config.worker_config_path)
    worker=r.encoded(dict(json.loads(worker_path.read_bytes()),region='EU-CZ-1'))
    worker_path.write_bytes(worker)
    s.config=s.config.model_copy(update={'deployment':s.config.deployment.model_copy(update={'region':'EU-CZ-1'}),
        'worker_config_sha256':'sha256:'+r.digest(worker)})
    submitted=queue(s)
    identity={key:submitted[key] for key in ('job_id','request_id','worker_id')}
    from probe_core import runpod_provider
    monkeypatch.setattr(runpod_provider.RunPodConfig,'load',lambda _:SimpleNamespace(api_key_file='unused'))
    monkeypatch.setattr(runpod_provider,'RunPodHTTP',lambda _:s.http)
    source=r.SNAPSHOT.replace('/var/lib/probe-core/research.sqlite',str(s.root/'research.sqlite')).replace(
        '/var/lib/probe-provider/runpod.sqlite',str(s.backend.path))
    def snapshot(target=None):
        out=io.StringIO()
        with redirect_stdout(out):exec(source,{'PENDING_JSON':json.dumps(target or identity)})
        return json.loads(out.getvalue())
    return SimpleNamespace(s=s,identity=identity,submitted=submitted,snapshot=snapshot,
                           plan=s.plan.model_dump(mode='json'),config=s.config.model_dump(mode='json'))


def test_real_never_approved_job_cancels_via_research_and_cannot_consume_approval(pending):
    f=pending
    r.validate_pending(f.snapshot(),f.identity,f.plan,f.config,f.submitted)
    f.s.rpc.call('cancel_job',{'job_id':f.identity['job_id']})
    r.validate_pending(f.snapshot(),f.identity,f.plan,f.config,f.submitted,cancelled=True)
    nonce=ApprovalNonce(approval_id=f.submitted['approval_id'],token='s'*48,pod_id=f.identity['worker_id'],
        batch_hash=f.submitted['batch_hash'],max_runtime_seconds=900,price_ceiling_usd_per_hour=.8,
        issued_at=f.s.clock(),expires_at=f.s.clock()+timedelta(minutes=5))
    f.s.ledger.register_approval(nonce)
    with pytest.raises(ApprovalError,match='approved jobs must be pending'):
        f.s.ledger.consume_approval(nonce.approval_id,nonce.token.get_secret_value(),pod_id=nonce.pod_id,
            job_ids=[f.identity['job_id']],live_price_usd_per_hour=.74,requested_runtime_seconds=900)
    assert f.s.http.purchases==[]
    assert f.s.ledger.get_job(f.identity['job_id']).attempt_count==0
    assert any(row['payload'].get('tool')=='cancel_job' for row in f.s.ledger.audit_records())


@pytest.mark.parametrize('fault',['approval','linked_approval','attempt','intent','pod','other_job','open_approval',
    'started_request','observed_provider','deadline','changed_config','changed_spec','changed_binding'])
def test_no_authority_gates_refuse_changed_or_ambiguous_state(pending,fault):
    f=pending;snapshot=f.snapshot();submitted=deepcopy(f.submitted)
    if fault in {'approval','linked_approval','attempt','intent'}:
        snapshot[{'approval':'approvals','linked_approval':'approval_jobs','attempt':'attempts','intent':'intents'}[fault]]=[{'unexpected':True}]
    elif fault in {'pod','other_job','open_approval'}:
        snapshot[{'pod':'pods','other_job':'other_unfinished_jobs','open_approval':'open_approvals'}[fault]]=1
    elif fault=='started_request':snapshot['requests'][0]['state']='PREPARING'
    elif fault=='observed_provider':snapshot['requests'][0]['observed_provider_id']='pod'
    elif fault=='deadline':snapshot['requests'][0]['deadline']=123
    elif fault=='changed_config':snapshot['requests'][0]['configuration_hash']='sha256:'+'f'*64
    elif fault=='changed_spec':snapshot['jobs'][0]['spec_json']='{}'
    else:submitted['request_id']='different'
    with pytest.raises((r.RetargetError,ValueError)):
        r.validate_pending(snapshot,f.identity,f.plan,f.config,submitted)


@pytest.fixture
def operation(pending,monkeypatch):
    f=pending;s=f.s
    for name in ('PUBLIC','SUBMIT','STATE','UNITS'):
        path=s.root/('retarget-'+name.lower());path.mkdir(mode=0o700)
        monkeypatch.setattr(r,name,path)
    old={'plan.json':r.encoded(f.plan),'acceptance.json':r.encoded(f.config),
         'worker.json':Path(s.config.worker_config_path).read_bytes()}
    old_path=str(Path(s.config.plan_path).parent/'acceptance.json').encode()
    for name in r.CALIBRATION:
        old[name]=(PROJECT/'deploy'/name).read_bytes().replace(b'/etc/probe-calibration/acceptance.json',old_path)
        put(r.UNITS/name,old[name])
    new=changed(old);public,config=r.validate_retarget(old,new,'a'*32,'EU-CZ-1','EU-RO-1')
    events=[];fault=[None];active={name:False for name in r.CALIBRATION}
    a=SimpleNamespace(Activation=lambda:None,trusted=lambda _:None,write_file=lambda path,raw,**_:put(path,raw))
    instance=r.Retarget(a,None,{'pending':f.identity,'installed_upgrade':{'wheel_sha256':'b'*64}}, {},'c'*64)
    instance.work=s.root/'capsule';instance.work.mkdir()
    instance.old,instance.new,instance.old_config,instance.config=old,new,f.config,config
    instance.submitted=f.submitted
    instance.public=public;instance.paths=(public,Path(config['submission_state_directory']),Path(config['trusted_state_directory']))
    instance.marker=public/'prepared';instance.guard_name='50-probe-calibration-retarget.conf'
    instance.guard=('[Unit]\nConditionPathExists='+str(instance.marker)+'\n').encode()
    instance.snapshot=f.snapshot
    def backup():
        events.append('backup')
        if fault[0]=='backup':raise RuntimeError('backup failed')
    instance.backup=backup
    instance.identities=lambda:events.append('identities') if fault[0]!='identity' else (_ for _ in ()).throw(RuntimeError('identity failed'))
    def ctl(*args):
        events.append(args)
        if args[0]=='stop':
            for name in args[1:]:active[name]=False
        elif args==('start',r.CALIBRATION[0]):
            assert instance.marker.exists() and events.index('identities')<len(events)-1
            if fault[0]=='submit':raise RuntimeError('submit failed')
            with runner.State(instance.paths[1]) as state:
                runner.submit(runner.RunnerConfig.model_validate(config),runner.AcceptancePlan.model_validate_json(new['plan.json']),s.rpc,state)
        elif args==('start',r.CALIBRATION[1]):
            if fault[0]=='runner':raise RuntimeError('runner failed')
            active[r.CALIBRATION[1]]=True
            put(instance.paths[2]/'binding.json',r.encoded({'config_sha256':runner.digest(runner.RunnerConfig.model_validate(config).model_dump(mode='json')),
                                                        'plan_sha256':config['plan_sha256']}))
        return b''
    def command(argv,**_):
        events.append('cancel-rpc')
        assert argv[:6]==['/usr/sbin/runuser','-u','probe-research','-g','probe-research','--']
        assert 'cancel_job' in argv[-1] and f.identity['job_id'] in argv[-1]
        answer=s.rpc.call('cancel_job',{'job_id':f.identity['job_id']})
        if fault[0]=='lost_cancel':raise RuntimeError('lost response')
        return r.encoded(answer)
    instance.operation=SimpleNamespace(users=(os.geteuid(),os.geteuid(),os.geteuid()),ctl=ctl,command=command,
                                       idle=lambda:events.append('idle'))
    instance.unit_state=lambda name:{'ActiveState':'active' if active[name] else 'inactive','MainPID':'42' if active[name] else '0','ControlPID':'0'}
    original_record=instance.record
    def record(name,value):
        if name=='prepare-report.json' and fault[0]=='receipt':raise OSError('receipt failed')
        return original_record(name,value)
    instance.record=record
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    monkeypatch.setattr(r.os,'chown',lambda *_:None)
    monkeypatch.setattr(r.grp,'getgrnam',lambda _:SimpleNamespace(gr_gid=os.getegid()))
    return SimpleNamespace(instance=instance,events=events,fault=fault,pending=f,old=old)


def test_guarded_retarget_preserves_old_records_and_starts_only_unapproved_new_proposal(operation):
    f=operation;instance=f.instance
    result=instance.execute()
    assert result['status']=='prepared' and result['approval_issued'] is False and result['application_reinstalled'] is False
    assert f.events.index('backup')<f.events.index(('stop',*r.CALIBRATION))<f.events.index('cancel-rpc')<f.events.index('identities')
    assert f.events.index(('start',r.CALIBRATION[0]))<f.events.index(('start',r.CALIBRATION[1]))
    assert instance.marker.exists()
    assert (instance.work/'cancel-intent.json').exists() and (instance.work/'cancelled.json').exists()
    assert instance.paths[1].stat().st_mode & 0o777==instance.paths[2].stat().st_mode & 0o777==0o700
    snapshot=instance.snapshot({key:result['submission'][key] for key in ('job_id','request_id','worker_id')})
    assert snapshot['approvals']==snapshot['attempts']==snapshot['intents']==[]
    assert f.pending.s.ledger.get_job(f.pending.identity['job_id']).failure_kind=='cancelled'
    assert f.pending.s.http.purchases==[]
    assert all('probe-controller' not in str(event) and 'probe-research.service' not in str(event) for event in f.events)


@pytest.mark.parametrize('fault',['backup','lost_cancel','identity','submit','runner','receipt'])
def test_partial_failure_stays_closed_and_does_not_replay_mutations(operation,fault):
    f=operation;f.fault[0]=fault
    with pytest.raises((RuntimeError,OSError)):f.instance.execute()
    assert not f.instance.marker.exists() and f.pending.s.http.purchases==[]
    if fault=='backup':
        assert f.events==['backup']
        assert f.pending.s.ledger.get_job(f.pending.identity['job_id']).state=='PENDING'
    else:
        assert f.instance.closed is True
        assert all((r.UNITS/(name+'.d')/f.instance.guard_name).read_bytes()==f.instance.guard for name in r.CALIBRATION)
        assert ('stop',*r.CALIBRATION) in f.events
        count=f.events.count('cancel-rpc')
        with pytest.raises(r.RetargetError):f.instance.execute()
        assert f.events.count('cancel-rpc')==count==1
    assert all('probe-controller' not in str(event) and 'probe-research.service' not in str(event) for event in f.events)


def test_state_is_rechecked_after_service_stop_before_cancel(operation):
    f=operation;original=f.instance.stop
    def stop_then_race():
        original()
        f.instance.snapshot=lambda *_:dict(f.pending.snapshot(),open_approvals=1)
    f.instance.stop=stop_then_race
    with pytest.raises(r.RetargetError,match='AUTHORITY_OR_OTHER_WORK_PRESENT'):f.instance.execute()
    assert 'cancel-rpc' not in f.events and f.pending.s.ledger.get_job(f.pending.identity['job_id']).state=='PENDING'


def test_manifest_pins_exact_old_new_files_and_all_executed_helpers(tmp_path,monkeypatch):
    files={name:('# pinned '+name).encode() for name in r.FILES}
    for name,raw in files.items():put(tmp_path/name,raw)
    manifest={'schema_version':1,'original_manifest_sha256':'a'*64,
        'installed_upgrade':{'wheel_sha256':'b'*64,'release_manifest_sha256':'c'*64},'retarget_id':'d'*32,
        'from_region':'EU-CZ-1','to_region':'EU-RO-1','pending':{'job_id':'job','request_id':'request','worker_id':'worker'},
        'files':{name:r.digest(raw) for name,raw in files.items()}}
    path=tmp_path/'manifest.json';put(path,r.encoded(manifest));pin=r.digest(path.read_bytes())
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    assert r.inputs(path,pin)[2]==files
    put(tmp_path/'upgrade-controller.py',b'changed helper')
    with pytest.raises(r.RetargetError,match='FILE_PIN_CHANGED'):r.inputs(path,pin)
    with pytest.raises(r.RetargetError,match='MANIFEST_PIN_CHANGED'):r.inputs(path,'f'*64)


@pytest.mark.parametrize('fault',[None,'service','stale','unverified'])
def test_existing_backup_service_must_produce_fresh_verified_archive_before_mutation(tmp_path,monkeypatch,fault):
    outbox,receipts=tmp_path/'outbox',tmp_path/'receipts'
    outbox.mkdir();receipts.mkdir()
    monkeypatch.setattr(r,'OUTBOX',outbox);monkeypatch.setattr(r,'RECEIPTS',receipts)
    monkeypatch.setattr(r,'read',lambda path,**_:Path(path).read_bytes())
    monkeypatch.setattr(r.pwd,'getpwnam',lambda _:SimpleNamespace(pw_uid=os.geteuid()))
    def archive(name,raw,verified=True):
        put(outbox/name,raw)
        put(receipts/(name+'.json'),r.encoded({'archive_sha256':r.digest(raw),'verified':verified,
            'readback_verified':verified,'restore_verified':verified}))
    archive('probe-1.tar',b'old verified archive')
    a=SimpleNamespace(Activation=lambda:None,write_file=lambda path,raw,**_:put(path,raw))
    instance=r.Retarget(a,None,{}, {},'a'*64)
    instance.work=tmp_path/'capsule';instance.work.mkdir()
    instance.checker=(PROJECT/'deploy/verify-installed-identities.py').read_bytes()
    calls=[]
    def command(args,**kwargs):
        assert args==['/usr/bin/systemctl','start','probe-backup.service'] and kwargs['timeout']==360
        calls.append(args)
        if len(calls)==2 and fault!='stale':archive('probe-2.tar',b'fresh verified archive',fault!='unverified')
        return b''
    instance.operation=SimpleNamespace(users=(os.geteuid(),),command=command,
        ctl=lambda *args:b'Result=failed\nExecMainStatus=1\n' if fault=='service' else b'Result=success\nExecMainStatus=0\n')
    if fault:
        with pytest.raises(Exception):instance.backup()
        assert not (instance.work/'before.tar').exists()
    else:
        instance.backup()
        assert (instance.work/'before.tar').read_bytes()==b'fresh verified archive'
        assert json.loads((instance.work/'backup.json').read_bytes())['archive_sha256']==r.digest(b'fresh verified archive')
    assert len(calls)==(1 if fault=='service' else 2)


def test_non_root_and_unexpected_failures_never_run_host_actions(monkeypatch,capsys):
    monkeypatch.setattr(r.os,'geteuid',lambda:1000)
    assert r.main(['--manifest','/not-read','--manifest-sha256','a'*64,'--human','human'])==1
    assert json.loads(capsys.readouterr().out)['reason']=='ADMINISTRATOR_REQUIRED'
    monkeypatch.setattr(r.os,'geteuid',lambda:0);monkeypatch.setattr(r.os,'umask',lambda _:None)
    monkeypatch.setattr(r,'inputs',lambda *_:(_ for _ in ()).throw(RuntimeError('PRIVATE_TEXT')))
    assert r.main(['--manifest','/not-read','--manifest-sha256','a'*64,'--human','human'])==1
    report=capsys.readouterr().out
    assert 'PRIVATE_TEXT' not in report and json.loads(report)['reason']=='RETARGET_UNAVAILABLE'
