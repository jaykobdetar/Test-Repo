#!/usr/bin/env python3
"""One-time, pinned public calibration activation after a successful installed upgrade.

Run with /opt/probe-core/venv/bin/python -I. No approval or cloud mutation is
issued here. Failure after staging leaves durable service guards and a receipt.
"""
import argparse
import ast
import configparser
import ctypes
from datetime import datetime, timezone
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import struct
import subprocess
import tempfile
import time

ROOT = Path('/opt/probe-core')
CONFIG = Path('/etc/probe-core')
UNITS = Path('/etc/systemd/system')
PUBLIC = Path('/etc/probe-calibration')
SUBMIT = Path('/var/lib/probe-calibration-submit')
STATE = Path('/var/lib/probe-core/gpu-acceptance')
DATASET = 'sha256:6867f3b38b8587c8f7b71955bbb597eac27445ee69b2aa033904619df4df5bbe'
OLD_LAUNCH = 'sha256:628accf2379eb69331d4b2c2ca35e96cffd26df1440eb7be0c7d2676763df9af'
REPOSITORY = 'ghcr.io/jaykobdetar/probe-mcp-worker'
CALIBRATION = ('probe-calibration-submit.service', 'probe-calibration-run.service')
SERVICES = ('probe-provider-stop.service', 'probe-controller.service', 'probe-research.service')
FILES = {'plan.json', 'acceptance.json', 'launch.json', 'worker.json', 'worker-token', 'worker-ssh-key', *CALIBRATION}
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}
HEX = re.compile(r'[0-9a-f]{64}\Z')
GUARD = b'[Unit]\nConditionPathExists=/etc/probe-core/calibration-activation-confirmed\n'


class ActivationError(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise ActivationError(code)


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False, 'NONFINITE_JSON'))


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def trusted(path, owner=0):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        info = part.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, owner}
                and not info.st_mode & 0o022, 'UNTRUSTED_DIRECTORY')


def read_file(path, *, owner=None, limit=1048576):
    path = Path(path).absolute()
    require(all(not part.is_symlink() for part in path.parents), 'SYMLINK_PARENT')
    if owner is not None:
        trusted(path.parent, owner)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit
                and (owner is None or info.st_uid == owner and not info.st_mode & 0o022), 'UNSAFE_FILE')
        raw = stream.read(limit + 1)
        require(len(raw) == info.st_size and len(raw) <= limit, 'FILE_CHANGED')
        return raw


def write_file(path, raw, *, uid, gid, mode):
    fd, temporary = tempfile.mkstemp(prefix='.activation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def manifest_inputs(path, digest, now):
    require(type(digest) is str and HEX.fullmatch(digest), 'MANIFEST_PIN_INVALID')
    raw = read_file(path)
    require(hashlib.sha256(raw).hexdigest() == digest, 'MANIFEST_HASH_MISMATCH')
    manifest = decode(raw)
    require(set(manifest) == {'schema_version', 'storage_checked_at', 'upgrade_directory',
                             'identity_checker_sha256', 'files'} and type(manifest['schema_version']) is int and manifest['schema_version'] == 1, 'MANIFEST_SCHEMA')
    require(type(manifest['files']) is dict and set(manifest['files']) == FILES
            and all(type(v) is str and HEX.fullmatch(v) for v in manifest['files'].values())
            and type(manifest['identity_checker_sha256']) is str and HEX.fullmatch(manifest['identity_checker_sha256']), 'FILE_PINS_INVALID')
    date = datetime.fromisoformat(manifest['storage_checked_at'].replace('Z', '+00:00'))
    require(date.tzinfo is not None and 0 <= (now - date).total_seconds() <= 86400, 'STORAGE_REVIEW_NOT_FRESH')
    bodies = {name: read_file(path.parent / name) for name in sorted(FILES)}
    require(all(hashlib.sha256(body).hexdigest() == manifest['files'][name] for name, body in bodies.items()), 'INPUT_HASH_MISMATCH')
    return raw, manifest, bodies


def private_key_fd(raw):
    if hasattr(os, 'memfd_create'):
        fd = os.memfd_create('probe-calibration-key', 1)
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.memfd_create.argtypes, libc.memfd_create.restype = [ctypes.c_char_p, ctypes.c_uint], ctypes.c_int
        fd = libc.memfd_create(b'probe-calibration-key', 1)
    require(fd >= 0, 'PRIVATE_KEY_DESCRIPTOR_FAILED')
    try:
        os.fchmod(fd, 0o600)
        require(os.write(fd, raw) == len(raw), 'PRIVATE_KEY_DESCRIPTOR_FAILED')
        return fd
    except BaseException:
        os.close(fd)
        raise


def identity_gate(report):
    checks = report.get('checks', {})
    require(report.get('passed') is True and type(checks) is dict and len(checks) >= 26
            and all(value is True for value in checks.values()) and report.get('failure_codes') == []
            and report.get('check_count') == report.get('passed_count') == len(checks)
            and report.get('normal_research_audit_events', 0) >= 2
            and report.get('paid_actions_performed') is False, 'IDENTITY_GATE_FAILED')


def extract_reader(raw):
    found = [node.value for node in ast.parse(raw).body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == 'STATE_READER' for target in node.targets)]
    require(len(found) == 1 and isinstance(found[0], ast.Constant) and type(found[0].value) is str
            and 0 < len(found[0].value) <= 65536, 'HISTORY_READER_INVALID')
    return found[0].value


