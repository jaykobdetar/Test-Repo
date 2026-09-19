"""Trusted, one-shot lifecycle actions for already-running acceptance attempts.

No approval is issued or consumed, no job is submitted, and no provider is called.
The normal dispatcher retains responsibility for lease renewal and reconciliation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

from .audit import canonical_json
from .dispatcher import Dispatcher, SSHTunnel, TransportError, WorkerClient
from .gpu_acceptance import AcceptancePlan
from .ledger import Ledger, LedgerError
from .worker_contracts import ExecutionReceipt, ExecutionRequest, WorkerState


class ActionError(ValueError):
    pass


def digest(value):
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _directory(path: Path, *, create=False):
    """Walk through directory descriptors, never following a path symlink."""
    if not path.is_absolute() or any(part in {".", ".."} for part in str(path).split("/")):
        raise ActionError("ACTION_PATH_INVALID")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            if create and index == len(path.parts) - 2:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_owned(fd, name, *, bound=1024 * 1024):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > bound):
            raise ActionError("ACTION_FILE_UNSAFE")
        data = stream.read(bound + 1)
        if len(data) > bound:
            raise ActionError("ACTION_FILE_TOO_LARGE")
        return data


def private_bytes(path: Path):
    fd = _directory(path.parent)
    try:
        return _read_owned(fd, path.name)
    finally:
        os.close(fd)


class ActionStore:
    def __init__(self, directory, *, create=True):
        self.fd = _directory(Path(directory), create=create)
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            self.close()
            raise ActionError("ACTION_DIRECTORY_UNSAFE")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def _name(action_id, kind):
        if not re.fullmatch(r"[0-9a-f]{64}", action_id) or kind not in {"intent", "ack", "result"}:
            raise ActionError("ACTION_RECORD_ID_INVALID")
        return action_id + "." + kind + ".json"

    def read(self, action_id, kind):
        try:
            value = json.loads(_read_owned(self.fd, self._name(action_id, kind)))
            if not isinstance(value, dict):
                raise ActionError("ACTION_RECORD_INVALID")
            return value
        except FileNotFoundError:
            return None

    def publish(self, action_id, kind, value):
        """Exclusive durable publication. An interrupted write prevents replay."""
        raw = canonical_json(value).encode() + b"\n"
        if len(raw) > 1024 * 1024:
            raise ActionError("ACTION_RECORD_TOO_LARGE")
        name = self._name(action_id, kind)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(self.fd)
        return True


def _bounded_command(command, payload, *, timeout=15):
    raw = canonical_json(payload).encode() + b"\n"
    if len(raw) > 262144:
        raise ActionError("LIFECYCLE_REQUEST_TOO_LARGE")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, close_fds=True,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            remaining = memoryview(raw)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TransportError("LIFECYCLE_RESPONSE_UNCERTAIN")
                for key, _ in selector.select(left):
                    if key.fileobj is process.stdin:
                        try:
                            remaining = remaining[os.write(process.stdin.fileno(), remaining):]
                        except BrokenPipeError:
                            raise TransportError("LIFECYCLE_RESPONSE_UNCERTAIN") from None
                        if not remaining:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            selector.unregister(process.stdout)
                        output.extend(chunk)
                        if len(output) > 262144:
                            raise TransportError("LIFECYCLE_RESPONSE_TOO_LARGE")
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode != 0:
            raise TransportError("LIFECYCLE_HELPER_REFUSED_OR_FAILED")
        try:
            value = json.loads(output)
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError()
            return value
        except ValueError:
            raise TransportError("LIFECYCLE_RESPONSE_INVALID") from None
    except (OSError, subprocess.TimeoutExpired):
        raise TransportError("LIFECYCLE_RESPONSE_UNCERTAIN") from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


class SSHActionClient:
    """Only one fixed helper command; no caller-supplied remote shell text."""
    def __init__(self, settings):
        required = {"host", "user", "identity_file", "known_hosts_file", "ssh_port",
                    "config_path", "config_sha256", "token_path"}
        if set(settings) != required or settings["user"] != "root":
            raise ActionError("LIFECYCLE_SETTINGS_INVALID")
        self.config_sha256 = settings["config_sha256"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.config_sha256):
            raise ActionError("LIFECYCLE_CONFIG_HASH_INVALID")
        for key in ("config_path", "token_path"):
            value = settings[key]
            if not isinstance(value, str) or not value.startswith("/") or any(
                    part in {".", "..", ""} for part in value.split("/")[1:]):
                raise ActionError("LIFECYCLE_REMOTE_PATH_INVALID")
        tunnel = SSHTunnel(settings["host"], user=settings["user"],
                           identity_file=Path(settings["identity_file"]),
                           known_hosts_file=Path(settings["known_hosts_file"]),
                           ssh_port=settings["ssh_port"])
        self.endpoint_identity = digest({**settings,
            "known_hosts_sha256": "sha256:" + hashlib.sha256(tunnel.known_hosts_file.read_bytes()).hexdigest()})
        self.config_path, self.token_path = settings["config_path"], settings["token_path"]
        self.command = ["ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
            "-o", "PermitLocalCommand=no", "-o", "UserKnownHostsFile=" + str(tunnel.known_hosts_file),
            "-i", str(tunnel.identity_file), "-p", str(tunnel.ssh_port), tunnel.user + "@" + tunnel.host,
            "/opt/probe-core/venv/bin/python", "-I", "-m", "probe_core.gpu_lifecycle"]

    def _call(self, operation, request, **extra):
        return _bounded_command(self.command, {"operation": operation,
            "config_path": self.config_path, "config_sha256": self.config_sha256,
            "token_path": self.token_path, "request": request.model_dump(mode="json"), **extra})

    def inspect(self, request):
        result = self._call("inspect", request)
        if result.get("operation") != "inspect":
            raise TransportError("LIFECYCLE_OPERATION_MISMATCH")
        return result["snapshot"]

    def restart(self, request, action_id, *, expected_before):
        return self._call("restart", request, action_id=action_id, expected_before=expected_before)


def _receipt(value, request):
    receipt = value if isinstance(value, ExecutionReceipt) else ExecutionReceipt.model_validate(value)
    WorkerClient._identity(receipt, request.job_id, request.attempt_id)
    return receipt


def _snapshot(value, request):
    if not isinstance(value, dict) or ExecutionRequest.model_validate(value["request"]) != request:
        raise ActionError("LIFECYCLE_REQUEST_MISMATCH")
    for key in ("request_sha256", "process_sha256", "execution_started_sha256", "loaded_config_sha256"):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value[key]):
            raise ActionError("LIFECYCLE_HASH_MISSING")
    child = value["child"]
    for identity, minimum_pid in ((child, 2), (value["supervisor"], 2), (value["bootstrap"], 1)):
        if (not isinstance(identity, dict) or type(identity.get("pid")) is not int or identity["pid"] < minimum_pid or
                not isinstance(identity.get("identity"), str) or not identity["identity"].isdigit() or
                not isinstance(identity.get("boot_id"), str) or not identity["boot_id"]):
            raise ActionError("LIFECYCLE_PROCESS_IDENTITY_INVALID")
    for key in ("deadline", "monotonic_deadline"):
        if type(child.get(key)) not in (float, int) or not math.isfinite(child[key]) or child[key] <= 0:
            raise ActionError("LIFECYCLE_DEADLINE_INVALID")
    if child["deadline"] > request.deadline.timestamp() or type(value.get("child_alive")) is not bool:
        raise ActionError("LIFECYCLE_AUTHORITY_MISMATCH")
    observed = datetime.fromisoformat(value["observed_at"])
    if (observed.tzinfo is None or type(value.get("observed_monotonic")) not in (float, int) or
            not math.isfinite(value["observed_monotonic"]) or value["observed_monotonic"] < 0):
        raise ActionError("LIFECYCLE_OBSERVATION_CLOCK_INVALID")
    _receipt(value["receipt"], request)
    return value


def _running(snapshot, request, now):
    receipt = _receipt(snapshot["receipt"], request)
    return (snapshot["child_alive"] and receipt.state == WorkerState.RUNNING and not receipt.process_stopped and
            now.timestamp() < snapshot["child"]["deadline"] <= request.deadline.timestamp() and
            datetime.fromisoformat(snapshot["observed_at"]).timestamp() < snapshot["child"]["deadline"] and
            snapshot["observed_monotonic"] < snapshot["child"]["monotonic_deadline"])


def _stable(before, after):
    return all(before[key] == after[key] for key in (
        "request", "request_sha256", "process_sha256", "execution_started_sha256", "loaded_config_sha256", "child", "bootstrap"))


def _process_key(identity):
    return tuple(identity[key] for key in ("pid", "identity", "boot_id"))


def _cancel_proof(snapshot, before, request):
    proof = snapshot.get("cancellation")
    proof_hash = snapshot.get("cancellation_sha256")
    if (not isinstance(proof, dict) or not isinstance(proof_hash, str) or
            not re.fullmatch(r"sha256:[0-9a-f]{64}", proof_hash)):
        return False
    child = before["child"]
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1 or
            any(proof.get(key) != getattr(request, key) for key in ("job_id", "attempt_id", "worker_id", "approval_id")) or
            proof.get("request_sha256") != digest(request.model_dump(mode="json")) or
            proof.get("config_sha256") != before["loaded_config_sha256"] or
            any(proof.get(key) != child[key] for key in ("pid", "identity", "boot_id", "deadline", "monotonic_deadline", "cgroup")) or
            proof.get("signal") != "SIGKILL" or
            proof.get("signal_scope") != ("process_group" if child["cgroup"] is None else "process_and_cgroup") or
            proof.get("process_stopped") is not True or proof.get("job_scope_stopped") is not True or
            proof.get("result_present") is not False or snapshot["child_alive"]):
        return False
    try:
        sent, stopped = (datetime.fromisoformat(proof[key]) for key in ("signal_sent_at", "stopped_at"))
        return (sent.tzinfo is not None and stopped.tzinfo is not None and
                datetime.fromisoformat(before["observed_at"]) <= sent <= stopped <= datetime.fromisoformat(snapshot["observed_at"]) and
                sent.timestamp() < child["deadline"])
    except (ValueError, KeyError, TypeError):
        return False


def _authority(ledger, request, now):
    """One WAL snapshot binds the current lease and consumed approval interval."""
    with ledger.read_connection() as connection:
        row = connection.execute("""SELECT j.state, j.attempt_id AS current_attempt,
            j.worker_id AS current_worker, j.approval_id AS current_approval,
            j.lease_expires_at AS job_lease, a.job_id, a.worker_id, a.approval_id,
            a.lease_expires_at AS attempt_lease, a.execution_deadline, a.stopped_at,
            p.consumed_at, p.deadline AS approval_deadline, p.ended_at,
            EXISTS(SELECT 1 FROM approval_jobs aj WHERE aj.approval_id=p.approval_id
                   AND aj.job_id=j.job_id) AS batch_member
            FROM jobs j JOIN attempts a ON a.attempt_id=?
            JOIN approvals p ON p.approval_id=a.approval_id WHERE j.job_id=?""",
            (request.attempt_id, request.job_id)).fetchone()
    if row is None:
        return False
    timestamp = now.timestamp()
    return (row["state"] == "RUNNING" and row["current_attempt"] == request.attempt_id and
        row["current_worker"] == row["worker_id"] == request.worker_id and
        row["current_approval"] == row["approval_id"] == request.approval_id and
        row["job_id"] == request.job_id and row["stopped_at"] is None and row["batch_member"] == 1 and
        row["consumed_at"] is not None and row["consumed_at"] <= timestamp and row["ended_at"] is None and
        row["execution_deadline"] == request.deadline.timestamp() and
        timestamp < row["job_lease"] and timestamp < row["attempt_lease"] and
        timestamp < row["execution_deadline"] <= row["approval_deadline"])


def run_action(ledger, plan, *, case_name, job_id, attempt_id, approval_id, client,
               lifecycle, action_directory, observe_seconds=10, clock=None):
    """Target an explicit existing attempt; retries can only observe its intent."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    if not 0 < observe_seconds <= 30:
        raise ActionError("ACTION_OBSERVATION_BOUND_INVALID")
    matches = [case for case in plan.cases if case.name == case_name]
    if len(matches) != 1 or matches[0].action == "wait":
        raise ActionError("ACTION_CASE_INVALID")
    case = matches[0]
    if case.spec.experiment_stage.value != "calibration" or case.spec.model != plan.model:
        raise ActionError("ACTION_REQUIRES_CALIBRATION_PLAN")
    job = ledger.get_job(job_id)
    if job.spec != case.spec or (job.attempt_id, job.approval_id) != (attempt_id, approval_id):
        raise ActionError("ACTION_ATTEMPT_MISMATCH")
    request = Dispatcher(ledger, client, worker_id=job.worker_id,
                         transfer_directory=Path(action_directory))._request(job)
    binding = {"plan_sha256": digest(plan.model_dump(mode="json")), "case": case_name,
               "action": case.action, "request": request.model_dump(mode="json"),
               "worker_config_sha256": lifecycle.config_sha256, "endpoint_identity": lifecycle.endpoint_identity}
    action_id = digest(binding).removeprefix("sha256:")
    store = ActionStore(action_directory)
    try:
        existing = store.read(action_id, "result")
        if existing is not None:
            if existing.get("binding") != binding or existing.get("action_id") != action_id:
                raise ActionError("ACTION_RECORD_MISMATCH")
            return existing
        intent = store.read(action_id, "intent")
        if intent is None:
            if not _authority(ledger, request, clock()):
                raise ActionError("ACTION_REQUIRES_RUNNING_ATTEMPT")
            before = _snapshot(lifecycle.inspect(request), request)
            remote = _receipt(client.status(attempt_id), request)
            # The first inspection and HTTP response must refer to the same live
            # process. A changed attempt can never be substituted by a later lookup.
            checked = _snapshot(lifecycle.inspect(request), request)
            if (not _running(before, request, clock()) or not _running(checked, request, clock()) or
                    not _stable(before, checked) or before["supervisor"] != checked["supervisor"] or
                    remote.state != WorkerState.RUNNING or remote.process_stopped):
                raise ActionError("ACTION_RUNNING_EVIDENCE_INCONCLUSIVE")
            intent = {"schema_version": 1, "action_id": action_id, "binding": binding,
                      "created_at": clock().isoformat(), "before": checked,
                      "running_receipt": remote.model_dump(mode="json")}
            first = store.publish(action_id, "intent", intent)
            if first:
                # Everything below is a single mutation attempt. A transport
                # failure cannot erase this intent or authorize another attempt.
                if not _authority(ledger, request, clock()):
                    return _report(binding, action_id, "inconclusive", "AUTHORITY_CHANGED_BEFORE_ACTION", intent)
                try:
                    if case.action == "cancel_after_running":
                        ack = {"operation": "cancel", "receipt": _receipt(client.cancel(attempt_id), request).model_dump(mode="json")}
                    else:
                        ack = lifecycle.restart(request, action_id, expected_before=checked)
                        if (ack.get("action_id") != action_id or ack.get("operation") != "restart" or
                                ack.get("signal_sent") is not True or ack.get("replayed") is not False or
                                not _stable(checked, _snapshot(ack["before"], request)) or
                                ack["before"]["supervisor"] != checked["supervisor"]):
                            return _report(binding, action_id, "uncertain", "RESTART_SIGNAL_NOT_ESTABLISHED", intent)
                    store.publish(action_id, "ack", {"binding": binding, "action_id": action_id, "ack": ack})
                except (TransportError, OSError, ValueError, KeyError, TypeError):
                    return _report(binding, action_id, "uncertain", "ACTION_RESPONSE_UNCERTAIN", intent)
            else:
                intent = store.read(action_id, "intent")
        if intent.get("binding") != binding or intent.get("action_id") != action_id:
            raise ActionError("ACTION_INTENT_MISMATCH")
        acknowledged = store.read(action_id, "ack")
        if acknowledged is None:
            return _report(binding, action_id, "uncertain", "ACTION_INTENT_WITHOUT_ACKNOWLEDGMENT", intent)
        if acknowledged.get("binding") != binding or acknowledged.get("action_id") != action_id:
            raise ActionError("ACTION_ACK_MISMATCH")
        before = _snapshot(intent["before"], request)
        ack = acknowledged["ack"]
        deadline = time.monotonic() + observe_seconds
        while True:
            try:
                if time.monotonic() >= deadline:
                    return _report(binding, action_id, "uncertain", "ACTION_OBSERVATION_UNAVAILABLE", intent, ack)
                after = _snapshot(lifecycle.inspect(request), request)
                if time.monotonic() >= deadline:
                    return _report(binding, action_id, "uncertain", "ACTION_OBSERVATION_UNAVAILABLE", intent, ack)
                receipt = _receipt(client.status(attempt_id), request)
                if time.monotonic() >= deadline:
                    return _report(binding, action_id, "uncertain", "ACTION_OBSERVATION_UNAVAILABLE", intent, ack)
                checked = _snapshot(lifecycle.inspect(request), request)
                if time.monotonic() >= deadline:
                    return _report(binding, action_id, "uncertain", "ACTION_OBSERVATION_UNAVAILABLE", intent, ack)
                if not _stable(before, after) or not _stable(before, checked):
                    result = _report(binding, action_id, "inconclusive", "EXECUTION_AUTHORITY_CHANGED", intent, ack, checked, receipt)
                    break
                if case.action == "cancel_after_running":
                    cancelled = _receipt(ack["receipt"], request)
                    receipts = [cancelled, receipt, _receipt(after["receipt"], request), _receipt(checked["receipt"], request)]
                    if any(item.state != WorkerState.CANCELLED or item.failure_kind != "cancelled" or not item.process_stopped for item in receipts):
                        result = _report(binding, action_id, "inconclusive", "CANCELLATION_NOT_ESTABLISHED", intent, ack, checked, receipt)
                        break
                    if (not _cancel_proof(after, before, request) or not _cancel_proof(checked, before, request) or
                            after["cancellation"] != checked["cancellation"] or after["cancellation_sha256"] != checked["cancellation_sha256"]):
                        result = _report(binding, action_id, "inconclusive", "CANCELLATION_SIGNAL_EVIDENCE_MISSING", intent, ack, checked, receipt)
                        break
                    if not after["child_alive"] and not checked["child_alive"]:
                        result = _report(binding, action_id, "passed", "EXACT_RUNNING_ATTEMPT_CANCELLED", intent, ack, checked, receipt)
                        break
                else:
                    if not _running(after, request, clock()) or not _running(checked, request, clock()) or receipt.state != WorkerState.RUNNING or receipt.process_stopped:
                        result = _report(binding, action_id, "inconclusive", "RESTART_DID_NOT_PRESERVE_LIVE_ATTEMPT", intent, ack, checked, receipt)
                        break
                    if (after["supervisor"] == checked["supervisor"] and
                            _process_key(after["supervisor"]) != _process_key(before["supervisor"]) and
                            _authority(ledger, request, clock())):
                        result = _report(binding, action_id, "passed", "REPLACEMENT_ADOPTED_EXACT_ATTEMPT", intent, ack, checked, receipt)
                        break
            except (TransportError, OSError, ValueError, KeyError, TypeError):
                pass  # Observation may recover; the action is never replayed.
            if time.monotonic() >= deadline:
                return _report(binding, action_id, "uncertain", "ACTION_OBSERVATION_UNAVAILABLE", intent, ack)
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        store.publish(action_id, "result", result)
        return result
    finally:
        store.close()


