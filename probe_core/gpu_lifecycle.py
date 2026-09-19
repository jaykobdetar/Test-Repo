"""Narrow, root-only worker-local inspection and one-shot supervisor crash action.

No queue, provider, or approval operation exists here. A persisted intent means
that a signal may have happened; retrying that intent can only observe it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import stat
import sys
import time
from typing import Callable

from .audit import canonical_json
from .sandbox_lifecycle import _pidfd_open, _kill_pidfd
from .worker_contracts import ExecutionReceipt, ExecutionRequest, WorkerConfig, WorkerState

MAX_FILE = 1024 * 1024
MAX_OUTPUT = 4 * MAX_FILE
WORKER_UID = 10001


class LifecycleError(RuntimeError):
    """Only fixed, non-sensitive refusal codes cross the CLI boundary."""


def _require(condition, code):
    if not condition:
        raise LifecycleError(code)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(LifecycleError("NONFINITE_JSON")))
    except (ValueError, UnicodeError, RecursionError):
        raise LifecycleError("INVALID_JSON") from None


@dataclass(frozen=True)
class _Policy:
    # Fixture injection is deliberately absent from the CLI protocol.
    worker_uid: int = WORKER_UID
    worker_gid: int = WORKER_UID
    root_uid: int = 0
    pid_file: Path = Path("/run/probe-worker-supervisor.pid")
    intent_root: Path = Path("/run/probe-lifecycle")
    path_root: Path = Path("/")
    interpreter: str = sys.executable
    process_inspector: Callable | None = None


@contextmanager
def _directory(path: Path, policy: _Policy, *, private=False, root_only=False):
    _require(path.is_absolute() and ".." not in path.parts, "INVALID_PATH")
    try:
        parts = path.relative_to(policy.path_root).parts
    except ValueError:
        raise LifecycleError("PATH_OUTSIDE_TRUSTED_ROOT") from None
    descriptor = os.open(policy.path_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in (None, *parts):
            if component is not None:
                following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            info = os.fstat(descriptor)
            _require(info.st_uid in {policy.root_uid, policy.worker_uid}
                     and not info.st_mode & 0o022, "UNSAFE_DIRECTORY")
        info = os.fstat(descriptor)
        if private:
            _require(not info.st_mode & 0o077, "DIRECTORY_NOT_PRIVATE")
        if root_only:
            _require(info.st_uid == policy.root_uid, "DIRECTORY_NOT_ROOT_OWNED")
        yield descriptor
    finally:
        os.close(descriptor)


def _read_at(directory, name, *, uid, private=False, limit=MAX_FILE):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_uid == uid and not before.st_mode & 0o022
                 and (not private or not before.st_mode & 0o077)
                 and before.st_size <= limit, "UNSAFE_FILE")
        chunks, length = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            _require(length <= limit, "FILE_TOO_LARGE")
        after = os.fstat(descriptor)
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                 "FILE_CHANGED_DURING_READ")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read(path, policy, *, uid, private=False, parent_private=False, limit=MAX_FILE):
    with _directory(path.parent, policy, private=parent_private) as directory:
        return _read_at(directory, path.name, uid=uid, private=private, limit=limit)


def _proc_read(path: Path, limit=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        chunks, size = [], 0
        while True:
            data = os.read(descriptor, min(4096, limit + 1 - size))
            if not data:
                return b"".join(chunks)
            chunks.append(data)
            size += len(data)
            _require(size <= limit, "PROCESS_METADATA_TOO_LARGE")
    finally:
        os.close(descriptor)


def _boot_id():
    value = _proc_read(Path("/proc/sys/kernel/random/boot_id"), 128).decode().strip()
    _require(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value), "INVALID_BOOT_ID")
    return value


def _inspect_process(pid: int):
    # The bootstrap is normally Docker ENTRYPOINT PID1. Callers separately
    # require numerical children and supervisors to have PID greater than one.
    _require(type(pid) is int and pid >= 1, "INVALID_PROCESS_ID")
    directory = Path("/proc") / str(pid)
    try:
        first = _proc_read(directory / "stat").decode().rsplit(")", 1)[1].split()
        if first[0] == "Z":
            return None
        status = dict(line.split(":", 1) for line in _proc_read(directory / "status").decode().splitlines())
        args = _proc_read(directory / "cmdline").rstrip(b"\0").decode().split("\0")
        executable = os.readlink(directory / "exe")
        last = _proc_read(directory / "stat").decode().rsplit(")", 1)[1].split()
        _require(first[19] == last[19] and first[1] == last[1] and last[0] != "Z", "PROCESS_CHANGED")
        return {"pid": pid, "identity": first[19], "boot_id": _boot_id(), "ppid": int(first[1]),
                "uid": int(status["Uid"].split()[1]), "gid": int(status["Gid"].split()[1]),
                "uids": [int(value) for value in status["Uid"].split()],
                "gids": [int(value) for value in status["Gid"].split()],
                "groups": [int(value) for value in status["Groups"].split()],
                "executable": executable, "args": args}
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (ValueError, IndexError, KeyError, UnicodeError):
        raise LifecycleError("INVALID_PROCESS_METADATA") from None


def _identity_equal(left, right):
    return left is not None and all(left[key] == right[key] for key in ("pid", "identity", "boot_id"))


def _worker_identity(process, policy):
    _require(process is not None and process["uids"] == [policy.worker_uid] * 4
             and process["gids"] == [policy.worker_gid] * 4
             and process["groups"] == [], "WRONG_WORKER_IDENTITY")


def _load_config(config_path, expected, config_sha256, policy):
    _require(isinstance(config_sha256, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", config_sha256),
             "CONFIG_HASH_REQUIRED")
    config_bytes = _read(config_path, policy, uid=policy.worker_uid, private=True)
    _require(_sha(config_bytes) == config_sha256, "CONFIG_HASH_MISMATCH")
    config = WorkerConfig.model_validate(_json(config_bytes))
    _require(config.model == expected.spec.model, "MODEL_MISMATCH")
    return config


def _snapshot(config_path, token_path, expected, config_sha256, policy):
    config = _load_config(config_path, expected, config_sha256, policy)
    loaded_config_sha256 = _sha(canonical_json(config.model_dump(mode="json")).encode())
    # Only validate token storage; never include its contents or digest in output.
    token = _read(token_path, policy, uid=policy.worker_uid, private=True, limit=4096)
    _require(bool(token.strip()), "EMPTY_WORKER_TOKEN")
    directory_path = Path(config.output_directory) / expected.attempt_id
    with _directory(directory_path, policy, private=True) as directory:
        raw = {name: _read_at(directory, name, uid=policy.worker_uid)
               for name in ("request.json", "process.json", "execution-started.json", "receipt.json")}
        try:
            cancellation_bytes = _read_at(directory, "cancellation.json", uid=policy.worker_uid, limit=8192)
        except FileNotFoundError:
            cancellation_bytes = None
        try:
            os.stat("result.json", dir_fd=directory, follow_symlinks=False)
            result_present = True
        except FileNotFoundError:
            result_present = False
    request = ExecutionRequest.model_validate(_json(raw["request.json"]))
    _require(request == expected, "REQUEST_MISMATCH")
    child = _json(raw["process.json"])
    _require(type(child) is dict and set(child) == {"pid", "identity", "boot_id", "deadline", "monotonic_deadline", "cgroup"},
             "INVALID_CHILD_METADATA")
    _require(type(child["pid"]) is int and child["pid"] > 1
             and isinstance(child["identity"], str) and child["identity"].isdigit()
             and child["boot_id"] == _boot_id(), "INVALID_CHILD_IDENTITY")
    for key in ("deadline", "monotonic_deadline"):
        _require(type(child[key]) in (float, int) and math.isfinite(child[key]) and child[key] > 0,
                 "INVALID_EXECUTION_DEADLINE")
    expected_cgroup = None if config.cgroup_directory is None else str(Path(config.cgroup_directory) / ("probe-" + request.attempt_id))
    _require(child["cgroup"] == expected_cgroup, "CGROUP_MISMATCH")
    receipt = ExecutionReceipt.model_validate(_json(raw["receipt.json"]))
    _require(receipt.job_id == expected.job_id and receipt.attempt_id == expected.attempt_id, "RECEIPT_MISMATCH")
    expected_deadline = min(expected.deadline.timestamp(), receipt.started_at.timestamp() + expected.spec.limits.max_runtime_seconds)
    _require(abs(child["deadline"] - expected_deadline) < 0.000001, "DEADLINE_MISMATCH")
    marker = _json(raw["execution-started.json"])
    marker_fields = {"schema_version", "job_id", "attempt_id", "worker_id", "approval_id", "pid", "identity", "boot_id", "request_sha256", "config_sha256", "started_at"}
    _require(type(marker) is dict and set(marker) == marker_fields and type(marker["schema_version"]) is int
             and marker["schema_version"] == 1, "INVALID_STARTED_MARKER")
    _require(all(marker[key] == getattr(expected, key) for key in ("job_id", "attempt_id", "worker_id", "approval_id"))
             and all(marker[key] == child[key] for key in ("pid", "identity", "boot_id"))
             and marker["request_sha256"] == _sha(canonical_json(expected.model_dump(mode="json")).encode())
             and marker["config_sha256"] == loaded_config_sha256,
             "STARTED_MARKER_MISMATCH")
    try:
        started = datetime.fromisoformat(marker["started_at"])
        _require(started.tzinfo is not None and receipt.started_at <= started
                 and started.timestamp() <= child["deadline"], "INVALID_STARTED_TIME")
    except (TypeError, ValueError):
        raise LifecycleError("INVALID_STARTED_TIME") from None
    inspector = policy.process_inspector or _inspect_process
    live_child = inspector(child["pid"])
    child_alive = _identity_equal(live_child, child)
    if child_alive:
        _worker_identity(live_child, policy)
    pid_bytes = _read(policy.pid_file, policy, uid=policy.root_uid, limit=32)
    _require(re.fullmatch(rb"[1-9][0-9]*\n?", pid_bytes) and int(pid_bytes) > 1, "INVALID_SUPERVISOR_HINT")
    supervisor = inspector(int(pid_bytes))
    _worker_identity(supervisor, policy)
    interpreter = str(Path(policy.interpreter).resolve(strict=True))
    _require(supervisor["executable"] == interpreter
             and supervisor["args"] == [policy.interpreter, "-m", "probe_core.worker", "--config", str(config_path),
                                        "--token-file", str(token_path), "--port", "8080"], "WRONG_SUPERVISOR_COMMAND")
    bootstrap = inspector(supervisor["ppid"])
    _require(bootstrap is not None and bootstrap["uids"] == [policy.root_uid] * 4
             and bootstrap["executable"] == interpreter
             and bootstrap["args"] in (["python", "-m", "probe_core.gpu_launch"],
                                         [policy.interpreter, "-m", "probe_core.gpu_launch"])
             and bootstrap["boot_id"] == supervisor["boot_id"] == child["boot_id"], "WRONG_BOOTSTRAP_IDENTITY")
    _require(child["pid"] not in {supervisor["pid"], bootstrap["pid"]}, "OVERLAPPING_PROCESS_IDENTITIES")
    _require(inspector(supervisor["pid"]) == supervisor
             and inspector(bootstrap["pid"]) == bootstrap, "PROCESS_CHANGED")
    observed_at, observed_monotonic = datetime.now(timezone.utc), time.monotonic()
    cancellation = None
    if cancellation_bytes is not None:
        cancellation = _json(cancellation_bytes)
        fields = {"schema_version", "job_id", "attempt_id", "worker_id", "approval_id", "request_sha256", "config_sha256",
                  "pid", "identity", "boot_id", "deadline", "monotonic_deadline", "cgroup", "signal", "signal_scope",
                  "signal_sent_at", "stopped_at", "process_stopped", "job_scope_stopped", "result_present"}
        _require(type(cancellation) is dict and set(cancellation) == fields
                 and type(cancellation["schema_version"]) is int and cancellation["schema_version"] == 1,
                 "INVALID_CANCELLATION_PROOF")
        _require(all(cancellation[key] == getattr(expected, key) for key in ("job_id", "attempt_id", "worker_id", "approval_id"))
                 and all(cancellation[key] == child[key] for key in ("pid", "identity", "boot_id", "deadline", "monotonic_deadline", "cgroup"))
                 and cancellation["request_sha256"] == marker["request_sha256"]
                 and cancellation["config_sha256"] == loaded_config_sha256,
                 "CANCELLATION_PROOF_MISMATCH")
        _require(cancellation["signal"] == "SIGKILL"
                 and cancellation["signal_scope"] == ("process_group" if child["cgroup"] is None else "process_and_cgroup")
                 and cancellation["process_stopped"] is True and cancellation["job_scope_stopped"] is True
                 and cancellation["result_present"] is False and not result_present and not child_alive
                 and receipt.state == WorkerState.CANCELLED and receipt.failure_kind == "cancelled"
                 and receipt.process_stopped is True, "CANCELLATION_STOP_NOT_PROVEN")
        try:
            sent = datetime.fromisoformat(cancellation["signal_sent_at"])
            stopped = datetime.fromisoformat(cancellation["stopped_at"])
            _require(sent.tzinfo is not None and stopped.tzinfo is not None
                     and started <= sent <= stopped <= observed_at
                     and sent.timestamp() < child["deadline"] and stopped == receipt.finished_at,
                     "INVALID_CANCELLATION_TIME")
        except (TypeError, ValueError):
            raise LifecycleError("INVALID_CANCELLATION_TIME") from None
    return {"request": request.model_dump(mode="json"), "request_sha256": _sha(raw["request.json"]),
            "process_sha256": _sha(raw["process.json"]), "execution_started_sha256": _sha(raw["execution-started.json"]),
            "loaded_config_sha256": loaded_config_sha256,
            "child": child, "child_alive": child_alive, "receipt": receipt.model_dump(mode="json"),
            "supervisor": supervisor, "bootstrap": bootstrap,
            "observed_at": observed_at.isoformat(), "observed_monotonic": observed_monotonic,
            "cancellation": cancellation, "cancellation_sha256": None if cancellation_bytes is None else _sha(cancellation_bytes)}


def inspect(config_path: Path, token_path: Path, expected: ExecutionRequest, *, config_sha256: str,
            _policy: _Policy | None = None) -> dict:
    return {"schema_version": 1, "operation": "inspect",
            "snapshot": _snapshot(config_path, token_path, expected, config_sha256, _policy or _Policy())}


def _write_exclusive(directory, name, value):
    data = canonical_json(value).encode()
    _require(len(data) <= MAX_OUTPUT, "INTENT_TOO_LARGE")
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(directory)


def _running(snapshot):
    _require(snapshot["child_alive"] and snapshot["receipt"]["state"] == WorkerState.RUNNING
             and snapshot["receipt"]["process_stopped"] is False, "ATTEMPT_NOT_RUNNING")
    child = snapshot["child"]
    _require(time.time() < child["deadline"] and time.monotonic() < child["monotonic_deadline"], "EXECUTION_DEADLINE_EXPIRED")
    _require(child["monotonic_deadline"] - time.monotonic()
             <= snapshot["request"]["spec"]["limits"]["max_runtime_seconds"] + 1, "INVALID_EXECUTION_DEADLINE")


def _stable(snapshot):
    keys = ("request", "request_sha256", "process_sha256", "execution_started_sha256", "loaded_config_sha256", "child", "supervisor", "bootstrap")
    _require(type(snapshot) is dict and all(key in snapshot for key in keys), "INVALID_EXPECTED_SNAPSHOT")
    return {key: snapshot[key] for key in keys}


def restart(config_path: Path, token_path: Path, expected: ExecutionRequest, action_id: str, *,
            config_sha256: str, expected_before: dict, _policy: _Policy | None = None) -> dict:
    policy = _policy or _Policy()
    _require(isinstance(action_id, str) and re.fullmatch(r"[0-9a-f]{64}", action_id), "INVALID_ACTION_ID")
    # Validate the pinned config even when replaying an existing intent.
    _load_config(config_path, expected, config_sha256, policy)
    expected_stable = _stable(expected_before)
    with _directory(policy.intent_root.parent, policy, root_only=True) as parent:
        try:
            os.mkdir(policy.intent_root.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    with _directory(policy.intent_root, policy, private=True, root_only=True) as directory:
        lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            info = os.fstat(lock)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == policy.root_uid
                     and not info.st_mode & 0o077, "UNSAFE_INTENT_LOCK")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LifecycleError("ACTION_BUSY") from None
            # Fence the attempt, not just a caller-chosen action ID.
            key = hashlib.sha256(expected.attempt_id.encode()).hexdigest()
            intent_name, result_name = key + ".intent.json", key + ".result.json"
            binding = {"operation": "restart", "action_id": action_id, "config_sha256": config_sha256,
                       "request_sha256": _sha(canonical_json(expected.model_dump(mode="json")).encode())}
            try:
                previous = _json(_read_at(directory, intent_name, uid=policy.root_uid, private=True, limit=MAX_OUTPUT))
            except FileNotFoundError:
                previous = None
            if previous is not None:
                _require(all(previous.get(key) == value for key, value in binding.items()), "ACTION_INTENT_CONFLICT")
                _require(_stable(previous["before"]) == expected_stable, "ACTION_INTENT_CONFLICT")
                try:
                    result = _json(_read_at(directory, result_name, uid=policy.root_uid, private=True, limit=MAX_OUTPUT))
                    _require(result["action_id"] == action_id and type(result["signal_sent"]) is bool,
                             "INVALID_ACTION_RESULT")
                except FileNotFoundError:
                    result = {"signal_sent": None}
                return {"schema_version": 1, "operation": "restart", "action_id": action_id,
                        "replayed": True, "signal_sent": result["signal_sent"], "outcome": "replayed",
                        "before": previous["before"]}
            before = _snapshot(config_path, token_path, expected, config_sha256, policy)
            _running(before)
            _require(_stable(before) == expected_stable, "TARGET_NOT_EXPECTED")
            descriptor = _pidfd_open(before["supervisor"]["pid"])
            try:
                fresh = _snapshot(config_path, token_path, expected, config_sha256, policy)
                _running(fresh)
                _require(_stable(before) == _stable(fresh)
                         and not select.select([descriptor], [], [], 0)[0], "TARGET_CHANGED_BEFORE_SIGNAL")
                _write_exclusive(directory, intent_name, {"schema_version": 1, **binding, "before": before})
                # Persisting this intent is the last reversible boundary. Any
                # subsequent exception leaves uncertainty and can never resend.
                final = _snapshot(config_path, token_path, expected, config_sha256, policy)
                _running(final)
                _require(_stable(fresh) == _stable(final)
                         and not select.select([descriptor], [], [], 0)[0], "TARGET_CHANGED_BEFORE_SIGNAL")
                _kill_pidfd(descriptor)
                result = {"schema_version": 1, "operation": "restart", "action_id": action_id,
                          "replayed": False, "signal_sent": True, "outcome": "signal_sent", "before": before}
                _write_exclusive(directory, result_name, result)
                return result
            finally:
                os.close(descriptor)
        finally:
            os.close(lock)


def main():
    def timeout(*_):
        raise LifecycleError("HELPER_TIMEOUT")
    try:
        _require(os.geteuid() == 0, "ROOT_REQUIRED")
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(20)
        data = sys.stdin.buffer.read(MAX_FILE + 1)
        _require(len(data) <= MAX_FILE, "INPUT_TOO_LARGE")
        command = _json(data)
        _require(type(command) is dict, "INVALID_COMMAND")
        operation = command.get("operation")
        fields = {"operation", "config_path", "config_sha256", "token_path", "request"}
        _require(operation in {"inspect", "restart"} and set(command) == fields | ({"action_id", "expected_before"} if operation == "restart" else set()),
                 "INVALID_COMMAND")
        request = ExecutionRequest.model_validate(command["request"])
        args = (Path(command["config_path"]), Path(command["token_path"]), request)
        if operation == "inspect":
            result = inspect(*args, config_sha256=command["config_sha256"])
        else:
            result = restart(*args, command["action_id"], config_sha256=command["config_sha256"], expected_before=command["expected_before"])
        output = canonical_json(result).encode()
        _require(len(output) <= MAX_OUTPUT, "OUTPUT_TOO_LARGE")
        sys.stdout.buffer.write(output + b"\n")
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, LifecycleError) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", str(error)) else "LIFECYCLE_INSPECTION_FAILED"
        print(canonical_json({"schema_version": 1, "error_code": code}), flush=True)
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
