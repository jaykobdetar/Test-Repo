from types import SimpleNamespace
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
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
    monkeypatch.setattr(launch, '_fresh_environment', lambda *args: None)
    monkeypatch.setattr(launch, '_bootstrap_deadline', lambda: time.time()+300)
    def incompatible(*args): raise ValueError('cgroup-v2 namespace required')
    monkeypatch.setattr(launch, '_prepare_worker_cgroups', incompatible)
    calls = []
    monkeypatch.setattr(launch.subprocess, 'Popen', lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match='cgroup-v2'): launch.main()
    assert not calls


@pytest.mark.parametrize('mode,arguments', [
    ('bootstrap', []), ('configure', ['--configure', '/run/probe-worker-bootstrap.json']),
    ('check-paths', ['--check-paths', '/workspace/probe/config/worker.json', '/workspace/probe/config/worker-token']),
])
@pytest.mark.parametrize('unset_first', [False, True])
def test_real_exec_removes_secret_bytes_and_preserves_only_role_bindings(mode, arguments, unset_first, tmp_path):
    """Actual exec and same-UID /proc reads; the privileged UID boundary is separate."""
    project = str(Path(launch.__file__).resolve().parents[1])
    child_code = """
import json, os
from pathlib import Path
raw = Path('/proc/self/environ').read_bytes()
parent = Path('/proc') / str(os.getppid()) / 'environ'
print(json.dumps({'environment': dict(os.environ), 'initial': raw.decode().split('\\0'),
                  'parent_initial': parent.read_bytes().decode().split('\\0'),
                  'uid': os.getuid(), 'parent_uid': parent.stat().st_uid}))
"""
    observer = f"""
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, {project!r})
from probe_core import gpu_launch as launch
launch.GPU_LIBRARY_DIRECTORIES = ()  # This CPU subprocess has no NVIDIA image mounts.
environment = dict(os.environ)
initial = launch._initial_environment()
child = subprocess.run([sys.executable, '-I', '-c', {child_code!r}],
    env=launch._environment('worker'), check=True, capture_output=True, text=True)
print(json.dumps({{'environment':environment, 'initial':initial, 'pid':os.getpid(),
                  'uid':os.getuid(), 'isolated':sys.flags.isolated, 'child':json.loads(child.stdout)}}))
"""
    code = f"""
import os, sys
sys.path.insert(0, {project!r})
from probe_core import gpu_launch as launch
launch.TRUSTED_PYTHON = sys.executable
launch.GPU_LIBRARY_DIRECTORIES = ()
real_exec = os.execve
def observed_exec(executable, arguments, environment):
    assert executable == sys.executable
    assert arguments == [sys.executable, '-I', '-m', 'probe_core.gpu_launch', *{arguments!r}]
    real_exec(executable, [executable, '-I', '-c', {observer!r}], environment)
launch.os.execve = observed_exec
if {unset_first!r}:
    clean = launch._environment({mode!r})
    os.environ.clear()
    os.environ.update(clean)
    assert launch._initial_environment() != clean
launch._fresh_environment({mode!r}, {arguments!r})
raise AssertionError('initial process did not exec')
"""
    bindings = {'PROBE_ABSOLUTE_DEADLINE': '2027-01-01T00:00:00+00:00',
                'PROBE_WORKER_ID': 'test-worker', 'PROBE_REQUEST_ID': 'test-request',
                'PROBE_CONFIGURATION_HASH': 'sha256:'+'a'*64, 'PUBLIC_KEY': 'ssh-ed25519 AAAAsynthetic'}
    hostile = {'RUNPOD_API_KEY': 'synthetic-provider-secret', 'HF_TOKEN': 'synthetic-hf-secret',
               'AWS_SECRET_ACCESS_KEY': 'synthetic-aws-secret', 'HTTP_PROXY': 'http://synthetic-proxy',
               'PROBE_ENV_CLEAN': '1', 'PYTHONPATH': str(tmp_path), 'PYTHONSTARTUP': '/synthetic/startup',
               'LD_LIBRARY_PATH': str(tmp_path), 'CUDA_VISIBLE_DEVICES': '0', **bindings}
    (tmp_path/'sitecustomize.py').write_text("raise RuntimeError('untrusted Python startup executed')\n")
    with subprocess.Popen([sys.executable, '-I', '-c', code], env=hostile,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result['pid'] == process.pid and result['isolated'] == 1
    assert result['environment'] == result['initial']
    assert result['environment']['CUDA_VISIBLE_DEVICES'] == '0'
    assert result['environment']['HOME'] == ('/home/probe-worker' if mode == 'check-paths' else '/root')
    for key, value in bindings.items():
        assert (result['environment'].get(key) == value) == (mode == 'bootstrap')
    for forbidden in ('RUNPOD_API_KEY', 'HF_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'HTTP_PROXY',
                      'PROBE_ENV_CLEAN', 'PYTHONPATH', 'PYTHONSTARTUP'):
        assert forbidden not in result['environment']
        assert forbidden not in result['child']['environment']
    assert str(tmp_path) not in result['environment'].get('LD_LIBRARY_PATH', '')
    assert result['child']['uid'] == result['uid'] == result['child']['parent_uid']
    assert not set(bindings).intersection(result['child']['environment'])
    assert result['child']['environment']['HF_HUB_OFFLINE'] == '1'
    for secret in ('synthetic-provider-secret', 'synthetic-hf-secret', 'synthetic-aws-secret'):
        assert secret not in json.dumps(result)


@pytest.mark.parametrize('arguments', [['--bad'], ['--configure'], ['--configure', 'a', 'b'],
                                      ['--check-paths', 'a'], ['--check-paths', 'a', 'b', 'c']])
def test_unknown_modes_refuse_before_exec_or_bootstrap(arguments, monkeypatch):
    monkeypatch.setattr(launch.sys, 'argv', ['gpu_launch', *arguments])
    monkeypatch.setattr(launch, '_fresh_environment', lambda *args: pytest.fail('unexpected exec'))
    with pytest.raises(ValueError, match='unsupported bootstrap command'):
        launch.main()


@pytest.mark.parametrize('value', ['0,1', '', '../../dev/nvidia0', 'all', '$(injected)'])
def test_invalid_gpu_selector_is_not_inherited(value):
    with pytest.raises(ValueError, match='single-GPU'):
        launch._environment('worker', {'CUDA_VISIBLE_DEVICES': value})


def test_library_search_path_must_be_root_controlled(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, 'GPU_LIBRARY_DIRECTORIES', (str(tmp_path),))
    with pytest.raises(ValueError, match='root controlled'):
        launch._environment('worker', {})


def test_clean_environment_is_not_reexecuted(monkeypatch):
    monkeypatch.setattr(launch, 'GPU_LIBRARY_DIRECTORIES', ())
    clean = launch._environment('worker', {})
    monkeypatch.setattr(launch.os, 'environ', clean)
    monkeypatch.setattr(launch, '_initial_environment', lambda: clean.copy())
    monkeypatch.setattr(launch, 'TRUSTED_PYTHON', sys.executable)
    monkeypatch.setattr(launch.sys, 'flags', SimpleNamespace(isolated=1))
    monkeypatch.setattr(launch.os, 'execve', lambda *args: pytest.fail('clean process reexecuted'))
    launch._fresh_environment('check-paths', ['--check-paths', 'a', 'b'])


@pytest.mark.parametrize('readable', [False, True])
def test_path_preflight_requires_kernel_denial_of_root_parent_environment(monkeypatch, readable):
    expected = Path('/proc') / str(os.getppid()) / 'environ'
    monkeypatch.setattr(launch, 'ROOT_UID', expected.parent.stat().st_uid)
    opened, closed = [], []
    def open_parent(path, flags):
        assert path == expected and flags & os.O_NOFOLLOW
        opened.append(path)
        if not readable:
            raise PermissionError('synthetic kernel denial')
        return 54321
    monkeypatch.setattr(launch.os, 'open', open_parent)
    monkeypatch.setattr(launch.os, 'close', closed.append)
    if readable:
        with pytest.raises(ValueError, match='can read root bootstrap'):
            launch._root_parent_environment_private()
        assert closed == [54321]
    else:
        launch._root_parent_environment_private()
        assert not closed
    assert opened == [expected]


def test_resource_gates_and_every_supervisor_restart_use_clean_worker_environment(configured_fixture, monkeypatch, tmp_path):
    launch.configure(launch.CONFIG_BUNDLE)
    monkeypatch.setattr(launch, 'GPU_LIBRARY_DIRECTORIES', ())
    monkeypatch.setattr(launch, 'SUPERVISOR_PID', tmp_path/'supervisor.pid')
    for key in ('RUNPOD_API_KEY', 'HF_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'PUBLIC_KEY', 'PROBE_REQUEST_ID', 'HTTP_PROXY'):
        monkeypatch.setenv(key, 'synthetic-secret')
    calls, workers = [], []
    scope = object()
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        worker = SimpleNamespace(pid=1000+len(workers), poll=lambda: 1, wait=lambda **kw: 1)
        workers.append(worker)
        return worker
    monkeypatch.setattr(launch.subprocess, 'run', run)
    monkeypatch.setattr(launch.subprocess, 'Popen', popen)
    monkeypatch.setattr(launch.time, 'sleep', lambda _: None)
    launch._run_numerical_worker(scope, time.time()+300, SimpleNamespace(poll=lambda: None), lambda: False)
    assert len(workers) == 4 and len(calls) == 7
    for command, kwargs in calls:
        assert command[:2] == [launch.TRUSTED_PYTHON, '-I']
        assert kwargs['preexec_fn'].args == (scope,)
        assert kwargs['preexec_fn'].func.__name__ == 'enter_supervisor_and_drop'
        environment = kwargs['env']
        assert environment == launch._environment('worker')
        assert environment['HOME'] == '/home/probe-worker'
        assert not any(key.startswith(('PROBE_', 'RUNPOD_', 'AWS_')) for key in environment)
        assert not {'PUBLIC_KEY', 'HF_TOKEN', 'HTTP_PROXY'}.intersection(environment)
    assert (tmp_path/'supervisor.pid').read_text() == '1003\n'


def test_ssh_and_keygen_receive_only_fixed_root_environment(tmp_path, monkeypatch):
    real_path = Path
    monkeypatch.setattr(launch, 'ROOT_UID', os.getuid())
    monkeypatch.setattr(launch.sys, 'argv', ['gpu_launch'])
    monkeypatch.setattr(launch, '_fresh_environment', lambda *args: None)
    monkeypatch.setattr(launch, '_bootstrap_deadline', lambda: time.time()+300)
    monkeypatch.setattr(launch, '_prepare_worker_cgroups', lambda *args: object())
    monkeypatch.setattr(launch, '_run_numerical_worker', lambda *args: None)
    monkeypatch.setattr(launch.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(launch, 'Path', lambda value: tmp_path/'ssh' if value == '/root/.ssh' else
                        tmp_path/'sshd_config' if value == '/etc/ssh/sshd_config.probe-worker' else real_path(value))
    monkeypatch.setenv('PUBLIC_KEY', 'ssh-ed25519 AAAAsynthetic')
    monkeypatch.setenv('PROBE_REQUEST_ID', 'request')
    monkeypatch.setenv('HTTP_PROXY', 'http://synthetic-proxy')
    calls = []
    def record(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(poll=lambda: 0, wait=lambda **kw: 0)
    monkeypatch.setattr(launch.subprocess, 'run', record)
    monkeypatch.setattr(launch.subprocess, 'Popen', record)
    launch.main()
    assert len(calls) == 4
    assert [command[0] for command, _ in calls] == ['/usr/bin/ssh-keygen']*3+['/usr/sbin/sshd']
    for _, kwargs in calls:
        assert kwargs['env'] == launch._environment('ssh', {})
        assert not {'PUBLIC_KEY', 'PROBE_REQUEST_ID', 'HTTP_PROXY'}.intersection(kwargs['env'])


def test_image_uses_isolated_absolute_bootstrap_interpreter():
    dockerfile = Path(__file__).resolve().parents[1]/'deploy/gpu/Dockerfile.worker'
    assert 'ENTRYPOINT ["/opt/probe-core/venv/bin/python", "-I", "-m", "probe_core.gpu_launch"]' in dockerfile.read_text()


@pytest.mark.parametrize('invalid', [None, 'expired', 'too_long', 'naive', 'binding', 'missing'])
def test_clean_bootstrap_still_validates_original_deadline_and_bindings(tmp_path, monkeypatch, invalid):
    now = time.time()
    deadline = datetime.fromtimestamp(now+600, timezone.utc).isoformat()
    bindings = {'PROBE_ABSOLUTE_DEADLINE': deadline, 'PROBE_WORKER_ID': 'worker',
                'PROBE_REQUEST_ID': 'request', 'PROBE_CONFIGURATION_HASH': 'sha256:'+'a'*64}
    if invalid == 'expired': bindings['PROBE_ABSOLUTE_DEADLINE'] = datetime.fromtimestamp(now-1, timezone.utc).isoformat()
    if invalid == 'too_long': bindings['PROBE_ABSOLUTE_DEADLINE'] = datetime.fromtimestamp(now+901, timezone.utc).isoformat()
    if invalid == 'naive': bindings['PROBE_ABSOLUTE_DEADLINE'] = '2027-01-01T00:00:00'
    if invalid == 'binding': bindings['PROBE_REQUEST_ID'] = 'request\nother'
    if invalid == 'missing': del bindings['PROBE_REQUEST_ID']
    monkeypatch.setattr(launch, 'GPU_LIBRARY_DIRECTORIES', ())
    monkeypatch.setattr(launch.os, 'environ', launch._environment('bootstrap', bindings))
    monkeypatch.setattr(launch, 'BOOTSTRAP_STATE', tmp_path/'bootstrap.json')
    published = []
    monkeypatch.setattr(launch, '_publish', lambda path, raw: published.append((path, json.loads(raw))))
    if invalid:
        with pytest.raises((ValueError, KeyError)):
            launch._bootstrap_deadline()
        assert not published
    else:
        assert launch._bootstrap_deadline() == datetime.fromisoformat(deadline).timestamp()
        assert published == [(launch.BOOTSTRAP_STATE, {'deadline': datetime.fromisoformat(deadline).timestamp(),
                              **{key: value for key, value in bindings.items() if key != 'PROBE_ABSOLUTE_DEADLINE'}})]


def test_standard_library_mount_is_kept_but_inherited_search_path_is_ignored(monkeypatch):
    # The tool sandbox remaps the host root UID; model that read-only metadata,
    # while production requires container root UID0 for every path component.
    directory = Path('/usr/lib').resolve()
    monkeypatch.setattr(launch, 'ROOT_UID', directory.stat().st_uid)
    monkeypatch.setattr(launch, 'GPU_LIBRARY_DIRECTORIES', (str(directory),))
    result = launch._gpu_environment({'LD_LIBRARY_PATH': '/untrusted', 'NVIDIA_VISIBLE_DEVICES': 'all'})
    assert result == {'LD_LIBRARY_PATH': str(directory)}


def test_failure_receipt_contains_only_fixed_code_type_and_local_line():
    secret = 'synthetic-provider-secret-must-not-be-logged'
    try:
        exec(compile('\nraise ValueError(secret)\n', launch.__file__, 'exec'), {'secret': secret})
    except ValueError as error:
        receipt = launch._failure_receipt(error)
    assert receipt == {'status': 'failed', 'code': 'WORKER_BOOTSTRAP_FAILED',
                       'error_type': 'ValueError', 'bootstrap_line': 2}
    assert secret not in json.dumps(receipt) and launch.__file__ not in json.dumps(receipt)


def test_module_failure_emits_safe_receipt_without_traceback():
    project = str(Path(launch.__file__).resolve().parents[1])
    code = (f'import sys, runpy; sys.path.insert(0, {project!r}); '
            "sys.argv=['gpu_launch','synthetic-secret-unsupported']; "
            "runpy.run_module('probe_core.gpu_launch',run_name='__main__')")
    result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 1 and not result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['code'] == 'WORKER_BOOTSTRAP_FAILED' and receipt['error_type'] == 'ValueError'
    assert type(receipt['bootstrap_line']) is int and receipt['bootstrap_line'] > 0
    assert 'synthetic-secret' not in result.stdout