def _report(binding, action_id, status, reason, intent, ack=None, after=None, receipt=None):
    return {"schema_version": 1, "kind": "gpu_acceptance_action", "action_id": action_id,
            "binding": binding, "status": status, "reason": reason, "before": intent["before"],
            "action_acknowledgment": ack, "after": after,
            "observed_receipt": None if receipt is None else receipt.model_dump(mode="json"),
            "compute_started": False, "approval_consumed": False, "scientific_evidence": False}


def _recorded_proof(report, intent, acknowledgment, request, action):
    """Revalidate the retained transcript without performing any action."""
    if not all(isinstance(value, dict) for value in (report, intent, acknowledgment)):
        return False
    if (report.get("schema_version") != 1 or report.get("kind") != "gpu_acceptance_action" or
            report.get("status") != "passed" or report.get("compute_started") is not False or
            report.get("approval_consumed") is not False or report.get("scientific_evidence") is not False or
            intent.get("binding") != report["binding"] or acknowledgment.get("binding") != report["binding"] or
            intent.get("before") != report.get("before") or acknowledgment.get("ack") != report.get("action_acknowledgment")):
        return False
    before, after = (_snapshot(report[key], request) for key in ("before", "after"))
    if not _stable(before, after) or not _running(before, request, datetime.fromisoformat(before["observed_at"])):
        return False
    initial = _receipt(intent["running_receipt"], request)
    if initial.state != WorkerState.RUNNING or initial.process_stopped:
        return False
    receipt = _receipt(report["observed_receipt"], request)
    ack = report["action_acknowledgment"]
    if not isinstance(ack, dict):
        return False
    if action == "cancel_after_running":
        outcomes = [receipt, _receipt(after["receipt"], request), _receipt(ack["receipt"], request)]
        return (ack.get("operation") == "cancel" and _cancel_proof(after, before, request) and
                all(item.state == WorkerState.CANCELLED and item.failure_kind == "cancelled" and item.process_stopped for item in outcomes))
    return (ack.get("operation") == "restart" and ack.get("action_id") == report["action_id"] and
            ack.get("signal_sent") is True and ack.get("replayed") is False and
            _stable(before, _snapshot(ack["before"], request)) and ack["before"]["supervisor"] == before["supervisor"] and
            _process_key(before["supervisor"]) != _process_key(after["supervisor"]) and
            _running(after, request, datetime.fromisoformat(after["observed_at"])) and
            receipt.state == WorkerState.RUNNING and not receipt.process_stopped)


