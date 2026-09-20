#!/usr/bin/env python3
"""Retire one never-approved proposal and prepare the same calibration elsewhere.

Configuration only: no application/image upgrade, approval, provider mutation,
credential change or direct ledger write. Run from a hash-pinned root capsule
using the verified installed interpreter. Partial operations remain guarded.
"""
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time
import types

ROOT = Path('/opt/probe-core')
PUBLIC = Path('/etc/probe-calibration')
SUBMIT = Path('/var/lib/probe-calibration-submit')
STATE = Path('/var/lib/probe-core/gpu-acceptance')
UNITS = Path('/etc/systemd/system')
OUTBOX = Path('/var/lib/probe-backups/outbox')
RECEIPTS = Path('/var/lib/probe-backups/receipts')
CALIBRATION = ('probe-calibration-submit.service', 'probe-calibration-run.service')
INPUTS = {'plan.json', 'acceptance.json', 'worker.json', *CALIBRATION}
HELPERS = {'activate-gpu-calibration.py', 'upgrade-controller.py', 'retarget-gpu-calibration.py'}
FILES = HELPERS | {'old/' + name for name in INPUTS} | {'new/' + name for name in INPUTS}
HEX = re.compile(r'[0-9a-f]{64}\Z')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
MAX_BACKUP = 64 * 1024**2


class RetargetError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise RetargetError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, *, owner=0, limit=1048576):
    path = Path(path).absolute()
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, owner} and not info.st_mode & 0o022,
                'UNTRUSTED_PARENT')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
                and not info.st_mode & 0o022 and info.st_size <= limit, 'UNTRUSTED_FILE')
        raw = stream.read(limit + 1)
        require(len(raw) == info.st_size, 'INPUT_CHANGED')
        return raw


def decoded(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, 'DUPLICATE_KEY')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: require(False, 'NONFINITE_JSON'))


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def inputs(path, pin, *, owner=0):
    raw = read(path, owner=owner)
    require(type(pin) is str and HEX.fullmatch(pin) and digest(raw) == pin, 'MANIFEST_PIN_CHANGED')
    body = decoded(raw)
    require(set(body) == {'schema_version', 'original_manifest_sha256', 'installed_upgrade', 'retarget_id',
                         'from_region', 'to_region', 'pending', 'files'}
            and type(body['schema_version']) is int and body['schema_version'] == 1, 'MANIFEST_SCHEMA')
    require(type(body['original_manifest_sha256']) is str and HEX.fullmatch(body['original_manifest_sha256'])
            and type(body['retarget_id']) is str and re.fullmatch(r'[0-9a-f]{32}', body['retarget_id'])
            and body['from_region'] == 'EU-CZ-1' and body['to_region'] == 'EU-RO-1', 'RETARGET_SCOPE_CHANGED')
    reference = body['installed_upgrade']
    require(type(reference) is dict and set(reference) == {'wheel_sha256', 'release_manifest_sha256'}
            and all(type(value) is str and HEX.fullmatch(value) for value in reference.values()), 'INSTALLED_RELEASE_PIN_INVALID')
    require(type(body['pending']) is dict and set(body['pending']) == {'request_id', 'worker_id', 'job_id'}
            and all(type(value) is str and IDENT.fullmatch(value) for value in body['pending'].values()), 'PENDING_IDENTITY_INVALID')
    require(type(body['files']) is dict and set(body['files']) == FILES
            and all(type(value) is str and HEX.fullmatch(value) for value in body['files'].values()), 'FILE_PINS_INVALID')
    files = {name: read(path.parent / name, owner=owner) for name in sorted(FILES)}
    require(all(digest(value) == body['files'][name] for name, value in files.items()), 'FILE_PIN_CHANGED')
    return raw, body, files


def helper(raw, name):
    module = types.ModuleType(name)
    module.__file__ = name
    exec(compile(raw, name, 'exec'), module.__dict__)
    return module


