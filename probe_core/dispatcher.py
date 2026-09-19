"""Controller-owned SSH transport and durable Ledger-to-worker dispatch.

The research agent never receives this client, its HTTP bearer secret, or SSH key.
A lost connection leaves the remote job running. Reconciliation uses the same
attempt identity; it never assumes that a network failure stopped computation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from pydantic import SecretStr

from .audit import canonical_json
from .ledger import ApprovalError, JobState, LeaseError, Ledger, LedgerError
from .schemas import TensorArtifact
from .worker_contracts import ExecutionReceipt, ExecutionRequest, ScienceMetadata, WorkerState


class TransportError(LedgerError):
    """Worker response is absent or untrusted; remote liveness remains unresolved."""


class TunnelExited(TransportError):
    """The owned SSH process is gone; the service manager must restart transport."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class WorkerClient:
    def __init__(self, base_url: str, bearer_secret: str, *, timeout_seconds: float = 15):
        url = urlsplit(base_url)
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path not in {"", "/"} or url.username or url.password or url.query or url.fragment:
            raise ValueError("worker client must use an explicit loopback HTTP endpoint")
        if not 32 <= len(bearer_secret) <= 512:
            raise ValueError("worker authentication requires a strong shared secret")
        self.base_url = base_url.rstrip("/")
        self._secret = SecretStr(bearer_secret)
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _open(self, path: str, *, payload=None):
        if not path.startswith("/v1/") or ".." in path or "?" in path or "#" in path:
            raise TransportError("invalid worker endpoint")
        data = None if payload is None else canonical_json(payload).encode()
        request = Request(self.base_url + path, data=data, headers={"Authorization": "Bearer " + self._secret.get_secret_value(), "Content-Type": "application/json"}, method="GET" if data is None else "POST")
        try:
            return self._opener.open(request, timeout=self.timeout_seconds)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            # Do not attach exception URLs/headers/bodies that may include secrets.
            raise TransportError("worker transport failed; reconcile the same attempt") from None

    def _receipt(self, path, *, payload=None):
        with self._open(path, payload=payload) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise TransportError("worker receipt exceeds the response bound")
        try:
            return ExecutionReceipt.model_validate_json(body)
        except ValueError:
            raise TransportError("worker returned an invalid receipt") from None

    def submit(self, request: ExecutionRequest) -> ExecutionReceipt:
        receipt = self._receipt("/v1/jobs", payload=request.model_dump(mode="json"))
        self._identity(receipt, request.job_id, request.attempt_id)
        return receipt

    @staticmethod
    def _identity(receipt, job_id, attempt_id):
        if receipt.job_id != job_id or receipt.attempt_id != attempt_id:
            raise TransportError("worker returned a different execution identity")

    def status(self, attempt_id: str) -> ExecutionReceipt:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", attempt_id):
            raise ValueError("invalid attempt identifier")
        receipt = self._receipt(f"/v1/jobs/{attempt_id}")
        if receipt.attempt_id != attempt_id:
            raise TransportError("worker returned a different attempt identity")
        return receipt

    def cancel(self, attempt_id: str) -> ExecutionReceipt:
        self.status(attempt_id)  # Validate and establish the exact remote identity.
        return self._receipt(f"/v1/jobs/{attempt_id}/cancel", payload={})

    def upload_tensor(self, source: Path, *, expected_sha256: str) -> dict:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_sha256):
            raise TransportError("invalid tensor content hash")
        if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
            raise TransportError("tensor source must be an unshared regular file")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if "sha256:" + digest != expected_sha256:
            raise TransportError("registered input tensor checksum mismatch")
        with source.open("rb") as stream:
            request = Request(self.base_url + "/v1/tensors/" + digest, data=stream, method="PUT", headers={"Authorization": "Bearer " + self._secret.get_secret_value(), "Content-Type": "application/octet-stream", "Content-Length": str(source.stat().st_size)})
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    body = response.read(65537)
            except (HTTPError, URLError, TimeoutError, OSError):
                raise TransportError("tensor upload failed; no execution was submitted") from None
        if len(body) > 65536:
            raise TransportError("tensor upload receipt exceeds response bound")
        try:
            receipt = json.loads(body)
            if receipt["path"] != digest + "/tensor.safetensors" or receipt["sha256"] != expected_sha256 or not 1 <= len(receipt["tensors"]) <= 128:
                raise ValueError("upload identity mismatch")
        except (ValueError, KeyError, TypeError):
            raise TransportError("invalid tensor upload receipt") from None
        return receipt

    def download(self, receipt: ExecutionReceipt, destination: Path, *, max_bytes: int) -> Path:
        if not receipt.process_stopped or receipt.state != WorkerState.SUCCEEDED or receipt.manifest is None:
            raise TransportError("artifact download requires a stopped successful execution")
        if receipt.manifest.cost.bytes_persisted > max_bytes:
            raise TransportError("manifest output size exceeds the dispatch budget")
        if destination.is_symlink():
            raise TransportError("transfer destination cannot be a symlink")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        total = 0
        for artifact in receipt.manifest.artifacts:
            target = destination / artifact.path
            parent = destination
            for part in Path(artifact.path).parts[:-1]:
                parent /= part
                if parent.is_symlink():
                    raise TransportError("artifact parent symlink rejected")
                parent.mkdir(exist_ok=True, mode=0o700)
            if target.is_symlink():
                raise TransportError("artifact symlink rejected")
            temporary = target.with_name(target.name + ".download")
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
            digest = hashlib.sha256()
            try:
                with self._open(f"/v1/jobs/{receipt.attempt_id}/artifacts/{artifact.path}") as response, temporary.open("xb") as stream:
                    while chunk := response.read(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise TransportError("worker artifact stream exceeds the byte budget")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if digest.hexdigest() != artifact.sha256:
                    raise TransportError("worker artifact checksum mismatch")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if total != receipt.manifest.cost.bytes_persisted:
            raise TransportError("downloaded bytes do not match the retained manifest")
        return destination


class SSHTunnel:
    """Strict host-key-verified SSH forwarding owned by the trusted controller."""
    def __init__(self, host: str, *, user: str, identity_file: Path, known_hosts_file: Path, ssh_port=22, remote_port=8080, local_port=0):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", user):
            raise ValueError("invalid SSH endpoint")
        for number in (ssh_port, remote_port):
            if type(number) is not int or not 1 <= number <= 65535:
                raise ValueError("invalid SSH port")
        if type(local_port) is not int or not 0 <= local_port <= 65535:
            raise ValueError("invalid local port")
        for path, private in ((identity_file, True), (known_hosts_file, False)):
            info = path.stat()
            if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & (0o077 if private else 0o022):
                raise ValueError("SSH trust files must be owned and have appropriate private permissions")
        self.host, self.user = host, user
        self.identity_file, self.known_hosts_file = identity_file.absolute(), known_hosts_file.absolute()
        self.ssh_port, self.remote_port, self.local_port = ssh_port, remote_port, local_port
        self.process = None

    def command(self):
        return ["ssh", "-F", "/dev/null", "-N", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-o", "PermitLocalCommand=no", "-o", "UserKnownHostsFile=" + str(self.known_hosts_file), "-i", str(self.identity_file), "-p", str(self.ssh_port), "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}", self.user + "@" + self.host]

    def start(self, timeout_seconds=10):
        if self.process is not None:
            raise RuntimeError("SSH tunnel is already started")
        if not self.local_port:
            with socket.socket() as temporary:
                temporary.bind(("127.0.0.1", 0))
                self.local_port = temporary.getsockname()[1]
        self.process = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"})
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.close()
                raise TransportError("SSH tunnel failed to establish")
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=0.1):
                    return f"http://127.0.0.1:{self.local_port}"
            except OSError:
                time.sleep(0.05)
        self.close()
        raise TransportError("SSH tunnel establishment timed out")

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None

    def ensure_alive(self):
        if self.process is None or self.process.poll() is not None:
            raise TunnelExited("owned SSH tunnel exited; restart dispatcher to reconcile the existing attempt")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()


