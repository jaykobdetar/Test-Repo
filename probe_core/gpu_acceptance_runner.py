"""One fixed public calibration through the installed research/approval pipeline.

The submission process is the research UID. The runner is the Ledger owner.
Neither mode issues or consumes an approval. The human uses the existing admin
socket; the independently supervised watchdog retains the absolute deadline.
"""
from __future__ import annotations

import argparse
import base64
from decimal import Decimal
import fcntl
import hashlib
from importlib.resources import files
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import time
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from pydantic import Field, model_validator

from .audit import canonical_json
from .controller import ControllerClient
from .dispatcher import Dispatcher, DispatcherService, SSHTunnel, WorkerClient
from .gpu_acceptance import AcceptancePlan, collect
from .ledger import JobState, Ledger
from .provider import DeploymentSpec, WorkerState
from .rpc import UnixRPCClient, decode
from .runpod_provider import (ProviderHTTPError, ProviderResponseError, RunPodConfig,
                              RunPodProvider, _NoRedirect, _read_owned_file)
from .schemas import FrozenModel, GitSHA, SHA256
from .worker_contracts import WorkerConfig

LEDGER = Path('/var/lib/probe-core/research.sqlite')
RESEARCH_SOCKET = '/run/probe-research/research.sock'
CONTROLLER_SOCKET = '/run/probe-controller/research.sock'
BOUND = 1024 * 1024


class RunnerError(ValueError):
    """Only fixed, non-sensitive reason codes cross the CLI boundary."""


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
    require(case.name == 'backend-parity' and case.action == 'wait' and case.expected_state == 'COMPLETED'
            and case.expected_failure_kind is None and case.spec.operation.kind == 'backend_parity'
            and case.spec.experiment_stage.value == 'calibration' and case.spec.model == plan.model,
            'FIXED_BACKEND_PARITY_REQUIRED')
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


