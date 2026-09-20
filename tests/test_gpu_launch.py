from types import SimpleNamespace
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

import pytest

from probe_core.gpu_launch import _prepare_worker_cgroups
from probe_core import gpu_launch as launch
from probe_core.audit import canonical_json
from test_schemas import manifest_data


@pytest.mark.parametrize('path', ['/sys/fs/cgroup', '/sys/fs/cgroup/another-service',
                                 '/sys/fs/cgroup/probe-jobs/../probe-jobs', None])
def test_runpod_launch_rejects_other_cgroup_scopes_before_mutation(path, monkeypatch):
    import probe_core.pod_bootstrap as bootstrap
    calls = []
    monkeypatch.setattr(bootstrap, 'prepare', lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match='must be /sys/fs/cgroup/probe-jobs'):
        _prepare_worker_cgroups(SimpleNamespace(cgroup_directory=path))
    assert calls == []


@pytest.fixture
def configured_fixture(tmp_path, monkeypatch, manifest_data):
    monkeypatch.setattr(launch, 'ROOT_UID', os.getuid())
    monkeypatch.setattr(launch, 'WORKER_UID', os.getuid())
    for key, name in [('BOOTSTRAP_STATE', 'bootstrap.json'), ('CONFIGURED', 'ready.json'),
                      ('CONFIG_BUNDLE', 'bundle.json'), ('BAKED_MANIFEST', 'assets.json'), ('PROVENANCE', 'provenance.json')]:
        monkeypatch.setattr(launch, key, tmp_path / name)
    workspace = tmp_path / 'workspace'
    workspace.mkdir(mode=0o755)
    monkeypatch.setattr(launch, 'CONFIG_ROOT', workspace / 'probe/config')
    body = dict(model_directory='/opt/probe-assets/models/Qwen/base', model=manifest_data['model'],
                assets=[{'path': 'config.json', 'sha256': 'sha256:'+'a'*64}],
                datasets=[{'path': '/opt/probe-assets/datasets/public.json', 'sha256': 'sha256:'+'b'*64}],
                tensor_directory='/workspace/probe/tensors', output_directory='/workspace/probe/attempts',
                device='cuda:0', backend='nnsight', code_git_commit='c'*40,
                container_image_digest='sha256:'+'d'*64, environment_lock_path='/opt/probe-core/uv.lock',
                provider_backend='runpod', region='EU-CZ-1', live_price_usd_per_hour=.74,
                cgroup_directory='/sys/fs/cgroup/probe-jobs')
    bundle = {'schema_version': 1, 'worker_config': body, 'bearer_token': 'synthetic-'+'t'*48}
    assets = {'schema_version': 1, 'assets': [
        {'path': 'models/Qwen/base/config.json', 'sha256': 'sha256:'+'a'*64},
        {'path': 'datasets/public.json', 'sha256': 'sha256:'+'b'*64}]}
    for path, value in [(launch.BOOTSTRAP_STATE, {'deadline': time.time()+300}),
                        (launch.PROVENANCE, {'source_commit': 'c'*40}), (launch.BAKED_MANIFEST, assets),
                        (launch.CONFIG_BUNDLE, bundle)]:
        path.write_text(canonical_json(value))
        path.chmod(0o600)
    return bundle


def test_private_configuration_published_once_without_inference(configured_fixture):
    bundle = configured_fixture
    result = launch.configure(launch.CONFIG_BUNDLE)
    assert result['configured'] and result['bundle_sha256'] == launch._configuration_digest(bundle)
    assert not launch.CONFIG_BUNDLE.exists()
    assert json.loads((launch.CONFIG_ROOT/'worker.json').read_text()) == bundle['worker_config']
    assert (launch.CONFIG_ROOT/'worker-token').stat().st_mode & 0o777 == 0o600
    assert (launch.CONFIG_ROOT.parent/'attempts').stat().st_mode & 0o777 == 0o700
    assert 'bearer_token' not in canonical_json(result)
    # A lost SSH reply can only confirm the same immutable bootstrap.
    launch.CONFIG_BUNDLE.write_text(canonical_json(bundle));launch.CONFIG_BUNDLE.chmod(0o600)
    assert launch.configure(launch.CONFIG_BUNDLE) == result


def test_different_configuration_cannot_rebind_running_bootstrap(configured_fixture):
    launch.configure(launch.CONFIG_BUNDLE)
    configured_fixture['bearer_token'] = 'different-'+'s'*48
    launch.CONFIG_BUNDLE.write_text(canonical_json(configured_fixture));launch.CONFIG_BUNDLE.chmod(0o600)
    with pytest.raises(ValueError, match='different configuration'):
        launch.configure(launch.CONFIG_BUNDLE)


@pytest.mark.parametrize('change', ['source', 'model_path', 'hash', 'dataset_path', 'output_path', 'deadline', 'permissions'])
def test_invalid_staging_refused_before_private_configuration(configured_fixture, change):
    body = configured_fixture['worker_config']
    if change == 'source': body['code_git_commit'] = 'e'*40
    if change == 'model_path': body['model_directory'] = '/etc'
    if change == 'hash': body['assets'][0]['sha256'] = 'sha256:'+'e'*64
    if change == 'dataset_path': body['datasets'][0]['path'] = '/opt/probe-assets/datasets/../secret'
    if change == 'output_path': body['output_directory'] = '/opt/probe-assets'
    if change == 'deadline': launch.BOOTSTRAP_STATE.write_text(canonical_json({'deadline': time.time()-1}))
    launch.CONFIG_BUNDLE.write_text(canonical_json(configured_fixture))
    if change == 'permissions': launch.CONFIG_BUNDLE.chmod(0o644)
    with pytest.raises(ValueError): launch.configure(launch.CONFIG_BUNDLE)
    assert not launch.CONFIGURED.exists() and not launch.CONFIG_ROOT.exists()


def test_expired_unconfigured_worker_never_launches_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, 'CONFIGURED', tmp_path/'missing')
    called = []
    monkeypatch.setattr(launch.subprocess, 'run', lambda *a, **kw: called.append(a))
    with pytest.raises(ValueError, match='original allowance'):
        launch._run_numerical_worker(None, time.time()-1, SimpleNamespace(poll=lambda: None), lambda: False)
    assert not called


def test_nonprivate_root_input_is_rejected(tmp_path):
    path = tmp_path/'public.json';path.write_text('{}');path.chmod(0o666)
    with pytest.raises(ValueError): launch._root_file(path)


def test_incompatible_host_never_opens_ssh_or_loads_model(monkeypatch):
    monkeypatch.setattr(launch, 'ROOT_UID', os.getuid())
    monkeypatch.setattr(launch.sys, 'argv', ['gpu_launch'])
    monkeypatch.setattr(launch, '_bootstrap_deadline', lambda: time.time()+300)
    def incompatible(*args): raise ValueError('cgroup-v2 namespace required')
    monkeypatch.setattr(launch, '_prepare_worker_cgroups', incompatible)
    calls = []
    monkeypatch.setattr(launch.subprocess, 'Popen', lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match='cgroup-v2'): launch.main()
    assert not calls
