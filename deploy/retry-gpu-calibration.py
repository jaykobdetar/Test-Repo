#!/usr/bin/env python3
"""Close one undispatched failed calibration, then prepare its pinned replacement.

The two phases straddle the ordinary verified wheel upgrade. Neither phase
approves compute or writes the ledger directly; cancellation/submission use the
existing research identity. Existing evidence and credentials are retained.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import grp
import re
import stat
import time
import types

ROOT = Path('/opt/probe-core')
PUBLIC = Path('/etc/probe-calibration')
SUBMIT = Path('/var/lib/probe-calibration-submit')
STATE = Path('/var/lib/probe-core/gpu-acceptance')
UNITS = Path('/etc/systemd/system')
CALIBRATION = ('probe-calibration-submit.service', 'probe-calibration-run.service')
FILES = {'activate-gpu-calibration.py', 'upgrade-controller.py', 'plan.json', 'acceptance.json', *CALIBRATION}
HEX = re.compile(r'[0-9a-f]{64}\Z')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')


class RetryError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise RetryError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, *, owner=0, limit=1048576):
    path = Path(path).absolute()
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, owner} and not info.st_mode & 0o022,
                'UNTRUSTED_PARENT')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
                and not info.st_mode & 0o022 and info.st_size <= limit, 'UNTRUSTED_FILE')
        raw = stream.read(limit+1)
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
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def inputs(path, pin, *, owner=0):
    raw = read(path, owner=owner)
    require(HEX.fullmatch(pin) and digest(raw) == pin, 'MANIFEST_PIN_CHANGED')
    body = decoded(raw)
    require(set(body) == {'schema_version', 'original_manifest_sha256', 'previous_upgrade', 'target_upgrade',
                         'previous_activation_manifest_sha256', 'failed', 'retry_id', 'files'}
            and type(body['schema_version']) is int and body['schema_version'] == 1, 'MANIFEST_SCHEMA')
    require(HEX.fullmatch(body['original_manifest_sha256']) and HEX.fullmatch(body['previous_activation_manifest_sha256'])
            and re.fullmatch(r'[0-9a-f]{32}', body['retry_id']), 'MANIFEST_IDENTITY')
    for name in ('previous_upgrade', 'target_upgrade'):
        value = body[name]
        require(type(value) is dict and set(value) == {'wheel_sha256', 'release_manifest_sha256'}
                and all(type(v) is str and HEX.fullmatch(v) for v in value.values()), 'UPGRADE_PIN_INVALID')
    require(body['previous_upgrade'] != body['target_upgrade'], 'NEW_UPGRADE_REQUIRED')
    require(type(body['failed']) is dict and set(body['failed']) == {'request_id', 'worker_id', 'job_id', 'provider_id'}
            and all(type(v) is str and IDENT.fullmatch(v) for v in body['failed'].values()), 'FAILED_CASE_INVALID')
    require(type(body['files']) is dict and set(body['files']) == FILES
            and all(type(v) is str and HEX.fullmatch(v) for v in body['files'].values()), 'FILE_PINS_INVALID')
    files = {name: read(path.parent/name, owner=owner) for name in sorted(FILES)}
    require(all(digest(value) == body['files'][name] for name, value in files.items()), 'FILE_PIN_CHANGED')
    return raw, body, files


def helper(raw, name):
    module = types.ModuleType(name)
    module.__file__ = name
    exec(compile(raw, name, 'exec'), module.__dict__)
    return module


def validate_replacement(old, new, retry_id):
    old_plan, plan = decoded(old['plan.json']), decoded(new['plan.json'])
    require(plan['label'] != old_plan['label'] and len(plan['cases']) == len(old_plan['cases']) == 1,
            'FRESH_PLAN_REQUIRED')
    key = plan['cases'][0]['spec']['idempotency_key']
    require(key != old_plan['cases'][0]['spec']['idempotency_key'], 'FRESH_JOB_KEY_REQUIRED')
    compare = json.loads(json.dumps(plan))
    compare['label'] = old_plan['label']
    compare['cases'][0]['spec']['idempotency_key'] = old_plan['cases'][0]['spec']['idempotency_key']
    require(compare == old_plan, 'CALIBRATION_SCOPE_CHANGED')
    before, after = decoded(old['acceptance.json']), decoded(new['acceptance.json'])
    public = PUBLIC/('retry-'+retry_id)
    expected = dict(before, plan_path=str(public/'plan.json'), plan_sha256='sha256:'+digest(new['plan.json']),
                    submission_state_directory=str(SUBMIT/('retry-'+retry_id)),
                    trusted_state_directory=str(STATE/('retry-'+retry_id)))
    require(after == expected, 'RUNNER_SCOPE_CHANGED')
    for name in CALIBRATION:
        search = ('--config '+str(PUBLIC/'acceptance.json')).encode()
        replacement = ('--config '+str(public/'acceptance.json')).encode()
        require(old[name].count(search) == 1 and new[name] == old[name].replace(search, replacement), 'UNIT_SCOPE_CHANGED')
    from probe_core.gpu_acceptance import AcceptancePlan
    from probe_core.gpu_acceptance_runner import RunnerConfig, validate_plan
    validate_plan(RunnerConfig.model_validate(after), AcceptancePlan.model_validate(plan))
    return public, after


# Only this read-only, fixed query runs under the ledger/provider-owning identity.
# No tokens, credential bodies or arbitrary SQL/paths are returned.
SNAPSHOT = r'''
import json,sqlite3
from pathlib import Path
from probe_core.runpod_provider import RunPodConfig,RunPodHTTP,ProviderHTTPError
failed=json.loads(FAILED_JSON)
def rows(path,query,values=()):
    connection=sqlite3.connect(Path(path).as_uri()+"?mode=ro",uri=True,timeout=5)
    connection.row_factory=sqlite3.Row
    try:return [dict(row) for row in connection.execute(query,values)]
    finally:connection.close()
ledger="/var/lib/probe-core/research.sqlite"
provider="/var/lib/probe-provider/runpod.sqlite"
result={"jobs":rows(ledger,"SELECT * FROM jobs WHERE job_id=?",(failed["job_id"],)),
        "requests":rows(ledger,"SELECT * FROM compute_requests WHERE request_id=?",(failed["request_id"],)),
        "attempts":rows(ledger,"SELECT attempt_id FROM attempts WHERE job_id=?",(failed["job_id"],)),
        "intents":rows(provider,"SELECT worker_id,request_key,configuration_hash,provider_id,provider_seen FROM runpod_intents WHERE worker_id=?",(failed["worker_id"],))}
result["other_unfinished_jobs"]=rows(ledger,"SELECT count(*) AS n FROM jobs WHERE job_id!=? AND state NOT IN ('COMPLETED','FAILED')",(failed["job_id"],))[0]["n"]
result["unconfirmed_attempts"]=rows(ledger,"SELECT count(*) AS n FROM attempts WHERE stopped_at IS NULL")[0]["n"]
result["open_approvals"]=rows(ledger,"SELECT count(*) AS n FROM approvals WHERE consumed_at IS NOT NULL AND ended_at IS NULL")[0]["n"]
if len(result["requests"])==1:
    approval=result["requests"][0]["approval_id"]
    result["approvals"]=rows(ledger,"SELECT approval_id,document,consumed_at,deadline,ended_at FROM approvals WHERE approval_id=?",(approval,))
    result["approval_jobs"]=rows(ledger,"SELECT job_id FROM approval_jobs WHERE approval_id=?",(approval,))
else:result["approvals"],result["approval_jobs"]=[],[]
config=RunPodConfig.load("/etc/probe-core/runpod.json")
transport=RunPodHTTP(config.api_key_file)
try:
    transport.request("GET","/v2/pods/"+failed["provider_id"])
    result["provider_absent"]=False
except ProviderHTTPError as error:
    if error.status!=404:raise
    result["provider_absent"]=True
inventory=transport.request("GET","/v2/pods?includeClusterPods=true&limit=1000")
assert type(inventory.get("pods")) is list and inventory.get("pagination",{}).get("hasNextPage") is False
result["pods"]=len(inventory["pods"])
print(json.dumps(result,sort_keys=True))
'''


def validate_failed(snapshot, failed, old_plan, result, bound, submitted, *, cancelled=False):
    from probe_core.schemas import JobSpec
    require(all(snapshot.get(name) == 0 for name in ('other_unfinished_jobs', 'unconfirmed_attempts', 'open_approvals')),
            'OTHER_AUTHORITY_OR_WORK_PRESENT')
    require(len(snapshot['jobs']) == len(snapshot['requests']) == len(snapshot['approvals']) == len(snapshot['intents']) == 1,
            'FAILED_CASE_NOT_UNIQUE')
    job, request, approval, intent = (snapshot[name][0] for name in ('jobs', 'requests', 'approvals', 'intents'))
    require(job['job_id'] == failed['job_id']
            and decoded(job['spec_json']) == JobSpec.model_validate(old_plan['cases'][0]['spec']).model_dump(mode='json')
            and job['attempt_id'] is None and job['attempt_count'] == job['retry_count'] == 0 and not snapshot['attempts'],
            'FAILED_CASE_EXECUTED_OR_CHANGED')
    require((job['state'] == 'PENDING' and not cancelled) or
            (job['state'] == 'FAILED' and job['failure_kind'] == 'cancelled'
             and job['failure_reason'] == 'research client cancellation' and cancelled), 'FAILED_JOB_STATE_CHANGED')
    require(request['request_id'] == failed['request_id'] and request['worker_id'] == failed['worker_id']
            and request['observed_provider_id'] == failed['provider_id'] and request['state'] == 'STOPPED'
            and decoded(request['job_ids']) == [failed['job_id']] and request['action'] == 'CREATE'
            and request['infrastructure'] is None, 'FAILED_REQUEST_NOT_CLOSED')
    require(approval['approval_id'] == request['approval_id'] and approval['consumed_at'] is not None
            and approval['ended_at'] is not None and approval['ended_at'] >= approval['consumed_at']
            and snapshot['approval_jobs'] == [{'job_id': failed['job_id']}], 'FAILED_APPROVAL_NOT_CLOSED')
    require(intent['worker_id'] == failed['worker_id'] and intent['request_key'] == failed['request_id']
            and intent['provider_id'] == failed['provider_id'] and intent['provider_seen'] == 1
            and intent['configuration_hash'] == request['configuration_hash']
            and snapshot['provider_absent'] is True and snapshot['pods'] == 0, 'PROVIDER_DELETION_UNCONFIRMED')
    require(result.get('status') == 'failed' and result.get('stage') == 'verified_worker_startup'
            and result.get('reason') == 'ACCEPTANCE_RUNTIME_UNAVAILABLE'
            and all(result.get(key) == failed[key] for key in ('request_id', 'worker_id', 'provider_id'))
            and result.get('teardown') == {'provider_id': failed['provider_id'], 'state': 'ABSENT', 'confirmed': True}
            and result.get('approval_consumed_by_runner') is False, 'FAILED_RESULT_CHANGED')
    require(all(bound.get(key) == request[key] for key in ('request_id', 'worker_id', 'approval_id', 'batch_hash', 'deadline', 'observed_provider_id'))
            and all(submitted.get(key) == failed[key] for key in ('request_id', 'worker_id', 'job_id'))
            and submitted.get('approval_id') == approval['approval_id'], 'FAILED_BINDING_CHANGED')


class Recovery:
    def __init__(self, activation, upgrader, manifest, files, pin):
        self.a, self.u, self.m, self.files, self.pin = activation, upgrader, manifest, files, pin
        self.operation = activation.Activation()
        self.work = ROOT/'calibration-retries'/pin
        self.stage = 'validation'

    def record(self, name, value):
        raw = value if isinstance(value, bytes) else encoded(value)
        path = self.work/name
        if path.exists() or path.is_symlink():
            require(read(path) == raw, 'RECOVERY_RECORD_CHANGED')
        else:
            self.a.write_file(path, raw, uid=0, gid=0, mode=0o600)

    def initialize(self, mode, human):
        reference = self.m['previous_upgrade' if mode == 'close-failed' else 'target_upgrade']
        _, raw, _, _, _ = self.u.verify_baseline(ROOT, self.m['original_manifest_sha256'], reference, owner=0)
        require(digest(raw) == reference['wheel_sha256'], 'INSTALLED_WHEEL_CHANGED')
        directory = ROOT/'upgrades'/reference['wheel_sha256']
        checker = read(directory/'verify-installed-identities.py')
        self.operation.reader = self.a.extract_reader(checker)
        self.operation.users = tuple(pwd.getpwnam(name).pw_uid for name in ('probe-trusted', 'probe-research', human))
        require(len(set(self.operation.users)) == 3 and min(self.operation.users) > 0, 'IDENTITIES_CHANGED')
        capsule = ROOT/'calibrations'/self.m['previous_activation_manifest_sha256']
        old_manifest_raw = read(capsule/'activation-manifest.json')
        require(digest(old_manifest_raw) == self.m['previous_activation_manifest_sha256'], 'ORIGINAL_ACTIVATION_PIN_CHANGED')
        old_manifest = decoded(old_manifest_raw)
        activation_report = decoded(read(capsule/'activation-report.json'))
        require(activation_report.get('status') == 'passed' and activation_report.get('manifest_sha256') == self.m['previous_activation_manifest_sha256']
                and activation_report.get('approval_issued') is False and activation_report.get('cloud_mutations_performed') is False,
                'ORIGINAL_ACTIVATION_INCOMPLETE')
        self.old = {name: read(capsule/name) for name in ('plan.json', 'acceptance.json', 'worker.json', *CALIBRATION)}
        require(all(digest(raw) == old_manifest['files'][name] for name, raw in self.old.items()), 'ORIGINAL_INPUT_CHANGED')
        for name in ('plan.json', 'acceptance.json', 'worker.json'):
            require(read(PUBLIC/name) == self.old[name], 'PUBLIC_CONFIGURATION_CHANGED')
        for name in CALIBRATION:
            require(read(UNITS/name) == self.old[name], 'INSTALLED_UNIT_CHANGED')
            values = self.unit_state(name)
            require(values.get('ActiveState') in {'inactive', 'failed'} and values.get('MainPID') == '0'
                    and values.get('ControlPID') == '0' and values.get('DropInPaths') == '',
                    'CALIBRATION_PROCESS_OR_OVERRIDE_PRESENT')
        self.public, self.config = validate_replacement(self.old, self.files, self.m['retry_id'])
        require((self.config['service_uid'], self.config['research_uid'], self.config['admin_uid']) == self.operation.users,
                'RUNNER_IDENTITIES_CHANGED')
        self.result = decoded(read(STATE/'result.json', owner=self.operation.users[0]))
        self.bound = decoded(read(STATE/'bound-request.json', owner=self.operation.users[0]))
        self.submitted = decoded(read(SUBMIT/'submitted.json', owner=self.operation.users[1]))
        self.failure_hash = digest(encoded({'result': self.result, 'bound': self.bound, 'submitted': self.submitted}))
        parent = self.work.parent
        if parent.exists(): self.a.trusted(parent)
        else: parent.mkdir(mode=0o700)
        if self.work.exists(): self.a.trusted(self.work)
        else: self.work.mkdir(mode=0o700)
        self.record('manifest.json', encoded(self.m))
        for name, raw in self.files.items(): self.record(name, raw)

    def unit_state(self, name):
        raw = self.operation.command(['/usr/bin/systemctl', 'show', name,
                                     '--property=ActiveState,MainPID,ControlPID,DropInPaths'])
        return dict(line.split('=', 1) for line in raw.decode().splitlines())

    def snapshot(self):
        script = 'FAILED_JSON='+repr(json.dumps(self.m['failed']))+'\n'+SNAPSHOT
        raw = self.operation.command(['/usr/sbin/runuser', '-u', 'probe-trusted', '-g', 'probe-trusted', '--',
                                     str(ROOT/'venv/bin/python'), '-I', '-c', script], timeout=60)
        return decoded(raw)

    def verify_failure(self, snapshot, *, cancelled=False):
        validate_failed(snapshot, self.m['failed'], decoded(self.old['plan.json']), self.result, self.bound, self.submitted,
                        cancelled=cancelled)

    def close_failed(self):
        self.stage = 'close_failed'
        intent = self.work/'close-intent.json'
        snapshot = self.snapshot()
        already = snapshot['jobs'][0]['state'] == 'FAILED' if len(snapshot['jobs']) == 1 else False
        if already:
            require(intent.exists(), 'CANCELLATION_INTENT_MISSING')
        self.verify_failure(snapshot, cancelled=already)
        self.record('close-intent.json', {'manifest_sha256': self.pin, 'failed_evidence_sha256': self.failure_hash})
        if not already:
            script = ('from probe_core.rpc import UnixRPCClient\nimport json\n'
                      +'rpc=UnixRPCClient("/run/probe-research/research.sock",expected_server_uid='+str(self.operation.users[0])+')\n'
                      +'print(json.dumps(rpc.call("cancel_job",{"job_id":'+repr(self.m['failed']['job_id'])+'})))\n')
            self.operation.command(['/usr/sbin/runuser', '-u', 'probe-research', '-g', 'probe-research', '--',
                                    str(ROOT/'venv/bin/python'), '-I', '-c', script], timeout=30)
        self.verify_failure(self.snapshot(), cancelled=True)
        history = self.operation.idle()
        previous = self.work/'close-report.json'
        if previous.exists():
            receipt = decoded(read(previous))
            require(receipt.get('manifest_sha256') == self.pin and receipt.get('failed_evidence_sha256') == self.failure_hash,
                    'CLOSE_RECEIPT_CHANGED')
            self.operation.baseline = receipt['history']
            self.operation.idle()
            return receipt
        receipt = {'schema_version': 1, 'status': 'closed', 'manifest_sha256': self.pin,
                   'failed_evidence_sha256': self.failure_hash, 'history': history,
                   'original_job_cancelled': True, 'attempts_created': False, 'approval_issued': False}
        self.record('close-report.json', receipt)
        return receipt

    def prepare(self):
        self.stage = 'prepare'
        closed = decoded(read(self.work/'close-report.json'))
        require(closed.get('status') == 'closed' and closed.get('manifest_sha256') == self.pin
                and closed.get('failed_evidence_sha256') == self.failure_hash, 'CLOSE_RECEIPT_CHANGED')
        self.verify_failure(self.snapshot(), cancelled=True)
        self.operation.baseline = closed['history']
        self.operation.idle()
        paths = (self.public, Path(self.config['submission_state_directory']), Path(self.config['trusted_state_directory']))
        require(all(not os.path.lexists(path) for path in paths), 'RETRY_TARGET_EXISTS')
        guard_name = '50-probe-calibration-retry.conf'
        marker = self.public/'prepared'
        guard = ('[Unit]\nConditionPathExists='+str(marker)+'\n').encode()
        def guards():
            for name in CALIBRATION:
                directory = UNITS/(name+'.d')
                directory.mkdir(mode=0o755, exist_ok=True)
                self.a.trusted(directory)
                require(set(directory.iterdir()) <= {directory/guard_name}, 'CALIBRATION_OVERRIDE_PRESENT')
                self.a.write_file(directory/guard_name, guard, uid=0, gid=0, mode=0o644)
            self.operation.ctl('daemon-reload')
        try:
            for name in CALIBRATION:
                directory = UNITS/(name+'.d')
                require(not directory.exists(), 'CALIBRATION_OVERRIDE_PRESENT')
                directory.mkdir(mode=0o755)
                self.a.write_file(directory/guard_name, guard, uid=0, gid=0, mode=0o644)
            self.operation.ctl('daemon-reload')
            for path, uid, gid in ((paths[0], 0, 0), (paths[1], self.operation.users[1], grp.getgrnam('probe-research').gr_gid),
                                   (paths[2], self.operation.users[0], grp.getgrnam('probe-trusted').gr_gid)):
                path.mkdir(mode=0o755 if uid == 0 else 0o700)
                os.chown(path, uid, gid)
                os.chmod(path, 0o755 if uid == 0 else 0o700)
            for name in ('plan.json', 'acceptance.json'):
                self.a.write_file(self.public/name, self.files[name], uid=0, gid=0, mode=0o444)
            for name in CALIBRATION:
                self.a.write_file(UNITS/name, self.files[name], uid=0, gid=0, mode=0o644)
            self.operation.ctl('daemon-reload')
            self.operation.idle()
            self.a.write_file(marker, b'Pinned retry prepared; compute still requires human approval.\n', uid=0, gid=0, mode=0o600)
            self.operation.ctl('start', CALIBRATION[0])
            submitted = decoded(read(paths[1]/'submitted.json', owner=self.operation.users[1]))
            require(submitted.get('job_id') != self.m['failed']['job_id']
                    and submitted.get('request_id') != self.m['failed']['request_id']
                    and submitted.get('worker_id') != self.m['failed']['worker_id']
                    and submitted.get('approval_consumed_by_runner') is False, 'RETRY_SUBMISSION_CHANGED')
            self.operation.ctl('start', CALIBRATION[1])
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                state = self.unit_state(CALIBRATION[1])
                require(state.get('ActiveState') not in {'failed', 'inactive'}, 'RETRY_RUNNER_EXITED')
                if state.get('ActiveState') == 'active' and state.get('MainPID', '0').isdigit() and int(state['MainPID']) > 0:
                    binding = paths[2]/'binding.json'
                    if binding.exists():
                        from probe_core.gpu_acceptance_runner import RunnerConfig, digest as canonical_digest
                        expected = {'config_sha256': canonical_digest(RunnerConfig.model_validate(self.config).model_dump(mode='json')),
                                    'plan_sha256': self.config['plan_sha256']}
                        require(decoded(read(binding, owner=self.operation.users[0])) == expected, 'RETRY_RUNNER_BINDING_CHANGED')
                        break
                time.sleep(.1)
            else:
                raise RetryError('RETRY_RUNNER_NOT_READY')
            for name in CALIBRATION:
                path = UNITS/(name+'.d')/guard_name
                require(read(path) == guard, 'RETRY_GUARD_CHANGED')
                path.unlink(); path.parent.rmdir()
            self.operation.ctl('daemon-reload')
            receipt = {'schema_version': 1, 'status': 'prepared', 'manifest_sha256': self.pin,
                       'target_wheel_sha256': self.m['target_upgrade']['wheel_sha256'], 'submission': submitted,
                       'old_history_preserved': True, 'approval_issued': False, 'cloud_mutations_performed': False}
            self.record('prepare-report.json', receipt)
            return receipt
        except BaseException:
            # Calibration-only guards. The original research/controller/watchdog
            # remain available; no shared credential/configuration is changed.
            errors = []
            for action in (lambda: marker.unlink(missing_ok=True), guards,
                           lambda: self.operation.ctl('stop', *CALIBRATION)):
                try: action()
                except BaseException: errors.append(True)
            self.record('prepare-failure.json', {'status': 'failed', 'stage': self.stage, 'cleanup_confirmed': not errors})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('close-failed', 'prepare'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--human', required=True)
    args = parser.parse_args(argv)
    recovery = None
    safe_errors = (RetryError,)
    try:
        require(os.geteuid() == 0, 'ADMINISTRATOR_REQUIRED')
        os.umask(0o077)
        _, manifest, files = inputs(args.manifest, args.manifest_sha256)
        a = helper(files['activate-gpu-calibration.py'], 'pinned_calibration_activation')
        u = helper(files['upgrade-controller.py'], 'pinned_controller_upgrade')
        safe_errors = (RetryError, a.ActivationError, u.UpgradeError)
        recovery = Recovery(a, u, manifest, files, args.manifest_sha256)
        recovery.initialize(args.mode, args.human)
        report = recovery.close_failed() if args.mode == 'close-failed' else recovery.prepare()
        print(json.dumps(report, sort_keys=True))
        return 0
    except BaseException as error:
        reason = str(error)
        safe = reason if isinstance(error, safe_errors) and re.fullmatch(r'[A-Z0-9_]{1,100}', reason) else 'RECOVERY_REFUSED'
        diagnostic = {}
        if safe == 'RECOVERY_REFUSED':
            diagnostic['exception_type'] = type(error).__name__[:80]
            trace = error.__traceback__
            while trace is not None:
                if Path(trace.tb_frame.f_code.co_filename).name == Path(__file__).name:
                    diagnostic['helper_line'] = trace.tb_lineno
                trace = trace.tb_next
        print(json.dumps({'status': 'failed', 'stage': recovery.stage if recovery else 'validation',
                          'reason': safe, **diagnostic, 'approval_issued': False, 'cloud_mutations_performed': False}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
