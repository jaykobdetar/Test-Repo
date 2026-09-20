"""One fixed public calibration through the installed research/approval pipeline.

The submission process is the research UID. The runner is the Ledger owner.
Neither mode issues or consumes an approval. The human uses the existing admin
socket; the independently supervised watchdog retains the absolute deadline.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
from importlib.resources import files
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import selectors
import shlex
import signal
import socket
import stat
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from pydantic import Field, model_validator

from .audit import canonical_json
from .controller import ControllerClient
from .dispatcher import (Dispatcher, DispatcherService, SSHTunnel, WorkerClient,
                         SSH_FAILURE_PATTERNS, TransportError, ssh_failure_classification)
from .direction_transfer import DIRECTION_SHA256, READBACK_PROGRAM, DirectionDispatcher
from .gpu_acceptance import AcceptancePlan, collect, fixed_direction_plan, fixed_plan
from .gpu_acceptance_actions import SSHActionClient, run_action
from .ledger import JobState, Ledger
from .provider import DeploymentSpec, WorkerState
from .rpc import UnixRPCClient, decode
from .runpod_provider import (ProviderHTTPError, ProviderResponseError, RunPodConfig,
                              RunPodProvider, _NoRedirect, _read_owned_file, provider_http_metadata)
from .schemas import FrozenModel, GitSHA, SHA256
from .worker_contracts import WorkerConfig

LEDGER = Path('/var/lib/probe-core/research.sqlite')
RESEARCH_SOCKET = '/run/probe-research/research.sock'
CONTROLLER_SOCKET = '/run/probe-controller/research.sock'
BOUND = 1024 * 1024
COLLECTION_DELETION_RESERVE_SECONDS = 120


class RunnerError(ValueError):
    """Only fixed, non-sensitive reason codes cross the CLI boundary."""

    def __init__(self, message, *, diagnostic=None, provider_diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic
        self.provider_diagnostic = provider_diagnostic


BOOTSTRAP_EXCEPTION_TYPES = frozenset({
    'ValueError', 'TypeError', 'RuntimeError', 'OSError', 'PermissionError',
    'FileNotFoundError', 'FileExistsError', 'NotADirectoryError', 'IsADirectoryError',
    'ProcessLookupError', 'TimeoutError', 'KeyError', 'AttributeError', 'AssertionError',
    'ImportError', 'ModuleNotFoundError', 'UnicodeDecodeError', 'JSONDecodeError',
    'ValidationError', 'CalledProcessError', 'BootstrapRefused',
})
COMMAND_CLASSIFICATIONS = frozenset({
    'PROCESS_EXITED', 'COMMAND_TIMEOUT', 'OUTPUT_BOUND', 'SPAWN_FAILED',
    'INVALID_OUTPUT', 'COMMAND_FAILED', 'RECEIPT_MISMATCH', 'TUNNEL_EXITED', 'TUNNEL_TIMEOUT',
}) | frozenset(code for code, _ in SSH_FAILURE_PATTERNS)


def safe_provider_diagnostic(value):
    if (type(value) is not dict or type(value.get('phase')) is not str
            or value['phase'] not in {'endpoint_lookup', 'provider_logs'}
            or type(value.get('http_status')) is not int or not 100 <= value['http_status'] <= 599
            or type(value.get('content_type')) is not str or value['content_type'] not in {'json', 'html', 'other'}
            or (value.get('retry_after_seconds') is not None
                and (type(value['retry_after_seconds']) is not int or not 0 <= value['retry_after_seconds'] <= 86400))
            or type(value.get('cf_mitigated_challenge')) is not bool):
        return None
    return {key: value.get(key) for key in ('phase', 'http_status', 'content_type',
                                           'retry_after_seconds', 'cf_mitigated_challenge')}


def bootstrap_record(raw):
    """Accept only the launcher's fixed receipt; never return surrounding text."""
    try:
        if not isinstance(raw, (bytes, str)) or len(raw) > 1024:
            return None
        record = decode(raw.encode() if isinstance(raw, str) else raw)
        if (type(record) is not dict or set(record) != {'status', 'code', 'error_type', 'bootstrap_line'}
                or record['status'] != 'failed' or record['code'] != 'WORKER_BOOTSTRAP_FAILED'
                or record['error_type'] not in BOOTSTRAP_EXCEPTION_TYPES
                or type(record['bootstrap_line']) is not int or not 1 <= record['bootstrap_line'] <= 1000000):
            return None
        return record
    except (TypeError, ValueError, UnicodeError):
        return None


def safe_command_diagnostic(value, *, phase=None):
    """An exception attribute is not permission to publish arbitrary metadata."""
    if type(value) is not dict:
        return None
    phase = phase or value.get('phase')
    status, timeout, classification = value.get('exit_status'), value.get('timeout'), value.get('classification')
    if (type(phase) is not str or phase not in {'verification', 'upload', 'configure', 'tunnel'}
            or (status is not None and (type(status) is not int or not -255 <= status <= 255))
            or type(timeout) is not bool or type(classification) is not str or classification not in COMMAND_CLASSIFICATIONS):
        return None
    result = {'phase': phase, 'exit_status': status, 'timeout': timeout, 'classification': classification}
    record = value.get('bootstrap')
    if type(record) is dict:
        try:
            record = bootstrap_record(canonical_json(record))
        except (TypeError, ValueError):
            record = None
        if record is not None:
            result['bootstrap'] = record
    return result


def safe_bootstrap_diagnostic(value):
    result = {'status': 'unavailable', 'records': []}
    if (type(value) is not dict or type(value.get('status')) is not str
            or value['status'] not in {'observed', 'unavailable', 'timeout'}
            or type(value.get('records')) is not list):
        return result
    result['status'] = value['status']
    provider = safe_provider_diagnostic(value.get('provider'))
    if provider is not None:
        result['provider'] = provider
    for item in value['records'][:4]:
        try:
            record = bootstrap_record(canonical_json(item))
        except (TypeError, ValueError):
            record = None
        if record is not None:
            result['records'].append(record)
    return result


def require(condition, code):
    if not condition:
        raise RunnerError(code)


def exception_diagnostic(error):
    """Report a type and our own source line, never values or traceback text."""
    name = type(error).__name__
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', name):
        name = 'Exception'
    line = None
    trace = error.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == __file__:
            line = trace.tb_lineno
        trace = trace.tb_next
    return {'exception_type': name, 'location': 'gpu_acceptance_runner.py' + (f':{line}' if line else '')}


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def read_file(path, *, owner, private=False, bound=BOUND):
    path = Path(path).absolute()
    # Fixture callers can inspect their own files in a user namespace where the
    # outer root UID is unmapped. Production root-owned configuration still
    # requires literal UID0 throughout its parent chain.
    root_owner = 0 if owner == 0 else Path('/').stat().st_uid
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode), 'SYMLINK_PARENT')
        temporary_root = owner != 0 and info.st_uid == root_owner and bool(info.st_mode & stat.S_ISVTX)
        require(info.st_uid in {root_owner, owner} and (not info.st_mode & 0o022 or temporary_root), 'UNTRUSTED_INPUT_PARENT')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
                and not info.st_mode & (0o077 if private else 0o022)
                and info.st_size <= bound, 'UNSAFE_INPUT_FILE')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            body = stream.read(bound + 1)
        require(len(body) <= bound, 'INPUT_TOO_LARGE')
        return body
    finally:
        os.close(fd)


