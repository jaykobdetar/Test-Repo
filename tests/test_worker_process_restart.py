"""Real supervisor death/adoption; the scientific payload is a bounded CPU wait.

The production spawn, limits, process identities, persisted requests, monitoring,
and termination code run unchanged. This does not claim numerical or GPU parity.
"""

from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest

from probe_core.dispatcher import Dispatcher
from probe_core.audit import canonical_json
from probe_core.ledger import Ledger
from probe_core.worker import _process_identity, _same_process
from probe_core.worker_contracts import ExecutionReceipt, ExecutionRequest
from test_ledger import approve
from test_worker import tiny_bundle, make_request


# multiprocessing spawn re-imports this temporary script in the numerical child,
# so only WorkerEngine is substituted there. Supervisor and _child_entry remain
# production code, including network denial and process/deadline enforcement.
HARNESS = r"""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, SOURCE_ROOT)
import probe_core.worker as worker
from probe_core.worker_contracts import ExecutionRequest, WorkerConfig

class BoundedWaitingEngine:
    def __init__(self, config):
        pass

    def execute(self, request, directory):
        directory.mkdir()
        temporary = directory / ".synthetic-child-ready.tmp"
        temporary.write_text(json.dumps({
            "pid": os.getpid(), "attempt_id": request.attempt_id,
            "approval_id": request.approval_id,
        }))
        os.replace(temporary, directory / "synthetic-child-ready.json")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.05)
        raise TimeoutError("bounded synthetic CPU payload expired")

worker.WorkerEngine = BoundedWaitingEngine

def main():
    config = WorkerConfig.model_validate_json(Path(sys.argv[1]).read_text())
    supervisor = worker.Supervisor(config)
    submissions = 0
    try:
        print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
        for line in sys.stdin:
            command = json.loads(line)
            if command["action"] == "submit":
                assert sys.argv[2] == "initial", "replacement must never submit"
                submissions += 1
                receipt = supervisor.submit(ExecutionRequest.model_validate(command["request"]))
                response = receipt.model_dump(mode="json")
            elif command["action"] == "snapshot":
                attempt_id = command["attempt_id"]
                response = {
                    "receipt": supervisor.status(attempt_id).model_dump(mode="json"),
                    "request": supervisor._requests[attempt_id].model_dump(mode="json"),
                    "owned_child_processes": list(supervisor._processes),
                    "submissions": submissions,
                }
            elif command["action"] == "close":
                break
            else:
                raise AssertionError("unexpected test harness command")
            print(json.dumps(response), flush=True)
    finally:
        supervisor.close()

if __name__ == "__main__":
    main()
"""


class SupervisorProcess:
    def __init__(self, harness, config_path, mode, directory):
        self.log = (directory / (mode + "-supervisor.log")).open("wb")
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-u", str(harness), str(config_path), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            start_new_session=True,
        )
        self.buffer = bytearray()

    def read(self, timeout=5):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise AssertionError("supervisor response exceeded the test deadline")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise AssertionError("supervisor exited without its expected response")
            self.buffer.extend(chunk)
            assert len(self.buffer) <= 1024 * 1024, "unbounded supervisor test response"
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        return json.loads(line)

    def command(self, payload, timeout=5):
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        self.process.stdin.flush()
        return self.read(timeout)

    def submit(self, request):
        return ExecutionReceipt.model_validate(
            self.command(
                {
                    "action": "submit",
                    "request": request.model_dump(mode="json"),
                }
            )
        )

    def snapshot(self, attempt_id):
        return self.command({"action": "snapshot", "attempt_id": attempt_id})

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(b'{"action":"close"}\n')
                    self.process.stdin.flush()
                    self.process.wait(timeout=7)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    self.process.kill()
                    self.process.wait(timeout=3)
        finally:
            self.process.stdin.close()
            self.process.stdout.close()
            self.log.close()


def authority_snapshot(ledger):
    with ledger.read_connection() as connection:
        return {
            table: [tuple(row) for row in connection.execute("SELECT * FROM " + table)]
            for table in ("jobs", "attempts", "approvals", "approval_jobs")
        }


