"""Local file/process fixtures only: no installed actions, provider calls or credentials."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('activate_calibration', PROJECT / 'deploy/activate-gpu-calibration.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
NOW = datetime.now(timezone.utc)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def put(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o600)


def gate():
    return dict(passed=True, checks={f'check-{i}': True for i in range(26)}, failure_codes=[], check_count=26,
                passed_count=26, normal_research_audit_events=2, paid_actions_performed=False)


@pytest.fixture
def package(tmp_path):
    path = tmp_path / 'package' / 'manifest.json'
    bodies = {name: b'fixture-' + name.encode() for name in a.FILES}
    manifest = dict(schema_version=1, storage_checked_at=NOW.isoformat(), upgrade_directory='/opt/probe-core/upgrades/'+'a'*64,
                    identity_checker_sha256='b'*64, files={name: digest(raw) for name, raw in bodies.items()})
    for name, raw in bodies.items():
        put(path.parent / name, raw)
    raw = a.encoded(manifest)
    put(path, raw)
    return SimpleNamespace(path=path, raw=raw, manifest=manifest, bodies=bodies)


def test_all_pins_verified_and_worker_json_required(package):
    raw, manifest, bodies = a.manifest_inputs(package.path, digest(package.raw), NOW)
    assert raw == package.raw and bodies == package.bodies and 'worker.json' in bodies


@pytest.mark.parametrize('kind', ['wrong_pin', 'modified_file', 'symlink', 'hardlink', 'extra_file_entry', 'missing_worker', 'stale', 'future'])
def test_package_refusals_leave_every_input_unchanged(package, kind):
    manifest = deepcopy(package.manifest)
    pin = digest(package.raw)
    target = package.path.parent / 'plan.json'
    if kind == 'wrong_pin':
        pin = 'f'*64
    elif kind == 'modified_file':
        target.write_bytes(b'changed')
    elif kind == 'symlink':
        target.unlink()
        target.symlink_to(package.path.parent / 'worker.json')
    elif kind == 'hardlink':
        target.unlink()
        os.link(package.path.parent / 'worker.json', target)
    else:
        if kind == 'extra_file_entry':
            manifest['files']['../elsewhere'] = 'a'*64
        elif kind == 'missing_worker':
            del manifest['files']['worker.json']
        else:
            manifest['storage_checked_at'] = (NOW + timedelta(seconds=1) if kind == 'future' else NOW - timedelta(days=2)).isoformat()
        put(package.path, a.encoded(manifest))
        pin = digest(package.path.read_bytes())
    before = {p.name: (p.lstat().st_mode, p.read_bytes()) for p in package.path.parent.iterdir()}
    with pytest.raises((a.ActivationError, OSError)):
        a.manifest_inputs(package.path, pin, NOW)
    assert before == {p.name: (p.lstat().st_mode, p.read_bytes()) for p in package.path.parent.iterdir()}


@pytest.mark.parametrize('field,value', [('passed', False), ('check_count', 25), ('paid_actions_performed', True),
                                        ('normal_research_audit_events', 0), ('failure_codes', ['FAILED'])])
def test_identity_report_must_pass_every_required_check(field, value):
    report = gate()
    report[field] = value
    with pytest.raises(a.ActivationError, match='IDENTITY_GATE_FAILED'):
        a.identity_gate(report)


def test_identity_false_check_rejected_even_when_counts_claim_pass():
    report = gate()
    report['checks']['check-0'] = False
    with pytest.raises(a.ActivationError):
        a.identity_gate(report)


@pytest.mark.parametrize('mode', ['submit', 'run'])
def test_exact_real_units_pass_and_other_executable_is_refused(mode):
    raw = (PROJECT / f'deploy/probe-calibration-{mode}.service').read_bytes()
    a.validate_unit(raw, mode)
    for changed in (raw.replace(b' -I -m ', b' -m '), raw.replace(b'NoNewPrivileges=yes', b'NoNewPrivileges=no'),
                    raw.replace(b'ExecStart=', b'ExecStartPre=/bin/true\nExecStart=')):
        with pytest.raises(a.ActivationError):
            a.validate_unit(changed, mode)


def test_private_key_descriptor_fallback_supports_real_ssh_keygen(tmp_path, monkeypatch):
    key = tmp_path / 'key'
    subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    monkeypatch.delattr(os, 'memfd_create', raising=False)
    fd = a.private_key_fd(key.read_bytes())
    try:
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        result = subprocess.run(['/usr/bin/ssh-keygen', '-y', '-P', '', '-f', f'/proc/self/fd/{fd}'], pass_fds=(fd,), capture_output=True, check=True)
        assert result.stdout.split()[:2] == key.with_suffix('.pub').read_bytes().split()[:2]
    finally:
        os.close(fd)


def test_history_reader_is_literal_and_never_imported():
    assert a.extract_reader(b'STATE_READER = "sentinel"\nraise RuntimeError("must not run")') == 'sentinel'
    for raw in (b'STATE_READER = str("x")', b'STATE_READER="x"\nSTATE_READER="y"'):
        with pytest.raises(a.ActivationError):
            a.extract_reader(raw)


@pytest.fixture
def orchestration(tmp_path, monkeypatch):
    for name, path in {'ROOT': 'opt', 'CONFIG': 'etc/core', 'UNITS': 'etc/units', 'PUBLIC': 'etc/calibration',
                       'SUBMIT': 'var/submit', 'STATE': 'var/core/acceptance'}.items():
        monkeypatch.setattr(a, name, tmp_path / path)
    for path in (a.ROOT, a.CONFIG, a.UNITS, a.SUBMIT.parent, a.STATE.parent):
        path.mkdir(parents=True, exist_ok=True)
    before_provider, before_research = b'{"old_provider":true}', b'{"sandbox_image":"sha256:abc","policy":{}}'
    put(a.CONFIG / 'runpod.json', before_provider)
    put(a.CONFIG / 'research.json', before_research)
    checker = b'STATE_READER="fixture"\n'
    upgrade = a.ROOT / 'upgrades' / ('a'*64)
    put(upgrade / 'upgrade-report.json', a.encoded(dict(status='passed', sandbox_checks=16, identity_checks=26,
          cloud_mutations_performed=False, wheel_sha256='a'*64)))
    put(upgrade / 'upgrade-release.json', a.encoded(dict(wheel_sha256='a'*64, identity_checker_sha256=digest(checker))))
    put(upgrade / 'verify-installed-identities.py', checker)
    put(upgrade / 'identity-acceptance.json', a.encoded(gate()))
    path = tmp_path / 'package/manifest.json'
    bodies = {name: b'fixture-' + name.encode() for name in a.FILES}
    for name, raw in bodies.items():
        put(path.parent / name, raw)
    manifest = dict(schema_version=1, storage_checked_at=NOW.isoformat(), upgrade_directory=str(upgrade),
                    identity_checker_sha256=digest(checker), files={name: digest(raw) for name, raw in bodies.items()})
    put(path, a.encoded(manifest))
    args = SimpleNamespace(manifest=path, manifest_sha256=digest(path.read_bytes()), human='human')
    monkeypatch.setattr(a, 'trusted', lambda *args, **kwargs: None)  # Temp fixtures do not own root's real ancestors.
    original_read, original_write = a.read_file, a.write_file
    monkeypatch.setattr(a, 'read_file', lambda path, **kwargs: original_read(path, owner=None))
    def write(path, raw, **kwargs):
        original_write(path, raw, uid=os.geteuid(), gid=os.getegid(), mode=kwargs['mode'])
    monkeypatch.setattr(a, 'write_file', write)
    monkeypatch.setattr(a.os, 'chown', lambda *args: None)
    monkeypatch.setattr(a.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid={'probe-trusted': 994, 'probe-research': 993, 'human': 1000}[name]))
    monkeypatch.setattr(a.grp, 'getgrnam', lambda name: SimpleNamespace(gr_gid=982))
    monkeypatch.setattr(a, 'configurations', lambda *args: (b'{"new_provider":true}', b'{"new_research":true}', 'ssh-ed25519 AAAA'))
    monkeypatch.setattr(a, 'validate_unit', lambda *args: None)
    events = []
    class Fake(a.Activation):
        def command(self, command, **kwargs):
            events.append(tuple(command))
            if command[0].endswith('ssh-keygen'):
                return b'ssh-ed25519 AAAA\n'
            if '--output' in command:
                write(Path(command[-1]), a.encoded(gate()), mode=0o600)
            return b''
        def idle(self):
            events.append(('idle',))
            return {'idle': True}
        def ready(self):
            events.append(('ready',))
    instance = Fake()
    return SimpleNamespace(instance=instance, args=args, events=events, write=write,
                           before_provider=before_provider, before_research=before_research)


def test_activation_starts_only_after_new_gate_and_never_enables_units(orchestration):
    s = orchestration
    report = s.instance.execute(s.args)
    assert report['status'] == 'passed' and report['approval_issued'] is False
    checker = next(i for i, cmd in enumerate(s.events) if '--output' in cmd)
    submit = s.events.index(('/usr/bin/systemctl', 'start', a.CALIBRATION[0]))
    runner = s.events.index(('/usr/bin/systemctl', 'start', a.CALIBRATION[1]))
    assert checker < submit < runner
    assert s.events[checker + 1] == ('idle',)
    assert not any('enable' in cmd or 'approve' in cmd for cmd in s.events)
    assert (s.instance.work / 'runpod-before.json').read_bytes() == s.before_provider
    assert (s.instance.work / 'research-before.json').read_bytes() == s.before_research
    assert stat.S_IMODE(a.PUBLIC.stat().st_mode) == 0o755
    assert stat.S_IMODE((a.PUBLIC / 'worker.json').stat().st_mode) == 0o444
    assert stat.S_IMODE((a.CONFIG / 'worker-token').stat().st_mode) == 0o600
    assert stat.S_IMODE(s.instance.work.stat().st_mode) == 0o700
    assert stat.S_IMODE(a.SUBMIT.stat().st_mode) == 0o700


def test_preexisting_state_refuses_before_any_service_or_file_mutation(orchestration):
    s = orchestration
    a.STATE.mkdir()
    with pytest.raises(a.ActivationError, match='ACTIVATION_TARGET_EXISTS'):
        s.instance.execute(s.args)
    assert not s.events and not (a.ROOT / 'calibrations').exists()
    assert (a.CONFIG / 'runpod.json').read_bytes() == s.before_provider


@pytest.mark.parametrize('failure', ['provider_write', 'identity_gate', 'submit_start'])
def test_partial_activation_closes_services_durably(orchestration, monkeypatch, failure):
    s = orchestration
    original_command = s.instance.command
    fired = False
    def write(path, raw, **kwargs):
        nonlocal fired
        if failure == 'provider_write' and path == a.CONFIG / 'runpod.json' and not fired:
            fired = True
            raise OSError('injected')
        s.write(path, raw, **kwargs)
    def command(command, **kwargs):
        if failure == 'identity_gate' and '--output' in command:
            raise a.ActivationError('IDENTITY_GATE_FAILED')
        if failure == 'submit_start' and command == ['/usr/bin/systemctl', 'start', a.CALIBRATION[0]]:
            raise a.ActivationError('SUBMIT_FAILED')
        return original_command(command, **kwargs)
    monkeypatch.setattr(a, 'write_file', write)
    monkeypatch.setattr(s.instance, 'command', command)
    with pytest.raises((a.ActivationError, OSError)):
        s.instance.execute(s.args)
    assert s.instance.closed is True
    disabled = json.loads((a.CONFIG / 'research.json').read_bytes())
    assert disabled['sandbox_image'] is None and disabled['policy'] == {'discovery_datasets': [], 'allow_calibration': False}
    assert all((a.UNITS / (name+'.d') / '50-probe-calibration.conf').read_bytes() == a.GUARD
               for name in (*a.CALIBRATION, 'probe-controller.service', 'probe-research.service'))
    assert s.events[-1] == ('/usr/bin/systemctl', 'stop', *a.CALIBRATION, 'probe-research.service', 'probe-controller.service')
    assert ('/usr/bin/systemctl', 'start', a.CALIBRATION[1]) not in s.events


@pytest.fixture
def typed_inputs(monkeypatch):
    from probe_core.runpod_provider import RunPodLaunchConfig
    from test_schemas import manifest_data
    # Standalone synthetic public fixture, with actual typed contracts and canonical model identity.
    data = manifest_data.__wrapped__()
    model = data['model']
    model.update(repo='Qwen/Qwen3-1.7B-Base', revision_sha='ea980cb0a6c2ae4b936e82123acc929f1cec04c1',
                 tokenizer_revision='ea980cb0a6c2ae4b936e82123acc929f1cec04c1', thinking_mode=None, chat_template_hash=None)
    inputs = data['inputs']
    inputs.update(dataset_revision=a.DATASET, prompt_ids=['public-short', 'public-long'], generation={'max_new_tokens': 4, 'temperature': 0.0})
    spec = dict(experiment_stage='calibration', idempotency_key='public-test', model=model, inputs=inputs,
                operation={'kind': 'backend_parity'}, limits={'max_runtime_seconds': 240, 'max_output_bytes': 33554432})
    plan = dict(label='public-test', model=model, cases=[dict(name='backend-parity', action='wait', expected_state='COMPLETED', spec=spec)])
    public = 'ssh-ed25519 ' + 'A'*68
    old_launch = dict(image_repository='ghcr.io/jaykobdetar/probe-mcp-diagnostic', ports=['22/tcp'], environment={'PUBLIC_KEY': public})
    launch = dict(image_repository=a.REPOSITORY, ports=['22/tcp'], environment={'PUBLIC_KEY': public})
    typed_launch = RunPodLaunchConfig.model_validate_json(a.encoded(launch))
    monkeypatch.setattr(a, 'OLD_LAUNCH', RunPodLaunchConfig.model_validate_json(a.encoded(old_launch)).digest)
    old_provider = dict(state_path='/var/lib/probe-provider/runpod.sqlite', api_key_file='/etc/probe-core/runpod-api-key',
                        launch=old_launch, storage_rates={'checked_at': (NOW-timedelta(days=3)).isoformat()},
                        mode='supervised_acceptance', request_timeout_seconds=17, max_runtime_seconds=300, ready_timeout_seconds=120)
    old_research = json.loads((PROJECT / 'deploy/research.json.example').read_bytes())
    old_research.update(service_uid=994, research_uid=993, admin_uid=1000, sandbox_image='sha256:'+'c'*64)
    image, commit = 'sha256:'+'d'*64, 'e'*40
    worker = dict(model_directory='/opt/probe-assets/models/Qwen3-1.7B-Base/'+model['revision_sha'], model=model,
                  assets=[{'path': 'model.safetensors', 'sha256': model['local_weight_hashes'][0]}],
                  datasets=[{'path': '/opt/probe-assets/datasets/public-calibration-prompts.json', 'sha256': a.DATASET}],
                  tensor_directory='/workspace/probe/tensors', output_directory='/workspace/probe/attempts',
                  backend='nnsight', device='cuda:0', code_git_commit=commit, container_image_digest=image,
                  provider_backend='runpod', region='EU-CZ-1', live_price_usd_per_hour=.74, cgroup_directory='/sys/fs/cgroup/probe-jobs')
    bodies = {'plan.json': a.encoded(plan), 'launch.json': a.encoded(launch), 'worker.json': a.encoded(worker), 'worker-token': b'x'*48}
    runner = dict(service_uid=994, research_uid=993, admin_uid=1000, plan_path='/etc/probe-calibration/plan.json',
                  plan_sha256='sha256:'+digest(bodies['plan.json']), worker_config_path='/etc/probe-calibration/worker.json',
                  worker_config_sha256='sha256:'+digest(bodies['worker.json']), source_commit=commit,
                  expected_worker_price_usd_per_hour=.74, max_runtime_seconds=900,
                  deployment={'gpu_model': 'NVIDIA GeForce RTX 4090', 'image_digest': image, 'volume_id': None,
                              'region': 'EU-CZ-1', 'volume_gb': 0, 'image_repository': a.REPOSITORY,
                              'launch_config_hash': typed_launch.digest, 'storage_mode': 'disposable_research'})
    bodies['acceptance.json'] = a.encoded(runner)
    return SimpleNamespace(bodies=bodies, provider=old_provider, research=old_research,
                           manifest={'storage_checked_at': NOW.isoformat()}, users=(994,993,1000))


def test_typed_configuration_preserves_all_unrelated_existing_fields(typed_inputs):
    s = typed_inputs
    provider, research, public = a.configurations(s.bodies, a.encoded(s.provider), a.encoded(s.research), s.manifest, s.users)
    expected_provider = deepcopy(s.provider)
    expected_provider.update(launch=json.loads(s.bodies['launch.json']), max_runtime_seconds=900, ready_timeout_seconds=300)
    from probe_core.runpod_provider import RunPodLaunchConfig
    expected_provider['launch'] = RunPodLaunchConfig.model_validate_json(s.bodies['launch.json']).model_dump(mode='json')
    expected_provider['storage_rates']['checked_at'] = NOW.isoformat()
    expected_research = deepcopy(s.research)
    expected_research['policy'] = dict(discovery_datasets=[a.DATASET], allow_calibration=True)
    assert json.loads(provider) == expected_provider
    assert json.loads(research) == expected_research
    assert public == s.provider['launch']['environment']['PUBLIC_KEY']


@pytest.mark.parametrize('change', ['api_key', 'state_path', 'mode', 'old_launch', 'other_dataset', 'disk', 'cuda', 'args', 'start_ssh',
                                    'ssh_key', 'extra_case', 'plan_dataset', 'worker_path', 'worker_hash', 'worker_image', 'worker_directory',
                                    'runtime', 'plan_hash', 'other_gpu', 'identity', 'volume'])
def test_configuration_bounds_fail_before_mutation(typed_inputs, change):
    s = typed_inputs
    if change == 'api_key':
        s.provider['api_key_file'] = '/etc/other-key'
    elif change == 'state_path':
        s.provider['state_path'] = '/var/lib/other.sqlite'
    elif change == 'mode':
        s.provider['mode'] = 'disabled'
    elif change == 'old_launch':
        s.provider['launch']['container_disk_gb'] = 21
    elif change == 'other_dataset':
        s.research['policy']['discovery_datasets'] = ['sha256:'+'a'*64]
    elif change in {'disk','cuda','args','start_ssh','ssh_key'}:
        launch = json.loads(s.bodies['launch.json'])
        field, value = {'disk': ('container_disk_gb', 21), 'cuda': ('min_cuda_version', '12.8'), 'args': ('args','bash'),
                        'start_ssh': ('start_ssh',True), 'ssh_key': ('environment', {'PUBLIC_KEY': 'ssh-ed25519 '+'B'*68})}[change]
        launch[field] = value
        s.bodies['launch.json'] = a.encoded(launch)
    elif change in {'extra_case', 'plan_dataset'}:
        plan = json.loads(s.bodies['plan.json'])
        if change == 'extra_case':
            plan['cases'] *= 2
        else:
            plan['cases'][0]['spec']['inputs']['dataset_revision'] = 'sha256:'+'a'*64
        s.bodies['plan.json'] = a.encoded(plan)
    elif change in {'worker_image', 'worker_directory'}:
        worker = json.loads(s.bodies['worker.json'])
        worker['container_image_digest' if change == 'worker_image' else 'tensor_directory'] = 'sha256:'+'f'*64 if change == 'worker_image' else '/etc/escape'
        s.bodies['worker.json'] = a.encoded(worker)
        runner = json.loads(s.bodies['acceptance.json'])
        runner['worker_config_sha256'] = 'sha256:'+digest(s.bodies['worker.json'])
        s.bodies['acceptance.json'] = a.encoded(runner)
    else:
        runner = json.loads(s.bodies['acceptance.json'])
        if change == 'other_gpu':
            runner['deployment']['gpu_model'] = 'NVIDIA A100'
        elif change == 'volume':
            runner['deployment'].update(storage_mode=None, volume_id='volume1', volume_gb=20)
        else:
            field, value = {'worker_path': ('worker_config_path','/etc/elsewhere.json'), 'worker_hash': ('worker_config_sha256','sha256:'+'f'*64),
                            'runtime': ('max_runtime_seconds',899), 'plan_hash': ('plan_sha256','sha256:'+'f'*64), 'identity': ('service_uid',995)}[change]
            runner[field] = value
        s.bodies['acceptance.json'] = a.encoded(runner)
    before = deepcopy(s.__dict__)
    with pytest.raises(ValueError):
        a.configurations(s.bodies, a.encoded(s.provider), a.encoded(s.research), s.manifest, s.users)
    assert before == s.__dict__


def test_installed_ledger_parent_uses_trusted_owner_not_root(orchestration, monkeypatch):
    calls = []
    monkeypatch.setattr(a, 'trusted', lambda path, owner=0: calls.append((path, owner)))
    orchestration.instance.execute(orchestration.args)
    assert (a.STATE.parent, 994) in calls
    assert (a.STATE.parent, 0) not in calls


def test_ssh_key_mismatch_is_rejected_before_staging_and_service_stops(orchestration, monkeypatch):
    s = orchestration
    original = s.instance.command
    def command(args, **kwargs):
        return b'ssh-ed25519 WRONG\n' if args[0].endswith('ssh-keygen') else original(args, **kwargs)
    monkeypatch.setattr(s.instance, 'command', command)
    with pytest.raises(a.ActivationError, match='SSH_KEY_MISMATCH'):
        s.instance.execute(s.args)
    assert s.instance.work is None and not (a.ROOT / 'calibrations').exists()
    assert not any('stop' in cmd or 'start' in cmd for cmd in s.events)


def test_fail_closed_attempts_guards_and_stop_when_config_write_fails(orchestration, monkeypatch):
    s = orchestration
    s.instance.old_research = s.before_research
    s.instance.metadata = {'research.json': dict(uid=0, gid=0, mode=0o640)}
    def write(path, raw, **kwargs):
        if path == a.CONFIG / 'research.json':
            raise OSError('injected failure')
        s.write(path, raw, **kwargs)
    monkeypatch.setattr(a, 'write_file', write)
    s.instance.fail_closed()
    assert s.instance.closed is False
    assert s.events[-1] == ('/usr/bin/systemctl', 'stop', *a.CALIBRATION, 'probe-research.service', 'probe-controller.service')
    assert (a.UNITS / 'probe-controller.service.d/50-probe-calibration.conf').exists()