def validate_unit(raw, mode):
    parsed = configparser.ConfigParser(interpolation=None, strict=True)
    parsed.optionxform = str
    parsed.read_string(raw.decode())
    common = {'Type': 'oneshot' if mode == 'submit' else 'simple', 'User': 'probe-research' if mode == 'submit' else 'probe-trusted',
              'Group': 'probe-research' if mode == 'submit' else 'probe-trusted', 'WorkingDirectory': str(ROOT),
              'ExecStart': f'{ROOT}/venv/bin/python -I -m probe_core.gpu_acceptance_runner {mode} --config {PUBLIC}/acceptance.json',
              'NoNewPrivileges': 'yes', 'PrivateTmp': 'yes', 'ProtectSystem': 'strict', 'ProtectHome': 'yes', 'UMask': '0077',
              'ReadWritePaths': str(SUBMIT) if mode == 'submit' else '/var/lib/probe-core /var/lib/probe-provider'}
    common.update({'TimeoutStartSec': '120'} if mode == 'submit' else
                  {'TimeoutStopSec': '15', 'KillMode': 'control-group', 'SupplementaryGroups': 'probe-ipc probe-ledger-read'})
    require(set(parsed.sections()) == {'Unit', 'Service', 'Install'} and dict(parsed['Service']) == common
            and dict(parsed['Install']) == {'WantedBy': 'multi-user.target'}, 'UNIT_PROFILE_CHANGED')
    expected = {'After': 'probe-research.service', 'Requires': 'probe-research.service'} if mode == 'submit' else {
        'After': 'network-online.target probe-controller.service probe-calibration-submit.service',
        'Wants': 'network-online.target probe-calibration-submit.service', 'Requires': 'probe-controller.service'}
    unit = dict(parsed['Unit'])
    description = unit.pop('Description', None)
    require(type(description) is str and '\n' not in description and unit == expected, 'UNIT_DEPENDENCIES_CHANGED')


