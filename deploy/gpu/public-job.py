"""One approved fixed public job per disposable Pod, transported over pinned SSH.

The detached root monitor owns the original deadline and all of its numerical
descendants. This is a trusted fixed-code process boundary, not hostile-code or
cgroup isolation. It never creates compute, grants approval, or replays a job.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import traceback
import uuid

from pydantic import TypeAdapter

from probe_core.audit import canonical_json
from probe_core.gpu_acceptance import fixed_plan
from probe_core.gpu_launch import _environment
from probe_core.rpc import decode
from probe_core.schemas import JobSpec, UTCTimestamp
from probe_core.worker import prompt_set_hash
from probe_core.worker_contracts import ExecutionReceipt, ExecutionRequest, ScienceMetadata, WorkerState

STATE = Path('/run/probe-public-job')
WORKSPACE = Path('/tmp/public-calibration')
CALIBRATION = Path('/opt/probe/public-calibration.py')
PYTHON = '/opt/probe-core/venv/bin/python'
UID = 10001
UTC = timezone.utc
MAX_JSON = 1024 * 1024
MAX_BYTES = 32 * 1024**2
DIRECTION = 'sha256:3a8c413d0c6d097b35489c6a17d110116eb49135b671fc3109927238fced4a44'
TERMINAL = {WorkerState.SUCCEEDED, WorkerState.FAILED, WorkerState.CANCELLED}


class Refused(ValueError):
    pass


def require(value, code):
    if not value:
        raise Refused(code)


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def calibration_module():
    name = '_probe_public_calibration'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, CALIBRATION)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def write_json(path, value, mode=0o600):
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        temporary.unlink(missing_ok=True)


def read_file(path, maximum=MAX_JSON):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum,
                'REGULAR_BOUNDED_FILE_REQUIRED')
        raw = stream.read(maximum + 1)
        require(len(raw) == info.st_size and len(raw) <= maximum, 'FILE_CHANGED')
        return raw


def read_json(path):
    return decode(read_file(path))


def ensure_directory(path, uid, mode):
    try:
        path.mkdir(mode=mode)
        os.chown(path, uid, uid)
        path.chmod(mode)
    except FileExistsError:
        pass
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
            and stat.S_IMODE(info.st_mode) == mode, 'DIRECTORY_BOUNDARY_CHANGED')


@contextmanager
def locked(state=STATE):
    ensure_directory(state, os.geteuid(), 0o755)
    descriptor = os.open(state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def process_identity(pid):
    try:
        data = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if data[0] == 'Z':
            return None
        return {'pid': pid, 'identity': data[19],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (FileNotFoundError, ProcessLookupError):
        return None


def same_process(identity):
    return identity is not None and process_identity(identity['pid']) == identity


def pidfd_signal(fd, sig):
    if hasattr(signal, 'pidfd_send_signal'):
        signal.pidfd_send_signal(fd, sig)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'pidfd_send_signal', None)
    require(function is not None, 'PIDFD_SIGNAL_REQUIRED')
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(fd, sig, None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def pidfd_open(pid):
    if hasattr(os, 'pidfd_open'):
        return os.pidfd_open(pid)
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'pidfd_open', None)
    require(function is not None, 'PIDFD_OPEN_REQUIRED')
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(pid, 0)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def prctl(option, value):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(ctypes.c_int(option), ctypes.c_ulong(value), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def validate_start(body, now=None):
    require(type(body) is dict and set(body) == {'config', 'request', 'absolute_deadline'}, 'START_FIELDS_INVALID')
    public = calibration_module()
    config = public.PublicCalibrationConfig.model_validate(body['config'])
    request = ExecutionRequest.model_validate(body['request'])
    absolute = TypeAdapter(UTCTimestamp).validate_python(body['absolute_deadline'])
    now = datetime.now(UTC) if now is None else now
    require(0 < (absolute - now).total_seconds() <= 900 and now < request.deadline <= absolute,
            'ORIGINAL_DEADLINE_INVALID')
    require(request.science == ScienceMetadata(), 'SCIENTIFIC_CLAIM_NOT_ALLOWED')
    dataset = public.validate_assets(config)
    plan = fixed_plan(config.model, 'fixed', public.DATASET_SHA,
                      prompt_set_hash(dataset, public.PROMPTS), public.PROMPTS)
    # Older compiler images have seven fixed cases. Tunnel reconnect is the
    # identical capture recipe; labels are identities, never scientific inputs.
    candidates = [case.spec.model_dump(mode='json') for case in plan.cases]
    direction = plan.cases[1].spec.model_dump(mode='json')
    direction['operation'] = {'kind': 'steer', 'target': {'layer': 14, 'component': 'residual'},
        'positions': ['last'], 'direction': {'path': DIRECTION[7:] + '/tensor.safetensors',
        'sha256': DIRECTION, 'tensor_name': 'public_basis_direction'}, 'strength': 0.5}
    candidates.append(JobSpec.model_validate(direction).model_dump(mode='json'))
    supplied = request.spec.model_dump(mode='json')
    supplied.pop('idempotency_key')
    require(any({k: v for k, v in candidate.items() if k != 'idempotency_key'} == supplied
                for candidate in candidates), 'FIXED_PUBLIC_JOB_REQUIRED')
    return config, request, absolute


def receipt_failure(request, started, kind, code, *, stopped=False, finished=None):
    finished = datetime.now(UTC) if finished is None else finished
    return ExecutionReceipt(job_id=request.job_id, attempt_id=request.attempt_id,
        state=WorkerState.CANCELLED if kind == 'cancelled' else WorkerState.FAILED,
        started_at=started, finished_at=finished, failure_kind=kind, error_code=code,
        process_stopped=stopped, wall_seconds=max(0, (finished - started).total_seconds()))


def launch_monitor(state):
    with (state / 'monitor.log').open('ab') as log:
        return subprocess.Popen([PYTHON, '-I', str(Path(__file__).resolve()), '_monitor'],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd='/',
            env=_environment('bootstrap', {'CUDA_VISIBLE_DEVICES': '0'}), start_new_session=True,
            close_fds=True)


def start(body, state=STATE, workspace=WORKSPACE):
    with locked(state):
        if (state / 'claim.json').exists():
            claim = read_json(state / 'claim.json')
            require(digest(body) == claim['start_sha256'], 'POD_ALREADY_CLAIMED')
            return status({'attempt_id': claim['request']['attempt_id']}, state)
        config, request, absolute = validate_start(body)
        ensure_directory(workspace, UID, 0o700)
        require(not (workspace / 'job-output').exists() and not (workspace / 'child-receipt.json').exists(),
                'OUTPUT_ALREADY_EXISTS')
        now = datetime.now(UTC)
        remaining = min((request.deadline - now).total_seconds(), (absolute - now).total_seconds(),
                        request.spec.limits.max_runtime_seconds)
        require(remaining > 0, 'ORIGINAL_DEADLINE_EXPIRED')
        claim = {'schema_version': 1, 'start_sha256': digest(body),
            'config_sha256': digest(config.model_dump(mode='json')),
            'request_sha256': digest(request.model_dump(mode='json')),
            'config': config.model_dump(mode='json'), 'request': request.model_dump(mode='json'),
            'original_deadline': absolute.isoformat(), 'started_at': now.isoformat(),
            'effective_job_deadline': (now + timedelta(seconds=remaining)).isoformat(),
            'monotonic_deadline': time.monotonic() + remaining,
            'boot_id': process_identity(os.getpid())['boot_id']}
        # Claim publication is irreversible, including failure during launch.
        write_json(state / 'claim.json', claim)
        write_json(state / 'config.json', claim['config'], 0o444)
        write_json(state / 'request.json', claim['request'], 0o444)
        initial = ExecutionReceipt(job_id=request.job_id, attempt_id=request.attempt_id,
                                   state=WorkerState.ACCEPTED, started_at=now)
        write_json(state / 'receipt.json', initial.model_dump(mode='json'))
        try:
            launch_monitor(state)
        except Exception:
            write_json(state / 'receipt.json', receipt_failure(request, now, 'infrastructure',
                       'MonitorLaunchFailed', stopped=True).model_dump(mode='json'))
            raise
        return initial


def read_claim(body, state):
    require(type(body) is dict and set(body) == {'attempt_id'}, 'ATTEMPT_FIELDS_INVALID')
    claim = read_json(state / 'claim.json')
    require(body['attempt_id'] == claim['request']['attempt_id'], 'ATTEMPT_MISMATCH')
    return claim


def status(body, state=STATE):
    claim = read_claim(body, state)
    receipt = ExecutionReceipt.model_validate(read_json(state / 'receipt.json'))
    require(receipt.attempt_id == claim['request']['attempt_id']
            and receipt.job_id == claim['request']['job_id'], 'RECEIPT_IDENTITY_MISMATCH')
    return receipt


def cancel(body, state=STATE):
    with locked(state):
        claim = read_claim(body, state)
        result = status(body, state)
        if result.state in TERMINAL:
            return result
        write_json(state / 'cancel.json', {'attempt_id': body['attempt_id'],
                   'request_sha256': claim['request_sha256'], 'requested_at': datetime.now(UTC).isoformat()})
    until = time.monotonic() + 10
    while time.monotonic() < until:
        result = status(body, state)
        if result.state in TERMINAL:
            return result
        time.sleep(.05)
    return result  # Missing positive stop evidence never becomes cancellation.


def inspect_job(body, state=STATE, workspace=WORKSPACE):
    claim = read_claim(body, state)
    def optional(name, directory=state):
        path = directory / name
        return read_json(path) if path.exists() else None
    return {'receipt': status(body, state).model_dump(mode='json'),
            'request_sha256': claim['request_sha256'], 'config_sha256': claim['config_sha256'],
            'child_identity': optional('child.json'), 'monitor_identity': optional('monitor.json'),
            'original_deadline': claim['original_deadline'],
            'effective_job_deadline': claim['effective_job_deadline'],
            'execution_started': optional('execution-started.json', workspace),
            'cancellation': optional('cancellation.json')}


class Descendants:
    """Monitor-only descendants, fenced by an opened descriptor and start time.

    The monitor is a subreaper, so orphaned/setsid grandchildren become its
    children. No namespace-wide or numeric-PID signal is ever sent.
    """
    def __init__(self):
        self.owner = os.getpid()
        self.fences = {}
        self.exit_codes = {}

    def refresh(self):
        table = {}
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                parts = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                table[int(path.name)] = int(parts[1])
            except (FileNotFoundError, ProcessLookupError):
                continue
        children = {self.owner}
        while True:
            expanded = children | {pid for pid, parent in table.items() if parent in children}
            if expanded == children:
                break
            children = expanded
        children.remove(self.owner)
        live = []
        for pid in children:
            identity = process_identity(pid)
            if identity is None:
                continue
            key = (pid, identity['identity'], identity['boot_id'])
            if key not in self.fences:
                try:
                    fd = pidfd_open(pid)
                except ProcessLookupError:
                    continue
                if process_identity(pid) != identity:
                    os.close(fd)
                    continue
                self.fences[key] = fd
            live.append((identity, self.fences[key]))
        return live

    def stop(self, timeout=5):
        signalled = []
        until = time.monotonic() + timeout
        while True:
            live = self.refresh()
            for identity, fd in live:
                try:
                    pidfd_signal(fd, signal.SIGKILL)
                    if identity not in signalled:
                        signalled.append(identity)
                except ProcessLookupError:
                    pass
            # Reap children only after opening descriptors for the whole tree;
            # adopted grandchildren are rediscovered on the next bounded round.
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                self.exit_codes[pid] = os.waitstatus_to_exitcode(status)
            if not self.refresh():
                return signalled
            require(time.monotonic() < until, 'DESCENDANT_STOP_UNCONFIRMED')
            time.sleep(.02)

    def close(self):
        for fd in self.fences.values():
            os.close(fd)
        self.fences.clear()


def output_size(workspace):
    root = workspace / 'job-output'
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob('*'):
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode), 'OUTPUT_SYMLINK_REFUSED')
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def child_result(workspace, request):
    path = workspace / 'child-receipt.json'
    if not path.exists():
        return None
    result = ExecutionReceipt.model_validate(read_json(path))
    require(result.job_id == request.job_id and result.attempt_id == request.attempt_id
            and result.state in {WorkerState.SUCCEEDED, WorkerState.FAILED}
            and not result.process_stopped, 'CHILD_RECEIPT_INVALID')
    return result


def drop_child(parent):
    os.setgroups([])
    os.setgid(UID)
    os.setuid(UID)
    prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
    prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG; monitor loss cannot restart a job.
    require(os.getppid() == parent, 'MONITOR_DISAPPEARED')


def deadline_expired(claim, request):
    now = datetime.now(UTC)
    return (time.monotonic() >= claim['monotonic_deadline'] or now >= request.deadline
            or now >= datetime.fromisoformat(claim['original_deadline']))


def run_monitor(state=STATE, workspace=WORKSPACE, *, command=None, drop=True):
    # command/drop are internal test seams, never CLI or request fields.
    descriptor = os.open(state / 'monitor.claim', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    claim = read_json(state / 'claim.json')
    request = ExecutionRequest.model_validate(claim['request'])
    started = datetime.fromisoformat(claim['started_at'])
    identity = process_identity(os.getpid())
    require(identity['boot_id'] == claim['boot_id'], 'MONITOR_BOOT_CHANGED')
    prctl(36, 1)  # PR_SET_CHILD_SUBREAPER; includes children that call setsid.
    probe = pidfd_open(os.getpid())
    try:
        pidfd_signal(probe, 0)
    finally:
        os.close(probe)
    write_json(state / 'monitor.json', identity)
    tree = Descendants()
    process = None
    release_read, release_write = os.pipe()
    try:
        if deadline_expired(claim, request):
            raise TimeoutError('original deadline expired before child launch')
        command = command(release_read) if callable(command) else command
        command = command or [PYTHON, '-I', str(Path(__file__).resolve()), '_child', str(release_read)]
        with (workspace / 'stdout.log').open('xb') as out, (workspace / 'stderr.log').open('xb') as err:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                cwd=workspace, env=_environment('worker', {'CUDA_VISIBLE_DEVICES': '0'}),
                pass_fds=(release_read,), start_new_session=True,
                preexec_fn=(lambda: drop_child(identity['pid'])) if drop else None)
        os.close(release_read)
        release_read = -1
        child = process_identity(process.pid)
        require(child is not None, 'CHILD_EXITED_BEFORE_RELEASE')
        write_json(state / 'child.json', child)
        tree.refresh()
        if deadline_expired(claim, request):
            raise TimeoutError('original deadline expired before child release')
        write_json(state / 'receipt.json', ExecutionReceipt(job_id=request.job_id, attempt_id=request.attempt_id,
            state=WorkerState.RUNNING, started_at=started).model_dump(mode='json'))
        os.write(release_write, b'1')
        os.close(release_write)
        release_write = -1
        cause = None
        cancellation = None
        while True:
            tree.refresh()
            result = child_result(workspace, request)
            alive = same_process(child)
            # A published completed result or already-dead leader wins over a
            # later cancellation request. Deadline checks never renew authority.
            if result is not None or not alive:
                break
            if deadline_expired(claim, request):
                cause = ('timeout', 'ExecutionDeadlineExceeded')
                break
            if output_size(workspace) > request.spec.limits.max_output_bytes:
                cause = ('policy', 'OutputLimitExceeded')
                break
            if sum((workspace / name).stat().st_size for name in ('stdout.log', 'stderr.log')) > MAX_BYTES:
                cause = ('policy', 'OutputLimitExceeded')
                break
            if (state / 'cancel.json').exists():
                cancellation = read_json(state / 'cancel.json')
                require(cancellation['request_sha256'] == claim['request_sha256'], 'CANCEL_BINDING_CHANGED')
                # Recheck publication before signalling; a completion racing
                # cancellation is preserved below, never reported cancelled.
                if child_result(workspace, request) is None and same_process(child):
                    cause = ('cancelled', 'CancelledByController')
                break
            time.sleep(.025)
        signalled = tree.stop()
        process.returncode = tree.exit_codes.get(process.pid)  # Preserve stop()'s actual waitpid result.
        result = child_result(workspace, request)
        if result is not None and result.finished_at > datetime.fromisoformat(claim['effective_job_deadline']):
            result = None
            cause = ('timeout', 'ExecutionDeadlineExceeded')
        if result is not None:
            final = result.model_copy(update={'process_stopped': True})
        elif cause == ('cancelled', 'CancelledByController') and child not in signalled:
            final = receipt_failure(request, started, 'infrastructure', 'CancellationUnconfirmed', stopped=True)
        elif cause is not None:
            final = receipt_failure(request, started, *cause, stopped=True)
        else:
            final = receipt_failure(request, started, 'infrastructure', 'ChildExitedWithoutReceipt', stopped=True)
        if final.state == WorkerState.CANCELLED:
            write_json(state / 'cancellation.json', {'schema_version': 1,
                'attempt_id': request.attempt_id, 'request_sha256': claim['request_sha256'],
                'config_sha256': claim['config_sha256'], 'child_identity': child,
                'original_deadline': claim['original_deadline'],
                'requested_at': cancellation['requested_at'], 'stopped_at': final.finished_at.isoformat(),
                'signal': 'SIGKILL', 'signal_scope': 'fenced_monitor_descendants',
                'signalled': signalled, 'process_stopped': True, 'descendants_stopped': True,
                'result_present': False})
        write_json(state / 'receipt.json', final.model_dump(mode='json'))
    except BaseException as error:
        stopped = False
        try:
            tree.stop()
            stopped = True
            if process is not None:
                process.returncode = tree.exit_codes.get(process.pid)
        except Exception:
            pass
        kind, code = ('timeout', 'ExecutionDeadlineExceeded') if isinstance(error, TimeoutError) else ('infrastructure', 'MonitorFailure')
        write_json(state / 'receipt.json', receipt_failure(request, started, kind,
                   code, stopped=stopped).model_dump(mode='json'))
        raise
    finally:
        for fd in (release_read, release_write):
            if fd >= 0:
                os.close(fd)
        tree.close()


def run_child(release, state=STATE, workspace=WORKSPACE):
    require(os.read(release, 1) == b'1', 'CHILD_NOT_RELEASED')
    os.close(release)
    public = calibration_module()
    public.process_boundary()
    config = public.PublicCalibrationConfig.model_validate(read_json(state / 'config.json'))
    request = ExecutionRequest.model_validate(read_json(state / 'request.json'))
    started = datetime.now(UTC)
    try:
        # The root checked assets before claiming; the child rechecks them under
        # its actual identity, then hashes all weights in the unchanged engine.
        public.validate_assets(config)
        identity = process_identity(os.getpid())
        require(identity is not None, 'CHILD_IDENTITY_MISSING')
        write_json(workspace / 'execution-started.json', {'schema_version': 1,
            'job_id': request.job_id, 'attempt_id': request.attempt_id, 'worker_id': request.worker_id,
            'approval_id': request.approval_id, 'request_sha256': digest(request.model_dump(mode='json')),
            'config_sha256': digest(config.model_dump(mode='json')), 'child_identity': identity,
            'deadline': request.deadline.isoformat(), 'started_at': started.isoformat()})
        result = public.PublicCalibrationEngine(config).execute(request, workspace / 'job-output')
    except Exception as error:
        torch = sys.modules.get('torch')
        if torch is not None and isinstance(error, torch.cuda.OutOfMemoryError):
            kind, code = 'oom', 'OutOfMemoryError'
        elif isinstance(error, TimeoutError):
            kind, code = 'timeout', 'ExecutionDeadlineExceeded'
        elif output_size(workspace) > request.spec.limits.max_output_bytes:
            kind, code = 'policy', 'OutputLimitExceeded'
        else:
            kind, code = 'policy', type(error).__name__
        traceback.print_exc(limit=12, file=sys.stderr)
        result = receipt_failure(request, started, kind, code)
    write_json(workspace / 'child-receipt.json', result.model_dump(mode='json'))


def tensor_path(digest_value, workspace):
    require(digest_value == DIRECTION, 'FIXED_PUBLIC_DIRECTION_REQUIRED')
    return workspace / 'tensors' / digest_value[7:] / 'tensor.safetensors'


def upload(metadata, stream, state=STATE, workspace=WORKSPACE):
    require(type(metadata) is dict and set(metadata) == {'sha256', 'length'}
            and type(metadata['length']) is int and metadata['length'] == 8288, 'TENSOR_METADATA_INVALID')
    target = tensor_path(metadata['sha256'], workspace)
    body = stream.read(metadata['length'] + 1)
    require(len(body) == metadata['length'] and 'sha256:' + hashlib.sha256(body).hexdigest() == DIRECTION,
            'TENSOR_CHECKSUM_MISMATCH')
    with locked(state):
        require(not (state / 'claim.json').exists(), 'TENSOR_UPLOAD_AFTER_CLAIM')
        ensure_directory(workspace, UID, 0o700)
        ensure_directory(workspace / 'tensors', os.geteuid(), 0o755)
        ensure_directory(target.parent, os.geteuid(), 0o755)
        if target.exists():
            require(read_file(target, MAX_BYTES) == body, 'TENSOR_CHANGED')
        else:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
            with os.fdopen(fd, 'wb') as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
    return {'path': DIRECTION[7:] + '/tensor.safetensors', 'sha256': DIRECTION,
            'tensors': [{'tensor_name': 'public_basis_direction', 'shape': [2048], 'dtype': 'F32'}]}


def read_tensor(body, workspace=WORKSPACE):
    require(type(body) is dict and set(body) == {'sha256'}, 'TENSOR_FIELDS_INVALID')
    raw = read_file(tensor_path(body['sha256'], workspace), MAX_BYTES)
    require('sha256:' + hashlib.sha256(raw).hexdigest() == body['sha256'], 'TENSOR_CHANGED')
    return raw


def artifact(body, state=STATE, workspace=WORKSPACE):
    require(type(body) is dict and set(body) == {'attempt_id', 'path'}, 'ARTIFACT_FIELDS_INVALID')
    receipt = status({'attempt_id': body['attempt_id']}, state)
    require(receipt.state == WorkerState.SUCCEEDED and receipt.process_stopped, 'STOPPED_SUCCESS_REQUIRED')
    record = next((item for item in receipt.manifest.artifacts if item.path == body['path']), None)
    require(record is not None and body['path'] in {'summary.json', 'tensors.safetensors'}, 'ARTIFACT_NOT_DECLARED')
    raw = read_file(workspace / 'job-output' / record.path, MAX_BYTES)
    require(hashlib.sha256(raw).hexdigest() == record.sha256, 'ARTIFACT_CHECKSUM_MISMATCH')
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'status', 'cancel', 'inspect', 'upload', 'read-tensor',
                                           'artifact', '_monitor', '_child'])
    parser.add_argument('release_fd', nargs='?', type=int)
    args = parser.parse_args(argv)
    if args.command == '_child':
        run_child(args.release_fd)
        return 0
    require(os.getresuid() == (0, 0, 0), 'ROOT_TRANSPORT_REQUIRED')
    if args.command == '_monitor':
        run_monitor()
        return 0
    raw = sys.stdin.buffer.readline(MAX_JSON + 1) if args.command == 'upload' else sys.stdin.buffer.read(MAX_JSON + 1)
    require(len(raw) <= MAX_JSON, 'REQUEST_TOO_LARGE')
    body = decode(raw)
    if args.command == 'upload':
        result = upload(body, sys.stdin.buffer)
    elif args.command in {'read-tensor', 'artifact'}:
        sys.stdout.buffer.write(read_tensor(body) if args.command == 'read-tensor' else artifact(body))
        return 0
    else:
        result = {'start': start, 'status': status, 'cancel': cancel, 'inspect': inspect_job}[args.command](body)
    if isinstance(result, ExecutionReceipt):
        result = result.model_dump(mode='json')
    print(canonical_json(result), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(canonical_json({'error': str(error) if isinstance(error, Refused) else type(error).__name__}),
              file=sys.stderr, flush=True)
        raise SystemExit(1)