class RunnerConfig(FrozenModel):
    service_uid: int = Field(strict=True, gt=0)
    research_uid: int = Field(strict=True, gt=0)
    admin_uid: int = Field(strict=True, gt=0)
    plan_path: str
    plan_sha256: SHA256
    worker_config_path: str
    worker_config_sha256: SHA256
    deployment: DeploymentSpec
    source_commit: GitSHA
    expected_worker_price_usd_per_hour: float = Field(strict=True, gt=0, lt=1.50, allow_inf_nan=False)
    max_runtime_seconds: int = Field(strict=True, ge=240, le=900)
    approval_wait_seconds: int = Field(default=3600, strict=True, ge=1, le=86400)
    submission_state_directory: str = '/var/lib/probe-calibration-submit'
    trusted_state_directory: str = '/var/lib/probe-core/gpu-acceptance'
    provider_config_path: str = '/etc/probe-core/runpod.json'
    ssh_identity_file: str = '/etc/probe-core/worker-ssh-key'
    bearer_secret_file: str = '/etc/probe-core/worker-token'

    @model_validator(mode='after')
    def fixed_profile(self):
        require(len({self.service_uid, self.research_uid, self.admin_uid}) == 3, 'IDENTITIES_OVERLAP')
        require(self.deployment.storage_mode == 'disposable_research' and self.deployment.volume_id is None
                and self.deployment.volume_gb == 0
                and self.deployment.image_repository is not None
                and self.deployment.launch_config_hash is not None, 'DISPOSABLE_RESEARCH_DEPLOYMENT_REQUIRED')
        for name in ('plan_path', 'worker_config_path', 'submission_state_directory', 'trusted_state_directory',
                     'provider_config_path', 'ssh_identity_file', 'bearer_secret_file'):
            value = getattr(self, name)
            require(value.startswith('/') and all(p not in ('', '.', '..') for p in value.split('/')[1:]),
                    'ABSOLUTE_FIXED_PATH_REQUIRED')
        return self


def load_inputs(path, *, mode, owner=0):
    config = RunnerConfig.model_validate_json(read_file(path, owner=owner))
    require(os.geteuid() == (config.research_uid if mode == 'submit' else config.service_uid), 'WRONG_PROCESS_IDENTITY')
    raw = read_file(config.plan_path, owner=owner)
    require('sha256:' + hashlib.sha256(raw).hexdigest() == config.plan_sha256, 'PLAN_HASH_MISMATCH')
    plan = AcceptancePlan.model_validate_json(raw)
    validate_plan(config, plan)
    load_worker_config(config, plan, owner=owner)
    return config, plan


def validate_plan(config, plan):
    require(len(plan.cases) == 1, 'SINGLE_CALIBRATION_CASE_REQUIRED')
    case = plan.cases[0]
    require(case.spec.experiment_stage.value == 'calibration'
            and case.spec.model == plan.model, 'FIXED_CALIBRATION_CASE_REQUIRED')
    if case.name == 'backend-parity':
        # Preserve the already installed parity plan's exact approval binding;
        # the four additional cases must match the existing generated recipe.
        require(case.action == 'wait' and case.expected_state == 'COMPLETED' and case.expected_failure_kind is None
                and case.spec.operation.kind == 'backend_parity', 'FIXED_BACKEND_PARITY_REQUIRED')
    elif case.name == 'public-direction-transfer':
        inputs = case.spec.inputs
        expected = fixed_direction_plan(plan.model, plan.label, inputs.dataset_revision,
                                        inputs.prompt_set_hash, inputs.prompt_ids).cases[0]
        require(case == expected, 'FIXED_CALIBRATION_CASE_REQUIRED')
    else:
        require(case.name in {'capture-retention', 'hard-deadline', 'output-limit', 'vram-limit',
                             'cancel-running', 'supervisor-restart'}, 'FIXED_CALIBRATION_CASE_REQUIRED')
        inputs = case.spec.inputs
        expected = next(item for item in fixed_plan(plan.model, plan.label, inputs.dataset_revision,
                        inputs.prompt_set_hash, inputs.prompt_ids).cases if item.name == case.name)
        require(case == expected, 'FIXED_CALIBRATION_CASE_REQUIRED')
    canonical = json.loads(files('probe_core').joinpath('resources/canonical-models.json').read_text())
    require(any((item['repo'], item['revision']) == (plan.model.repo, plan.model.revision_sha)
                for item in canonical['models']) and plan.model.dtype == 'bfloat16'
            and plan.model.quantized is False, 'CANONICAL_MODEL_REQUIRED')
    require(len(case.spec.inputs.prompt_ids) == 2 and case.spec.limits.max_runtime_seconds <= config.max_runtime_seconds,
            'CALIBRATION_LIMITS_INVALID')


def load_worker_config(config, plan, *, owner=0):
    raw = read_file(config.worker_config_path, owner=owner)
    require('sha256:' + hashlib.sha256(raw).hexdigest() == config.worker_config_sha256, 'WORKER_CONFIG_HASH_MISMATCH')
    body = decode(raw)
    worker = WorkerConfig.model_validate(body)
    require(worker.model == plan.model and worker.device == 'cuda:0' and worker.backend == 'nnsight'
            and worker.provider_backend == 'runpod' and worker.code_git_commit == config.source_commit
            and worker.container_image_digest == config.deployment.image_digest
            and worker.region == config.deployment.region
            and Decimal(str(worker.live_price_usd_per_hour)) == Decimal(str(config.expected_worker_price_usd_per_hour))
            and worker.cgroup_directory == '/sys/fs/cgroup/probe-jobs'
            and worker.tensor_directory == '/workspace/probe/tensors'
            and worker.output_directory == '/workspace/probe/attempts', 'WORKER_CONFIG_PROVENANCE_MISMATCH')
    require(worker.model_directory.startswith('/opt/probe-assets/models/') and len(worker.datasets) == 1
            and worker.datasets[0].path.startswith('/opt/probe-assets/datasets/')
            and worker.datasets[0].sha256 == plan.cases[0].spec.inputs.dataset_revision,
            'BAKED_PUBLIC_ASSETS_REQUIRED')
    for path in (worker.model_directory, worker.datasets[0].path):
        require(all(part not in ('', '.', '..') for part in path.split('/')[1:]), 'BAKED_ASSET_PATH_INVALID')
    return body