def configurations(bodies, old_provider, old_research, manifest, users):
    from probe_core.gpu_acceptance import AcceptancePlan
    from probe_core.gpu_acceptance_runner import RunnerConfig, validate_plan
    from probe_core.research_service import ServiceConfig
    from probe_core.runpod_provider import RunPodConfig, RunPodLaunchConfig
    from probe_core.worker_contracts import WorkerConfig
    provider = RunPodConfig.model_validate_json(old_provider)
    research = ServiceConfig.model_validate_json(old_research)
    launch = RunPodLaunchConfig.model_validate_json(bodies['launch.json'])
    runner = RunnerConfig.model_validate_json(bodies['acceptance.json'])
    plan = AcceptancePlan.model_validate_json(bodies['plan.json'])
    validate_plan(runner, plan)
    worker = WorkerConfig.model_validate_json(bodies['worker.json'])
    require(provider.state_path == '/var/lib/probe-provider/runpod.sqlite' and provider.api_key_file == '/etc/probe-core/runpod-api-key'
            and provider.mode == 'supervised_acceptance' and provider.launch.digest == OLD_LAUNCH, 'PROVIDER_BASELINE_CHANGED')
    require(launch.image_repository == REPOSITORY and launch.args == '' and launch.container_disk_gb == 20
            and launch.min_cuda_version == '13.0' and launch.ports == ('22/tcp',) and launch.start_ssh is False
            and launch.environment.get('PUBLIC_KEY') is not None
            and launch.environment['PUBLIC_KEY'] == provider.launch.environment.get('PUBLIC_KEY')
            and launch.environment.get('PROBE_CGROUP_ROOT', '/sys/fs/cgroup/probe-jobs') == '/sys/fs/cgroup/probe-jobs', 'LAUNCH_SCOPE_CHANGED')
    require(runner.plan_path == str(PUBLIC / 'plan.json') and runner.plan_sha256 == 'sha256:' + hashlib.sha256(bodies['plan.json']).hexdigest()
            and runner.submission_state_directory == str(SUBMIT) and runner.trusted_state_directory == str(STATE)
            and runner.provider_config_path == str(CONFIG / 'runpod.json') and runner.ssh_identity_file == str(CONFIG / 'worker-ssh-key')
            and runner.bearer_secret_file == str(CONFIG / 'worker-token') and runner.max_runtime_seconds == 900
            and runner.deployment.image_repository == REPOSITORY and runner.deployment.launch_config_hash == launch.digest
            and runner.deployment.volume_gb == 0 and runner.deployment.volume_id is None
            and runner.deployment.storage_mode == 'disposable_research' and runner.deployment.gpu_model == 'NVIDIA GeForce RTX 4090'
            and plan.cases[0].spec.inputs.dataset_revision == DATASET, 'ACCEPTANCE_SCOPE_CHANGED')
    require(runner.worker_config_path == str(PUBLIC / 'worker.json')
            and runner.worker_config_sha256 == 'sha256:' + hashlib.sha256(bodies['worker.json']).hexdigest()
            and worker.model == plan.model and worker.backend == 'nnsight' and worker.device == 'cuda:0'
            and worker.code_git_commit == runner.source_commit and worker.container_image_digest == runner.deployment.image_digest
            and worker.region == runner.deployment.region and worker.live_price_usd_per_hour == runner.expected_worker_price_usd_per_hour
            and worker.cgroup_directory == '/sys/fs/cgroup/probe-jobs' and len(worker.datasets) == 1
            and worker.datasets[0].sha256 == DATASET
            and worker.model_directory == '/opt/probe-assets/models/' + plan.model.repo.split('/')[-1] + '/' + plan.model.revision_sha
            and worker.tensor_directory == '/workspace/probe/tensors' and worker.output_directory == '/workspace/probe/attempts'
            and worker.datasets[0].path == '/opt/probe-assets/datasets/public-calibration-prompts.json', 'WORKER_BINDING_CHANGED')
    require((runner.service_uid, runner.research_uid, runner.admin_uid) == users
            and (research.service_uid, research.research_uid, research.admin_uid) == users, 'IDENTITY_BINDING_CHANGED')
    require(research.policy.discovery_datasets in {(), (DATASET,)}, 'EXISTING_DATASET_POLICY_CHANGED')
    new_provider, new_research = decode(old_provider), decode(old_research)
    new_provider.update(launch=launch.model_dump(mode='json'), max_runtime_seconds=900, ready_timeout_seconds=300)
    new_provider['storage_rates']['checked_at'] = manifest['storage_checked_at']
    new_research['policy'] = {'discovery_datasets': [DATASET], 'allow_calibration': True}
    RunPodConfig.model_validate_json(encoded(new_provider))
    ServiceConfig.model_validate_json(encoded(new_research))
    require(re.fullmatch(rb'[A-Za-z0-9_-]{32,256}\n?', bodies['worker-token']), 'WORKER_TOKEN_FORMAT')
    return encoded(new_provider), encoded(new_research), launch.environment['PUBLIC_KEY']


