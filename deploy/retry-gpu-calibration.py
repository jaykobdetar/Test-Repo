#!/usr/bin/env python3
"""Close one evidenced failed calibration, then prepare its pinned replacement.

The two phases straddle an ordinary verified wheel upgrade unless schema6 pins
the same installed wheel; that path verifies a fresh backup and installed
identities before preparation. Neither phase approves compute or writes the
ledger directly. Undispatched cancellation and fresh submission use the existing
research identity; a stopped failed attempt is preserved without cancellation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
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
OUTBOX = Path('/var/lib/probe-backups/outbox')
RECEIPTS = Path('/var/lib/probe-backups/receipts')
MAX_BACKUP = 64 * 1024**2
UNITS = Path('/etc/systemd/system')
CALIBRATION = ('probe-calibration-submit.service', 'probe-calibration-run.service')
FILES = {'activate-gpu-calibration.py', 'upgrade-controller.py', 'plan.json', 'acceptance.json', *CALIBRATION}
RETARGET_INPUTS = {'plan.json', 'acceptance.json', 'worker.json', *CALIBRATION}
RETARGET_FILES = {'activate-gpu-calibration.py', 'upgrade-controller.py', 'retarget-gpu-calibration.py'} | {
    prefix + name for prefix in ('old/', 'new/') for name in RETARGET_INPUTS}
RETARGET_GUARD = '50-probe-calibration-retarget.conf'
HEX = re.compile(r'[0-9a-f]{64}\Z')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
FAILURES = {'ACCEPTANCE_RUNTIME_UNAVAILABLE', 'WORKER_STARTUP_DEADLINE', 'REQUEST_CHANGED'}
STARTUP_STAGES = {'verified_worker_startup', 'worker_configuration', 'ssh_tunnel', 'worker_readiness'}
FAILURE_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,99}\Z')


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
    fields = {'schema_version', 'original_manifest_sha256', 'previous_upgrade', 'target_upgrade',
              'previous_activation_manifest_sha256', 'failed', 'retry_id', 'files'}
    require(type(body.get('schema_version')) is int and body['schema_version'] in {1, 2, 3, 4, 5, 6}, 'MANIFEST_SCHEMA')
    if body['schema_version'] >= 2:
        fields |= {'previous_retry_manifest_sha256', 'previous_retry_record_sha256', 'expected_failure_reason'}
        failures = ({'CALIBRATION_DID_NOT_PASS'} if body['schema_version'] == 6 else
                    {'ACCEPTANCE_RUNTIME_UNAVAILABLE'} if body['schema_version'] == 4 else
                    {'REQUEST_CHANGED'} if body['schema_version'] == 3 else FAILURES-{'REQUEST_CHANGED'})
        reason = body.get('expected_failure_reason')
        reason_valid = (type(reason) is str and FAILURE_CODE.fullmatch(reason)) if body['schema_version'] == 5 else reason in failures
        require(all(type(body.get(name)) is str and HEX.fullmatch(body[name]) for name in
                    ('previous_retry_manifest_sha256', 'previous_retry_record_sha256'))
                and reason_valid, 'PREVIOUS_RETRY_PIN_INVALID')
    if body['schema_version'] >= 3:
        fields.add('failed_result_canonical_sha256')
        require(type(body.get('failed_result_canonical_sha256')) is str
                and HEX.fullmatch(body['failed_result_canonical_sha256']), 'FAILED_RESULT_PIN_INVALID')
    if body['schema_version'] == 5:
        fields.add('expected_failure_stage')
        require(type(body.get('expected_failure_stage')) is str
                and body['expected_failure_stage'] in STARTUP_STAGES, 'FAILED_STAGE_INVALID')
        if 'previous_retarget_manifest_sha256' in body:
            fields.add('previous_retarget_manifest_sha256')
            require(type(body['previous_retarget_manifest_sha256']) is str
                    and HEX.fullmatch(body['previous_retarget_manifest_sha256']), 'PREVIOUS_RETARGET_PIN_INVALID')
    if body['schema_version'] == 6:
        fields |= {'expected_failure_stage', 'failed_attempt'}
        require(body.get('expected_failure_stage') == 'artifact_collection', 'FAILED_STAGE_INVALID')
        validate_attempt_identity(body.get('failed_attempt'))
    require(set(body) == fields, 'MANIFEST_SCHEMA')
    require(HEX.fullmatch(body['original_manifest_sha256']) and HEX.fullmatch(body['previous_activation_manifest_sha256'])
            and re.fullmatch(r'[0-9a-f]{32}', body['retry_id']), 'MANIFEST_IDENTITY')
    for name in ('previous_upgrade', 'target_upgrade'):
        value = body[name]
        require(type(value) is dict and set(value) == {'wheel_sha256', 'release_manifest_sha256'}
                and all(type(v) is str and HEX.fullmatch(v) for v in value.values()), 'UPGRADE_PIN_INVALID')
    require(body['schema_version'] == 6 or body['previous_upgrade'] != body['target_upgrade'], 'NEW_UPGRADE_REQUIRED')
    require(type(body['failed']) is dict and set(body['failed']) == {'request_id', 'worker_id', 'job_id', 'provider_id'}
            and all(type(v) is str and IDENT.fullmatch(v) for v in body['failed'].values()), 'FAILED_CASE_INVALID')
    names = FILES | ({'worker.json'} if body['schema_version'] in {3, 6} else set())
    require(type(body['files']) is dict and set(body['files']) == names
            and all(type(v) is str and HEX.fullmatch(v) for v in body['files'].values()), 'FILE_PINS_INVALID')
    files = {name: read(path.parent/name, owner=owner) for name in sorted(names)}
    require(all(digest(value) == body['files'][name] for name, value in files.items()), 'FILE_PIN_CHANGED')
    return raw, body, files


def helper(raw, name):
    module = types.ModuleType(name)
    module.__file__ = name
    exec(compile(raw, name, 'exec'), module.__dict__)
    return module


def validate_replacement(old, new, retry_id, *, schema_version=1):
    require(schema_version in {1, 2, 3, 4, 5, 6}, 'MANIFEST_SCHEMA')
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
    if schema_version >= 3:
        from probe_core.worker_contracts import WorkerConfig
        worker_before = decoded(old['worker.json'])
        WorkerConfig.model_validate(worker_before)
        require(before['worker_config_sha256'] == 'sha256:'+digest(old['worker.json'])
                and worker_before['code_git_commit'] == before['source_commit']
                and worker_before['container_image_digest'] == before['deployment']['image_digest'],
                'PREVIOUS_WORKER_BINDING_CHANGED')
    if schema_version in {3, 6}:
        worker_after = decoded(new['worker.json'])
        checked = WorkerConfig.model_validate(worker_after)
        compare_worker = dict(worker_after, code_git_commit=worker_before['code_git_commit'],
                              container_image_digest=worker_before['container_image_digest'])
        require(compare_worker == worker_before, 'WORKER_SCOPE_CHANGED')
        require(checked.code_git_commit != worker_before['code_git_commit']
                and checked.container_image_digest != worker_before['container_image_digest'], 'NEW_WORKER_IMAGE_REQUIRED')
        expected.update(source_commit=checked.code_git_commit,
                        deployment=dict(before['deployment'], image_digest=checked.container_image_digest),
                        worker_config_path=str(public/'worker.json'), worker_config_sha256='sha256:'+digest(new['worker.json']))
    require(after == expected, 'RUNNER_SCOPE_CHANGED')
    for name in CALIBRATION:
        search = ('--config '+str(Path(before['plan_path']).parent/'acceptance.json')).encode()
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
        "attempts":rows(ledger,"SELECT * FROM attempts WHERE job_id=?",(failed["job_id"],)),
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


def validate_attempt_identity(value):
    require(type(value) is dict and set(value) == {'attempt_id', 'failure_kind', 'failure_reason'}
            and type(value['attempt_id']) is str and IDENT.fullmatch(value['attempt_id'])
            and type(value['failure_kind']) is str
            and value['failure_kind'] in {'scientific', 'infrastructure', 'cancelled', 'policy', 'timeout', 'oom'}
            and type(value['failure_reason']) is str and 0 < len(value['failure_reason']) <= 4096,
            'FAILED_ATTEMPT_IDENTITY_INVALID')


def validate_failed(snapshot, failed, old_plan, result, bound, submitted, *, cancelled=False,
                    expected_failure_reason='ACCEPTANCE_RUNTIME_UNAVAILABLE', schema_version=1,
                    expected_failure_stage=None, failed_attempt=None):
    from probe_core.schemas import JobSpec
    require(all(snapshot.get(name) == 0 for name in ('other_unfinished_jobs', 'unconfirmed_attempts', 'open_approvals')),
            'OTHER_AUTHORITY_OR_WORK_PRESENT')
    require(len(snapshot['jobs']) == len(snapshot['requests']) == len(snapshot['approvals']) == len(snapshot['intents']) == 1,
            'FAILED_CASE_NOT_UNIQUE')
    job, request, approval, intent = (snapshot[name][0] for name in ('jobs', 'requests', 'approvals', 'intents'))
    require(job['job_id'] == failed['job_id']
            and decoded(job['spec_json']) == JobSpec.model_validate(old_plan['cases'][0]['spec']).model_dump(mode='json'),
            'FAILED_CASE_EXECUTED_OR_CHANGED')
    if schema_version == 6:
        validate_attempt_identity(failed_attempt)
        require(job['state'] == 'FAILED' and job['attempt_count'] == 1 and job['retry_count'] == 0
                and all(job.get(key) == value for key, value in failed_attempt.items())
                and job['worker_id'] == failed['worker_id'] and job['approval_id'] == request['approval_id']
                and len(snapshot['attempts']) == 1, 'FAILED_ATTEMPT_NOT_TERMINAL')
        attempt = snapshot['attempts'][0]
        require(attempt['attempt_id'] == failed_attempt['attempt_id'] and attempt['attempt_number'] == 1
                and attempt['job_id'] == failed['job_id'] and attempt['worker_id'] == failed['worker_id']
                and attempt['approval_id'] == request['approval_id']
                and attempt['outcome'] == failed_attempt['failure_kind'], 'FAILED_ATTEMPT_BINDING_CHANGED')
        timestamps = [attempt.get(key) for key in ('dispatched_at', 'execution_deadline', 'stopped_at')]
        timestamps += [approval.get(key) for key in ('consumed_at', 'deadline', 'ended_at')]
        require(all(type(value) in (int, float) and math.isfinite(value) and value > 0 for value in timestamps)
                and approval['consumed_at'] <= attempt['dispatched_at'] <= attempt['stopped_at'] <= approval['ended_at']
                and attempt['dispatched_at'] < attempt['execution_deadline'] <= request['deadline']
                and approval['consumed_at'] <= approval['deadline'] <= request['deadline'],
                'FAILED_ATTEMPT_STOP_UNCONFIRMED')
        document = decoded(approval['document'])
        require(document.get('approval_id') == approval['approval_id'] and document.get('pod_id') == failed['worker_id']
                and document.get('batch_hash') == request['batch_hash']
                and type(document.get('max_runtime_seconds')) is int and document['max_runtime_seconds'] > 0
                and approval['consumed_at'] + document['max_runtime_seconds'] == request['deadline'],
                'FAILED_APPROVAL_BINDING_CHANGED')
        require(all(result[key] == value for key, value in {**failed, 'attempt_id': failed_attempt['attempt_id']}.items()
                    if key in result), 'FAILED_RESULT_CHANGED')
    else:
        require(job['attempt_id'] is None and job['attempt_count'] == job['retry_count'] == 0 and not snapshot['attempts'],
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
    require(schema_version in {1, 2, 3, 4, 5, 6}, 'MANIFEST_SCHEMA')
    if schema_version == 4:
        require(expected_failure_reason == 'ACCEPTANCE_RUNTIME_UNAVAILABLE'
                and result.get('configuration') == {'configured': None, 'reason': 'CONFIGURATION_RESPONSE_UNCERTAIN'}
                and result.get('diagnostic') == {'exception_type': 'TransportError', 'location': 'gpu_acceptance_runner.py:684'},
                'FAILED_CONFIGURATION_RESULT_CHANGED')
    expected_stage = 'worker_configuration' if schema_version == 4 else 'verified_worker_startup'
    reason_valid = expected_failure_reason in FAILURES
    if schema_version == 5:
        require(type(expected_failure_stage) is str and expected_failure_stage in STARTUP_STAGES,
                'FAILED_STAGE_INVALID')
        require(type(result.get('schema_version')) is int and result['schema_version'] == 1
                and result.get('kind') == 'single_public_gpu_calibration'
                and result.get('scientific_evidence') is False, 'FAILED_RESULT_CHANGED')
        expected_stage = expected_failure_stage
        reason_valid = type(expected_failure_reason) is str and FAILURE_CODE.fullmatch(expected_failure_reason)
    if schema_version == 6:
        require(expected_failure_stage == 'artifact_collection'
                and type(result.get('schema_version')) is int and result['schema_version'] == 1
                and result.get('kind') == 'single_public_gpu_calibration'
                and result.get('scientific_evidence') is False, 'FAILED_RESULT_CHANGED')
        expected_stage = 'artifact_collection'
        reason_valid = expected_failure_reason == 'CALIBRATION_DID_NOT_PASS'
    require(reason_valid and result.get('status') == 'failed'
            and result.get('stage') == expected_stage and result.get('reason') == expected_failure_reason
            and all(result.get(key) == failed[key] for key in ('request_id', 'worker_id', 'provider_id'))
            and result.get('teardown') == {'provider_id': failed['provider_id'], 'state': 'ABSENT', 'confirmed': True}
            and result.get('approval_consumed_by_runner') is False, 'FAILED_RESULT_CHANGED')
    require(all(bound.get(key) == request[key] for key in ('request_id', 'worker_id', 'approval_id', 'batch_hash', 'deadline', 'observed_provider_id'))
            and all(submitted.get(key) == failed[key] for key in ('request_id', 'worker_id', 'job_id'))
            and submitted.get('approval_id') == approval['approval_id'], 'FAILED_BINDING_CHANGED')


def validate_local_gates(capsule, pins, activation):
    require(type(pins) is dict and set(pins) == {'before.tar', 'backup.json', 'identity-acceptance.json'}
            and all(type(value) is str and HEX.fullmatch(value) for value in pins.values()), 'LOCAL_GATES_MISSING')
    files = {name: read(capsule/name, limit=MAX_BACKUP if name == 'before.tar' else 1048576) for name in pins}
    require(all(digest(raw) == pins[name] for name, raw in files.items()), 'LOCAL_GATES_CHANGED')
    receipt = decoded(files['backup.json'])
    require(receipt.get('archive_sha256') == pins['before.tar'] and all(receipt.get(key) is True
            for key in ('verified', 'readback_verified', 'restore_verified')), 'BACKUP_NOT_VERIFIED')
    activation.identity_gate(decoded(files['identity-acceptance.json']))


class Recovery:
    def __init__(self, activation, upgrader, manifest, files, pin):
        self.a, self.u, self.m, self.files, self.pin = activation, upgrader, manifest, files, pin
        self.operation = activation.Activation()
        self.work = ROOT/'calibration-retries'/pin
        self.stage = 'validation'
        self.retarget_guard = None

    def record(self, name, value):
        raw = value if isinstance(value, bytes) else encoded(value)
        path = self.work/name
        if path.exists() or path.is_symlink():
            require(read(path) == raw, 'RECOVERY_RECORD_CHANGED')
        else:
            self.a.write_file(path, raw, uid=0, gid=0, mode=0o600)

    def initialize(self, mode, human):
        self.human = human
        reference = self.m['previous_upgrade' if mode == 'close-failed' else 'target_upgrade']
        _, raw, _, _, _ = self.u.verify_baseline(ROOT, self.m['original_manifest_sha256'], reference, owner=0)
        require(digest(raw) == reference['wheel_sha256'], 'INSTALLED_WHEEL_CHANGED')
        directory = ROOT/'upgrades'/reference['wheel_sha256']
        checker = read(directory/'verify-installed-identities.py')
        self.checker = checker
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
        original = {name: read(capsule/name) for name in ('plan.json', 'acceptance.json', 'worker.json', *CALIBRATION)}
        require(all(digest(raw) == old_manifest['files'][name] for name, raw in original.items()), 'ORIGINAL_INPUT_CHANGED')
        for name in ('plan.json', 'acceptance.json', 'worker.json'):
            require(read(PUBLIC/name) == original[name], 'PUBLIC_CONFIGURATION_CHANGED')
        self.old = self.previous_inputs(original)
        from probe_core.gpu_acceptance_runner import RunnerConfig
        old_config = RunnerConfig.model_validate_json(self.old['acceptance.json'])
        for name, path in (('plan.json', Path(old_config.plan_path)),
                           ('acceptance.json', Path(old_config.plan_path).parent/'acceptance.json'),
                           ('worker.json', Path(old_config.worker_config_path))):
            require(read(path) == self.old[name], 'PREVIOUS_PUBLIC_CONFIGURATION_CHANGED')
        for name in CALIBRATION:
            require(read(UNITS/name) == self.old[name], 'INSTALLED_UNIT_CHANGED')
            self.verify_inactive_unit(name)
        self.public, self.config = validate_replacement(self.old, self.files, self.m['retry_id'], schema_version=self.m['schema_version'])
        require((self.config['service_uid'], self.config['research_uid'], self.config['admin_uid']) == self.operation.users,
                'RUNNER_IDENTITIES_CHANGED')
        self.result = decoded(read(Path(old_config.trusted_state_directory)/'result.json', owner=self.operation.users[0]))
        self.verify_result_pin()
        self.bound = decoded(read(Path(old_config.trusted_state_directory)/'bound-request.json', owner=self.operation.users[0]))
        self.submitted = decoded(read(Path(old_config.submission_state_directory)/'submitted.json', owner=self.operation.users[1]))
        self.failure_hash = digest(encoded({'result': self.result, 'bound': self.bound, 'submitted': self.submitted}))
        parent = self.work.parent
        if parent.exists(): self.a.trusted(parent)
        else: parent.mkdir(mode=0o700)
        if self.work.exists(): self.a.trusted(self.work)
        else: self.work.mkdir(mode=0o700)
        self.record('manifest.json', encoded(self.m))
        if self.m['schema_version'] == 6:
            self.record('verify-installed-identities.py', checker)
        for name, raw in self.files.items(): self.record(name, raw)

    def previous_inputs(self, original, *, manifest=None, _seen=()):
        current = self.m if manifest is None else manifest
        if current['schema_version'] == 1:
            return original
        previous_pin = current['previous_retry_manifest_sha256']
        require(previous_pin not in _seen and len(_seen) < 32, 'PREVIOUS_RETRY_CHAIN_LIMIT')
        retarget = self.retarget_inputs(current) if 'previous_retarget_manifest_sha256' in current else None
        submitted_identity = retarget[0]['pending'] if retarget else current['failed']
        capsule = ROOT/'calibration-retries'/previous_pin
        self.a.trusted(capsule)
        raw = read(capsule/'manifest.json')
        # Version1 stored a canonical record rather than the original manifest
        # formatting. Its separately pinned bytes must not be mistaken for the
        # original manifest hash used as the capsule's identity.
        require(digest(raw) == current['previous_retry_record_sha256'], 'PREVIOUS_RETRY_RECORD_CHANGED')
        previous = decoded(raw)
        versions = ({5, 6} if current['schema_version'] == 6 else
                    {4, 5} if current['schema_version'] == 5 else {current['schema_version']-1})
        require(type(previous.get('schema_version')) is int and previous['schema_version'] in versions
                and previous.get('original_manifest_sha256') == current['original_manifest_sha256']
                and previous.get('previous_activation_manifest_sha256') == current['previous_activation_manifest_sha256']
                and previous.get('target_upgrade') == current['previous_upgrade']
                and previous.get('retry_id') != current['retry_id'], 'PREVIOUS_RETRY_CHAIN_CHANGED')
        if previous['schema_version'] >= 2:
            original_raw = read(capsule/'manifest-original.json')
            require(digest(original_raw) == previous_pin and decoded(original_raw) == previous,
                    'PREVIOUS_RETRY_ORIGINAL_CHANGED')
        report = decoded(read(capsule/'prepare-report.json'))
        require(report.get('schema_version') == 1 and report.get('status') == 'prepared'
                and report.get('manifest_sha256') == previous_pin
                and report.get('target_wheel_sha256') == current['previous_upgrade']['wheel_sha256']
                and report.get('old_history_preserved') is True and report.get('approval_issued') is False
                and report.get('cloud_mutations_performed') is False
                and all(report.get('submission', {}).get(key) == submitted_identity[key]
                        for key in ('job_id', 'request_id', 'worker_id'))
                and report.get('submission', {}).get('approval_consumed_by_runner') is False,
                'PREVIOUS_RETRY_NOT_COMPLETED')
        closed = decoded(read(capsule/'close-report.json'))
        require(closed.get('status') == 'closed' and closed.get('manifest_sha256') == previous_pin
                and closed.get('original_job_cancelled') is (previous['schema_version'] != 6)
                and (previous['schema_version'] != 6 or closed.get('failed_job_preserved') is True)
                and closed.get('attempts_created') is False
                and closed.get('approval_issued') is False, 'PREVIOUS_RETRY_CLOSE_MISSING')
        names = ('plan.json', 'acceptance.json', *CALIBRATION)
        if previous['schema_version'] in {3, 6}:
            names += ('worker.json',)
        old = {name: read(capsule/name) for name in names}
        require(all(digest(value) == previous['files'][name] for name, value in old.items()), 'PREVIOUS_RETRY_INPUT_CHANGED')
        earlier = self.previous_inputs(original, manifest=previous, _seen=(*_seen, previous_pin))
        validate_replacement(earlier, old, previous['retry_id'], schema_version=previous['schema_version'])
        if previous['schema_version'] == 6:
            snapshot = decoded(read(capsule/'failed-snapshot.json'))
            evidence = decoded(read(capsule/'failed-evidence.json'))
            require(closed.get('failed_attempt') == previous.get('failed_attempt')
                    and digest(encoded(snapshot)) == closed.get('failed_snapshot_sha256')
                    and digest(encoded(evidence)) == closed.get('failed_evidence_sha256')
                    and digest(encoded(evidence['result'])) == previous.get('failed_result_canonical_sha256'),
                    'PREVIOUS_FAILED_ATTEMPT_EVIDENCE_CHANGED')
            validate_failed(snapshot, previous['failed'], decoded(earlier['plan.json']),
                            evidence['result'], evidence['bound'], evidence['submitted'], schema_version=6,
                            expected_failure_stage=previous.get('expected_failure_stage'),
                            expected_failure_reason=previous.get('expected_failure_reason'),
                            failed_attempt=previous.get('failed_attempt'))
            if previous['previous_upgrade'] == previous['target_upgrade']:
                require(report.get('application_reinstalled') is False, 'PREVIOUS_RETRY_NOT_COMPLETED')
                validate_local_gates(capsule, report.get('local_gates'), self.a)
        if previous['schema_version'] not in {3, 6}:
            old['worker.json'] = earlier['worker.json']
        if retarget:
            old = self.apply_retarget(current, retarget, old, report['submission'])
            if manifest is None:
                marker = Path(decoded(old['acceptance.json'])['plan_path']).parent/'prepared'
                require(read(marker) == b'Pinned region retarget prepared; compute requires human approval.\n',
                        'PREVIOUS_RETARGET_MARKER_CHANGED')
                self.retarget_guard = ('[Unit]\nConditionPathExists='+str(marker)+'\n').encode()
        return old

    def retarget_inputs(self, current):
        """Read one pinned region-only overlay on the completed retry chain."""
        pin = current['previous_retarget_manifest_sha256']
        require(current['schema_version'] == 5 and type(pin) is str and HEX.fullmatch(pin),
                'PREVIOUS_RETARGET_PIN_INVALID')
        capsule = ROOT/'calibration-retargets'/pin
        self.a.trusted(capsule)
        raw = read(capsule/'manifest.json')
        require(digest(raw) == pin, 'PREVIOUS_RETARGET_MANIFEST_CHANGED')
        body = decoded(raw)
        require(set(body) == {'schema_version', 'original_manifest_sha256', 'installed_upgrade', 'retarget_id',
                             'from_region', 'to_region', 'pending', 'files'}
                and type(body['schema_version']) is int and body['schema_version'] == 1
                and body['original_manifest_sha256'] == current['original_manifest_sha256']
                and body['installed_upgrade'] == current['previous_upgrade']
                and type(body['retarget_id']) is str and re.fullmatch(r'[0-9a-f]{32}', body['retarget_id'])
                and body['from_region'] == 'EU-CZ-1' and body['to_region'] == 'EU-RO-1',
                'PREVIOUS_RETARGET_SCOPE_CHANGED')
        require(type(body['pending']) is dict and set(body['pending']) == {'job_id', 'request_id', 'worker_id'}
                and all(type(value) is str and IDENT.fullmatch(value) for value in body['pending'].values()),
                'PREVIOUS_RETARGET_IDENTITY_INVALID')
        require(type(body['files']) is dict and set(body['files']) == RETARGET_FILES
                and all(type(value) is str and HEX.fullmatch(value) for value in body['files'].values()),
                'PREVIOUS_RETARGET_FILE_PINS_INVALID')
        files = {name: read(capsule/name) for name in sorted(RETARGET_FILES)}
        require(all(digest(value) == body['files'][name] for name, value in files.items()),
                'PREVIOUS_RETARGET_INPUT_CHANGED')
        return body, files, capsule

    def apply_retarget(self, current, retarget, old, submitted):
        body, files, capsule = retarget
        pin = current['previous_retarget_manifest_sha256']
        require(all(files['old/'+name] == old[name] for name in RETARGET_INPUTS),
                'PREVIOUS_RETARGET_HISTORY_CHANGED')
        new = {name: files['new/'+name] for name in RETARGET_INPUTS}
        # This exact helper was included in the pinned, root-owned retarget
        # capsule. Reuse its strict transformation and never-approved gates;
        # no executable path, command or provider operation is invoked here.
        validator = helper(files['retarget-gpu-calibration.py'], 'pinned_previous_retarget')
        validator.validate_retarget(old, new, body['retarget_id'], body['from_region'], body['to_region'])
        require(decoded(read(capsule/'cancel-intent.json')) == {'manifest_sha256': pin, **body['pending']},
                'PREVIOUS_RETARGET_CANCEL_INTENT_CHANGED')
        validator.validate_pending(decoded(read(capsule/'cancelled.json')), body['pending'],
                                   decoded(old['plan.json']), decoded(old['acceptance.json']), submitted, cancelled=True)
        report = decoded(read(capsule/'prepare-report.json'))
        require(type(report.get('schema_version')) is int and report['schema_version'] == 1
                and report.get('status') == 'prepared' and report.get('manifest_sha256') == pin
                and report.get('installed_wheel_sha256') == current['previous_upgrade']['wheel_sha256']
                and report.get('original_job_cancelled') is True and report.get('old_history_preserved') is True
                and report.get('application_reinstalled') is False and report.get('approval_issued') is False
                and report.get('cloud_mutations_performed') is False
                and all(report.get('submission', {}).get(key) == current['failed'][key]
                        and current['failed'][key] != body['pending'][key] for key in body['pending'])
                and report.get('submission', {}).get('approval_consumed_by_runner') is False,
                'PREVIOUS_RETARGET_NOT_COMPLETED')
        return new

    def verify_inactive_unit(self, name, *, retry_guard=None):
        directory = UNITS/(name+'.d')
        expected = {}
        if self.retarget_guard is not None:
            expected[directory/RETARGET_GUARD] = self.retarget_guard
        if retry_guard is not None:
            expected[directory/'50-probe-calibration-retry.conf'] = retry_guard
        if directory.exists():
            self.a.trusted(directory)
            require(set(directory.iterdir()) == set(expected), 'CALIBRATION_OVERRIDE_PRESENT')
        else:
            require(not expected, 'CALIBRATION_OVERRIDE_PRESENT')
        require(all(read(path) == raw for path, raw in expected.items()), 'CALIBRATION_GUARD_CHANGED')
        values = self.unit_state(name)
        require(values.get('ActiveState') in {'inactive', 'failed'} and values.get('MainPID') == '0'
                and values.get('ControlPID') == '0'
                and values.get('DropInPaths', '').split() == sorted(map(str, expected)),
                'CALIBRATION_PROCESS_OR_OVERRIDE_PRESENT')

    def unit_state(self, name):
        raw = self.operation.command(['/usr/bin/systemctl', 'show', name,
                                     '--property=ActiveState,MainPID,ControlPID,DropInPaths'])
        return dict(line.split('=', 1) for line in raw.decode().splitlines())

    def verify_result_pin(self):
        if self.m.get('schema_version', 1) >= 3:
            # Pin canonical parsed JSON plus newline, not the file's formatting.
            require(digest(encoded(self.result)) == self.m['failed_result_canonical_sha256'], 'FAILED_RESULT_PIN_CHANGED')

    def snapshot(self):
        script = 'FAILED_JSON='+repr(json.dumps(self.m['failed']))+'\n'+SNAPSHOT
        raw = self.operation.command(['/usr/sbin/runuser', '-u', 'probe-trusted', '-g', 'probe-trusted', '--',
                                     str(ROOT/'venv/bin/python'), '-I', '-c', script], timeout=60)
        return decoded(raw)

    def verify_failure(self, snapshot, *, cancelled=False):
        self.verify_result_pin()
        validate_failed(snapshot, self.m['failed'], decoded(self.old['plan.json']), self.result, self.bound, self.submitted,
                        cancelled=cancelled, expected_failure_reason=self.m.get('expected_failure_reason', 'ACCEPTANCE_RUNTIME_UNAVAILABLE'),
                        schema_version=self.m.get('schema_version', 1), expected_failure_stage=self.m.get('expected_failure_stage'),
                        failed_attempt=self.m.get('failed_attempt'))

    def preserve_failed(self):
        """Schema6 records an already stopped failure; never sends cancellation."""
        snapshot = self.snapshot()
        self.verify_failure(snapshot)
        previous = self.work/'close-report.json'
        receipt = decoded(read(previous)) if previous.exists() else None
        if receipt is not None:
            self.verify_preserved_receipt(receipt, snapshot)
            self.operation.baseline = receipt['history']
        history = self.operation.idle()
        self.record('close-intent.json', {'manifest_sha256': self.pin, 'failed_evidence_sha256': self.failure_hash})
        self.record('failed-evidence.json', {'result': self.result, 'bound': self.bound, 'submitted': self.submitted})
        self.record('failed-snapshot.json', snapshot)
        after = self.snapshot()
        self.verify_failure(after)
        require(after == snapshot, 'FAILED_HISTORY_CHANGED')
        # idle() retains its baseline and verifies every historical row and the
        # audit prefix again; this phase performs no ledger mutation.
        self.operation.idle()
        if receipt is not None:
            return receipt
        receipt = {'schema_version': 1, 'status': 'closed', 'manifest_sha256': self.pin,
                   'failed_evidence_sha256': self.failure_hash, 'failed_snapshot_sha256': digest(encoded(snapshot)),
                   'failed_attempt': self.m['failed_attempt'], 'history': history,
                   'original_job_cancelled': False, 'failed_job_preserved': True,
                   'attempts_created': False, 'approval_issued': False}
        self.record('close-report.json', receipt)
        return receipt

    def verify_preserved_receipt(self, receipt, snapshot):
        require(receipt.get('status') == 'closed' and receipt.get('manifest_sha256') == self.pin
                and receipt.get('failed_evidence_sha256') == self.failure_hash
                and receipt.get('failed_snapshot_sha256') == digest(encoded(snapshot))
                and receipt.get('failed_attempt') == self.m['failed_attempt']
                and receipt.get('original_job_cancelled') is False and receipt.get('failed_job_preserved') is True
                and receipt.get('attempts_created') is False and receipt.get('approval_issued') is False,
                'CLOSE_RECEIPT_CHANGED')
        require(decoded(read(self.work/'failed-snapshot.json')) == snapshot
                and digest(read(self.work/'failed-evidence.json')) == self.failure_hash,
                'FAILED_HISTORY_CHANGED')

    def local_gates(self):
        """Verify backup and installed identities when only the remote image changes."""
        self.stage = 'backup'
        checker = helper(self.checker, 'pinned_retry_identity_checker')
        previous = set(OUTBOX.glob('probe-*.tar'))
        backup_uid = pwd.getpwnam('probe-backup').pw_uid
        for _ in range(2):
            # A pending upload can consume the first run; the second then
            # produces a fresh archive through the unchanged service profile.
            self.operation.command(['/usr/bin/systemctl', 'start', 'probe-backup.service'], timeout=360)
            values = self.operation.ctl('show', 'probe-backup.service', '--property=Result,ExecMainStatus')
            require(dict(line.split('=', 1) for line in values.decode().splitlines())
                    == {'Result': 'success', 'ExecMainStatus': '0'}, 'BACKUP_SERVICE_NOT_SUCCESSFUL')
            deadline = time.monotonic()+30
            while True:
                state = self.operation.ctl('show', 'probe-backup-prune.service', '--property=ActiveState', '--value').strip()
                if state == b'inactive':
                    break
                require(state in {b'active', b'activating', b'deactivating'} and time.monotonic() < deadline,
                        'BACKUP_PRUNE_NOT_FINISHED')
                time.sleep(.1)
            archive = checker.completed_archive(outbox=OUTBOX, receipts=RECEIPTS,
                trusted_uid=self.operation.users[0], backup_uid=backup_uid)
            if archive not in previous:
                raw = read(archive, owner=self.operation.users[0], limit=MAX_BACKUP)
                checksum = digest(raw)
                paths = list(RECEIPTS.glob('*.json'))
                require(len(paths) <= 100, 'BACKUP_RECEIPTS_UNBOUNDED')
                matching = [decoded(read(path, owner=backup_uid, limit=65536)) for path in paths]
                matching = [value for value in matching if value.get('archive_sha256') == checksum]
                require(len(matching) == 1 and all(matching[0].get(key) is True
                        for key in ('verified', 'readback_verified', 'restore_verified')), 'BACKUP_NOT_VERIFIED')
                self.record('before.tar', raw)
                self.record('backup.json', matching[0])
                break
        else:
            raise RetryError('FRESH_BACKUP_REQUIRED')
        self.operation.idle()
        self.stage = 'identity_verification'
        reference = self.m['previous_upgrade']
        _, raw, _, _, _ = self.u.verify_baseline(ROOT, self.m['original_manifest_sha256'], reference, owner=0)
        require(digest(raw) == reference['wheel_sha256'], 'INSTALLED_WHEEL_CHANGED')
        self.operation.command(['/usr/bin/python3', '-I', str(self.work/'verify-installed-identities.py'),
                                '--human', self.human, '--output', str(self.work/'identity-acceptance.json')], timeout=120)
        self.a.identity_gate(decoded(read(self.work/'identity-acceptance.json')))
        self.operation.ready()
        self.operation.idle()
        pins = {name: digest(read(self.work/name, limit=MAX_BACKUP if name == 'before.tar' else 1048576))
                for name in ('before.tar', 'backup.json', 'identity-acceptance.json')}
        validate_local_gates(self.work, pins, self.a)
        return pins

    def close_failed(self):
        self.stage = 'close_failed'
        if self.m.get('schema_version') == 6:
            return self.preserve_failed()
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
        snapshot = self.snapshot()
        self.verify_failure(snapshot, cancelled=True)
        if self.m.get('schema_version') == 6:
            self.verify_preserved_receipt(closed, snapshot)
        self.operation.baseline = closed['history']
        self.operation.idle()
        paths = (self.public, Path(self.config['submission_state_directory']), Path(self.config['trusted_state_directory']))
        require(all(not os.path.lexists(path) for path in paths), 'RETRY_TARGET_EXISTS')
        local_gates = None
        if self.m.get('schema_version') == 6 and self.m['previous_upgrade'] == self.m['target_upgrade']:
            local_gates = self.local_gates()
            self.verify_failure(self.snapshot())
            self.operation.idle()
            self.stage = 'prepare'
        guard_name = '50-probe-calibration-retry.conf'
        marker = self.public/'prepared'
        guard = ('[Unit]\nConditionPathExists='+str(marker)+'\n').encode()
        def guards():
            for name in CALIBRATION:
                directory = UNITS/(name+'.d')
                directory.mkdir(mode=0o755, exist_ok=True)
                self.a.trusted(directory)
                allowed = {directory/guard_name}
                if self.retarget_guard is not None:
                    allowed.add(directory/RETARGET_GUARD)
                    if (directory/RETARGET_GUARD).exists():
                        require(read(directory/RETARGET_GUARD) == self.retarget_guard, 'CALIBRATION_GUARD_CHANGED')
                require(set(directory.iterdir()) <= allowed, 'CALIBRATION_OVERRIDE_PRESENT')
                self.a.write_file(directory/guard_name, guard, uid=0, gid=0, mode=0o644)
            self.operation.ctl('daemon-reload')
        try:
            for name in CALIBRATION:
                directory = UNITS/(name+'.d')
                if self.retarget_guard is not None:
                    self.verify_inactive_unit(name)
                else:
                    require(not directory.exists(), 'CALIBRATION_OVERRIDE_PRESENT')
                    directory.mkdir(mode=0o755)
                self.a.write_file(directory/guard_name, guard, uid=0, gid=0, mode=0o644)
            self.operation.ctl('daemon-reload')
            if self.retarget_guard is not None:
                # Both units are inactive and both new blocking guards are
                # installed before either prior guard can be removed.
                for name in CALIBRATION:
                    self.verify_inactive_unit(name, retry_guard=guard)
                for name in CALIBRATION:
                    (UNITS/(name+'.d')/RETARGET_GUARD).unlink()
                self.retarget_guard = None
                self.operation.ctl('daemon-reload')
            for path, uid, gid in ((paths[0], 0, 0), (paths[1], self.operation.users[1], grp.getgrnam('probe-research').gr_gid),
                                   (paths[2], self.operation.users[0], grp.getgrnam('probe-trusted').gr_gid)):
                path.mkdir(mode=0o755 if uid == 0 else 0o700)
                os.chown(path, uid, gid)
                os.chmod(path, 0o755 if uid == 0 else 0o700)
            public_files = ('plan.json', 'acceptance.json', 'worker.json') if self.m.get('schema_version') in {3, 6} else ('plan.json', 'acceptance.json')
            for name in public_files:
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
            if local_gates is not None:
                receipt.update(application_reinstalled=False, local_gates=local_gates)
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
        manifest_raw, manifest, files = inputs(args.manifest, args.manifest_sha256)
        a = helper(files['activate-gpu-calibration.py'], 'pinned_calibration_activation')
        u = helper(files['upgrade-controller.py'], 'pinned_controller_upgrade')
        safe_errors = (RetryError, a.ActivationError, u.UpgradeError)
        recovery = Recovery(a, u, manifest, files, args.manifest_sha256)
        recovery.initialize(args.mode, args.human)
        recovery.record('manifest-original.json', manifest_raw)
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
