"""Install one pinned additive supervised suite; never upgrade or approve compute.

Run with the installed Python in isolated mode from a reviewed root staging
directory. All offered bytes are captured and hashed before installation. No
existing wheel, provider configuration, credential, account or permission policy
is changed. Different existing sidecar/config/unit bytes are a hard refusal.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import uuid

PYTHON = '/opt/probe-core/venv/bin/python'
SIDECAR = '/opt/probe-core/supervised'
CONFIG = '/etc/probe-supervised'
SYSTEMD = '/etc/systemd/system'
SIDE_FILES = {'supervised-entry.py', 'ssh_job_client.py', 'supervised_runner.py', 'public-job.py', 'files.json'}
MODULES = {'ssh_job_client.py', 'supervised_runner.py', 'public-job.py'}
UNITS = {'probe-supervised-submit.service': ('probe-research', 'submit'),
         'probe-supervised-run.service': ('probe-trusted', 'run')}
CORE_UNITS = {'probe-controller.service': ('probe-trusted', 'probe-ipc'),
              'probe-research.service': ('probe-trusted', 'probe-trusted'),
              'probe-provider-stop.service': ('probe-trusted', 'probe-stop'),
              'probe-watchdog.service': ('probe-watchdog', 'probe-watch-read')}
BOUND = 256 * 1024


class InstallError(ValueError):
    """Only fixed nonsecret codes are printed by main."""


def require(value, code):
    if not value:
        raise InstallError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def parsed(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def bounded_read(fd):
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= BOUND,
                'REGULAR_BOUNDED_INPUT_REQUIRED')
        raw = stream.read(BOUND + 1)
        require(len(raw) == info.st_size and len(raw) <= BOUND, 'INPUT_CHANGED')
        return raw


def relative_read(directory_fd, relative):
    parts = relative.split('/')
    require(all(part not in {'', '.', '..'} for part in parts) and not relative.startswith('/'), 'UNSAFE_INPUT_PATH')
    current = os.dup(directory_fd)
    try:
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = following
        return bounded_read(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current))
    finally:
        os.close(current)


def inventory(directory_fd, prefix=''):
    result = set()
    with os.scandir(directory_fd) as entries:
        items = list(entries)
    require(len(items) <= 80, 'INPUT_INVENTORY_TOO_LARGE')
    for item in items:
        name = prefix + item.name
        if item.is_dir(follow_symlinks=False):
            require(prefix == '' and item.name in {'sidecar', 'configs', 'units'}, 'UNEXPECTED_INPUT_DIRECTORY')
            child = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                result.update(inventory(child, name + '/'))
            finally:
                os.close(child)
        else:
            require(item.is_file(follow_symlinks=False), 'INPUT_SYMLINK_OR_SPECIAL_FILE')
            result.add(name)
    return result


def read_capsule(manifest, expected):
    require(re.fullmatch(r'[0-9a-f]{64}', expected) is not None, 'MANIFEST_HASH_INVALID')
    manifest = Path(manifest).absolute()
    root_fd = os.open(manifest.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        raw = relative_read(root_fd, manifest.name)
        require(sha(raw) == expected, 'MANIFEST_HASH_MISMATCH')
        value = parsed(raw)
        require(type(value) is dict and set(value) == {'schema_version', 'files'}
                and type(value['schema_version']) is int and value['schema_version'] == 1
                and type(value['files']) is dict and 12 <= len(value['files']) <= 57, 'MANIFEST_SCHEMA_INVALID')
        files = {}
        for name, checksum in value['files'].items():
            require(type(name) is str and (name == 'install-supervised.py'
                or name in {'sidecar/' + item for item in SIDE_FILES}
                or name in {'units/' + item for item in UNITS}
                or name == 'configs/suite.json'
                or re.fullmatch(r'configs/[a-z0-9][a-z0-9-]{0,95}-(acceptance|plan|worker)\.json', name)),
                'UNREVIEWED_INPUT_PATH')
            require(type(checksum) is str and re.fullmatch(r'[0-9a-f]{64}', checksum), 'MEMBER_HASH_INVALID')
            captured = relative_read(root_fd, name)
            require(sha(captured) == checksum, 'MEMBER_HASH_MISMATCH')
            files[name] = captured
        require(inventory(root_fd) == set(files) | {manifest.name}, 'INPUT_INVENTORY_MISMATCH')
        require(all('sidecar/' + name in files for name in SIDE_FILES)
                and all('units/' + name in files for name in UNITS)
                and 'install-supervised.py' in files and 'configs/suite.json' in files,
                'REQUIRED_INPUT_MISSING')
        require(files['install-supervised.py'] == Path(__file__).read_bytes(), 'INSTALLER_SELF_PIN_MISMATCH')
        release = parsed(files['sidecar/files.json'])
        require(type(release) is dict and set(release) == MODULES
                and all(release[name] == 'sha256:' + sha(files['sidecar/' + name]) for name in MODULES),
                'SIDECAR_HASH_MANIFEST_INVALID')
        return files
    finally:
        os.close(root_fd)


def validate_unit(raw, name):
    config = configparser.ConfigParser(interpolation=None, strict=True)
    config.optionxform = str
    config.read_string(raw.decode())
    require(set(config.sections()) == {'Unit', 'Service'}, 'UNIT_SECTIONS_INVALID')
    service = dict(config['Service'])
    account, mode = UNITS[name]
    expected = f'{PYTHON} -I {SIDECAR}/supervised-entry.py {mode} --suite {CONFIG}/suite.json'
    require(service.get('User') == service.get('Group') == account
            and service.get('ExecStart') == expected
            and not any(key.startswith('Exec') and key != 'ExecStart' for key in service)
            and service.get('NoNewPrivileges') == 'yes' and service.get('ProtectSystem') == 'strict'
            and service.get('ProtectHome') == 'yes' and service.get('PrivateTmp') == 'yes'
            and service.get('UMask') == '0077' and service.get('WorkingDirectory') == '/opt/probe-core',
            'UNIT_IDENTITY_OR_COMMAND_CHANGED')
    allowed = {'Type', 'User', 'Group', 'SupplementaryGroups', 'WorkingDirectory', 'ExecStart',
               'TimeoutStartSec', 'TimeoutStopSec', 'KillMode', 'NoNewPrivileges', 'PrivateTmp',
               'ProtectSystem', 'ProtectHome', 'ReadWritePaths', 'UMask'}
    require(set(service) <= allowed and service.get('Type') == ('oneshot' if mode == 'submit' else 'simple'),
            'UNIT_SERVICE_SCOPE_INVALID')
    require(service.get('ReadWritePaths') == ('/var/lib/probe-supervised-submit' if mode == 'submit'
                else '/var/lib/probe-core /var/lib/probe-provider'), 'UNIT_WRITE_SCOPE_INVALID')
    require(service.get('SupplementaryGroups', '') == ('' if mode == 'submit' else 'probe-ipc probe-ledger-read'),
            'UNIT_GROUP_SCOPE_INVALID')
    unit = dict(config['Unit'])
    require(set(unit) <= {'Description', 'After', 'Wants', 'Requires'}
            and unit.get('Requires') == ('probe-research.service' if mode == 'submit'
                 else 'probe-controller.service probe-supervised-submit.service')
            and unit.get('After') == ('probe-research.service' if mode == 'submit'
                 else 'network-online.target probe-controller.service probe-supervised-submit.service')
            and unit.get('Wants', '') == ('' if mode == 'submit' else 'network-online.target'), 'UNIT_DEPENDENCY_INVALID')


def describe(files):
    suite = parsed(files['configs/suite.json'])
    require(type(suite) is dict and set(suite) == {'schema_version', 'configurations'}
            and type(suite['schema_version']) is int and suite['schema_version'] == 1
            and type(suite['configurations']) is list and 1 <= len(suite['configurations']) <= 16,
            'SUITE_INVALID')
    cases = {}
    required = {'configs/suite.json'}
    for name in suite['configurations']:
        require(type(name) is str and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}-acceptance\.json', name), 'CASE_NAME_INVALID')
        case = name.removesuffix('-acceptance.json')
        require(case not in cases, 'DUPLICATE_CASE')
        trio = {'configs/' + case + '-' + kind + '.json' for kind in ('acceptance', 'plan', 'worker')}
        require(trio <= set(files), 'CASE_INPUT_MISSING')
        required.update(trio)
        config = parsed(files['configs/' + name])
        require(type(config) is dict and config.get('helper_path') == SIDECAR + '/public-job.py'
                and config.get('helper_sha256') == 'sha256:' + sha(files['sidecar/public-job.py'])
                and config.get('plan_path') == CONFIG + '/' + case + '-plan.json'
                and config.get('plan_sha256') == 'sha256:' + sha(files['configs/' + case + '-plan.json'])
                and config.get('worker_config_path') == CONFIG + '/' + case + '-worker.json'
                and config.get('worker_config_sha256') == 'sha256:' + sha(files['configs/' + case + '-worker.json'])
                and config.get('submission_state_directory') == '/var/lib/probe-supervised-submit/' + case
                and config.get('trusted_state_directory') == '/var/lib/probe-core/supervised/' + case,
                'CASE_PATH_OR_HASH_BINDING_INVALID')
        require(all(type(config.get(key)) is int and config[key] > 0
                    for key in ('service_uid', 'research_uid', 'admin_uid')), 'CASE_UID_INVALID')
        cases[case] = config
    require({name for name in files if name.startswith('configs/')} == required, 'UNREFERENCED_CONFIGURATION')
    for name in UNITS:
        validate_unit(files['units/' + name], name)
    return cases


PREFLIGHT = '''import importlib.util,json,pathlib,sys
root=pathlib.Path('/opt/probe-core/supervised')
spec=importlib.util.spec_from_file_location('probe_supervised_entry',root/'supervised-entry.py')
entry=importlib.util.module_from_spec(spec);sys.modules[spec.name]=entry;spec.loader.exec_module(entry)
runner=entry.load()
configurations=json.loads(sys.stdin.read())
for path in configurations:
    config,plan,worker=runner.load_inputs(pathlib.Path(path),mode=sys.argv[1])
    if sys.argv[1]=='run':
        from probe_core.runpod_provider import RunPodConfig
        provider=RunPodConfig.load(config.provider_config_path)
        if provider.launch.digest!=config.deployment.launch_config_hash or provider.launch.image_repository!=config.deployment.image_repository:
            raise ValueError('INSTALLED_PROVIDER_LAUNCH_MISMATCH')
print(json.dumps({'passed':True,'count':len(configurations),'mode':sys.argv[1]}))
'''


class Installer:
    def __init__(self, *, root=Path('/'), owner=0, run=subprocess.run):
        self.root, self.owner, self.run = Path(root), owner, run
        self.stage = 'initialization'

    def path(self, absolute):
        return self.root / absolute.lstrip('/')

    def execute(self, command, *, input=None, timeout=60):
        try:
            result = self.run(command, input=input, capture_output=True, text=True, timeout=timeout,
                cwd='/', env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
        except (OSError, subprocess.TimeoutExpired):
            raise InstallError('COMMAND_UNAVAILABLE_' + self.stage.upper()) from None
        require(result.returncode == 0, 'COMMAND_FAILED_' + self.stage.upper())
        return result.stdout

    def accounts(self, cases):
        records = {name: pwd.getpwnam(name) for name in {'probe-trusted', 'probe-research', 'probe-watchdog'}}
        require(len({record.pw_uid for record in records.values()}) == 3
                and all(record.pw_uid > 0 for record in records.values()), 'SEPARATE_INSTALLED_IDENTITIES_REQUIRED')
        for config in cases.values():
            require(config['service_uid'] == records['probe-trusted'].pw_uid
                    and config['research_uid'] == records['probe-research'].pw_uid
                    and config['admin_uid'] not in {0, *[item.pw_uid for item in records.values()]},
                    'INSTALLED_ACCOUNT_MISMATCH')
            pwd.getpwuid(config['admin_uid'])
        return records

    def show(self, unit):
        raw = self.execute(['/usr/bin/systemctl', 'show', unit, '--property=LoadState,ActiveState,SubState,User,Group,MainPID,ControlPID,FragmentPath,DropInPaths'])
        result = {}
        for line in raw.splitlines():
            key, separator, value = line.partition('=')
            require(separator and key not in result, 'SERVICE_READBACK_INVALID')
            result[key] = value
        return result

    def runtime(self, accounts):
        require(self.path(PYTHON).is_file(), 'INSTALLED_RUNTIME_MISSING')
        for unit, (account, group) in CORE_UNITS.items():
            state = self.show(unit)
            require(state.get('LoadState') == 'loaded' and state.get('ActiveState') == 'active'
                    and state.get('SubState') == 'running' and state.get('User') == account
                    and state.get('Group') == group and state.get('MainPID', '').isdigit()
                    and int(state['MainPID']) > 0, 'CORE_SERVICE_IDENTITY_UNAVAILABLE')
            identity = self.path('/proc/' + state['MainPID'] + '/status').read_text()
            uid = next((line.split()[1:] for line in identity.splitlines() if line.startswith('Uid:')), None)
            require(uid == [str(accounts[account].pw_uid)] * 4, 'CORE_PROCESS_IDENTITY_CHANGED')
        for unit in (*UNITS, 'probe-calibration-run.service', 'probe-calibration-submit.service'):
            state = self.show(unit)
            require(state.get('ActiveState') in {'inactive', 'failed'}
                    and state.get('MainPID') == state.get('ControlPID') == '0', 'CALIBRATION_SERVICE_ALREADY_ACTIVE')
            if unit in UNITS:
                require(not state.get('DropInPaths'), 'SUPERVISED_UNIT_OVERRIDE_PRESENT')
        # Isolated installed imports only; no model, service object, DB or API call.
        self.execute([PYTHON, '-I', '-c', 'from probe_core.gpu_acceptance_runner import RunnerConfig; '
            'from probe_core.dispatcher import Dispatcher; from probe_core.worker_contracts import ExecutionReceipt; '
            'from probe_core.runpod_provider import RunPodConfig; print("installed-imports-ok")'])

    def check_parents(self, path, *, owners=None):
        owners = {self.owner} if owners is None else owners
        current = self.root
        for part in path.relative_to(self.root).parts[:-1]:
            current /= part
            if current.exists() or current.is_symlink():
                info = current.lstat()
                require(stat.S_ISDIR(info.st_mode) and info.st_uid in owners
                        and not info.st_mode & 0o022, 'DESTINATION_PARENT_UNSAFE')

    def existing(self, path, raw):
        self.check_parents(path)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == self.owner
                    and not info.st_mode & 0o022 and info.st_nlink == 1, 'EXISTING_FILE_UNSAFE')
            require(bounded_read(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) == raw, 'EXISTING_FILE_DIFFERS')

    def directory(self, path, uid, gid, mode):
        # Service-owned state parents must not turn root mkdir/chown into a
        # symlink traversal. Pin every ancestor, then operate on the opened dir.
        parts = path.relative_to(self.root).parts
        parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        target = None
        try:
            for part in parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = following
                info = os.fstat(parent)
                require(info.st_uid in {self.owner, uid} and not info.st_mode & 0o022,
                        'DESTINATION_PARENT_UNSAFE')
            created = False
            try:
                os.mkdir(parts[-1], mode=mode, dir_fd=parent)
                created = True
            except FileExistsError:
                pass
            target = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            if created:
                os.fchown(target, uid, gid)
                os.fchmod(target, mode)
            info = os.fstat(target)
            require(info.st_uid == uid and info.st_gid == gid and stat.S_IMODE(info.st_mode) == mode,
                    'STATE_DIRECTORY_BOUNDARY_CHANGED')
        finally:
            if target is not None:
                os.close(target)
            os.close(parent)

    def publish(self, path, raw):
        self.existing(path, raw)
        if path.exists():
            return
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chown(temporary, self.owner, 0 if self.owner == 0 else os.getegid())
            temporary.chmod(0o644)
            # Exclusive publication cannot overwrite a changed existing file.
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
            folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(folder)
            finally: os.close(folder)
        finally:
            temporary.unlink(missing_ok=True)

    def preflight(self, cases):
        paths = [CONFIG + '/' + case + '-acceptance.json' for case in cases]
        for account, mode in (('probe-research', 'submit'), ('probe-trusted', 'run')):
            raw = self.execute(['/usr/sbin/runuser', '-u', account, '-g', account, '--', PYTHON,
                                '-I', '-c', PREFLIGHT, mode], input=json.dumps(paths))
            require(parsed(raw) == {'passed': True, 'count': len(paths), 'mode': mode}, 'SIDECAR_IDENTITY_PREFLIGHT_FAILED')

    def install(self, files, manifest_sha256):
        self.stage = 'capsule_validation'
        cases = describe(files)
        accounts = self.accounts(cases)
        self.stage = 'installed_runtime_preflight'
        self.runtime(accounts)
        destinations = {}
        for name, raw in files.items():
            if name.startswith('sidecar/'):
                absolute = SIDECAR + '/' + name.split('/')[1]
            elif name.startswith('configs/'):
                absolute = CONFIG + '/' + name.split('/')[1]
            elif name.startswith('units/'):
                absolute = SYSTEMD + '/' + name.split('/')[1]
            else:
                continue
            destinations[self.path(absolute)] = raw
        # Detect every conflict before publishing any sidecar/configuration.
        self.stage = 'destination_preflight'
        for path, raw in destinations.items():
            self.existing(path, raw)
        gid = 0 if self.owner == 0 else os.getegid()
        self.stage = 'sidecar_installation'
        for absolute in (SIDECAR, CONFIG):
            self.directory(self.path(absolute), self.owner, gid, 0o755)
        for account, parent in (('probe-trusted', '/var/lib/probe-core/supervised'),
                                ('probe-research', '/var/lib/probe-supervised-submit')):
            record = accounts[account]
            self.directory(self.path(parent), record.pw_uid, record.pw_gid, 0o700)
            for case in cases:
                self.directory(self.path(parent + '/' + case), record.pw_uid, record.pw_gid, 0o700)
        for path, raw in destinations.items():
            self.publish(path, raw)
        self.stage = 'account_input_preflight'
        self.preflight(cases)
        # Recheck no older or competing calibration became active during copy.
        self.stage = 'runtime_recheck'
        self.runtime(accounts)
        self.stage = 'daemon_reload'
        self.execute(['/usr/bin/systemctl', 'daemon-reload'])
        self.stage = 'suite_start'
        self.execute(['/usr/bin/systemctl', 'start', 'probe-supervised-run.service'], timeout=330)
        return {'schema_version': 1, 'status': 'prepared', 'manifest_sha256': manifest_sha256,
                'case_count': len(cases), 'wheel_changed': False, 'credentials_changed': False,
                'automatic_approval': False, 'automatic_compute_start': False,
                'autoboot_enabled': False, 'run_service_started': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args(argv)
    require(os.getresuid() == (0, 0, 0) and sys.flags.isolated, 'ROOT_ISOLATED_RUNTIME_REQUIRED')
    os.umask(0o077)
    files = read_capsule(args.manifest, args.manifest_sha256)
    result = Installer().install(files, args.manifest_sha256)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status': 'refused', 'code': str(error) if isinstance(error, InstallError)
                          else type(error).__name__}), file=sys.stderr, flush=True)
        raise SystemExit(1)