def validate_retarget(old, new, retarget_id, from_region, to_region):
    from probe_core.gpu_acceptance import AcceptancePlan
    from probe_core.gpu_acceptance_runner import RunnerConfig, validate_plan
    from probe_core.worker_contracts import WorkerConfig
    require(from_region == 'EU-CZ-1' and to_region == 'EU-RO-1', 'RETARGET_REGION_INVALID')
    before_plan, plan = decoded(old['plan.json']), decoded(new['plan.json'])
    require(len(before_plan['cases']) == len(plan['cases']) == 1
            and before_plan['cases'][0]['name'] == 'backend-parity'
            and plan['label'] != before_plan['label']
            and plan['cases'][0]['spec']['idempotency_key'] != before_plan['cases'][0]['spec']['idempotency_key'],
            'FRESH_PARITY_PLAN_REQUIRED')
    compare = decoded(new['plan.json'])
    compare['label'] = before_plan['label']
    compare['cases'][0]['spec']['idempotency_key'] = before_plan['cases'][0]['spec']['idempotency_key']
    require(compare == before_plan, 'PLAN_SCOPE_CHANGED')
    before, after = decoded(old['acceptance.json']), decoded(new['acceptance.json'])
    worker_before, worker_after = decoded(old['worker.json']), decoded(new['worker.json'])
    for config, document, raw in ((before, worker_before, old['worker.json']), (after, worker_after, new['worker.json'])):
        worker = WorkerConfig.model_validate(document)
        require(config['worker_config_sha256'] == 'sha256:' + digest(raw)
                and worker.region == config['deployment']['region']
                and worker.code_git_commit == config['source_commit']
                and worker.container_image_digest == config['deployment']['image_digest']
                and worker.live_price_usd_per_hour == config['expected_worker_price_usd_per_hour'], 'WORKER_BINDING_CHANGED')
    require(before['deployment']['region'] == from_region and worker_after == dict(worker_before, region=to_region),
            'WORKER_SCOPE_CHANGED')
    public = PUBLIC / ('retarget-' + retarget_id)
    expected = dict(before, deployment=dict(before['deployment'], region=to_region),
                    plan_path=str(public / 'plan.json'), plan_sha256='sha256:' + digest(new['plan.json']),
                    worker_config_path=str(public / 'worker.json'), worker_config_sha256='sha256:' + digest(new['worker.json']),
                    submission_state_directory=str(SUBMIT / ('retarget-' + retarget_id)),
                    trusted_state_directory=str(STATE / ('retarget-' + retarget_id)))
    require(after == expected, 'RUNNER_SCOPE_CHANGED')
    for name in CALIBRATION:
        search = ('--config ' + str(Path(before['plan_path']).parent / 'acceptance.json')).encode()
        replacement = ('--config ' + str(public / 'acceptance.json')).encode()
        require(old[name].count(search) == 1 and new[name] == old[name].replace(search, replacement), 'UNIT_SCOPE_CHANGED')
    for config, document in ((before, before_plan), (after, plan)):
        validate_plan(RunnerConfig.model_validate(config), AcceptancePlan.model_validate(document))
    return public, after


