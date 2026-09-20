"""Pure transport/ledger checks; no SSH server, cloud, Torch or model loading."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import signal
import struct
import sys
import time

import pytest

from probe_core.audit import canonical_json
from probe_core.dispatcher import Dispatcher, TransportError
from probe_core.ledger import Ledger
from probe_core.schemas import ApprovalNonce, JobSpec, RunManifest
from probe_core.ssh_job_client import HELPER, SSHJobClient, bounded_command, _tensor_descriptions
from probe_core.worker_contracts import ExecutionReceipt, ExecutionRequest, WorkerState

NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def encoded(value):
    return canonical_json(value).encode()+b'\n'


def tensor_bytes():
    header=encoded({'direction':{'dtype':'F32','shape':[2],'data_offsets':[0,8]}}).rstrip()
    header += b' '*(-len(header)%8)
    return len(header).to_bytes(8,'little')+header+struct.pack('<ff',1.0,0.0)


def sha(body):
    return 'sha256:'+hashlib.sha256(body).hexdigest()


@pytest.fixture
def public_config():
    fixture=json.loads((Path(__file__).parent/'fixtures/manifest.json').read_text())
    return {'model':fixture['model'],'assets':[{'path':'model.safetensors','sha256':'sha256:'+'a'*64}],
        'datasets':[{'path':'/opt/probe-assets/datasets/public-calibration-prompts.json',
                     'sha256':fixture['inputs']['dataset_revision']}],
        'code_git_commit':'c'*40,'container_image_digest':'sha256:'+'3'*64,
        'region':'US-TEST-1','live_price_usd_per_hour':0.5}


@pytest.fixture
def execution(public_config):
    fixture=json.loads((Path(__file__).parent/'fixtures/manifest.json').read_text())
    spec=JobSpec(model=public_config['model'],idempotency_key='fixed-capture',experiment_stage='calibration',
        inputs=fixture['inputs'],operation={'kind':'capture','modules':[{'layer':14,'component':'residual'}],
        'positions':['last']},limits={'max_runtime_seconds':120,'max_output_bytes':1024*1024})
    return ExecutionRequest(job_id='job',attempt_id='attempt',worker_id='worker',approval_id='approval',
                            deadline=NOW+timedelta(seconds=120),spec=spec)


def success(execution, artifacts):
    data=json.loads((Path(__file__).parent/'fixtures/manifest.json').read_text())
    data['run'].update(run_id=execution.job_id,started_at=NOW.isoformat(),experiment_stage='calibration',
        hypothesis_id=None,preregistration_hash=None,approval_id=execution.approval_id,
        explorer_session_id=execution.science.session_id,replicator_blinded=False)
    data['model']=execution.spec.model.model_dump(mode='json')
    data['inputs']=execution.spec.inputs.model_dump(mode='json')
    data['experiment'].update(tool='capture_activation',modules=['model.layers.14'],positions=['last'],
        intervention_hash=Ledger.operation_hash(execution.spec),predicted_direction=execution.science.predicted_direction,
        primary_metric=execution.science.primary_metric,falsifier=execution.science.falsifier,
        alternative_explanations=list(execution.science.alternative_explanations))
    data['controls']=execution.science.controls.model_dump(mode='json')
    data['results'].update(effect_size=None,confidence_interval=None,heldout=False,replication_status='not_applicable')
    data['cost']['bytes_persisted']=sum(len(body) for body in artifacts.values())
    data['artifacts']=[{'path':name,'sha256':hashlib.sha256(body).hexdigest(),'retention_class':'derived'}
                       for name,body in artifacts.items()]
    return ExecutionReceipt(job_id=execution.job_id,attempt_id=execution.attempt_id,state='SUCCEEDED',
        started_at=NOW,finished_at=NOW+timedelta(seconds=1),process_stopped=True,
        manifest=RunManifest.model_validate(data))


class FakeTransport:
    def __init__(self, execution):
        self.calls=[]
        self.execution=execution
        self.artifacts={'summary.json':encoded({'observation':'fixed public fixture'}),
                        'nested/tensors.safetensors':tensor_bytes()}
        self.receipt=ExecutionReceipt(job_id=execution.job_id,attempt_id=execution.attempt_id,
                                     state='RUNNING',started_at=NOW)
        self.tensors={}
        self.upload_receipt=None
        self.bad_readback=False
        self.lose_start=False

    def __call__(self, command, payload, *, timeout, max_output_bytes):
        remote=shlex.split(command[-1])
        operation='stage' if remote[2]=='-c' else remote[-1]
        self.calls.append((operation,command,payload,timeout,max_output_bytes))
        if operation in {'stage','upload'}:
            line,body=payload.split(b'\n',1)
            metadata=json.loads(line)
            assert len(body)==metadata['length'] and sha(body)==metadata['sha256']
            if operation=='stage':
                return encoded({'path':HELPER,'sha256':sha(body),'bytes':len(body),'uid':0,'mode':0o444})
            self.tensors[sha(body)]=body
            self.upload_receipt={'path':sha(body)[7:]+'/tensor.safetensors','sha256':sha(body),
                                 'tensors':_tensor_descriptions(body)}
            return encoded(self.upload_receipt)
        data=json.loads(payload)
        if operation=='start':
            self.execution=ExecutionRequest.model_validate(data['request'])
            self.receipt=ExecutionReceipt(job_id=self.execution.job_id,attempt_id=self.execution.attempt_id,
                                         state='RUNNING',started_at=NOW)
            if self.lose_start:
                raise OSError('synthetic-secret: lost response after remote acceptance')
        if operation=='cancel':
            self.receipt=self.receipt.model_copy(update={'state':WorkerState.CANCELLED,'failure_kind':'cancelled',
                'error_code':'OperatorCancelled','finished_at':NOW+timedelta(seconds=1),'process_stopped':True})
        if operation in {'start','status','cancel'}:
            return self.receipt.model_dump_json().encode()
        if operation=='read-tensor':
            return b'changed' if self.bad_readback else self.tensors[data['sha256']]
        if operation=='artifact':
            assert data['attempt_id']==self.receipt.attempt_id
            return self.artifacts[data['path']]
        raise AssertionError(operation)


@pytest.fixture
def setup(tmp_path, execution, public_config):
    key=tmp_path/'key';key.write_bytes(b'synthetic-private-key');key.chmod(0o600)
    known=tmp_path/'known_hosts';known.write_bytes(b'synthetic-pinned-host-key');known.chmod(0o600)
    settings={'host':'203.0.113.10','user':'root','ssh_port':40103,'remote_port':8080,
              'identity_file':key,'known_hosts_file':known}
    transport=FakeTransport(execution)
    client=SSHJobClient(settings,public_config,NOW+timedelta(seconds=900),
                        transport=transport,clock=lambda:NOW.timestamp())
    return client,transport,settings


def test_fixed_command_strict_ssh_and_exact_request(setup,execution):
    client,transport,_=setup
    assert client.submit(execution).attempt_id==execution.attempt_id
    operation,command,payload,timeout,bound=transport.calls[-1]
    assert operation=='start' and command[0]=='/usr/bin/ssh'
    assert command[-1]=='/opt/probe-core/venv/bin/python -I /opt/probe/public-job.py start'
    for option in ('StrictHostKeyChecking=yes','IdentitiesOnly=yes','BatchMode=yes','ClearAllForwardings=yes'):
        assert option in command
    assert command[1:3]==['-F','/dev/null']
    assert json.loads(payload)['request']==execution.model_dump(mode='json')
    assert timeout==15 and bound==1024*1024


def test_lost_start_preserves_request_and_never_automatically_retries(setup,execution):
    client,transport,_=setup;transport.lose_start=True
    with pytest.raises(TransportError) as error:client.submit(execution)
    assert 'synthetic-secret' not in str(error.value)
    assert [row[0] for row in transport.calls]==['start']
    assert client.status(execution.attempt_id).state==WorkerState.RUNNING
    different=execution.model_copy(update={'job_id':'different'})
    with pytest.raises(TransportError,match='cannot be rebound'):client.submit(different)
    assert len(transport.calls)==2
    transport.lose_start=False
    client.submit(execution)
    assert transport.execution==execution


def test_same_client_refuses_new_attempt_after_uncertain_start(setup,execution):
    client,transport,_=setup;transport.lose_start=True
    with pytest.raises(TransportError):client.submit(execution)
    changed=execution.model_copy(update={'attempt_id':'fresh-attempt'})
    with pytest.raises(TransportError,match='fresh attempt'):client.submit(changed)
    with pytest.raises(TransportError,match='fresh attempt'):client.status('fresh-attempt')
    assert [row[0] for row in transport.calls]==['start']


@pytest.mark.parametrize('field,value',[('job_id','other'),('attempt_id','other')])
def test_start_identity_mismatch_refused(setup,execution,field,value):
    client,transport,_=setup
    wrong=transport.receipt.model_copy(update={field:value})
    client._transport=lambda *args,**kwargs:wrong.model_dump_json().encode()
    with pytest.raises(TransportError,match='different execution identity'):client.submit(execution)


@pytest.mark.parametrize('raw',[b'{"bad":true}',b'{"job_id":"a","job_id":"b"}',b'synthetic-secret'])
def test_malformed_receipts_redacted(setup,execution,raw):
    client,_,_=setup;client._transport=lambda *args,**kwargs:raw
    with pytest.raises(TransportError) as error:client.submit(execution)
    assert 'synthetic-secret' not in str(error.value)


def test_original_deadline_refuses_submit_but_allows_stop_observation(setup,execution):
    client,transport,_=setup
    client._clock=lambda:(NOW+timedelta(seconds=901)).timestamp()
    with pytest.raises(TransportError):client.submit(execution)
    assert not transport.calls
    assert client.cancel(execution.attempt_id).process_stopped
    assert [row[0] for row in transport.calls]==['status','cancel']


def test_cancellation_preserves_completed_outcome(setup,execution):
    client,transport,_=setup;transport.receipt=success(execution,transport.artifacts)
    assert client.cancel(execution.attempt_id).state==WorkerState.SUCCEEDED
    assert [row[0] for row in transport.calls]==['status']


def test_pinned_helper_staging_checks_source_and_remote_readback(setup,tmp_path):
    client,transport,_=setup
    script=tmp_path/'helper.py';script.write_bytes(b'print("fixed")\n')
    result=client.stage_helper(script,sha(script.read_bytes()))
    assert result=={'path':HELPER,'sha256':sha(script.read_bytes())}
    assert [row[0] for row in transport.calls]==['stage']
    with pytest.raises(TransportError):client.stage_helper(script,'sha256:'+'0'*64)
    assert len(transport.calls)==1
    client._transport=lambda *args,**kwargs:encoded(dict(result,sha256='sha256:'+'0'*64,
                                                       bytes=script.stat().st_size,uid=0,mode=0o444))
    with pytest.raises(TransportError,match='readback'):client.stage_helper(script,sha(script.read_bytes()))


def test_changed_trust_file_prevents_any_remote_command(setup,execution):
    client,transport,settings=setup
    settings['known_hosts_file'].write_text('replacement')
    with pytest.raises(TransportError,match='trust file changed'):client.submit(execution)
    assert not transport.calls


def test_tensor_upload_has_actual_receipt_and_independent_readback(setup,tmp_path):
    client,transport,_=setup;path=tmp_path/'direction.safetensors';path.write_bytes(tensor_bytes())
    result=client.upload_tensor(path,expected_sha256=sha(path.read_bytes()))
    assert result==transport.upload_receipt
    assert [row[0] for row in transport.calls]==['upload','read-tensor']
    assert result['tensors']==[{'tensor_name':'direction','shape':[2],'dtype':'F32'}]


@pytest.mark.parametrize('fault',['local_hash','header','upload_receipt','readback'])
def test_corrupt_tensor_transfer_never_succeeds(setup,tmp_path,fault):
    client,transport,_=setup;body=tensor_bytes();path=tmp_path/'tensor';path.write_bytes(body)
    expected=sha(body)
    if fault=='local_hash':expected='sha256:'+'0'*64
    if fault=='header':path.write_bytes(b'invalid');expected=sha(path.read_bytes())
    if fault=='readback':transport.bad_readback=True
    if fault=='upload_receipt':client._transport=lambda *args,**kwargs:encoded({'path':'wrong'})
    with pytest.raises(TransportError):client.upload_tensor(path,expected_sha256=expected)
    assert not any(row[0]=='start' for row in transport.calls)


def test_download_verified_real_manifest_bytes_and_resume(setup,execution,tmp_path):
    client,transport,_=setup;transport.receipt=success(execution,transport.artifacts)
    target=tmp_path/'copied'
    assert client.download(transport.receipt,target,max_bytes=1024*1024)==target
    assert {name:(target/name).read_bytes() for name in transport.artifacts}==transport.artifacts
    count=len(transport.calls)
    client.download(transport.receipt,target,max_bytes=1024*1024)
    assert len(transport.calls)==count


@pytest.mark.parametrize('fault',['hash','total','limit','symlink','hardlink','oversized_stream'])
def test_download_refuses_bad_evidence_or_destination(setup,execution,tmp_path,fault):
    client,transport,_=setup;receipt=success(execution,transport.artifacts)
    target=tmp_path/'copied';max_bytes=1024*1024
    if fault=='hash':transport.artifacts['summary.json']+=b'tampered'
    if fault=='total':
        data=receipt.model_dump(mode='json');data['manifest']['cost']['bytes_persisted']+=1
        receipt=ExecutionReceipt.model_validate(data)
    if fault=='limit':max_bytes=1
    if fault in {'symlink','hardlink'}:
        target.mkdir(mode=0o700)
        other=tmp_path/'elsewhere';other.write_bytes(transport.artifacts['summary.json'])
        if fault=='symlink':(target/'summary.json').symlink_to(other)
        else:os.link(other,target/'summary.json')
    if fault=='oversized_stream':client._transport=lambda *args,**kwargs:b'x'*(kwargs['max_output_bytes']+1)
    with pytest.raises(TransportError):client.download(receipt,target,max_bytes=max_bytes)
    assert not list(tmp_path.rglob('.download-*'))


def test_dispatcher_can_seal_real_bytes_via_ssh_adapter(setup,execution,tmp_path):
    client,transport,_=setup
    with Ledger(tmp_path/'ledger.sqlite',clock=lambda:NOW) as ledger:
        job=ledger.submit_job(execution.spec)
        approval=ApprovalNonce(approval_id='approval',token='x'*40,pod_id='worker',
            batch_hash=ledger.batch_hash([job.job_id]),max_runtime_seconds=900,
            price_ceiling_usd_per_hour=.8,issued_at=NOW,expires_at=NOW+timedelta(minutes=5))
        ledger.register_approval(approval)
        ledger.consume_approval('approval','x'*40,pod_id='worker',job_ids=[job.job_id],
                                live_price_usd_per_hour=.5,requested_runtime_seconds=900)
        dispatcher=Dispatcher(ledger,client,worker_id='worker',transfer_directory=tmp_path/'transfers')
        running=dispatcher.dispatch_next(approval_id='approval')
        assert running.attempt_count==1 and running.state.value=='RUNNING'
        transport.receipt=success(transport.execution,transport.artifacts)
        finished=dispatcher.reconcile(job.job_id)
        assert finished.state.value=='COMPLETED' and finished.attempt_id==running.attempt_id
        sealed=ledger.get_artifact_root(job.job_id)
        assert (sealed/'summary.json').read_bytes()==transport.artifacts['summary.json']
        assert ledger.get_manifest(job.job_id)==transport.receipt.manifest


def test_actual_byte_transport_bounds_output_and_redacts_stderr():
    command=[sys.executable,'-c','import sys;sys.stderr.write("synthetic-secret");sys.stdout.buffer.write(sys.stdin.buffer.read())']
    assert bounded_command(command,b'public',timeout=2,max_output_bytes=6)==b'public'
    with pytest.raises(TransportError) as error:
        bounded_command(command,b'public-overflow',timeout=2,max_output_bytes=6)
    assert 'synthetic-secret' not in str(error.value)


def test_actual_timeout_kills_group_after_leader_exits(tmp_path):
    pidfile=tmp_path/'child.pid'
    program='import os,sys,time\npid=os.fork()\nif pid:\n open(sys.argv[1],"w").write(str(pid));os._exit(0)\ntime.sleep(60)\n'
    with pytest.raises(TransportError,match='timed out'):
        bounded_command([sys.executable,'-c',program,str(pidfile)],b'',timeout=.25,max_output_bytes=100)
    child=int(pidfile.read_text())
    deadline=time.monotonic()+1
    while time.monotonic()<deadline:
        try:
            state=Path(f'/proc/{child}/stat').read_text().split(') ',1)[1].split()[0]
        except FileNotFoundError:
            state=None
        if state in {None,'Z'}:break
        time.sleep(.01)
    assert state in {None,'Z'}


@pytest.mark.parametrize('fault',[None,'config','request','attempt','deadline','process','fields'])
def test_inspect_binds_request_config_deadline_and_process_shapes(setup,execution,fault):
    client,transport,_=setup;client.submit(execution)
    identity={'pid':123,'identity':'456','boot_id':'00000000-0000-0000-0000-000000000001'}
    value={'receipt':transport.receipt.model_dump(mode='json'),
           'request_sha256':sha(canonical_json(execution.model_dump(mode='json')).encode()),
           'config_sha256':sha(canonical_json(client.config).encode()),
           'child_identity':identity,'monitor_identity':dict(identity,pid=122),
           'original_deadline':client.absolute_deadline.isoformat(),
           'effective_job_deadline':execution.deadline.isoformat(),
           'execution_started':None,'cancellation':None}
    if fault=='config':value['config_sha256']='sha256:'+'0'*64
    if fault=='request':value['request_sha256']='sha256:'+'0'*64
    if fault=='attempt':value['receipt']['attempt_id']='other'
    if fault=='deadline':value['original_deadline']=(client.absolute_deadline+timedelta(seconds=1)).isoformat()
    if fault=='process':value['child_identity']['pid']=True
    if fault=='fields':value['extra']='unexpected'
    client._transport=lambda *args,**kwargs:encoded(value)
    if fault:
        with pytest.raises(TransportError):client.inspect(execution.attempt_id)
    else:
        assert client.inspect(execution.attempt_id)==value


def test_actual_remote_helper_transfer_and_inspect_protocol(setup,execution,tmp_path,monkeypatch):
    """Use the producer functions themselves; no SSH server or model is involved."""
    import numpy as np
    from safetensors.numpy import save_file
    from probe_core.direction_transfer import DIRECTION_NAME,DIRECTION_SHA256
    path=Path(__file__).resolve().parents[1]/'deploy/gpu/public-job.py'
    specification=importlib.util.spec_from_file_location('ssh_client_protocol_helper',path)
    helper=importlib.util.module_from_spec(specification)
    specification.loader.exec_module(helper)
    monkeypatch.setattr(helper,'UID',os.geteuid())
    client,_,_=setup
    state=tmp_path/'remote-state';workspace=tmp_path/'remote-workspace'
    values=np.zeros(2048,dtype=np.float32);values[0]=1.0
    source=tmp_path/'public.safetensors';save_file({DIRECTION_NAME:values},str(source))
    assert sha(source.read_bytes())==DIRECTION_SHA256
    calls=[]
    def transport(command,payload,**_):
        operation=shlex.split(command[-1])[-1];calls.append(operation)
        if operation=='upload':
            line,body=payload.split(b'\n',1)
            return encoded(helper.upload(json.loads(line),io.BytesIO(body),state,workspace))
        body=json.loads(payload)
        if operation=='read-tensor':return helper.read_tensor(body,workspace)
        if operation=='artifact':return helper.artifact(body,state,workspace)
        if operation=='inspect':return encoded(helper.inspect_job(body,state,workspace))
        raise AssertionError(operation)
    client._transport=transport
    result=client.upload_tensor(source,expected_sha256=DIRECTION_SHA256)
    assert result['tensors']==[{'tensor_name':DIRECTION_NAME,'shape':[2048],'dtype':'F32'}]
    assert calls==['upload','read-tensor']
    outputs={'summary.json':encoded({'fixed':'producer fixture'}),'tensors.safetensors':source.read_bytes()}
    receipt=success(execution,outputs)
    output=workspace/'job-output';output.mkdir(mode=0o700)
    for name,body in outputs.items():(output/name).write_bytes(body)
    claim={'request':execution.model_dump(mode='json'),
           'request_sha256':sha(canonical_json(execution.model_dump(mode='json')).encode()),
           'config_sha256':sha(canonical_json(client.config).encode()),
           'original_deadline':client.absolute_deadline.isoformat(),
           'effective_job_deadline':execution.deadline.isoformat()}
    (state/'claim.json').write_bytes(encoded(claim))
    (state/'receipt.json').write_bytes(receipt.model_dump_json().encode())
    client._requests[execution.attempt_id]=execution
    assert client.inspect(execution.attempt_id)['receipt']==receipt.model_dump(mode='json')
    destination=client.download(receipt,tmp_path/'copied',max_bytes=1024*1024)
    assert {name:(destination/name).read_bytes() for name in outputs}==outputs
    assert calls==['upload','read-tensor','inspect','artifact','artifact']
