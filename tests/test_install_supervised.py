"""Offline additive-installer pins, boundaries, and ordering. No host actions."""
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/install-supervised.py'
spec = importlib.util.spec_from_file_location('install_supervised_test', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
OWNER = os.geteuid()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def capsule(tmp_path):
    folder = tmp_path / 'capsule'
    folder.mkdir()
    case = 'base-capture-retention'
    files = {'install-supervised.py': SCRIPT.read_bytes(),
             'configs/suite.json': encoded({'schema_version': 1, 'configurations': [case + '-acceptance.json']})}
    for name in installer.SIDE_FILES - {'files.json'}:
        source = ROOT / ('probe_core/' if name in {'ssh_job_client.py', 'supervised_runner.py'} else 'deploy/gpu/' if name == 'public-job.py' else 'deploy/') / name
        files['sidecar/' + name] = source.read_bytes()
    files['sidecar/files.json'] = encoded({name: 'sha256:' + installer.sha(files['sidecar/' + name])
                                          for name in installer.MODULES})
    for name in installer.UNITS:
        files['units/' + name] = (ROOT / 'deploy' / name).read_bytes()
    files['configs/' + case + '-plan.json'] = b'{"fixed":"plan"}'
    files['configs/' + case + '-worker.json'] = b'{"fixed":"worker"}'
    config = {'helper_path': installer.SIDECAR + '/public-job.py',
        'helper_sha256': 'sha256:' + installer.sha(files['sidecar/public-job.py']),
        'plan_path': installer.CONFIG + '/' + case + '-plan.json',
        'plan_sha256': 'sha256:' + installer.sha(files['configs/' + case + '-plan.json']),
        'worker_config_path': installer.CONFIG + '/' + case + '-worker.json',
        'worker_config_sha256': 'sha256:' + installer.sha(files['configs/' + case + '-worker.json']),
        'submission_state_directory': '/var/lib/probe-supervised-submit/' + case,
        'trusted_state_directory': '/var/lib/probe-core/supervised/' + case,
        'service_uid': 991, 'research_uid': 992, 'admin_uid': 1000}
    files['configs/' + case + '-acceptance.json'] = encoded(config)
    for name, raw in files.items():
        target = folder / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw)
    manifest = folder / 'manifest.json'
    manifest.write_bytes(encoded({'schema_version': 1, 'files': {name: installer.sha(raw) for name, raw in files.items()}}))
    return SimpleNamespace(folder=folder, manifest=manifest, checksum=installer.sha(manifest.read_bytes()), files=files,
                           case=case, config=config)


def test_capsule_captures_pinned_bytes_before_user_path_can_change(tmp_path):
    offer = capsule(tmp_path)
    captured = installer.read_capsule(offer.manifest, offer.checksum)
    (offer.folder / 'sidecar/public-job.py').write_bytes(b'changed after read')
    assert captured == offer.files
    assert list(installer.describe(captured)) == [offer.case]
    with pytest.raises(ValueError, match='MEMBER_HASH_MISMATCH'):
        installer.read_capsule(offer.manifest, offer.checksum)


@pytest.mark.parametrize('mutation', ['manifest', 'file', 'extra', 'symlink', 'hardlink', 'directory_symlink', 'unknown_path'])
def test_capsule_refuses_unpinned_or_unsafe_input(tmp_path, mutation):
    offer = capsule(tmp_path)
    target = offer.folder / 'sidecar/public-job.py'
    if mutation == 'manifest': offer.manifest.write_bytes(offer.manifest.read_bytes() + b' ')
    if mutation == 'file': target.write_bytes(b'changed')
    if mutation == 'extra': (offer.folder / 'unreviewed-token').write_bytes(b'not a credential, but forbidden')
    if mutation == 'symlink':
        target.unlink(); target.symlink_to(SCRIPT)
    if mutation == 'hardlink': os.link(target, tmp_path / 'second-link')
    if mutation == 'directory_symlink':
        (offer.folder / 'sidecar').rename(tmp_path / 'linked-sidecar')
        (offer.folder / 'sidecar').symlink_to(tmp_path / 'linked-sidecar')
    if mutation == 'unknown_path':
        manifest = json.loads(offer.manifest.read_text())
        manifest['files']['../outside'] = 'a' * 64
        offer.manifest.write_bytes(encoded(manifest)); offer.checksum = installer.sha(offer.manifest.read_bytes())
    with pytest.raises((ValueError, OSError)):
        installer.read_capsule(offer.manifest, offer.checksum)


@pytest.mark.parametrize('field,value', [
    ('helper_path', '/tmp/helper.py'), ('plan_path', '/etc/probe-core/runpod.json'),
    ('worker_config_path', '/etc/probe-core/worker-token'), ('helper_sha256', 'sha256:' + 'a' * 64),
    ('submission_state_directory', '/var/lib/probe-core'), ('trusted_state_directory', '/root'),
    ('service_uid', 0), ('research_uid', True),
])
def test_case_paths_pins_and_identity_fields_are_bounded(tmp_path, field, value):
    offer = capsule(tmp_path)
    offer.config[field] = value
    offer.files['configs/' + offer.case + '-acceptance.json'] = encoded(offer.config)
    with pytest.raises(ValueError):
        installer.describe(offer.files)