# Fixed read-only queries. A pending proposal has no approval, attempt, provider
# intent, deadline or observed provider identity. Inventory absence is positive.
SNAPSHOT = r'''
import json,sqlite3,time
from pathlib import Path
from probe_core.runpod_provider import RunPodConfig,RunPodHTTP
pending=json.loads(PENDING_JSON)
def rows(path,query,values=()):
    connection=sqlite3.connect(Path(path).as_uri()+"?mode=ro",uri=True,timeout=5)
    connection.row_factory=sqlite3.Row
    until=time.monotonic()+10
    connection.set_progress_handler(lambda:int(time.monotonic()>until),10000)
    try:return [dict(row) for row in connection.execute(query,values)]
    finally:connection.close()
ledger="/var/lib/probe-core/research.sqlite"
provider="/var/lib/probe-provider/runpod.sqlite"
result={"jobs":rows(ledger,"SELECT * FROM jobs WHERE job_id=?",(pending["job_id"],)),
        "requests":rows(ledger,"SELECT * FROM compute_requests WHERE request_id=?",(pending["request_id"],)),
        "attempts":rows(ledger,"SELECT attempt_id FROM attempts WHERE job_id=?",(pending["job_id"],)),
        "intents":rows(provider,"SELECT worker_id FROM runpod_intents WHERE worker_id=? OR request_key=?",(pending["worker_id"],pending["request_id"]))}
result["other_unfinished_jobs"]=rows(ledger,"SELECT count(*) AS n FROM jobs WHERE job_id!=? AND state NOT IN ('COMPLETED','FAILED')",(pending["job_id"],))[0]["n"]
result["unconfirmed_attempts"]=rows(ledger,"SELECT count(*) AS n FROM attempts WHERE stopped_at IS NULL")[0]["n"]
result["open_approvals"]=rows(ledger,"SELECT count(*) AS n FROM approvals WHERE ended_at IS NULL")[0]["n"]
result["unresolved_requests"]=rows(ledger,"SELECT count(*) AS n FROM compute_requests WHERE state NOT IN ('PENDING','STOPPED','REJECTED')")[0]["n"]
if len(result["requests"])==1:
    approval=result["requests"][0]["approval_id"]
    result["approvals"]=rows(ledger,"SELECT approval_id FROM approvals WHERE approval_id=?",(approval,))
    result["approval_jobs"]=rows(ledger,"SELECT job_id FROM approval_jobs WHERE job_id=? OR approval_id=?",(pending["job_id"],approval))
else:result["approvals"],result["approval_jobs"]=[],[]
config=RunPodConfig.load("/etc/probe-core/runpod.json")
inventory=RunPodHTTP(config.api_key_file).request("GET","/v2/pods?includeClusterPods=true&limit=1000")
assert type(inventory.get("pods")) is list and inventory.get("pagination",{}).get("hasNextPage") is False
result["pods"]=len(inventory["pods"])
print(json.dumps(result,sort_keys=True))
'''


def validate_pending(snapshot, pending, plan, config, submitted, *, cancelled=False):
    from probe_core.gpu_acceptance_runner import RunnerConfig
    from probe_core.schemas import JobSpec
    spec = JobSpec.model_validate(plan['cases'][0]['spec'])
    runner = RunnerConfig.model_validate(config)
    require(all(type(snapshot.get(name)) is int and snapshot[name] == 0 for name in
                ('other_unfinished_jobs', 'unconfirmed_attempts', 'open_approvals', 'unresolved_requests', 'pods')),
            'AUTHORITY_OR_OTHER_WORK_PRESENT')
    require(len(snapshot['jobs']) == len(snapshot['requests']) == 1
            and all(snapshot[name] == [] for name in ('attempts', 'approvals', 'approval_jobs', 'intents')), 'PENDING_AUTHORITY_PRESENT')
    job, request = snapshot['jobs'][0], snapshot['requests'][0]
    require(job['job_id'] == pending['job_id'] and decoded(job['spec_json']) == spec.model_dump(mode='json')
            and job['attempt_id'] is None and job['worker_id'] is None and job['approval_id'] is None
            and job['lease_expires_at'] is None and job['attempt_count'] == job['retry_count'] == 0, 'PENDING_JOB_CHANGED')
    require((job['state'] == 'PENDING' and job['failure_kind'] is None and job['failure_reason'] is None and not cancelled)
            or (job['state'] == 'FAILED' and job['failure_kind'] == 'cancelled'
                and job['failure_reason'] == 'research client cancellation' and cancelled), 'PENDING_JOB_STATE_CHANGED')
    require(request['request_id'] == pending['request_id'] and request['worker_id'] == pending['worker_id']
            and request['state'] == 'PENDING' and request['action'] == 'CREATE'
            and decoded(request['job_ids']) == [pending['job_id']] and request['infrastructure'] is None
            and request['replaces_worker_id'] is None and request['deadline'] is None
            and request['observed_provider_id'] is None and request['last_error_code'] is None
            and decoded(request['configuration']) == runner.deployment.model_dump(mode='json')
            and request['configuration_hash'] == runner.deployment.digest
            and request['max_runtime_seconds'] == runner.max_runtime_seconds, 'PENDING_REQUEST_CHANGED')
    expected = {**pending, 'approval_id': request['approval_id'], 'batch_hash': request['batch_hash'],
                'configuration_hash': request['configuration_hash'], 'approval_consumed_by_runner': False}
    require(submitted == expected, 'SUBMISSION_BINDING_CHANGED')