class State:
    """Exclusive immutable records, shared with no other UID; no tail rewriting."""
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        for path in (self.directory, *self.directory.parents):
            require(not path.is_symlink(), 'STATE_SYMLINK')
        info = self.directory.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'STATE_NOT_PRIVATE')
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.lock = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        info = os.fstat(self.lock)
        try:
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                    and not info.st_mode & 0o077, 'STATE_LOCK_INVALID')
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            os.close(self.fd)
            raise RunnerError('RUNNER_ALREADY_ACTIVE') from None
        except BaseException:
            os.close(self.lock)
            os.close(self.fd)
            raise

    def read(self, name):
        try:
            return decode(read_file(self.directory / name, owner=os.geteuid(), private=True))
        except FileNotFoundError:
            return None

    def publish(self, name, value):
        previous = self.read(name)
        if previous is not None:
            require(previous == value, 'STATE_BINDING_CONFLICT')
            return False
        raw = canonical_json(value).encode()
        require(len(raw) <= BOUND, 'STATE_RECORD_TOO_LARGE')
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)
        return True

    def close(self):
        os.close(self.lock)
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def matches_request(row, config, job_id):
    return (row.get('action') == 'CREATE' and row.get('job_ids') == [job_id]
            and row.get('configuration_hash') == config.deployment.digest
            and row.get('configuration') == config.deployment.model_dump(mode='json')
            and row.get('max_runtime_seconds') == config.max_runtime_seconds
            and row.get('infrastructure') is None and row.get('replaces_worker_id') is None)