def read_provider_logs(provider_config, pod_id, *, seconds=3):
    """Official v2 authenticated SSE; retain only bounded fingerprint records.

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
                if line.startswith(b'data:') and b'SHA256:' in line and b'(ED25519)' in line:
                    event = decode(line[5:].strip())
                    require(isinstance(event, dict) and isinstance(event.get('line'), str), 'PROVIDER_LOG_INVALID')
                    if 'SHA256:' in event['line'] and '(ED25519)' in event['line']:
                        events.append({k: event.get(k) for k in ('source', 'line', 'ts')})
    except RunnerError:
        raise
    except Exception:
        raise RunnerError('PROVIDER_LOGS_UNAVAILABLE') from None
    return events


def command_bytes(command, *, timeout=5):
    # Fixed commands produce public keys or a configuration hash receipt.
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True, cwd='/',
                                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C'}, start_new_session=True)
        body, deadline = bytearray(), time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                require(time.monotonic() < deadline, 'SSH_VERIFICATION_COMMAND_FAILED')
                for key, _ in selector.select(min(0.1, max(0, deadline-time.monotonic()))):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    body.extend(chunk)
                    require(len(body) <= 65536, 'SSH_VERIFICATION_COMMAND_FAILED')
        require(process.wait(timeout=max(0.1, deadline-time.monotonic())) == 0, 'SSH_VERIFICATION_COMMAND_FAILED')
        return body.decode('ascii')
    except RunnerError:
        raise
    except Exception:
        raise RunnerError('SSH_VERIFICATION_COMMAND_FAILED') from None
    finally:
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            process.stdout.close()


def verified_endpoint(config, request, backend, state, *, logs=read_provider_logs, command=command_bytes):
    intent = backend._intent(request['worker_id'])
    require(intent is not None and intent['provider_id'] == request['observed_provider_id']
            and intent['request_key'] == request['request_id'] and intent['configuration_hash'] == config.deployment.digest
            and intent['deadline'] == request['deadline'], 'PROVIDER_INTENT_MISMATCH')
    try:
        pod = backend.transport.request('GET', '/v2/pods/' + quote(intent['provider_id'], safe=''))
    except ProviderHTTPError as error:
        if error.status in {408, 429} or 500 <= error.status < 600:
            raise RunnerError('PROVIDER_ENDPOINT_UNAVAILABLE') from None
        raise RunnerError('PROVIDER_ENDPOINT_REFUSED') from None
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
    content = f'[{host}]:{port} {key}\n'.encode()
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
        return receipt or {'configured': None, 'reason': 'CONFIGURATION_RESPONSE_UNCERTAIN'}
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
    try:
        command(['/usr/bin/scp', '-q', '-p', *options, '-P', str(settings['ssh_port']),
                 str(local), 'root@' + destination + ':' + remote],
                timeout=min(15, max(0.1, deadline-clock())))
        require(clock() < deadline, 'WORKER_STARTUP_DEADLINE')
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
            raise
        return {'configured': None, 'reason': 'CONFIGURATION_RESPONSE_UNCERTAIN'}
    except Exception:
        return {'configured': None, 'reason': 'CONFIGURATION_RESPONSE_UNCERTAIN'}
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


def run(config, plan, ledger, backend, cloud, state, *, clock=time.time, sleep=time.sleep,
        endpoint=verified_endpoint, tunnel_factory=SSHTunnel, client_factory=WorkerClient,
        readiness=wait_for_worker, configure=configure_worker, progress=lambda _: None):
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
    try:
        result['stage'] = 'approval_verification'
        deadline = validate_authority(ledger, config, plan, request, now=clock())
        state.publish('bound-request.json', {key: request[key] for key in ('request_id', 'worker_id', 'approval_id', 'observed_provider_id', 'deadline', 'batch_hash')})
        result['stage'] = 'verified_worker_startup'
        progress(result['stage'])
        startup_deadline = min(deadline, clock() + 180)
        while clock() < startup_deadline:
            validate_authority(ledger, config, plan, request, now=clock())
            try:
                settings = endpoint(config, request, backend, state)
                break
            except RunnerError as error:
                if str(error) not in {'DIRECT_SSH_ENDPOINT_UNAVAILABLE', 'HOST_FINGERPRINT_UNAVAILABLE',
                                      'PROVIDER_LOGS_UNAVAILABLE', 'SSH_VERIFICATION_COMMAND_FAILED',
                                      'PROVIDER_ENDPOINT_UNAVAILABLE'}:
                    raise
                sleep(1)
        else:
            raise RunnerError('WORKER_STARTUP_DEADLINE')
        result['stage'] = 'worker_configuration'
        progress(result['stage'])
        result['configuration'] = configure(config, plan, settings, state, deadline=startup_deadline, clock=clock)
        secret = read_file(config.bearer_secret_file, owner=os.geteuid(), private=True, bound=513).decode().strip()
        require(32 <= len(secret) <= 512, 'WORKER_SECRET_INVALID')
        with tunnel_factory(**settings) as tunnel:
            client = client_factory(f'http://127.0.0.1:{tunnel.local_port}', secret, timeout_seconds=5)
            readiness(client, deadline=startup_deadline, clock=clock, sleep=sleep)
            result['stage'] = 'approved_dispatch'
            progress(result['stage'])
            dispatcher = Dispatcher(ledger, client, worker_id=request['worker_id'],
                                    transfer_directory=state.directory / 'transfers',
                                    input_artifact_root=LEDGER.parent / 'input-artifacts', lease_seconds=30)
            service = DispatcherService(dispatcher, tunnel=tunnel)
            while clock() < deadline:
                current = find_request(ledger, config, plan)
                validate_authority(ledger, config, plan, current, now=clock())
                service.tick()
                job = ledger.get_job(request['job_ids'][0])
                if job.state in (JobState.COMPLETED, JobState.FAILED):
                    break
                sleep(0.25)
            else:
                raise RunnerError('APPROVED_DEADLINE_REACHED')
            result['stage'] = 'artifact_collection'
            progress(result['stage'])
            observation = collect(ledger, plan, client)
            state.publish('observations.json', observation)
            require(observation['case_results_passed'] is True, 'CALIBRATION_DID_NOT_PASS')
            manifest = ledger.get_manifest(request['job_ids'][0])
            require(manifest.software.container_image_digest == config.deployment.image_digest
                    and manifest.software.probe_mcp_git_commit == config.source_commit
                    and manifest.hardware.region == config.deployment.region
                    and Decimal(str(manifest.hardware.live_price_usd_per_hour)) == Decimal(str(config.expected_worker_price_usd_per_hour)),
                    'MANIFEST_PROVENANCE_MISMATCH')
            result.update(status='passed', stage='completed', job_id=job.job_id, attempt_id=job.attempt_id,
                          manifest_sha256=digest(manifest.model_dump(mode='json')))
    except RunnerError as error:
        result['reason'] = str(error)
    except Exception as error:
        result['reason'] = 'ACCEPTANCE_RUNTIME_UNAVAILABLE'
        result['diagnostic'] = exception_diagnostic(error)
    finally:
        progress('provider_deletion')
        result['teardown'] = stop_and_observe(cloud, backend, request, sleep=sleep)
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