class Dispatcher:
    """Move one already-approved Ledger attempt through the trusted worker.

    Methods do not authorize/consume wake approvals or start provider resources.
    ``dispatch_next`` returns promptly after submission. Call ``reconcile`` from
    the controller service at less than one third of the configured lease period.
    """
    def __init__(self, ledger: Ledger, client: WorkerClient, *, worker_id: str, transfer_directory: Path, lease_seconds=30, input_artifact_root: Path | None = None):
        self.ledger, self.client = ledger, client
        self.worker_id = worker_id
        self.transfer_directory = Path(transfer_directory)
        self.lease_seconds = lease_seconds
        self.input_artifact_root = Path(input_artifact_root).absolute() if input_artifact_root is not None else None
        self._lock = threading.RLock()

    def _request(self, job):
        with self.ledger.read_connection() as connection:
            row = connection.execute("SELECT execution_deadline FROM attempts WHERE attempt_id=?", (job.attempt_id,)).fetchone()
        if row is None:
            raise LedgerError("dispatch has no persisted execution attempt")
        science = ScienceMetadata()
        if job.spec.hypothesis_id:
            hypothesis = self.ledger.get_hypothesis(job.spec.hypothesis_id)
            plan = hypothesis.preregistration_plan
            if plan:
                science = ScienceMetadata(primary_metric=plan.primary_metric, predicted_direction=hypothesis.predicted_direction, falsifier=hypothesis.falsifier, controls=plan.controls, preregistration_hash=hypothesis.preregistration_hash)
        return ExecutionRequest(job_id=job.job_id, attempt_id=job.attempt_id, worker_id=job.worker_id, approval_id=job.approval_id, deadline=datetime.fromtimestamp(row[0], timezone.utc), spec=job.spec, science=science)

    def stage_tensor(self, source: Path, tensor_name: str) -> TensorArtifact:
        """Trusted-controller helper; research APIs expose artifact IDs instead."""
        with source.open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        receipt = self.client.upload_tensor(source, expected_sha256=digest)
        if tensor_name not in {item["tensor_name"] for item in receipt["tensors"]}:
            raise TransportError("named input tensor is absent")
        return TensorArtifact(path=receipt["path"], sha256=digest, tensor_name=tensor_name)

    def _stage_inputs(self, job):
        operation = job.spec.operation
        references = [getattr(operation, name, None) for name in ("source", "baseline", "direction", "activations", "labels")]
        staged = set()
        for reference in (item for item in references if item is not None):
            if self.input_artifact_root is None:
                raise TransportError("input tensors require a controller-owned artifact registry")
            canonical = reference.sha256.removeprefix("sha256:") + "/tensor.safetensors"
            if reference.path != canonical:
                raise TransportError("input tensor must use its registered content-addressed path")
            source = self.input_artifact_root
            if source.is_symlink():
                raise TransportError("input registry root cannot be a symlink")
            for part in Path(canonical).parts:
                source /= part
                if source.is_symlink():
                    raise TransportError("input registry symlink rejected")
            if reference.sha256 not in staged:
                self.client.upload_tensor(source, expected_sha256=reference.sha256)
                staged.add(reference.sha256)

    def dispatch_next(self, *, approval_id: str):
        with self._lock:
            job = self.ledger.dispatch_next(self.worker_id, approval_id=approval_id, lease_seconds=self.lease_seconds)
            if job is None:
                return None
            request = self._request(job)
            try:
                self._stage_inputs(job)
                self.ledger.heartbeat(job.job_id, job.attempt_id, self.worker_id, lease_seconds=self.lease_seconds)
            except (TransportError, OSError, LeaseError, ApprovalError):
                # No execution POST has occurred: it is safe to acknowledge that
                # this attempt never launched; do not consume an infrastructure retry.
                self.ledger.recover_expired()
                latest = self.ledger.get_job(job.job_id)
                if latest.state != JobState.FAILED:
                    self.ledger.fail_job(job.job_id, job.attempt_id, self.worker_id, failure_kind="policy", reason="InputStagingFailed")
                self.ledger.confirm_stopped(job.job_id, job.attempt_id)
                raise
            self.client.submit(request)
            self.ledger.start_job(job.job_id, job.attempt_id, self.worker_id)
            return self.ledger.get_job(job.job_id)

    def resume_submission(self, job_id: str):
        """Retry an ambiguous HTTP submission using the existing exact attempt."""
        with self._lock:
            job = self.ledger.get_job(job_id)
            if job.state != JobState.DISPATCHED or job.worker_id != self.worker_id:
                raise LedgerError("only this dispatcher's unresolved submission may be resumed")
            self.client.submit(self._request(job))
            self.ledger.start_job(job.job_id, job.attempt_id, self.worker_id)
            return self.ledger.get_job(job_id)

    def reconcile(self, job_id: str):
        with self._lock:
            job = self.ledger.get_job(job_id)
            if job.attempt_id is None or job.worker_id != self.worker_id:
                raise LedgerError("job has no attempt owned by this dispatcher")
            if job.state == JobState.COMPLETED:
                return job
            receipt = self.client.status(job.attempt_id)
            WorkerClient._identity(receipt, job.job_id, job.attempt_id)
            if receipt.state in {WorkerState.ACCEPTED, WorkerState.RUNNING}:
                try:
                    if job.state == JobState.DISPATCHED:
                        self.ledger.start_job(job_id, job.attempt_id, self.worker_id)
                    if job.state in {JobState.FAILED, JobState.COMPLETED}:
                        raise LeaseError("local attempt no longer has execution authority")
                    if (job.lease_expires_at - datetime.now(timezone.utc)).total_seconds() < self.lease_seconds * 2 / 3:
                        self.ledger.heartbeat(job_id, job.attempt_id, self.worker_id, lease_seconds=self.lease_seconds)
                except (LeaseError, ApprovalError):
                    self.ledger.recover_expired()
                    stopped = self.client.cancel(job.attempt_id)
                    if stopped.process_stopped:
                        latest = self.ledger.get_job(job_id)
                        if latest.state not in {JobState.FAILED, JobState.COMPLETED}:
                            self.ledger.cancel_job(job_id, "execution authority expired")
                        self.ledger.confirm_stopped(job_id, job.attempt_id)
                return self.ledger.get_job(job_id)
            if not receipt.process_stopped:
                raise TransportError("worker terminal receipt lacks positive process-stop evidence")
            self.ledger.recover_expired()
            job = self.ledger.get_job(job_id)
            if job.state == JobState.FAILED:
                self.ledger.confirm_stopped(job_id, job.attempt_id)
                return job
            if receipt.state == WorkerState.SUCCEEDED:
                if receipt.manifest is None:
                    raise TransportError("successful worker receipt has no manifest")
                if job.state == JobState.DISPATCHED:
                    self.ledger.start_job(job_id, job.attempt_id, self.worker_id)
                if job.state != JobState.FINALIZING:
                    self.ledger.begin_finalization(job_id, job.attempt_id, self.worker_id)
                self.ledger.confirm_stopped(job_id, job.attempt_id)
                directory = self.transfer_directory / job_id / job.attempt_id
                self.client.download(receipt, directory, max_bytes=job.spec.limits.max_output_bytes)
                return self.ledger.complete_job(job_id, job.attempt_id, self.worker_id, receipt.manifest, artifact_root=directory)
            failed = self.ledger.fail_job(job_id, job.attempt_id, self.worker_id, failure_kind=receipt.failure_kind or "infrastructure", reason=receipt.error_code or "WorkerReportedFailure")
            self.ledger.confirm_stopped(job_id, job.attempt_id)
            return failed

    def cancel(self, job_id: str):
        with self._lock:
            job = self.ledger.get_job(job_id)
            if job.attempt_id is None:
                return self.ledger.cancel_job(job_id, "operator cancelled pending job")
            if job.state == JobState.COMPLETED:
                raise LedgerError("completed job cannot be cancelled")
            receipt = self.client.cancel(job.attempt_id)
            WorkerClient._identity(receipt, job_id, job.attempt_id)
            if not receipt.process_stopped:
                raise TransportError("cancellation has no positive process-stop acknowledgement")
            failed = self.ledger.cancel_job(job_id, "operator cancelled execution") if job.state != JobState.FAILED else job
            self.ledger.confirm_stopped(job_id, job.attempt_id)
            return failed