class Retarget:
    def __init__(self, activation, upgrader, manifest, files, pin):
        self.a, self.u, self.m, self.files, self.pin = activation, upgrader, manifest, files, pin
        self.operation = activation.Activation()
        self.work = ROOT / 'calibration-retargets' / pin
        self.stage = 'validation'
        self.closed = False

    def record(self, name, value):
        path = self.work / name
        raw = value if isinstance(value, bytes) else encoded(value)
        if path.exists() or path.is_symlink():
            require(read(path) == raw, 'RECORD_CHANGED')
        else:
            self.a.write_file(path, raw, uid=0, gid=0, mode=0o600)

    def baseline(self):
        reference = self.m['installed_upgrade']
        _, raw, _, _, _ = self.u.verify_baseline(ROOT, self.m['original_manifest_sha256'], reference, owner=0)
        require(digest(raw) == reference['wheel_sha256'], 'INSTALLED_WHEEL_CHANGED')

    def unit_state(self, name):
        raw = self.operation.ctl('show', name, '--property=LoadState,FragmentPath,ActiveState,MainPID,ControlPID,DropInPaths')
        return dict(line.split('=', 1) for line in raw.decode().splitlines())

    def initialize(self, human, manifest_raw):
        self.baseline()
        self.human = human
        self.operation.users = tuple(pwd.getpwnam(name).pw_uid for name in ('probe-trusted', 'probe-research', human))
        require(len(set(self.operation.users)) == 3 and min(self.operation.users) > 0, 'IDENTITIES_CHANGED')
        self.old = {name: self.files['old/' + name] for name in INPUTS}
        self.new = {name: self.files['new/' + name] for name in INPUTS}
        self.public, self.config = validate_retarget(self.old, self.new, self.m['retarget_id'], self.m['from_region'], self.m['to_region'])
        self.old_config = decoded(self.old['acceptance.json'])
        require((self.config['service_uid'], self.config['research_uid'], self.config['admin_uid']) == self.operation.users,
                'RUNNER_IDENTITIES_CHANGED')
        for name, path in (('plan.json', Path(self.old_config['plan_path'])),
                           ('acceptance.json', Path(self.old_config['plan_path']).parent / 'acceptance.json'),
                           ('worker.json', Path(self.old_config['worker_config_path']))):
            require(read(path) == self.old[name], 'OLD_INPUT_CHANGED')
        self.submitted = decoded(read(Path(self.old_config['submission_state_directory']) / 'submitted.json', owner=self.operation.users[1]))
        old_state = Path(self.old_config['trusted_state_directory'])
        require(not any(os.path.lexists(old_state / name) for name in ('result.json', 'bound-request.json', 'startup-window.json')),
                'OLD_RUNNER_ACQUIRED_AUTHORITY')
        from probe_core.gpu_acceptance_runner import RunnerConfig, digest as canonical_digest
        expected = {'config_sha256': canonical_digest(RunnerConfig.model_validate(self.old_config).model_dump(mode='json')),
                    'plan_sha256': self.old_config['plan_sha256']}
        require(decoded(read(old_state / 'binding.json', owner=self.operation.users[0])) == expected, 'OLD_RUNNER_BINDING_CHANGED')
        for name, mode in zip(CALIBRATION, ('submit', 'run')):
            require(read(UNITS / name) == self.old[name], 'INSTALLED_UNIT_CHANGED')
            standard = self.old[name].replace(('--config ' + str(Path(self.old_config['plan_path']).parent / 'acceptance.json')).encode(),
                                             b'--config /etc/probe-calibration/acceptance.json')
            self.a.validate_unit(standard, mode)
            values = self.unit_state(name)
            require(values.get('LoadState') == 'loaded' and values.get('FragmentPath') == str(UNITS / name)
                    and values.get('DropInPaths') == '', 'CALIBRATION_UNIT_CHANGED')
        self.operation.ready()
        release_dir = ROOT / 'upgrades' / self.m['installed_upgrade']['wheel_sha256']
        self.release = decoded(read(release_dir / 'upgrade-release.json'))
        self.checker = read(release_dir / 'verify-installed-identities.py')
        require(digest(self.checker) == self.release['identity_checker_sha256'], 'CHECKER_PIN_CHANGED')
        self.operation.reader = self.a.extract_reader(self.checker)
        self.paths = (self.public, Path(self.config['submission_state_directory']), Path(self.config['trusted_state_directory']))
        self.marker = self.public / 'prepared'
        self.guard_name = '50-probe-calibration-retarget.conf'
        self.guard = ('[Unit]\nConditionPathExists=' + str(self.marker) + '\n').encode()
        require(not os.path.lexists(self.work) and all(not os.path.lexists(path) for path in self.paths), 'RETARGET_TARGET_EXISTS')
        for path in (self.work.parent,):
            if path.exists(): self.a.trusted(path)
            else: path.mkdir(mode=0o700)
        self.work.mkdir(mode=0o700)
        self.record('manifest.json', manifest_raw)
        self.record('verify-installed-identities.py', self.checker)
        for name, raw in self.files.items():
            if '/' in name: (self.work / name).parent.mkdir(mode=0o700, exist_ok=True)
            self.record(name, raw)

    def snapshot(self, pending=None):
        script = 'PENDING_JSON=' + repr(json.dumps(pending or self.m['pending'])) + '\n' + SNAPSHOT
        return decoded(self.operation.command(['/usr/sbin/runuser', '-u', 'probe-trusted', '-g', 'probe-trusted', '--',
            str(ROOT / 'venv/bin/python'), '-I', '-c', script], timeout=60))

    def verify_old(self, *, cancelled=False):
        result = self.snapshot()
        validate_pending(result, self.m['pending'], decoded(self.old['plan.json']), self.old_config, self.submitted, cancelled=cancelled)
        return result

    def backup(self):
        self.stage = 'backup'
        checker = helper(self.checker, 'pinned_retarget_identity_checker')
        outbox = OUTBOX
        previous = set(outbox.glob('probe-*.tar'))
        backup_uid = pwd.getpwnam('probe-backup').pw_uid
        # The first successful run may only drain old pending snapshots. A
        # second ordinary run then creates and verifies a current snapshot.
        for _ in range(2):
            self.operation.command(['/usr/bin/systemctl', 'start', 'probe-backup.service'], timeout=360)
            values = self.operation.ctl('show', 'probe-backup.service', '--property=Result,ExecMainStatus').decode().splitlines()
            require(dict(line.split('=', 1) for line in values) == {'Result': 'success', 'ExecMainStatus': '0'},
                    'BACKUP_SERVICE_NOT_SUCCESSFUL')
            archive = checker.completed_archive(outbox=outbox, receipts=RECEIPTS,
                trusted_uid=self.operation.users[0], backup_uid=backup_uid)
            if archive not in previous:
                raw = read(archive, owner=self.operation.users[0], limit=MAX_BACKUP)
                checksum = digest(raw)
                receipts = list(RECEIPTS.glob('*.json'))
                require(len(receipts) <= 100, 'BACKUP_RECEIPTS_UNBOUNDED')
                matching = [decoded(read(path, owner=backup_uid, limit=65536)) for path in receipts]
                matching = [value for value in matching if value.get('archive_sha256') == checksum]
                require(len(matching) == 1 and all(matching[0].get(key) is True for key in
                        ('verified', 'readback_verified', 'restore_verified')), 'BACKUP_NOT_VERIFIED')
                self.record('before.tar', raw)
                self.record('backup.json', {**matching[0], 'retained_archive': str(self.work / 'before.tar')})
                return
        raise RetargetError('FRESH_BACKUP_REQUIRED')

    def guards(self):
        for name in CALIBRATION:
            directory = UNITS / (name + '.d')
            directory.mkdir(mode=0o755, exist_ok=True)
            self.a.trusted(directory)
            require(set(directory.iterdir()) <= {directory / self.guard_name}, 'CALIBRATION_OVERRIDE_PRESENT')
            self.a.write_file(directory / self.guard_name, self.guard, uid=0, gid=0, mode=0o644)
        self.operation.ctl('daemon-reload')

    def stop(self):
        self.operation.ctl('stop', *CALIBRATION)
        for name in CALIBRATION:
            state = self.unit_state(name)
            require(state.get('ActiveState') in {'inactive', 'failed'} and state.get('MainPID') == state.get('ControlPID') == '0',
                    'CALIBRATION_NOT_STOPPED')

    def cancel(self):
        self.stage = 'cancel'
        self.record('before-cancel.json', self.verify_old())
        self.record('cancel-intent.json', {'manifest_sha256': self.pin, **self.m['pending']})
        script = ('import json\nfrom probe_core.rpc import UnixRPCClient\nrpc=UnixRPCClient("/run/probe-research/research.sock",expected_server_uid='
                  + str(self.operation.users[0]) + ')\nprint(json.dumps(rpc.call("cancel_job",{"job_id":'
                  + repr(self.m['pending']['job_id']) + '})))\n')
        self.operation.command(['/usr/sbin/runuser', '-u', 'probe-research', '-g', 'probe-research', '--',
                                str(ROOT / 'venv/bin/python'), '-I', '-c', script], timeout=30)
        self.record('cancelled.json', self.verify_old(cancelled=True))

    def identities(self):
        self.stage = 'identity_verification'
        self.baseline()
        self.operation.command(['/usr/bin/python3', '-I', str(self.work / 'verify-installed-identities.py'),
            '--human', self.human, '--output', str(self.work / 'identity-acceptance.json')], timeout=120)
        self.a.identity_gate(decoded(read(self.work / 'identity-acceptance.json')))
        self.operation.ready()
        self.operation.idle()

    def prepare(self):
        self.stage = 'prepare'
        self.verify_old(cancelled=True)
        for path, uid, gid in ((self.paths[0], 0, 0), (self.paths[1], self.operation.users[1], grp.getgrnam('probe-research').gr_gid),
                               (self.paths[2], self.operation.users[0], grp.getgrnam('probe-trusted').gr_gid)):
            path.mkdir(mode=0o755 if uid == 0 else 0o700)
            os.chown(path, uid, gid)
            os.chmod(path, 0o755 if uid == 0 else 0o700)
        for name in ('plan.json', 'acceptance.json', 'worker.json'):
            self.a.write_file(self.public / name, self.new[name], uid=0, gid=0, mode=0o444)
        for name in CALIBRATION:
            self.a.write_file(UNITS / name, self.new[name], uid=0, gid=0, mode=0o644)
        self.operation.ctl('daemon-reload')
        self.operation.idle()
        self.a.write_file(self.marker, b'Pinned region retarget prepared; compute requires human approval.\n', uid=0, gid=0, mode=0o600)
        self.operation.ctl('start', CALIBRATION[0])
        submitted = decoded(read(self.paths[1] / 'submitted.json', owner=self.operation.users[1]))
        pending = {key: submitted[key] for key in ('request_id', 'worker_id', 'job_id')}
        require(all(value != self.m['pending'][key] for key, value in pending.items())
                and submitted['approval_id'] != self.submitted['approval_id'], 'FRESH_SUBMISSION_REQUIRED')
        validate_pending(self.snapshot(pending), pending, decoded(self.new['plan.json']), self.config, submitted)
        self.operation.ctl('start', CALIBRATION[1])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.unit_state(CALIBRATION[1])
            require(state.get('ActiveState') not in {'failed', 'inactive'}, 'NEW_RUNNER_EXITED')
            if state.get('ActiveState') == 'active' and state.get('MainPID', '0').isdigit() and int(state['MainPID']) > 0:
                binding = self.paths[2] / 'binding.json'
                if binding.exists():
                    from probe_core.gpu_acceptance_runner import RunnerConfig, digest as canonical_digest
                    expected = {'config_sha256': canonical_digest(RunnerConfig.model_validate(self.config).model_dump(mode='json')),
                                'plan_sha256': self.config['plan_sha256']}
                    require(decoded(read(binding, owner=self.operation.users[0])) == expected, 'NEW_RUNNER_BINDING_CHANGED')
                    break
            time.sleep(.1)
        else:
            raise RetargetError('NEW_RUNNER_NOT_READY')
        validate_pending(self.snapshot(pending), pending, decoded(self.new['plan.json']), self.config, submitted)
        # Keep these exact marker guards. Later service restarts still depend on
        # the fully published capsule, and partial installations never launch.
        receipt = {'schema_version': 1, 'status': 'prepared', 'manifest_sha256': self.pin,
                   'installed_wheel_sha256': self.m['installed_upgrade']['wheel_sha256'], 'submission': submitted,
                   'original_job_cancelled': True, 'old_history_preserved': True, 'application_reinstalled': False,
                   'approval_issued': False, 'cloud_mutations_performed': False}
        self.record('prepare-report.json', receipt)
        return receipt

    def execute(self):
        self.record('before.json', self.verify_old())
        self.backup()
        self.verify_old()
        try:
            self.stage = 'calibration_stop'
            self.guards()
            self.stop()
            self.cancel()
            self.identities()
            return self.prepare()
        except BaseException:
            errors = []
            for action in (lambda: self.marker.unlink(missing_ok=True), self.guards, self.stop):
                try: action()
                except BaseException: errors.append(True)
            self.closed = not errors
            self.record('failure.json', {'status': 'failed', 'stage': self.stage, 'fail_closed_confirmed': self.closed})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--human', required=True)
    args = parser.parse_args(argv)
    operation = None
    safe_errors = (RetargetError,)
    try:
        require(os.geteuid() == 0, 'ADMINISTRATOR_REQUIRED')
        os.umask(0o077)
        raw, manifest, files = inputs(args.manifest, args.manifest_sha256)
        require(read(Path(__file__)) == files['retarget-gpu-calibration.py'], 'EXECUTED_HELPER_CHANGED')
        activation = helper(files['activate-gpu-calibration.py'], 'pinned_retarget_activation')
        upgrader = helper(files['upgrade-controller.py'], 'pinned_retarget_upgrader')
        safe_errors = (RetargetError, activation.ActivationError, upgrader.UpgradeError)
        operation = Retarget(activation, upgrader, manifest, files, args.manifest_sha256)
        operation.initialize(args.human, raw)
        report = operation.execute()
    except BaseException as error:
        report = {'schema_version': 1, 'status': 'failed', 'stage': operation.stage if operation else 'validation',
                  'reason': str(error) if isinstance(error, safe_errors) else 'RETARGET_UNAVAILABLE',
                  'fail_closed_confirmed': operation.closed if operation else False,
                  'approval_issued': False, 'cloud_mutations_performed': False}
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'prepared' else 1


if __name__ == '__main__':
    raise SystemExit(main())