@pytest.mark.parametrize('replacement', [
    ('User=probe-trusted', 'User=root'),
    ('NoNewPrivileges=yes', 'NoNewPrivileges=no'),
    ('ProtectSystem=strict', 'ProtectSystem=no'),
    ('ReadWritePaths=/var/lib/probe-core /var/lib/probe-provider', 'ReadWritePaths=/'),
    ('Type=simple', 'ExecStartPre=/bin/sh -c evil\nType=simple'),
])
def test_unit_cannot_add_root_execution_or_expand_writes(tmp_path, replacement):
    offer = capsule(tmp_path)
    name = 'units/probe-supervised-run.service'
    offer.files[name] = offer.files[name].replace(*(item.encode() for item in replacement))
    with pytest.raises(ValueError):
        installer.describe(offer.files)


class FakeInstaller(installer.Installer):
    def __init__(self, root):
        super().__init__(root=root, owner=OWNER)
        self.actions = []
        for absolute in ('/opt/probe-core', '/etc/systemd/system', '/var/lib/probe-core'):
            path = self.path(absolute)
            path.mkdir(parents=True, exist_ok=True)
            for parent in (path, *path.parents):
                if parent == root: break
                parent.chmod(0o755)
    def accounts(self, cases):
        self.actions.append('accounts')
        return {name: SimpleNamespace(pw_uid=OWNER, pw_gid=os.getegid())
                for name in ('probe-trusted', 'probe-research', 'probe-watchdog')}
    def runtime(self, accounts):
        self.actions.append('runtime')
    def preflight(self, cases):
        for name in installer.SIDE_FILES:
            path = self.path(installer.SIDECAR + '/' + name)
            assert path.is_file() and path.stat().st_mode & 0o777 == 0o644
        self.actions.append('identity-preflight')
    def execute(self, command, **kwargs):
        self.actions.append(tuple(command))
        return ''


def test_install_only_adds_sidecar_and_starts_after_both_preflights(tmp_path):
    offer = capsule(tmp_path)
    root = tmp_path / 'host'; root.mkdir()
    operation = FakeInstaller(root)
    untouched = operation.path('/opt/probe-core/installed-wheel.whl')
    untouched.write_bytes(b'existing wheel bytes')
    result = operation.install(offer.files, offer.checksum)
    assert result['case_count'] == 1 and not result['wheel_changed']
    assert not result['automatic_approval'] and not result['automatic_compute_start']
    assert untouched.read_bytes() == b'existing wheel bytes'
    assert operation.actions == ['accounts', 'runtime', 'identity-preflight', 'runtime',
                                ('/usr/bin/systemctl', 'daemon-reload'),
                                ('/usr/bin/systemctl', 'start', 'probe-supervised-run.service')]
    for base in ('/var/lib/probe-core/supervised', '/var/lib/probe-supervised-submit'):
        assert operation.path(base + '/' + offer.case).stat().st_mode & 0o777 == 0o700
    # Exact previously installed bytes are reusable; no enabled/autoboot action.
    operation.actions.clear()
    operation.install(offer.files, offer.checksum)
    assert not any('enable' in action for action in operation.actions)


@pytest.mark.parametrize('location', ['sidecar/public-job.py', 'configs/suite.json', 'units/probe-supervised-run.service'])
def test_conflict_is_detected_before_any_publication_or_start(tmp_path, location):
    offer = capsule(tmp_path)
    root = tmp_path / 'host'; root.mkdir()
    operation = FakeInstaller(root)
    prefix = {'sidecar': installer.SIDECAR, 'configs': installer.CONFIG, 'units': installer.SYSTEMD}
    directory, name = location.split('/')
    path = operation.path(prefix[directory] + '/' + name)
    path.parent.mkdir(exist_ok=True)
    path.parent.chmod(0o755)
    path.write_bytes(b'existing different bytes')
    path.chmod(0o644)
    with pytest.raises(ValueError, match='EXISTING_FILE_DIFFERS'):
        operation.install(offer.files, offer.checksum)
    assert path.read_bytes() == b'existing different bytes'
    assert operation.actions == ['accounts', 'runtime']
    assert not operation.path('/var/lib/probe-supervised-submit').exists()


def test_failed_account_preflight_never_reloads_or_starts_units(tmp_path, monkeypatch):
    offer = capsule(tmp_path)
    root = tmp_path / 'host'; root.mkdir()
    operation = FakeInstaller(root)
    def fail(_): raise installer.InstallError('fixture-preflight-failed')
    monkeypatch.setattr(operation, 'preflight', fail)
    with pytest.raises(ValueError, match='fixture-preflight-failed'):
        operation.install(offer.files, offer.checksum)
    assert not any(isinstance(action, tuple) for action in operation.actions)
    assert operation.path(installer.SIDECAR + '/public-job.py').read_bytes() == offer.files['sidecar/public-job.py']


