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
