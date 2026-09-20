"""Lifecycle checks for supervised public calibration."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
from probe_core.runpod_provider import ProviderHTTPError

def load(name):
    path=Path(__file__).resolve().parents[1]/'deploy/gpu'/name
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
entry=load('public-calibration-entrypoint.py')
runner=load('run-public-calibration.py')

@pytest.mark.parametrize('deadline',['2000-01-01T00:00:00+00:00','2099-01-01T00:00:00+00:00','2026-09-20T22:00:00'])
def test_invalid_deadlines_refused(deadline):
    with pytest.raises(ValueError):entry.deadline_seconds(deadline)

def test_environment_never_inherits_provider_keys(monkeypatch):
    from probe_core import gpu_launch
    monkeypatch.setattr(gpu_launch,'GPU_LIBRARY_DIRECTORIES',())
    for key in ('RUNPOD_API_KEY','HF_TOKEN','PYTHONPATH'):monkeypatch.setenv(key,'never-forward')
    env=entry.clean_environment()
    assert not {'RUNPOD_API_KEY','HF_TOKEN','PYTHONPATH'} & env.keys()
    assert env['HF_HUB_OFFLINE']=='1' and env['CUDA_VISIBLE_DEVICES']=='0'

def test_process_group_actually_stopped():
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    try:
        entry.stop_group(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:process.kill();process.wait()

class DeletedProvider:
    def __init__(self):self.stops=0;self.transport=self
    def _intent(self,worker):return {'provider_id':'owned-pod'}
    def stop(self,worker):self.stops+=1
    def request(self,method,path):
        assert method=='GET' and path=='/v2/pods/owned-pod'
        raise ProviderHTTPError(404)

def test_delete_requires_get_404():
    provider=DeletedProvider();result=runner.cleanup(provider,'worker')
    assert provider.stops==1 and result=={'confirmed':True,'pod_id':'owned-pod','state':'ABSENT'}

def test_delete_ack_alone_not_success(monkeypatch):
    provider=DeletedProvider();provider.request=lambda *args:{'status':'RUNNING'}
    clock=iter([0,0,2]);monkeypatch.setattr(runner.time,'monotonic',lambda:next(clock))
    monkeypatch.setattr(runner.time,'sleep',lambda _:None)
    assert runner.cleanup(provider,'worker',seconds=1)=={'confirmed':False}

def test_guard_deletes_at_deadline(tmp_path):
    provider=DeletedProvider()
    runner.guard(tmp_path,{'deadline':time.time()-1,'worker_id':'worker'},provider)
    assert json.loads((tmp_path/'guard-stop.json').read_text())['confirmed']
    assert provider.stops==1

def test_creation_failure_still_deletes_owned_intent(tmp_path,monkeypatch):
    provider=DeletedProvider();provider._pods=lambda:[]
    provider.create=lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('uncertain create'))
    monkeypatch.setattr(runner.DeploymentSpec,'model_validate',lambda _:object())
    deadline=time.time()+600
    (tmp_path/'guard-ready.json').write_text(json.dumps({'deadline':deadline,'pid':os.getpid()}))
    record={'deadline':deadline,'worker_id':'worker','request_id':'request','deployment':{}}
    assert runner.run(tmp_path,record,provider)==1
    report=json.loads((tmp_path/'result.json').read_text())
    assert report['status']=='failed' and report['teardown']['confirmed'] and provider.stops==1
    with pytest.raises(FileExistsError):runner.run(tmp_path,record,provider)

@pytest.fixture
def copied_results(tmp_path):
    import hashlib
    from datetime import datetime, timezone
    from probe_core.audit import canonical_json
    config={'model':{'repo':'test-fixture'},'container_image_digest':'sha256:'+'a'*64,
            'code_git_commit':'b'*40,'region':'EU-RO-1','live_price_usd_per_hour':.74}
    record={'deadline':1789932000.1234562,'calibration_script_sha256':'sha256:'+'c'*64}
    (tmp_path/'calibration.json').write_text(json.dumps(config))
    summary={'suite':'backend_parity_v1','passed':True,'scientific_evidence':False,
        'input_lengths':[5,20],'checks':[{'passed':True,'rtol':0,'atol':0,'max_absolute_error':0} for _ in range(25)]+[{'passed':True} for _ in range(4)]}
    (tmp_path/'summary.json').write_text(json.dumps(summary))
    header=json.dumps({str(i):{'dtype':'F32','shape':[1],'data_offsets':[4*i,4*i+4]} for i in range(9)}).encode()
    (tmp_path/'tensors.safetensors').write_bytes(len(header).to_bytes(8,'little')+header+b'\0'*36)
    (tmp_path/'process.json').write_text(json.dumps({'returncode':0,'process_stopped':True}))
    manifest={'kind':'standalone_public_calibration','status':'passed','installed_ledger_used':False,
        'heldout_data_used':False,'lifecycle_acceptance':False,'nested_cgroup_limits_enforced':False,
        'model':config['model'],'operation':{'kind':'backend_parity'},
        'config_sha256':'sha256:'+hashlib.sha256(canonical_json(config).encode()).hexdigest(),
        'run_id':'public-calibration','absolute_deadline':datetime.fromtimestamp(record['deadline'],timezone.utc).isoformat(),
        'software':{'container_image_digest':config['container_image_digest'],'probe_mcp_git_commit':config['code_git_commit']},
        'hardware':{'region':'EU-RO-1','live_price_usd_per_hour':.74,'gpu_count':1},
        'calibration_script_sha256':record['calibration_script_sha256'],
        'artifacts':[{'path':name,'sha256':hashlib.sha256((tmp_path/name).read_bytes()).hexdigest()} for name in ('summary.json','tensors.safetensors')]}
    (tmp_path/'standalone-manifest.json').write_text(json.dumps(manifest))
    return tmp_path,record

def test_valid_producer_timestamp_roundtrip(copied_results):
    directory,record=copied_results
    assert runner.verify_results(directory,record)['checks']==29

@pytest.mark.parametrize('mutation',['artifact','image','configuration','deadline'])
def test_copied_result_mutation_rejected(copied_results,mutation):
    directory,record=copied_results
    if mutation=='artifact':
        with (directory/'tensors.safetensors').open('ab') as stream:stream.write(b'changed')
    else:
        p=directory/'standalone-manifest.json';m=json.loads(p.read_text())
        if mutation=='image':m['software']['container_image_digest']='sha256:'+'d'*64
        if mutation=='configuration':m['config_sha256']='sha256:'+'d'*64
        if mutation=='deadline':m['absolute_deadline']='2099-01-01T00:00:00+00:00'
        p.write_text(json.dumps(m))
    with pytest.raises(ValueError):runner.verify_results(directory,record)

def test_guard_loads_real_json_roundtrip(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from probe_core.runpod_provider import RunPodConfig,RunPodLaunchConfig,StorageRates
    provider=RunPodConfig(state_path=str(tmp_path/'state.sqlite'),api_key_file=str(tmp_path/'unused-key'),
        launch=RunPodLaunchConfig(image_repository='ghcr.io/example/image',ports=('22/tcp',)),
        storage_rates=StorageRates(checked_at=datetime.now(timezone.utc)))
    record={'provider':provider.model_dump(mode='json'),'deadline':time.time()-1,'worker_id':'worker'}
    (tmp_path/'run.json').write_text(json.dumps(record))
    monkeypatch.setattr(sys,'argv',['runner','guard',str(tmp_path)])
    assert runner.main()==0
    assert json.loads((tmp_path/'guard-stop.json').read_text())=={'confirmed':True,'created':False}