class Activation:
    def __init__(self):
        self.work = None
        self.stage = 'validation'
        self.closed = False
        self.baseline = None

    def command(self, args, timeout=60, pass_fds=()):
        result = subprocess.run(args, cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, check=False, umask=0o022, pass_fds=pass_fds)
        require(result.returncode == 0 and len(result.stdout) <= 1048576, 'COMMAND_FAILED_' + self.stage.upper())
        return result.stdout

    def ctl(self, *args):
        return self.command(['/usr/bin/systemctl', *args], timeout=120)

    def idle(self):
        script = ('import base64,hashlib,json,re,sqlite3,time\nfrom datetime import datetime,timezone\nfrom pathlib import Path\n'
                  + self.reader + '\nfrom probe_core.runpod_provider import RunPodConfig,RunPodHTTP\n'
                  + 'result=idle_history_snapshot("/var/lib/probe-core/research.sqlite","/var/lib/probe-provider/runpod.sqlite",expected='
                  + repr(self.baseline) + ')\nconfig=RunPodConfig.load("/etc/probe-core/runpod.json")\n'
                  + 'pods=RunPodHTTP(config.api_key_file).request("GET","/v2/pods?includeClusterPods=true&limit=1000")\n'
                  + 'assert type(pods.get("pods")) is list and pods.get("pagination",{}).get("hasNextPage") is False\n'
                  + 'result["pods"]=len(pods["pods"])\nprint(json.dumps(result))\n')
        prefix = ['/usr/sbin/runuser', '-u', 'probe-trusted', '-g', 'probe-trusted', '--']
        result = decode(self.command(prefix + [str(ROOT / 'venv/bin/python'), '-I', '-c', script]))
        require(result.get('idle') is True and result.get('audit_valid') is True and result.get('pods') == 0, 'NOT_IDLE')
        containers = self.command(prefix + ['env', 'HOME=/var/lib/probe-sandbox', f'XDG_RUNTIME_DIR=/run/user/{self.users[0]}',
                  f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{self.users[0]}/bus', '/usr/bin/podman', '--remote=false',
                  'ps', '--all', '--quiet', '--no-trunc'])
        require(containers.strip() == b'', 'LOCAL_CONTAINERS_PRESENT')
        if self.baseline is None:
            self.baseline = result
        return result

    def ready(self):
        expected = (('/run/probe-research/research.sock', 'probe-research'), ('/run/probe-controller/admin.sock', 'probe-ipc'),
                    ('/run/probe-controller/research.sock', 'probe-ipc'), ('/run/probe-provider/stop.sock', 'probe-stop'))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                for path, group in expected:
                    info = Path(path).lstat()
                    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == self.users[0] and info.st_gid == grp.getgrnam(group).gr_gid
                            and stat.S_IMODE(info.st_mode) == 0o660, 'LISTENER_NOT_READY')
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                        sock.settimeout(.25)
                        sock.connect(path)
                        require(struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] == self.users[0], 'LISTENER_NOT_READY')
                return
            except (OSError, ActivationError):
                time.sleep(.1)
        raise ActivationError('LISTENER_NOT_READY')

    def fail_closed(self):
        errors = []
        disabled = decode(self.old_research)
        disabled.update(sandbox_image=None, policy={'discovery_datasets': [], 'allow_calibration': False})
        actions = [lambda: write_file(CONFIG / 'research.json', encoded(disabled), **self.metadata['research.json'])]
        def guards():
            for name in (*CALIBRATION, 'probe-research.service', 'probe-controller.service'):
                directory = UNITS / (name + '.d')
                directory.mkdir(mode=0o755, exist_ok=True)
                trusted(directory)
                write_file(directory / '50-probe-calibration.conf', GUARD, uid=0, gid=0, mode=0o644)
            self.ctl('daemon-reload')
        actions.extend([guards, lambda: self.ctl('stop', *CALIBRATION, 'probe-research.service', 'probe-controller.service')])
        for action in actions:
            try:
                action()
            except BaseException:
                errors.append(True)
        self.closed = not errors

    def execute(self, args):
        raw, manifest, bodies = manifest_inputs(args.manifest, args.manifest_sha256, datetime.now(timezone.utc))
        upgrade = Path(manifest['upgrade_directory'])
        require(upgrade.parent == ROOT / 'upgrades' and HEX.fullmatch(upgrade.name), 'UPGRADE_DIRECTORY_INVALID')
        trusted(upgrade)
        receipt = decode(read_file(upgrade / 'upgrade-report.json', owner=0))
        release = decode(read_file(upgrade / 'upgrade-release.json', owner=0))
        require(receipt.get('status') == 'passed' and receipt.get('sandbox_checks') == 16 and receipt.get('identity_checks', 0) >= 26
                and receipt.get('cloud_mutations_performed') is False and receipt.get('wheel_sha256') == upgrade.name
                and release.get('wheel_sha256') == upgrade.name and release.get('identity_checker_sha256') == manifest['identity_checker_sha256'], 'UPGRADE_NOT_COMPLETED')
        identity_gate(decode(read_file(upgrade / 'identity-acceptance.json', owner=0)))
        checker = read_file(upgrade / 'verify-installed-identities.py', owner=0)
        require(hashlib.sha256(checker).hexdigest() == manifest['identity_checker_sha256'], 'CHECKER_PIN_CHANGED')
        self.reader = extract_reader(checker)
        self.users = tuple(pwd.getpwnam(name).pw_uid for name in ('probe-trusted', 'probe-research', args.human))
        require(len(set(self.users)) == 3 and min(self.users) > 0, 'IDENTITIES_NOT_DISTINCT')
        self.metadata, originals = {}, {}
        for name in ('runpod.json', 'research.json'):
            originals[name] = read_file(CONFIG / name, owner=0)
            info = (CONFIG / name).lstat()
            self.metadata[name] = dict(uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode))
        self.old_research = originals['research.json']
        provider, research, public_key = configurations(bodies, originals['runpod.json'], self.old_research, manifest, self.users)
        for name, mode in zip(CALIBRATION, ('submit', 'run')):
            validate_unit(bodies[name], mode)
        for directory in (ROOT, CONFIG, UNITS, SUBMIT.parent):
            trusted(directory)
        trusted(STATE.parent, owner=self.users[0])
        fresh = (PUBLIC, SUBMIT, STATE, CONFIG / 'worker-token', CONFIG / 'worker-ssh-key', *(UNITS / name for name in CALIBRATION))
        require(all(not os.path.lexists(path) for path in fresh)
                and not os.path.lexists(CONFIG / 'calibration-activation-confirmed')
                and not os.path.lexists(CONFIG / 'upgrade-blocked')
                and not os.path.lexists(ROOT / 'calibrations' / args.manifest_sha256), 'ACTIVATION_TARGET_EXISTS')
        for name in (*SERVICES, 'probe-watchdog.service', *CALIBRATION):
            require(self.ctl('show', name, '--property=DropInPaths', '--value').strip() == b'', 'SERVICE_OVERRIDES_PRESENT')
        self.ctl('is-active', '--quiet', *SERVICES, 'probe-watchdog.service')
        fd = private_key_fd(bodies['worker-ssh-key'])
        try:
            public = self.command(['/usr/bin/ssh-keygen', '-y', '-P', '', '-f', f'/proc/self/fd/{fd}'], pass_fds=(fd,)).decode().strip()
            require(public.split()[:2] == public_key.split()[:2], 'SSH_KEY_MISMATCH')
        finally:
            os.close(fd)
        self.idle()
        parent = ROOT / 'calibrations'
        if parent.exists():
            trusted(parent)
        else:
            parent.mkdir(mode=0o700)
        self.work = parent / args.manifest_sha256
        self.work.mkdir(mode=0o700)
        try:
            for name, content in {**bodies, 'activation-manifest.json': raw, 'verify-installed-identities.py': checker,
                                  'runpod-before.json': originals['runpod.json'], 'research-before.json': self.old_research}.items():
                write_file(self.work / name, content, uid=0, gid=0, mode=0o600)
            self.stage = 'close_services'
            self.ctl('stop', 'probe-research.service', 'probe-controller.service')
            self.idle()  # Keep the stop broker and watchdog available until authority is absent.
            self.ctl('stop', 'probe-provider-stop.service')
            require(all(read_file(CONFIG / name, owner=0) == body for name, body in originals.items()), 'CONFIG_CHANGED_DURING_ACTIVATION')
            self.stage = 'install_configuration'
            for path, uid, gid, mode in ((PUBLIC, 0, 0, 0o755), (SUBMIT, self.users[1], grp.getgrnam('probe-research').gr_gid, 0o700),
                                        (STATE, self.users[0], grp.getgrnam('probe-trusted').gr_gid, 0o700)):
                path.mkdir(mode=mode)
                os.chown(path, uid, gid)
                os.chmod(path, mode)
            for name in ('plan.json', 'acceptance.json', 'worker.json'):
                write_file(PUBLIC / name, bodies[name], uid=0, gid=0, mode=0o444)
            for name in ('worker-token', 'worker-ssh-key'):
                write_file(CONFIG / name, bodies[name], uid=self.users[0], gid=grp.getgrnam('probe-trusted').gr_gid, mode=0o600)
            for name in CALIBRATION:
                write_file(UNITS / name, bodies[name], uid=0, gid=0, mode=0o644)
            for name, content in (('runpod.json', provider), ('research.json', research)):
                write_file(CONFIG / name, content, **self.metadata[name])
            self.ctl('daemon-reload')
            self.ctl('start', *SERVICES)
            self.ready()
            self.stage = 'identity_acceptance'
            self.command(['/usr/bin/python3', '-I', str(self.work / 'verify-installed-identities.py'), '--human', args.human,
                          '--output', str(self.work / 'identity-acceptance.json')], timeout=120)
            identity = decode(read_file(self.work / 'identity-acceptance.json', owner=0))
            identity_gate(identity)
            self.idle()
            self.stage = 'submit_calibration'
            self.ctl('start', CALIBRATION[0])
            self.ctl('start', CALIBRATION[1])
            report = {'schema_version': 1, 'status': 'passed', 'manifest_sha256': args.manifest_sha256,
                      'identity_checks': identity['check_count'], 'historical_state_preserved': True,
                      'approval_issued': False, 'cloud_mutations_performed': False, 'units_enabled_at_boot': False}
            write_file(self.work / 'activation-report.json', encoded(report), uid=0, gid=0, mode=0o600)
            return report
        except BaseException:
            self.fail_closed()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--human', required=True)
    args = parser.parse_args(argv)
    activation = Activation()
    try:
        require(os.geteuid() == 0, 'ADMINISTRATOR_REQUIRED')
        os.umask(0o077)
        result = activation.execute(args)
    except BaseException as error:
        result = {'schema_version': 1, 'status': 'failed', 'stage': activation.stage,
                  'failure_code': str(error) if isinstance(error, ActivationError) else 'ACTIVATION_UNAVAILABLE',
                  'fail_closed_confirmed': activation.closed, 'approval_issued': False, 'cloud_mutations_performed': False}
        if activation.work is not None:
            try:
                write_file(activation.work / 'activation-failure.json', encoded(result), uid=0, gid=0, mode=0o600)
            except BaseException:
                result['failure_receipt_saved'] = False
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