class DispatcherService:
    """Restartable controller-side pump bound to one explicitly configured worker."""
    def __init__(self, dispatcher: Dispatcher, *, tunnel: SSHTunnel | None = None):
        self.dispatcher = dispatcher
        self.tunnel = tunnel
        self.last_error_code = None

    def tick(self):
        if self.tunnel is not None:
            self.tunnel.ensure_alive()
        ledger = self.dispatcher.ledger
        with ledger.read_connection() as reader:
            # No controller request means no dispatch, even if a bare approval
            # was manually inserted. This service follows observed RUNNING state.
            configured = reader.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='compute_requests'").fetchone()
            if configured is None:
                raise LedgerError("controller state must be initialized before the dispatcher service")
            active = reader.execute("""SELECT j.job_id FROM jobs j JOIN attempts a ON a.attempt_id=j.attempt_id
                WHERE j.worker_id=? AND (j.state IN ('DISPATCHED','RUNNING','FINALIZING')
                    OR (j.state='FAILED' AND a.stopped_at IS NULL)) ORDER BY j.created_at""", (self.dispatcher.worker_id,)).fetchall()
            grants = reader.execute("""SELECT approval_id FROM compute_requests
                WHERE worker_id=? AND state='RUNNING' ORDER BY created_at""", (self.dispatcher.worker_id,)).fetchall()
        results = []
        try:
            for row in active:
                # An ambiguous submission is queried, never automatically
                # resubmitted. Remote process identity must be reconciled first.
                results.append(self.dispatcher.reconcile(row[0]))
            if not active and len(grants) == 1:
                job = self.dispatcher.dispatch_next(approval_id=grants[0][0])
                if job is not None:
                    results.append(job)
            elif len(grants) > 1:
                raise LedgerError("multiple active grants violate worker exclusivity")
        except (TransportError, ApprovalError, LeaseError) as exc:
            # An HTTP outage can recover on the existing connection. A dead
            # owned SSH child cannot: let systemd restart the process/tunnel.
            # This changes no remote execution or durable approval state.
            if self.tunnel is not None:
                self.tunnel.ensure_alive()
            code = type(exc).__name__
            if code != self.last_error_code:
                ledger.record_event("policy_evaluation", {"decision": "dispatch_unavailable", "worker_id": self.dispatcher.worker_id, "reason_code": code})
            self.last_error_code = code
            return results
        if self.tunnel is not None:
            self.tunnel.ensure_alive()
        if self.last_error_code is not None:
            ledger.record_event("policy_evaluation", {"decision": "dispatch_recovered", "worker_id": self.dispatcher.worker_id})
        self.last_error_code = None
        return results

    def run(self, stop_event, *, poll_seconds=0.25):
        if not 0.01 <= poll_seconds < self.dispatcher.lease_seconds / 3:
            raise ValueError("poll interval must be below one third of the lease duration")
        while not stop_event.is_set():
            self.tick()
            stop_event.wait(poll_seconds)