def process_descriptor_functions():
    open_function = getattr(os, "pidfd_open", None)
    signal_function = getattr(signal, "pidfd_send_signal", None)
    if open_function is not None and signal_function is not None:
        return open_function, signal_function
    # Some Python builds omit these wrappers. The libc symbols are available
    # only since glibc 2.36, so feature-detect instead of requiring that version
    # on every otherwise supported Linux host.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return open_function, signal_function
    if open_function is None:
        open_function = getattr(libc, "pidfd_open", None)
        if open_function is not None:
            open_function.argtypes = [ctypes.c_int, ctypes.c_uint]
            open_function.restype = ctypes.c_int
    if signal_function is None:
        signal_function = getattr(libc, "pidfd_send_signal", None)
        if signal_function is not None:
            signal_function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
            signal_function.restype = ctypes.c_int
    return open_function, signal_function


OPEN_PIDFD, SEND_PIDFD_SIGNAL = process_descriptor_functions()
pytestmark = pytest.mark.skipif(
    OPEN_PIDFD is None or SEND_PIDFD_SIGNAL is None,
    reason="supervisor crash test requires pidfd_open and pidfd_send_signal in Python or host libc",
)


def open_process_descriptor(pid):
    fd = OPEN_PIDFD(pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return fd


def kill_process_descriptor(fd):
    result = SEND_PIDFD_SIGNAL(fd, signal.SIGKILL, None, 0)
    if result is not None and result < 0:
        raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


def test_sigkill_supervisor_adopts_same_child_and_original_deadline(tiny_bundle, make_request, tmp_path):
    config = tiny_bundle[0].model_copy(update={"output_directory": str(tmp_path / "attempts")})
    config_path = tmp_path / "worker.json"
    config_path.write_text(config.model_dump_json())
    harness = tmp_path / "supervisor_fixture.py"
    source_root = str(Path(__file__).resolve().parents[1])
    harness.write_text(HARNESS.replace("SOURCE_ROOT", repr(source_root)))
    initial = replacement = None
    child_fd = None
    metadata = None
    try:
        initial = SupervisorProcess(harness, config_path, "initial", tmp_path)
        assert initial.read()["pid"] == initial.process.pid
        with Ledger(tmp_path / "ledger.sqlite") as ledger:
            spec = make_request(key="process-restart").spec
            spec = spec.model_copy(update={"limits": spec.limits.model_copy(update={"max_runtime_seconds": 12})})
            pending = ledger.submit_job(spec)
            grant = approve(ledger, lambda: datetime.now(timezone.utc), [pending], runtime=30)
            dispatcher = Dispatcher(ledger, initial, worker_id="worker-1", transfer_directory=tmp_path / "transfers")
            job = dispatcher.dispatch_next(approval_id=grant.approval_id)
            assert job is not None and job.state.value == "RUNNING"
            directory = Path(config.output_directory) / job.attempt_id
            request_bytes = (directory / "request.json").read_bytes()
            process_bytes = (directory / "process.json").read_bytes()
            request = ExecutionRequest.model_validate_json(request_bytes)
            metadata = json.loads(process_bytes)
            child_fd = open_process_descriptor(metadata["pid"])
            assert _same_process(metadata)
            assert metadata["deadline"] == request.deadline.timestamp()
            assert metadata["monotonic_deadline"] > time.monotonic()

            ready = directory / "artifacts" / "synthetic-child-ready.json"
            ready_deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < ready_deadline:
                time.sleep(0.02)
            assert json.loads(ready.read_text()) == {
                "pid": metadata["pid"],
                "attempt_id": job.attempt_id,
                "approval_id": grant.approval_id,
            }
            execution_start_bytes = (directory / "execution-started.json").read_bytes()
            execution_start = json.loads(execution_start_bytes)
            assert execution_start == {
                "schema_version": 1,
                "job_id": job.job_id,
                "attempt_id": job.attempt_id,
                "worker_id": job.worker_id,
                "approval_id": grant.approval_id,
                "pid": metadata["pid"],
                "identity": metadata["identity"],
                "boot_id": metadata["boot_id"],
                "request_sha256": "sha256:"
                + hashlib.sha256(canonical_json(request.model_dump(mode="json")).encode()).hexdigest(),
                "config_sha256": "sha256:"
                + hashlib.sha256(canonical_json(config.model_dump(mode="json")).encode()).hexdigest(),
                "started_at": execution_start["started_at"],
            }
            assert datetime.fromisoformat(execution_start["started_at"]).utcoffset().total_seconds() == 0
            assert datetime.fromisoformat(execution_start["started_at"]) < request.deadline
            assert not (directory / "artifacts" / "execution-started.json").exists()
            assert _same_process(execution_start)
            before = initial.snapshot(job.attempt_id)
            assert before["submissions"] == 1 and before["receipt"]["state"] == "RUNNING"
            assert before["owned_child_processes"] == [job.attempt_id]
            authority = authority_snapshot(ledger)
            audit = ledger.audit_records()
            original_supervisor_identity = _process_identity(initial.process.pid)
            assert original_supervisor_identity is not None

            # SIGKILL exactly the Popen-owned supervisor, never its process group
            # or the separately fenced numerical child.
            initial.process.kill()
            assert initial.process.wait(timeout=3) == -signal.SIGKILL
            assert _same_process(metadata), "killing only the supervisor must leave the child alive"

            replacement = SupervisorProcess(harness, config_path, "replacement", tmp_path)
            assert replacement.read()["pid"] == replacement.process.pid
            assert replacement.process.pid != initial.process.pid
            assert _process_identity(replacement.process.pid) is not None
            adopted = replacement.snapshot(job.attempt_id)
            assert adopted["submissions"] == 0 and adopted["owned_child_processes"] == []
            assert adopted["request"] == request.model_dump(mode="json")
            assert adopted["receipt"]["state"] == "RUNNING" and not adopted["receipt"]["process_stopped"]
            assert _same_process(metadata)
            assert (directory / "request.json").read_bytes() == request_bytes
            assert (directory / "process.json").read_bytes() == process_bytes
            assert (directory / "execution-started.json").read_bytes() == execution_start_bytes
            assert list(Path(config.output_directory).iterdir()) == [directory]

            # The replacement has no multiprocessing.Process handle for this
            # orphan. Its persisted PID/start/boot fence must still let the
            # background monitor kill it at the original deadline. Do not call
            # status while waiting: that would itself refresh/terminate it and
            # could conceal a broken autonomous monitor.
            observation_deadline = metadata["monotonic_deadline"] + 3
            assert select.select([child_fd], [], [], max(0, observation_deadline - time.monotonic()))[0], (
                "background monitor did not terminate the original child by its deadline"
            )
            observed = replacement.snapshot(job.attempt_id)
            receipt = ExecutionReceipt.model_validate(observed["receipt"])
            assert receipt.process_stopped and receipt.state.value == "FAILED"
            assert receipt.failure_kind == "timeout" and receipt.error_code == "ExecutionDeadlineExceeded"
            assert receipt.job_id == job.job_id and receipt.attempt_id == job.attempt_id
            assert metadata["deadline"] <= receipt.finished_at.timestamp() <= metadata["deadline"] + 3
            assert not _same_process(metadata)
            assert select.select([child_fd], [], [], 0)[0], "original child must have exited"
            assert observed["submissions"] == 0 and observed["request"] == adopted["request"]
            assert (directory / "request.json").read_bytes() == request_bytes
            assert (directory / "process.json").read_bytes() == process_bytes
            assert (directory / "execution-started.json").read_bytes() == execution_start_bytes
            assert authority_snapshot(ledger) == authority and ledger.audit_records() == audit
    finally:
        # A failed assertion must not leave the orphan payload running. pidfd
        # targets the original child even if its numeric PID has been recycled.
        try:
            if child_fd is not None:
                try:
                    try:
                        kill_process_descriptor(child_fd)
                    except ProcessLookupError:
                        pass
                    assert select.select([child_fd], [], [], 3)[0], "test child cleanup did not finish"
                finally:
                    os.close(child_fd)
        finally:
            try:
                if replacement is not None:
                    replacement.close()
            finally:
                if initial is not None:
                    initial.close()