def collect_action_evidence(ledger, plan, action_directory):
    required = {case.name: case for case in plan.cases if case.action != "wait"}
    found = {}
    result = {"complete": False, "cases": []}
    try:
        store = ActionStore(action_directory, create=False)
    except (OSError, ValueError):
        store = None
    try:
        names = [] if store is None else os.listdir(store.fd)
        if len(names) > 1000:
            raise ActionError("ACTION_DIRECTORY_TOO_LARGE")
        for name in names:
            if not re.fullmatch(r"[0-9a-f]{64}\.result\.json", name):
                continue
            try:
                action_id = name[:64]
                report = store.read(action_id, "result")
                binding = report["binding"]
                case = required.get(binding["case"])
                if (case is None or binding["plan_sha256"] != digest(plan.model_dump(mode="json")) or
                        binding["action"] != case.action or report["action_id"] != action_id or
                        digest(binding) != "sha256:" + action_id):
                    continue
                request = ExecutionRequest.model_validate(binding["request"])
                job = ledger.get_job(request.job_id)
                actual = Dispatcher(ledger, None, worker_id=job.worker_id,
                                    transfer_directory=Path(action_directory))._request(job)
                if request != actual or request.spec != case.spec or request.spec.experiment_stage.value != "calibration":
                    continue
                intent, ack = (store.read(action_id, kind) for kind in ("intent", "ack"))
                if (intent["action_id"] == ack["action_id"] == action_id and
                        _recorded_proof(report, intent, ack, request, case.action)):
                    if case.name in found:
                        found[case.name] = None  # Ambiguous duplicate complete transcripts cannot pass.
                    else:
                        found[case.name] = action_id
            except (OSError, ValueError, KeyError, TypeError, LedgerError):
                continue
    except (OSError, ValueError):
        found = {}
    finally:
        if store is not None:
            store.close()
    result["cases"] = [{"case": case.name, "action": case.action,
        "passed": found.get(case.name) is not None, "action_id": found.get(case.name)} for case in required.values()]
    result["complete"] = bool(required) and all(row["passed"] for row in result["cases"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "ledger", "settings", "action-directory"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("case", "job-id", "attempt-id", "approval-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--observe-seconds", type=float, default=10)
    args = parser.parse_args()
    try:
        settings = json.loads(private_bytes(args.settings))
        if set(settings) != {"worker_url", "bearer_secret_file", "ssh"}:
            raise ActionError("ACTION_SETTINGS_INVALID")
        client = WorkerClient(settings["worker_url"], private_bytes(Path(settings["bearer_secret_file"])).decode().strip(), timeout_seconds=5)
        lifecycle = SSHActionClient(settings["ssh"])
        plan = AcceptancePlan.model_validate_json(args.plan.read_bytes())
        with Ledger(args.ledger) as ledger:
            result = run_action(ledger, plan, case_name=args.case, job_id=args.job_id,
                attempt_id=args.attempt_id, approval_id=args.approval_id, client=client,
                lifecycle=lifecycle, action_directory=args.action_directory, observe_seconds=args.observe_seconds)
        print(canonical_json(result))
        raise SystemExit(0 if result["status"] == "passed" else 2)
    except (LedgerError, OSError, ValueError, KeyError, TypeError):
        print(canonical_json({"status": "refused", "error_code": "ACTION_NOT_ESTABLISHED", "compute_started": False}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