def _private_json(path: Path):
    info = path.stat()
    if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("dispatcher configuration must be an owned mode0600 file")
    return json.loads(path.read_text())


def main():
    """Run with a private JSON file; secrets are read from a separate private file.

    Configuration fields: ledger_path, worker_id, transfer_directory,
    input_artifact_root, bearer_secret_file, lease_seconds (optional),
    poll_seconds (optional), and exactly one of base_url or ssh. SSH contains
    host,user,identity_file,known_hosts_file and optional ssh_port/remote_port.
    """
    import argparse
    import signal
    from contextlib import ExitStack
    parser = argparse.ArgumentParser(description="Run the trusted Probe dispatcher service")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = _private_json(Path(args.config))
    allowed = {"ledger_path", "worker_id", "transfer_directory", "input_artifact_root", "bearer_secret_file", "lease_seconds", "poll_seconds", "base_url", "ssh"}
    required = {"ledger_path", "worker_id", "transfer_directory", "input_artifact_root", "bearer_secret_file"}
    if set(config) - allowed or not required <= set(config) or ("base_url" in config) == ("ssh" in config):
        raise ValueError("dispatcher configuration has missing, unknown, or conflicting fields")
    secret_path = Path(config["bearer_secret_file"])
    info = secret_path.stat()
    if secret_path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("dispatcher authentication file must be owned and mode0600")
    secret = secret_path.read_text().strip()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with ExitStack() as stack:
        tunnel = None
        if "ssh" in config:
            ssh = dict(config["ssh"])
            ssh["identity_file"] = Path(ssh["identity_file"])
            ssh["known_hosts_file"] = Path(ssh["known_hosts_file"])
            tunnel = stack.enter_context(SSHTunnel(**ssh))
            url = f"http://127.0.0.1:{tunnel.local_port}"
        else:
            url = config["base_url"]
        ledger = stack.enter_context(Ledger(config["ledger_path"]))
        dispatcher = Dispatcher(ledger, WorkerClient(url, secret), worker_id=config["worker_id"], transfer_directory=Path(config["transfer_directory"]), input_artifact_root=Path(config["input_artifact_root"]), lease_seconds=config.get("lease_seconds", 30))
        service = DispatcherService(dispatcher, tunnel=tunnel)
        if args.once:
            service.tick()
        else:
            service.run(stop, poll_seconds=config.get("poll_seconds", 0.25))


if __name__ == "__main__":
    main()