def submit(config, plan, rpc, state):
    """Research RPC only. A lost provision reply is reconciled, never replayed."""
    validate_plan(config, plan)
    binding = {'config_sha256': digest(config.model_dump(mode='json')), 'plan_sha256': config.plan_sha256}
    state.publish('binding.json', binding)
    # Type=simple service ordering does not imply the facade socket is bound.
    # Only this harmless read is retried; a provision mutation is never retried.
    ready_until = time.monotonic() + 30
    while True:
        try:
            status = rpc.call('lab_status')
            require(status.get('gpu_start_authority') is False and isinstance(status.get('jobs'), list), 'RESEARCH_SURFACE_INVALID')
            break
        except RunnerError:
            raise
        except Exception:
            require(time.monotonic() < ready_until, 'RESEARCH_SERVICE_UNAVAILABLE')
            time.sleep(0.25)
    job = rpc.call('submit_job', {'spec': plan.cases[0].spec.model_dump(mode='json')})
    job_id = job.get('job_id')
    require(isinstance(job_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', job_id), 'JOB_ID_INVALID')
    parameters = {'deployment': config.deployment.model_dump(mode='json'), 'job_ids': [job_id],
                  'max_runtime_seconds': config.max_runtime_seconds}
    intent = {'binding': binding, 'job_id': job_id, 'parameters': parameters}
    existing = state.read('provision-intent.json')
    if existing is not None:
        require(existing == intent, 'PROVISION_INTENT_CONFLICT')
    status = rpc.call('gpu_status')
    require(status.get('configured') is True and isinstance(status.get('requests'), list), 'CONTROLLER_UNAVAILABLE')
    matches = [row for row in status['requests'] if matches_request(row, config, job_id)]
    require(len(matches) <= 1, 'DUPLICATE_PROVISION_REQUESTS')
    if not matches and existing is None:
        state.publish('provision-intent.json', intent)  # fsync before a potentially lost RPC reply
        try:
            row = rpc.call('request_gpu_provision', parameters)
            require(matches_request(row, config, job_id), 'PROVISION_REPLY_MISMATCH')
            matches = [row]
        except Exception:
            status = rpc.call('gpu_status')
            matches = [row for row in status.get('requests', []) if matches_request(row, config, job_id)]
    require(len(matches) == 1, 'PROVISION_RESPONSE_UNCERTAIN')
    row = matches[0]
    receipt = {'job_id': job_id, 'request_id': row['request_id'], 'worker_id': row['worker_id'],
               'approval_id': row['approval_id'], 'batch_hash': row['batch_hash'],
               'configuration_hash': config.deployment.digest, 'approval_consumed_by_runner': False}
    state.publish('submitted.json', receipt)
    return receipt


def find_request(ledger, config, plan):
    jobs = [job for job in ledger.list_jobs() if job.spec.idempotency_key == plan.cases[0].spec.idempotency_key]
    if not jobs:
        return None
    require(len(jobs) == 1 and jobs[0].spec == plan.cases[0].spec, 'LEDGER_JOB_MISMATCH')
    with ledger.read_connection() as conn:
        rows = conn.execute('SELECT * FROM compute_requests ORDER BY created_at').fetchall()
    matches = []
    for raw in rows:
        row = dict(raw)
        for name in ('configuration', 'job_ids', 'infrastructure'):
            row[name] = json.loads(row[name]) if row[name] else None
        if matches_request(row, config, jobs[0].job_id):
            matches.append(row)
    require(len(matches) <= 1, 'DUPLICATE_PROVISION_REQUESTS')
    return matches[0] if matches else None


def validate_authority(ledger, config, plan, request, *, now):
    require(request == find_request(ledger, config, plan), 'REQUEST_CHANGED')
    require(request['state'] == 'RUNNING' and isinstance(request['observed_provider_id'], str), 'COMPUTE_NOT_RUNNING')
    with ledger.read_connection() as conn:
        row = conn.execute('SELECT * FROM approvals WHERE approval_id=?', (request['approval_id'],)).fetchone()
        jobs = [v[0] for v in conn.execute('SELECT job_id FROM approval_jobs WHERE approval_id=?', (request['approval_id'],))]
    require(row is not None, 'APPROVAL_MISSING')
    public = json.loads(row['document'])
    require(row['consumed_at'] is not None and row['ended_at'] is None
            and jobs == request['job_ids'] and public.get('purpose', 'research') == 'research'
            and public['pod_id'] == request['worker_id'] and public['batch_hash'] == request['batch_hash']
            and public['max_runtime_seconds'] == config.max_runtime_seconds
            and config.expected_worker_price_usd_per_hour <= public['price_ceiling_usd_per_hour'] < 1.5
            and request['batch_hash'] == ledger.batch_hash(jobs)
            and row['deadline'] == request['deadline'] and math.isfinite(row['deadline'])
            and row['consumed_at'] <= now < row['deadline']
            and row['deadline'] - row['consumed_at'] <= config.max_runtime_seconds, 'APPROVAL_BINDING_INVALID')
    return row['deadline']


def fingerprint(key):
    try:
        parts = key.strip().split()
        require(len(parts) >= 2 and parts[0] == 'ssh-ed25519', 'ED25519_REQUIRED')
        raw = base64.b64decode(parts[1], validate=True)
        require(len(raw) == 51 and raw[:19] == b'\0\0\0\x0bssh-ed25519\0\0\0\x20', 'ED25519_KEY_INVALID')
        return 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
    except (ValueError, IndexError):
        raise RunnerError('ED25519_KEY_INVALID') from None


def host_fingerprints(events, *, client_fingerprint):
    result = set()
    for event in events:
        if event.get('source') != 'container':
            continue
        match = re.fullmatch(r'256 (SHA256:[A-Za-z0-9+/]{43}) [^\r\n]{1,256} \(ED25519\)', event.get('line', '').strip())
        if match and match[1] != client_fingerprint:
            result.add(match[1])
    require(len(result) <= 1, 'HOST_FINGERPRINT_AMBIGUOUS')
    require(len(result) == 1, 'HOST_FINGERPRINT_UNAVAILABLE')
    return result.pop()


def read_provider_logs(provider_config, pod_id, *, seconds=3, bootstrap_only=False):
    """Official v2 SSE; retain only fingerprints or strict bootstrap receipts.

    Source: https://api.runpod.io/v2/openapi.json, getPodLogs. A stream timeout
    after a complete record is normal; no raw log or authentication data is
    printed or copied to the worker.
    """
    require(re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', pod_id), 'PROVIDER_ID_INVALID')
    key = _read_owned_file(provider_config.api_key_file, os.geteuid(), private=True).decode().strip()
    url = 'https://api.runpod.io/v2/pods/' + quote(pod_id, safe='') + '/logs?' + urlencode({'source': 'container', 'tail': 1000})
    request = Request(url, headers={'Authorization': 'Bearer ' + key, 'User-Agent': 'Mozilla/5.0 Probe-MCP/0.2',
                                    'Accept': 'text/event-stream'})
    events, total, deadline = [], 0, time.monotonic() + seconds
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=seconds) as response:
            require(response.headers.get_content_type() == 'text/event-stream', 'PROVIDER_LOG_CONTENT_TYPE')
            while time.monotonic() < deadline:
                try:
                    line = response.readline(BOUND + 1)
                except (TimeoutError, socket.timeout):
                    break
                if not line:
                    break
                total += len(line)
                require(total <= BOUND, 'PROVIDER_LOG_BOUND_EXCEEDED')
                if bootstrap_only and line.startswith(b'data:') and b'WORKER_BOOTSTRAP_FAILED' in line:
                    event = decode(line[5:].strip())
                    if type(event) is dict and event.get('source') == 'container':
                        record = bootstrap_record(event.get('line'))
                        if record is not None:
                            events.append(record)
                            if len(events) == 4:
                                break
                elif not bootstrap_only and line.startswith(b'data:') and b'SHA256:' in line and b'(ED25519)' in line:
                    event = decode(line[5:].strip())
                    require(isinstance(event, dict) and isinstance(event.get('line'), str), 'PROVIDER_LOG_INVALID')
                    if 'SHA256:' in event['line'] and '(ED25519)' in event['line']:
                        events.append({k: event.get(k) for k in ('source', 'line', 'ts')})
    except HTTPError as error:
        with error:
            diagnostic = safe_provider_diagnostic({'phase': 'provider_logs',
                **provider_http_metadata(error.code, error.headers)})
        raise RunnerError('PROVIDER_LOGS_UNAVAILABLE', provider_diagnostic=diagnostic) from None
    except RunnerError:
        raise
    except Exception:
        raise RunnerError('PROVIDER_LOGS_UNAVAILABLE') from None
    return events


def provider_bootstrap_diagnostic(provider_config, pod_id, *, timeout_seconds=1):
    """One read with a caller deadline, including DNS/stream stalls.

    A daemon thread can finish its read after the caller deadline, but cannot
    mutate the result or delay the provider deletion. It only receives logs.
    """
    results = queue.Queue(maxsize=1)
    def observe():
        try:
            records = read_provider_logs(provider_config, pod_id, seconds=timeout_seconds, bootstrap_only=True)
            results.put({'status': 'observed', 'records': records})
        except Exception as error:
            result = {'status': 'unavailable', 'records': []}
            diagnostic = safe_provider_diagnostic(getattr(error, 'provider_diagnostic', None))
            if diagnostic is not None:
                result['provider'] = diagnostic
            results.put(result)
    threading.Thread(target=observe, daemon=True, name='probe-bootstrap-log-read').start()
    try:
        return results.get(timeout=timeout_seconds)
    except queue.Empty:
        return {'status': 'timeout', 'records': []}


def command_bytes(command, *, timeout=5):
    # Fixed commands produce public keys or a configuration hash receipt.
    process, failed = None, True
    body, stderr = bytearray(), bytearray()
    diagnostic = {'phase': 'verification', 'exit_status': None, 'timeout': False, 'classification': 'SPAWN_FAILED'}
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, close_fds=True, cwd='/',
                                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C'}, start_new_session=True)
        deadline = time.monotonic() + timeout
        diagnostic['classification'] = 'COMMAND_FAILED'
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    diagnostic.update(timeout=True, classification='COMMAND_TIMEOUT', exit_status=process.poll())
                    raise RunnerError('SSH_VERIFICATION_COMMAND_FAILED', diagnostic=diagnostic)
                for key, _ in selector.select(min(0.1, max(0, deadline-time.monotonic()))):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    (body if key.fileobj is process.stdout else stderr).extend(chunk)
                    if len(body) + len(stderr) > 65536:
                        diagnostic.update(classification='OUTPUT_BOUND', exit_status=process.poll())
                        raise RunnerError('SSH_VERIFICATION_COMMAND_FAILED', diagnostic=diagnostic)
        status = process.wait(timeout=max(0.1, deadline-time.monotonic()))
        diagnostic['exit_status'] = status
        if status != 0:
            diagnostic['classification'] = ssh_failure_classification(bytes(stderr)) or 'PROCESS_EXITED'
            record = bootstrap_record(bytes(body))
            if record is not None:
                diagnostic['bootstrap'] = record
            raise RunnerError('SSH_VERIFICATION_COMMAND_FAILED', diagnostic=diagnostic)
        diagnostic['classification'] = 'INVALID_OUTPUT'
        output = body.decode('ascii')
        failed = False
        return output
    except RunnerError:
        raise
    except subprocess.TimeoutExpired:
        diagnostic.update(timeout=True, classification='COMMAND_TIMEOUT', exit_status=process.poll())
        raise RunnerError('SSH_VERIFICATION_COMMAND_FAILED', diagnostic=diagnostic) from None
    except Exception:
        raise RunnerError('SSH_VERIFICATION_COMMAND_FAILED', diagnostic=diagnostic) from None
    finally:
        if process is not None:
            if failed or process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()


def verified_endpoint(config, request, backend, state, *, logs=read_provider_logs, command=command_bytes):
    intent = backend._intent(request['worker_id'])
    require(intent is not None and intent['provider_id'] == request['observed_provider_id']
            and intent['request_key'] == request['request_id'] and intent['configuration_hash'] == config.deployment.digest
            and intent['deadline'] == request['deadline'], 'PROVIDER_INTENT_MISMATCH')
    try:
        pod = backend.transport.request('GET', '/v2/pods/' + quote(intent['provider_id'], safe=''))
    except ProviderHTTPError as error:
        diagnostic = safe_provider_diagnostic({'phase': 'endpoint_lookup', **error.metadata})
        require(diagnostic is not None, 'PROVIDER_ENDPOINT_RESPONSE_INVALID')
        if error.status in {408, 429} or 500 <= error.status < 600:
            raise RunnerError('PROVIDER_ENDPOINT_UNAVAILABLE', provider_diagnostic=diagnostic) from None
        raise RunnerError('PROVIDER_ENDPOINT_REFUSED', provider_diagnostic=diagnostic) from None
    except ProviderResponseError as error:
        # RunPodHTTP wraps both network failures and malformed JSON. Retry only
        # explicit transport evidence, not an invalid response or provenance.
        if isinstance(error.__context__, (URLError, OSError)):
            raise RunnerError('PROVIDER_ENDPOINT_UNAVAILABLE') from None
        raise RunnerError('PROVIDER_ENDPOINT_RESPONSE_INVALID') from None
    except (TimeoutError, ConnectionError):
        raise RunnerError('PROVIDER_ENDPOINT_UNAVAILABLE') from None
    require(type(pod) is dict, 'PROVIDER_ENDPOINT_RESPONSE_INVALID')
    observed = backend._observe(pod, intent)
    require(observed.state == WorkerState.RUNNING and observed.provider_id == request['observed_provider_id'], 'PROVIDER_NOT_RUNNING')
    require(pod.get('dataCenterId') == config.deployment.region
            and pod.get('image') == config.deployment.image_repository + '@' + config.deployment.image_digest
            and Decimal(str(pod.get('cost'))) == Decimal(str(config.expected_worker_price_usd_per_hour)), 'WORKER_PROVENANCE_MISMATCH')
    runtime = pod.get('runtime')
    require(runtime is not None, 'DIRECT_SSH_ENDPOINT_UNAVAILABLE')
    require(type(runtime) is dict, 'PROVIDER_RUNTIME_INVALID')
    direct = runtime.get('ports')
    require(direct is not None, 'DIRECT_SSH_ENDPOINT_UNAVAILABLE')
    require(type(direct) is list and all(type(value) is dict for value in direct), 'PROVIDER_RUNTIME_INVALID')
    require(all(type(value.get('private')) is int and 1 <= value['private'] <= 65535
                and type(value.get('type')) is str for value in direct), 'PROVIDER_RUNTIME_INVALID')
    endpoints = [(v.get('ip'), v.get('public')) for v in direct
                 if type(v.get('private')) is int and v['private'] == 22 and v.get('type') == 'tcp']
    require(len(endpoints) == 1, 'DIRECT_SSH_ENDPOINT_UNAVAILABLE')
    host, port = endpoints[0]
    try:
        require(ipaddress.ip_address(host).is_global, 'DIRECT_SSH_ADDRESS_INVALID')
    except (ValueError, TypeError):
        raise RunnerError('DIRECT_SSH_ADDRESS_INVALID') from None
    require(type(port) is int and 1 <= port <= 65535, 'DIRECT_SSH_PORT_INVALID')
    read_file(config.ssh_identity_file, owner=os.geteuid(), private=True, bound=65536)
    client_key = command(['/usr/bin/ssh-keygen', '-y', '-f', config.ssh_identity_file])
    expected = host_fingerprints(logs(backend.config, observed.provider_id), client_fingerprint=fingerprint(client_key))
    scanned = command(['/usr/bin/ssh-keyscan', '-T', '3', '-p', str(port), '-t', 'ed25519', host])
    keys = {line.split(None, 1)[1] for line in scanned.splitlines() if line and not line.startswith('#') and len(line.split(None, 1)) == 2}
    require(len(keys) == 1, 'SCANNED_HOST_KEY_AMBIGUOUS')
    key = keys.pop()
    require(fingerprint(key) == expected, 'SSH_HOST_KEY_MISMATCH')
    binding = {'provider_id': observed.provider_id, 'worker_id': request['worker_id'],
               'request_id': request['request_id'], 'host': host, 'ssh_port': port,
               'fingerprint': expected, 'key': key, 'image': pod['image'],
               'price': pod['cost'], 'region': pod['dataCenterId']}
    state.publish('endpoint.json', binding)
    hosts = state.directory / 'known_hosts'
    # OpenSSH looks up a bare host at its standard port; brackets encode only
    # non-standard ports, including for IPv6 addresses.
    host_identity = host if port == 22 else f'[{host}]:{port}'
    content = f'{host_identity} {key}\n'.encode()
    try:
        fd = os.open(hosts, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        require(read_file(hosts, owner=os.geteuid(), private=True) == content, 'KNOWN_HOSTS_CHANGED')
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(state.fd)
    return dict(host=host, user='root', ssh_port=port, remote_port=8080,
                identity_file=Path(config.ssh_identity_file), known_hosts_file=hosts)


def configure_worker(config, plan, settings, state, *, deadline, clock=time.time,
                     command=command_bytes, owner=0):
    """Send one immutable private bundle through the already verified SSH key.

    The source file is private and removed locally afterward. The image's fixed
    configure handler consumes the remote source. A lost upload/configure reply
    never causes a different configuration or a provider action to be replayed.
    """
    worker = load_worker_config(config, plan, owner=owner)
    token = read_file(config.bearer_secret_file, owner=os.geteuid(), private=True, bound=513).decode().strip()
    require(32 <= len(token) <= 512, 'WORKER_SECRET_INVALID')
    bundle = {'schema_version': 1, 'worker_config': worker, 'bearer_token': token}
    expected = {'schema_version': 1, 'configured': True,
                'worker_config_sha256': digest(worker), 'bundle_sha256': digest(bundle)}
    local = state.directory / 'configuration-bundle.json'
    def remove_private_bundle():
        try:
            raw = read_file(local, owner=os.geteuid(), private=True)
        except FileNotFoundError:
            return
        require(decode(raw) == bundle, 'PRIVATE_BUNDLE_CHANGED')
        local.unlink()
        os.fsync(state.fd)
    intent = {'bundle_sha256': expected['bundle_sha256'], 'worker_config_sha256': config.worker_config_sha256,
              'endpoint_sha256': digest(state.read('endpoint.json'))}
    previous = state.read('configure-intent.json')
    if previous is not None:
        require(previous == intent, 'CONFIGURATION_INTENT_CONFLICT')
        receipt = state.read('configured.json')
        require(receipt is None or receipt == expected, 'CONFIGURATION_RECEIPT_CONFLICT')
        remove_private_bundle()
        if receipt is not None:
            return receipt
        result = {'configured': None, 'reason': 'CONFIGURATION_RESPONSE_UNCERTAIN'}
        failure = state.read('configure-failure.json')
        diagnostic = safe_command_diagnostic(failure.get('diagnostic')) if type(failure) is dict else None
        if diagnostic is not None:
            result['diagnostic'] = diagnostic
        return result
    require(clock() < deadline, 'WORKER_STARTUP_DEADLINE')
    state.publish('configure-intent.json', intent)
    # State publishes this one credential-bearing file only in the private
    # trusted directory. It is never included in any public report or audit.
    state.publish(local.name, bundle)
    options = ['-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectionAttempts=1',
               '-o', 'ConnectTimeout=5', '-o', 'PermitLocalCommand=no',
               '-o', 'UserKnownHostsFile=' + str(settings['known_hosts_file']),
               '-i', str(settings['identity_file'])]
    host = settings['host']
    destination = '[' + host + ']' if ':' in host else host
    remote = '/run/probe-worker-bootstrap.json'
    phase = 'upload'
    def uncertain(error):
        diagnostic = safe_command_diagnostic(getattr(error, 'diagnostic', None), phase=phase)
        diagnostic = diagnostic or {'phase': phase, 'exit_status': None, 'timeout': False, 'classification': 'COMMAND_FAILED'}
        result = {'configured': None, 'reason': 'CONFIGURATION_RESPONSE_UNCERTAIN', 'diagnostic': diagnostic}
        state.publish('configure-failure.json', result)
        return result
    try:
        command(['/usr/bin/scp', '-p', *options, '-P', str(settings['ssh_port']),
                 str(local), 'root@' + destination + ':' + remote],
                timeout=min(15, max(0.1, deadline-clock())))
        require(clock() < deadline, 'WORKER_STARTUP_DEADLINE')
        phase = 'configure'
        output = command(['/usr/bin/ssh', *options, '-T', '-p', str(settings['ssh_port']), 'root@' + host,
                          '/opt/probe-core/venv/bin/python', '-I', '-m', 'probe_core.gpu_launch', '--configure', remote],
                         timeout=min(15, max(0.1, deadline-clock())))
        try:
            received = decode(output.encode())
        except Exception:
            raise RunnerError('CONFIGURATION_RECEIPT_MISMATCH') from None
        require(received == expected, 'CONFIGURATION_RECEIPT_MISMATCH')
        state.publish('configured.json', expected)
        return expected
    except RunnerError as error:
        if str(error) != 'SSH_VERIFICATION_COMMAND_FAILED':
            if str(error) == 'CONFIGURATION_RECEIPT_MISMATCH':
                error.diagnostic = {'phase': phase, 'exit_status': 0, 'timeout': False, 'classification': 'RECEIPT_MISMATCH'}
            raise
        return uncertain(error)
    except Exception as error:
        return uncertain(error)
    finally:
        # Never unlink an unexpected replacement or a symlink.
        remove_private_bundle()


def stop_and_observe(cloud, backend, request, *, sleep=time.sleep):
    for _ in range(3):
        try:
            cloud.stop_gpu(request['worker_id'])
        except Exception:
            # This adapter's delete is limited to its already durable intent.
            try:
                backend.stop(request['worker_id'])
            except Exception:
                pass
        try:
            observed = backend.status(request['worker_id'])
            if observed.state == WorkerState.ABSENT and observed.provider_id == request['observed_provider_id']:
                return {'provider_id': observed.provider_id, 'state': 'ABSENT', 'confirmed': True}
        except Exception:
            pass
        sleep(1)
    return {'provider_id': request['observed_provider_id'], 'confirmed': False}


def wait_for_worker(client, *, deadline, clock=time.time, sleep=time.sleep):
    """A fixed, absent route authenticates without submitting a job.

    The existing worker handler authenticates every GET before routing, returning
    exactly 404/not_found for this route; a wrong bearer returns 401. Merely
    opening a local SSH-forward listener is not worker readiness.
    """
    while clock() < deadline:
        request = Request(client.base_url + '/v1/acceptance-readiness',
                          headers={'Authorization': 'Bearer ' + client._secret.get_secret_value()})
        try:
            with client._opener.open(request, timeout=min(3, max(0.1, deadline-clock()))) as response:
                raise RunnerError('UNEXPECTED_WORKER_READINESS_RESPONSE')
        except HTTPError as error:
            with error:
                body = error.read(1025)
                if error.code == 404 and len(body) <= 1024 and decode(body) == {'error': 'not_found'}:
                    return
                if error.code == 401:
                    raise RunnerError('WORKER_AUTHENTICATION_FAILED') from None
                raise RunnerError('UNEXPECTED_WORKER_READINESS_RESPONSE') from None
        except (URLError, OSError):
            sleep(0.25)
    raise RunnerError('WORKER_READINESS_DEADLINE')


class _ActionStopped(RuntimeError):
    """Interrupt observation without being mistaken for a retryable SSH failure."""


class RunningAction:
    """Observe/act once while the runner continues renewing the same lease.

    Only read-only helper inspections are polled before the execution-start
    marker exists. ``run_action`` owns the durable, once-only mutation intent.
    Closing the gate prevents any later call; deletion need not wait for an
    in-flight bounded SSH observation to finish.
    """
    def __init__(self, ledger, plan, request, client, lifecycle, directory, *,
                 deadline, clock=time.time, action=run_action):
        self.stop = threading.Event()
        self.done = threading.Event()
        self.entered = threading.Event()
        self.start_attempted = False
        self.report = None
        self.passed = False
        self.clock = clock
        self.deadline = min(deadline, request.deadline.timestamp())
        self.monotonic_deadline = time.monotonic() + max(0, self.deadline - clock())

        def guarded(function):
            def call(*args, **kwargs):
                self.check()
                return function(*args, **kwargs)
            return call

        remote = SimpleNamespace(status=guarded(client.status), cancel=guarded(client.cancel))
        helper = SimpleNamespace(config_sha256=lifecycle.config_sha256,
            endpoint_identity=lifecycle.endpoint_identity, inspect=guarded(lifecycle.inspect),
            restart=guarded(lifecycle.restart))

        def execute():
            self.entered.set()
            try:
                while True:
                    self.check()
                    try:
                        # The fixed helper requires execution-started.json and
                        # verifies its exact request and live process identities.
                        helper.inspect(request)
                        break
                    except TransportError:
                        self.stop.wait(0.1)
                self.check()
                self.report = action(ledger, plan, case_name=plan.cases[0].name,
                    job_id=request.job_id, attempt_id=request.attempt_id, approval_id=request.approval_id,
                    client=remote, lifecycle=helper, action_directory=directory,
                    observe_seconds=10, clock=lambda: datetime.fromtimestamp(clock(), timezone.utc))
                self.passed = isinstance(self.report, dict) and self.report.get('status') == 'passed'
            except Exception:
                # No exception text, SSH addresses or private paths leave here.
                self.passed = False
            finally:
                self.done.set()

        self.thread = threading.Thread(target=execute, name='probe-acceptance-action', daemon=True)

    def start(self):
        self.check()
        self.start_attempted = True
        self.thread.start()

    def check(self):
        if self.stop.is_set() or self.clock() >= self.deadline or time.monotonic() >= self.monotonic_deadline:
            raise _ActionStopped()

    def close(self):
        self.stop.set()
        # Helper SSH has a 15s timeout and at most 5s child cleanup. Do this
        # join after provider deletion, so slow diagnostics cannot defer it.
        if not self.start_attempted:
            return True
        if not self.entered.wait(timeout=1):
            return False  # Startup was interrupted; the closed gate still prevents actions.
        self.thread.join(timeout=20)
        return not self.thread.is_alive()


def lifecycle_settings(settings, configuration):
    require(configuration.get('configured') is True and isinstance(configuration.get('worker_config_sha256'), str)
            and re.fullmatch(r'sha256:[0-9a-f]{64}', configuration['worker_config_sha256']),
            'LIFECYCLE_CONFIGURATION_NOT_CONFIRMED')
    # gpu_launch writes canonical config bytes, which can differ from the
    # whitespace in the administrator-owned public WorkerConfig input file.
    return {**{key: str(settings[key]) for key in ('host', 'user', 'identity_file', 'known_hosts_file')},
            'ssh_port': settings['ssh_port'], 'config_path': '/workspace/probe/config/worker.json',
            'config_sha256': configuration['worker_config_sha256'],
            'token_path': '/workspace/probe/config/worker-token'}


def read_worker_direction(settings, phase, *, deadline, clock=time.time, command=command_bytes):
    """Bounded read/hash only, through this Pod's already pinned SSH identity."""
    require(phase in {'before', 'after'} and clock() < deadline, 'DIRECTION_READBACK_DEADLINE')
    argv = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=5',
        '-o', 'PermitLocalCommand=no', '-o', 'UserKnownHostsFile=' + str(settings['known_hosts_file']),
        '-i', str(settings['identity_file']), '-p', str(settings['ssh_port']), 'root@' + settings['host'],
        '/opt/probe-core/venv/bin/python', '-I', '-c', shlex.quote(READBACK_PROGRAM),
        '/workspace/probe/tensors', DIRECTION_SHA256.removeprefix('sha256:'), '10001', phase]
    output = command(argv, timeout=min(5, max(0.1, deadline-clock())))
    require(clock() < deadline, 'DIRECTION_READBACK_DEADLINE')
    require(len(output) <= 512, 'DIRECTION_READBACK_INVALID')
    value = decode(output.encode())
    # The staging gate compares the complete response with the pinned recipe;
    # nothing from this remote output is published unless that comparison passes.
    require(type(value) is dict, 'DIRECTION_READBACK_INVALID')
    return value


def run(config, plan, ledger, backend, cloud, state, *, clock=time.time, sleep=time.sleep,
        endpoint=verified_endpoint, tunnel_factory=SSHTunnel, client_factory=WorkerClient,
        readiness=wait_for_worker, configure=configure_worker, progress=lambda _: None,
        bootstrap_diagnostics=None, lifecycle_factory=SSHActionClient, action=run_action,
        direction_readback=read_worker_direction):
    validate_plan(config, plan)
    state.publish('binding.json', {'config_sha256': digest(config.model_dump(mode='json')), 'plan_sha256': config.plan_sha256})
    previous = state.read('result.json')
    if previous is not None:
        return previous
    wait_deadline = time.monotonic() + config.approval_wait_seconds
    request = None
    progress('waiting_for_human_approval')
    while time.monotonic() < wait_deadline:
        request = find_request(ledger, config, plan)
        if request is not None and request['state'] == 'RUNNING':
            break
        require(request is None or request['state'] in ('PENDING', 'PREPARING', 'STARTING'), 'REQUEST_TERMINATED_BEFORE_ACCEPTANCE')
        sleep(0.25)
    require(request is not None and request['state'] == 'RUNNING', 'APPROVAL_WAIT_EXPIRED')
    result = {'schema_version': 1, 'kind': 'single_public_gpu_calibration', 'status': 'failed',
              'request_id': request['request_id'], 'worker_id': request['worker_id'],
              'provider_id': request['observed_provider_id'], 'plan_sha256': config.plan_sha256,
              'approval_consumed_by_runner': False, 'scientific_evidence': False}
    action_task = None
    action_directory = state.directory / 'actions'
    action_case = plan.cases[0].action != 'wait'
    direction_case = plan.cases[0].name == 'public-direction-transfer'
    try:
        result['stage'] = 'approval_verification'
        deadline = validate_authority(ledger, config, plan, request, now=clock())
        state.publish('bound-request.json', {key: request[key] for key in ('request_id', 'worker_id', 'approval_id', 'observed_provider_id', 'deadline', 'batch_hash')})
        result['stage'] = 'verified_worker_startup'
        progress(result['stage'])
        # Cold image startup shares the already consumed allowance. Preserve
        # the full approved job runtime and time to collect/delete; never renew
        # the provider or approval deadline merely because startup was slow.
        job_runtime = plan.cases[0].spec.limits.max_runtime_seconds
        startup_deadline = deadline - job_runtime - COLLECTION_DELETION_RESERVE_SECONDS
        window = {'approval_deadline': deadline, 'dispatch_cutoff': startup_deadline,
                  'job_runtime_seconds': job_runtime,
                  'collection_deletion_reserve_seconds': COLLECTION_DELETION_RESERVE_SECONDS}
        state.publish('startup-window.json', window)
        result['startup'] = dict(window, endpoint_attempts=0)
        require(clock() < startup_deadline, 'INSUFFICIENT_EXECUTION_WINDOW')
        while clock() < startup_deadline:
            validate_authority(ledger, config, plan, request, now=clock())
            try:
                result['startup']['endpoint_attempts'] += 1
                settings = endpoint(config, request, backend, state)
                break
            except RunnerError as error:
                if str(error) not in {'DIRECT_SSH_ENDPOINT_UNAVAILABLE', 'HOST_FINGERPRINT_UNAVAILABLE',
                                      'PROVIDER_LOGS_UNAVAILABLE', 'SSH_VERIFICATION_COMMAND_FAILED',
                                      'PROVIDER_ENDPOINT_UNAVAILABLE'}:
                    raise
                result['startup']['last_endpoint_reason'] = str(error)
                diagnostic = safe_provider_diagnostic(getattr(error, 'provider_diagnostic', None))
                if diagnostic is not None:
                    result['startup']['last_provider_error'] = diagnostic
                sleep(1)
        else:
            raise RunnerError('WORKER_STARTUP_DEADLINE')
        require(clock() < startup_deadline, 'WORKER_STARTUP_DEADLINE')
        result['startup']['endpoint_ready_at'] = clock()
        result['stage'] = 'worker_configuration'
        progress(result['stage'])
        result['configuration'] = configure(config, plan, settings, state, deadline=startup_deadline, clock=clock)
        require(clock() < startup_deadline, 'WORKER_STARTUP_DEADLINE')
        secret = read_file(config.bearer_secret_file, owner=os.geteuid(), private=True, bound=513).decode().strip()
        require(32 <= len(secret) <= 512, 'WORKER_SECRET_INVALID')
        result['stage'] = 'ssh_tunnel'
        progress(result['stage'])
        with ExitStack() as resources:
            tunnel = resources.enter_context(tunnel_factory(**settings))
            client = client_factory(f'http://127.0.0.1:{tunnel.local_port}', secret, timeout_seconds=5)
            result['stage'] = 'worker_readiness'
            progress(result['stage'])
            readiness(client, deadline=startup_deadline, clock=clock, sleep=sleep)
            require(clock() < deadline, 'APPROVED_DEADLINE_REACHED')
            require(clock() < startup_deadline, 'WORKER_STARTUP_DEADLINE')
            result['startup']['worker_ready_at'] = clock()
            result['stage'] = 'approved_dispatch'
            progress(result['stage'])
            direction_options = ({'readback': lambda phase: direction_readback(settings, phase, deadline=deadline, clock=clock),
                                  'publish': lambda evidence: state.publish('direction-transfer.json', evidence)}
                                 if direction_case else {})
            dispatcher = (DirectionDispatcher if direction_case else Dispatcher)(ledger, client, worker_id=request['worker_id'],
                                    transfer_directory=state.directory / 'transfers',
                                    input_artifact_root=LEDGER.parent / 'input-artifacts', lease_seconds=30, **direction_options)
            service = DispatcherService(dispatcher, tunnel=tunnel)
            lifecycle = lifecycle_factory(lifecycle_settings(settings, result['configuration'])) if action_case else None
            while clock() < deadline:
                current = find_request(ledger, config, plan)
                validate_authority(ledger, config, plan, current, now=clock())
                if ledger.get_job(request['job_ids'][0]).attempt_id is None:
                    require(clock() < startup_deadline, 'WORKER_STARTUP_DEADLINE')
                service.tick()
                job = ledger.get_job(request['job_ids'][0])
                if action_case and action_task is None and job.state == JobState.RUNNING:
                    execution = dispatcher._request(job)
                    require(execution.worker_id == request['worker_id']
                            and execution.approval_id == request['approval_id'], 'LIFECYCLE_ATTEMPT_MISMATCH')
                    action_client = client_factory(f'http://127.0.0.1:{tunnel.local_port}', secret, timeout_seconds=5)
                    action_task = RunningAction(ledger, plan, execution, action_client, lifecycle, action_directory,
                                                deadline=deadline, clock=clock, action=action)
                    resources.callback(action_task.stop.set)  # Runs before the tunnel's potentially slow close.
                    action_task.start()  # Ownership and cleanup exist before a thread can run.
                if action_task is not None and action_task.done.is_set():
                    require(action_task.passed, 'LIFECYCLE_ACTION_NOT_ESTABLISHED')
                if job.state in (JobState.COMPLETED, JobState.FAILED):
                    if action_task is None or action_task.done.is_set():
                        break
                sleep(0.25)
            else:
                raise RunnerError('APPROVED_DEADLINE_REACHED')
            result['stage'] = 'artifact_collection'
            progress(result['stage'])
            observation = collect(ledger, plan, client, action_directory=action_directory if action_case else None,
                direction_evidence=state.read('direction-transfer.json') if direction_case else None,
                input_artifact_root=LEDGER.parent / 'input-artifacts' if direction_case else None)
            state.publish('observations.json', observation)
            require(observation['case_results_passed'] is True, 'CALIBRATION_DID_NOT_PASS')
            if action_case:
                require(observation['action_evidence']['complete'] is True, 'LIFECYCLE_ACTION_PROOF_MISSING')
                result['action_evidence_sha256'] = digest(observation['action_evidence'])
            evidence = observation['cases'][0]
            if direction_case:
                result['direction_transfer_sha256'] = digest(evidence['direction_transfer'])
            if plan.cases[0].expected_state == 'COMPLETED':
                manifest = ledger.get_manifest(request['job_ids'][0])
                require(manifest.software.container_image_digest == config.deployment.image_digest
                        and manifest.software.probe_mcp_git_commit == config.source_commit
                        and manifest.hardware.region == config.deployment.region
                        and Decimal(str(manifest.hardware.live_price_usd_per_hour)) == Decimal(str(config.expected_worker_price_usd_per_hour)),
                        'MANIFEST_PROVENANCE_MISMATCH')
                result['manifest_sha256'] = digest(manifest.model_dump(mode='json'))
            # Failed limit cases intentionally have no success manifest. The
            # collector requires an exact stopped worker receipt and the named
            # limit's specific failure, in addition to the Ledger outcome.
            result.update(status='passed', stage='completed', job_id=job.job_id, attempt_id=job.attempt_id,
                          case=plan.cases[0].name, observed_state=job.state.value,
                          observed_failure_kind=job.failure_kind,
                          worker_receipt_sha256=digest(evidence['observed_receipt']))
    except RunnerError as error:
        result['reason'] = str(error)
        diagnostic = safe_provider_diagnostic(getattr(error, 'provider_diagnostic', None))
        if diagnostic is not None:
            result['provider'] = diagnostic
        diagnostic = safe_command_diagnostic(getattr(error, 'diagnostic', None))
        if diagnostic is not None:
            result['transport'] = diagnostic
    except Exception as error:
        result['reason'] = 'ACCEPTANCE_RUNTIME_UNAVAILABLE'
        result['diagnostic'] = exception_diagnostic(error)
        diagnostic = safe_command_diagnostic(getattr(error, 'diagnostic', None))
        if diagnostic is not None:
            result['transport'] = diagnostic
    finally:
        if action_task is not None:
            action_task.stop.set()  # Gate all future action calls before provider deletion.
        if result['status'] == 'failed' and result.get('stage') in {
                'verified_worker_startup', 'worker_configuration', 'ssh_tunnel', 'worker_readiness'}:
            # Diagnostics are best effort and have less priority than deletion.
            # No query is made after the original allowance has expired.
            if clock() + 2 < request['deadline']:
                try:
                    observe = bootstrap_diagnostics or provider_bootstrap_diagnostic
                    result['bootstrap_diagnostic'] = safe_bootstrap_diagnostic(
                        observe(backend.config, request['observed_provider_id']))
                except Exception:
                    result['bootstrap_diagnostic'] = {'status': 'unavailable', 'records': []}
            else:
                result['bootstrap_diagnostic'] = {'status': 'skipped_deadline', 'records': []}
        progress('provider_deletion')
        result['teardown'] = stop_and_observe(cloud, backend, request, sleep=sleep)
        if action_task is not None and not action_task.close():
            result.update(status='failed', reason='LIFECYCLE_ACTION_CLEANUP_UNCONFIRMED')
        if not result['teardown']['confirmed']:
            result.update(status='failed', reason='PROVIDER_DELETION_UNCONFIRMED')
        state.publish('result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('submit', 'run'))
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    def interrupted(*_):
        raise RunnerError('RUNNER_INTERRUPTED')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        config, plan = load_inputs(args.config, mode=args.mode)
        directory = config.submission_state_directory if args.mode == 'submit' else config.trusted_state_directory
        with State(directory) as state:
            if args.mode == 'submit':
                result = submit(config, plan, UnixRPCClient(RESEARCH_SOCKET, expected_server_uid=config.service_uid), state)
            else:
                require(LEDGER.is_file() and not LEDGER.is_symlink(), 'INSTALLED_LEDGER_REQUIRED')
                provider = RunPodProvider(RunPodConfig.load(config.provider_config_path))
                with Ledger(LEDGER) as ledger:
                    result = run(config, plan, ledger, provider,
                                 ControllerClient(CONTROLLER_SOCKET, expected_server_uid=config.service_uid), state,
                                 progress=lambda stage: print(canonical_json({'stage': stage}), flush=True))
        print(canonical_json(result))
        if result.get('status') == 'failed':
            raise SystemExit(1)
    except Exception as error:
        reason = str(error) if isinstance(error, RunnerError) and re.fullmatch(r'[A-Z0-9_]{1,100}', str(error)) else 'ACCEPTANCE_RUNNER_REFUSED'
        result = {'status': 'failed', 'reason': reason, 'approval_consumed_by_runner': False}
        if reason == 'ACCEPTANCE_RUNNER_REFUSED':
            result['diagnostic'] = exception_diagnostic(error)
        print(canonical_json(result))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