def test_preflight_uses_actual_accounts_and_load_inputs_not_main(tmp_path):
    offer = capsule(tmp_path)
    operation = installer.Installer(root=tmp_path, owner=OWNER)
    calls = []
    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps({'passed': True, 'count': 1, 'mode': command[-1]})
    operation.execute = execute
    operation.preflight(installer.describe(offer.files))
    assert len(calls) == 2
    assert [call[0][2] for call in calls] == ['probe-research', 'probe-trusted']
    for command, kwargs in calls:
        assert command[6:9] == [installer.PYTHON, '-I', '-c']
        assert 'runner.load_inputs' in command[-2] and 'entry.load()' in command[-2]
        assert '.main(' not in command[-2]
        assert json.loads(kwargs['input']) == [installer.CONFIG + '/' + offer.case + '-acceptance.json']


@pytest.mark.parametrize('matches', [True, False])
def test_trusted_preflight_compares_installed_launch_without_provider_call(tmp_path, monkeypatch, capsys, matches):
    from probe_core.runpod_provider import RunPodConfig
    entry = tmp_path / 'supervised-entry.py'
    entry.write_text('from types import SimpleNamespace as NS\n'
        'def load():\n'
        '    config=NS(provider_config_path="/etc/probe-core/runpod.json",deployment=NS(launch_config_hash="sha256:expected",image_repository="ghcr.io/worker"))\n'
        '    return NS(load_inputs=lambda path,mode:(config,None,None))\n')
    calls = []
    def load(path):
        calls.append(str(path))
        return SimpleNamespace(launch=SimpleNamespace(digest='sha256:expected' if matches else 'sha256:changed',
                                                      image_repository='ghcr.io/worker'))
    monkeypatch.setattr(RunPodConfig, 'load', load)
    monkeypatch.setattr(sys, 'argv', ['preflight', 'run'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO('["/etc/probe-supervised/base-acceptance.json"]'))
    source = installer.PREFLIGHT.replace("pathlib.Path('/opt/probe-core/supervised')", f'pathlib.Path({str(tmp_path)!r})')
    if matches:
        exec(compile(source, '<exact-preflight-with-local-entry>', 'exec'), {})
        assert json.loads(capsys.readouterr().out) == {'passed': True, 'count': 1, 'mode': 'run'}
    else:
        with pytest.raises(ValueError, match='INSTALLED_PROVIDER_LAUNCH_MISMATCH'):
            exec(compile(source, '<exact-preflight-with-local-entry>', 'exec'), {})
    assert calls == ['/etc/probe-core/runpod.json']


def test_state_directory_cannot_follow_service_owned_symlink(tmp_path):
    root = tmp_path / 'host'; root.mkdir()
    operation = FakeInstaller(root)
    outside = tmp_path / 'outside'; outside.mkdir()
    operation.path('/var/lib/probe-core/supervised').symlink_to(outside)
    with pytest.raises(OSError):
        operation.directory(operation.path('/var/lib/probe-core/supervised/case'), OWNER, os.getegid(), 0o700)
    assert list(outside.iterdir()) == []


def test_start_command_gets_full_submission_timeout_without_leaking_stderr(tmp_path):
    calls = []
    def run(command, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=1, stdout='', stderr='fixture private text must not escape')
    operation = installer.Installer(root=tmp_path, owner=OWNER, run=run)
    operation.stage = 'suite_start'
    with pytest.raises(ValueError, match='^COMMAND_FAILED_SUITE_START$'):
        operation.execute(['/usr/bin/systemctl', 'start', 'probe-supervised-run.service'], timeout=330)
    assert calls[0]['timeout'] == 330


def test_runtime_checks_real_service_groups_and_process_ids(tmp_path):
    operation = installer.Installer(root=tmp_path, owner=OWNER)
    interpreter = operation.path(installer.PYTHON)
    interpreter.parent.mkdir(parents=True); interpreter.write_bytes(b'fixture')
    accounts = {'probe-trusted': SimpleNamespace(pw_uid=991), 'probe-research': SimpleNamespace(pw_uid=992),
                'probe-watchdog': SimpleNamespace(pw_uid=993)}
    states = {}
    for index, (name, (user, group)) in enumerate(installer.CORE_UNITS.items(), 100):
        states[name] = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running',
                        'User': user, 'Group': group, 'MainPID': str(index)}
        status = operation.path('/proc/' + str(index) + '/status')
        status.parent.mkdir(parents=True)
        status.write_text('Uid:\t' + '\t'.join([str(accounts[user].pw_uid)] * 4) + '\n')
    for name in (*installer.UNITS, 'probe-calibration-run.service', 'probe-calibration-submit.service'):
        states[name] = {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0', 'DropInPaths': ''}
    operation.show = lambda name: states[name]
    operation.execute = lambda *args, **kwargs: ''
    operation.runtime(accounts)
    states['probe-provider-stop.service']['Group'] = 'probe-trusted'
    with pytest.raises(ValueError, match='CORE_SERVICE_IDENTITY_UNAVAILABLE'):
        operation.runtime(accounts)
