"""Bounded SSH transport for the supervised fixed-job helper.

The caller supplies an already verified endpoint and an already approved request.
This module neither approves work nor calls a compute provider. Lost responses
are surfaced to the dispatcher, which retains the original execution identity.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from typing import Annotated

from pydantic import Field, TypeAdapter

from .audit import canonical_json
from .dispatcher import SSHTunnel, TransportError
from .gpu_acceptance_runner import read_file
from .rpc import decode
from .schemas import FrozenModel, GitSHA, Identifier, ModelIdentity, SHA256, UTCTimestamp
from .worker_contracts import DatasetAsset, ExecutionReceipt, ExecutionRequest, FileDigest, WorkerState

HELPER = "/opt/probe/public-job.py"
PYTHON = "/opt/probe-core/venv/bin/python"
MAX_JSON = 1024 * 1024
MAX_TENSOR = 32 * 1024**2


class _PublicConfig(FrozenModel):
    model: ModelIdentity
    assets: Annotated[tuple[FileDigest, ...], Field(min_length=1, max_length=128)]
    datasets: Annotated[tuple[DatasetAsset, ...], Field(min_length=1, max_length=1)]
    code_git_commit: GitSHA
    container_image_digest: SHA256
    region: Identifier
    live_price_usd_per_hour: Annotated[float, Field(strict=True, gt=0, lt=1.5, allow_inf_nan=False)]


def _sha(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _json(value):
    return canonical_json(value).encode() + b"\n"


def _read_regular(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise TransportError("SSH input is not a bounded unshared regular file")
        body = stream.read(limit + 1)
        if len(body) != info.st_size or len(body) > limit:
            raise TransportError("SSH input changed or exceeded its bound")
        return body


def _tensor_descriptions(body):
    """Validate bounded safetensors framing without importing Torch or a model."""
    widths = {"F16": 2, "BF16": 2, "F32": 4, "F64": 8, "I32": 4, "I64": 8}
    if len(body) < 8:
        raise ValueError("missing safetensors header")
    size = int.from_bytes(body[:8], "little")
    if not 1 <= size <= min(8 * 1024**2, len(body) - 8):
        raise ValueError("invalid safetensors header size")
    header = decode(body[8 : 8 + size])
    if type(header) is not dict:
        raise ValueError("invalid safetensors header")
    metadata = header.pop("__metadata__", {})
    if type(metadata) is not dict or not all(type(k) is str and type(v) is str for k, v in metadata.items()):
        raise ValueError("invalid safetensors metadata")
    if not 1 <= len(header) <= 128:
        raise ValueError("invalid safetensors tensor count")
    descriptions, ranges = [], []
    for name, item in sorted(header.items()):
        TypeAdapter(Identifier).validate_python(name)
        if type(item) is not dict or set(item) != {"dtype", "shape", "data_offsets"}:
            raise ValueError("invalid tensor description")
        shape, offsets, dtype = item["shape"], item["data_offsets"], item["dtype"]
        if (
            type(dtype) is not str
            or dtype not in widths
            or type(shape) is not list
            or len(shape) > 8
            or any(type(n) is not int or not 0 <= n <= 2**63 - 1 for n in shape)
            or type(offsets) is not list
            or len(offsets) != 2
            or any(type(n) is not int or n < 0 for n in offsets)
        ):
            raise ValueError("invalid tensor metadata")
        count = widths[dtype]
        for dimension in shape:
            count *= dimension
        begin, end = offsets
        if end - begin != count or end > len(body) - 8 - size:
            raise ValueError("tensor data length differs from shape")
        ranges.append((begin, end))
        descriptions.append({"tensor_name": name, "shape": shape, "dtype": dtype})
    previous = 0
    for begin, end in sorted(ranges):
        if begin != previous:
            raise ValueError("tensor data has gaps or overlaps")
        previous = end
    if previous != len(body) - 8 - size:
        raise ValueError("tensor data is incomplete")
    return descriptions


def _private_directory(path):
    """Open/create the destination without following an ancestor symlink."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise TransportError("artifact destination cannot contain traversal")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise TransportError("artifact destination must be private and owned")
    finally:
        os.close(fd)
    return path


def bounded_command(command, payload, *, timeout, max_output_bytes):
    """Run one fixed command; bound both pipe handling and owned process lifetime."""
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        end = time.monotonic() + timeout
        output = bytearray()
        diagnostic = bytearray()
        remaining = memoryview(payload)
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            if remaining:
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            while selector.get_map():
                left = end - time.monotonic()
                if left <= 0:
                    raise TransportError("SSH helper timed out; reconcile the same attempt")
                for key, _ in selector.select(left):
                    if key.fileobj is process.stdin:
                        remaining = remaining[os.write(process.stdin.fileno(), remaining[:65536]) :]
                        if not remaining:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    elif key.fileobj is process.stdout:
                        chunk = os.read(process.stdout.fileno(), min(65536, max_output_bytes - len(output) + 1))
                        if not chunk:
                            selector.unregister(process.stdout)
                        output.extend(chunk)
                        if len(output) > max_output_bytes:
                            raise TransportError("SSH helper output exceeds its bound")
                    else:
                        chunk = os.read(process.stderr.fileno(), 65536)
                        if not chunk:
                            selector.unregister(process.stderr)
                        diagnostic.extend(chunk)
                        del diagnostic[:-8192]
            process.wait(timeout=max(0.001, end - time.monotonic()))
        if process.returncode:
            # Only the helper's exact fixed-code envelope may leave this method.
            # SSH text, paths, addresses, traces and arbitrary stderr are discarded.
            code = None
            try:
                value = decode(bytes(diagnostic))
                if (
                    type(value) is dict
                    and set(value) == {"error"}
                    and type(value["error"]) is str
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value["error"])
                ):
                    code = value["error"]
            except (ValueError, TypeError):
                pass
            error = TransportError("SSH helper failed" + (": " + code if code else "") + "; reconcile the same attempt")
            error.helper_error_code = code
            raise error
        return bytes(output)
    except (OSError, subprocess.SubprocessError):
        raise TransportError("SSH transport failed; reconcile the same attempt") from None
    finally:
        if process is not None:
            # A descendant may retain stdout after the SSH leader exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


# The destination is fixed. Existing helper bytes may be inspected, never replaced.
# This bootstrap does not execute the transferred script.
STAGE_HELPER = r"""import hashlib,json,os,pathlib,stat,sys,tempfile
assert os.geteuid()==0
line=sys.stdin.buffer.readline(4097)
assert len(line)<=4096 and line.endswith(b'\n')
header=json.loads(line)
assert set(header)=={'sha256','length'} and type(header['length']) is int
assert 1<=header['length']<=1048576
body=sys.stdin.buffer.read(header['length']+1)
assert len(body)==header['length']
digest='sha256:'+hashlib.sha256(body).hexdigest()
assert digest==header['sha256']
target=pathlib.Path('/opt/probe/public-job.py')
for parent in target.parents:
 info=parent.lstat()
 assert stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022
fd,name=tempfile.mkstemp(prefix='.public-job-',dir=target.parent)
try:
 with os.fdopen(fd,'wb') as out:
  out.write(body);out.flush();os.fsync(out.fileno());os.fchmod(out.fileno(),0o444)
 try:os.link(name,target)
 except FileExistsError:pass
finally:os.unlink(name)
fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
with os.fdopen(fd,'rb') as inp:
 info=os.fstat(inp.fileno())
 assert stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1
 assert stat.S_IMODE(info.st_mode)==0o444 and info.st_size==header['length']
 assert 'sha256:'+hashlib.sha256(inp.read(1048577)).hexdigest()==digest
fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
try:os.fsync(fd)
finally:os.close(fd)
print(json.dumps({'path':str(target),'sha256':digest,'bytes':len(body),'uid':0,'mode':0o444}))
"""


class SSHJobClient:
    """WorkerClient-compatible fixed SSH commands with bounded byte transfers."""

    def __init__(self, settings, config, absolute_deadline, *, timeout_seconds=15, transport=None, clock=None):
        required = {"host", "user", "ssh_port", "identity_file", "known_hosts_file", "remote_port"}
        if set(settings) != required or settings["user"] != "root":
            raise ValueError("an exact verified root SSH endpoint is required")
        endpoint = SSHTunnel(
            settings["host"],
            user="root",
            ssh_port=settings["ssh_port"],
            identity_file=Path(settings["identity_file"]),
            known_hosts_file=Path(settings["known_hosts_file"]),
        )
        self.config = _PublicConfig.model_validate(config).model_dump(mode="json")
        self.absolute_deadline = TypeAdapter(UTCTimestamp).validate_python(absolute_deadline)
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60:
            raise ValueError("SSH timeout must be bounded to at most sixty seconds")
        self.timeout_seconds = timeout_seconds
        self._clock = clock or time.time
        self._transport = transport or bounded_command
        self._requests = {}
        self._attempt_id = None
        self._job_id = None
        self._trust = [(endpoint.identity_file, True), (endpoint.known_hosts_file, False)]
        self._trust_hashes = [
            _sha(read_file(path, owner=os.geteuid(), private=private, bound=65536)) for path, private in self._trust
        ]
        self._command = [
            "/usr/bin/ssh",
            "-F",
            "/dev/null",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            "UserKnownHostsFile=" + str(endpoint.known_hosts_file),
            "-i",
            str(endpoint.identity_file),
            "-p",
            str(endpoint.ssh_port),
            "root@" + endpoint.host,
        ]

    def _call(self, operation, payload, *, output_bound=MAX_JSON, mutating=False):
        return self._execute(
            shlex.join([PYTHON, "-I", HELPER, operation]), payload, output_bound=output_bound, mutating=mutating
        )

    def _execute(self, remote_command, payload, *, output_bound, mutating):
        try:
            if len(payload) > MAX_TENSOR + MAX_JSON:
                raise TransportError("SSH request exceeds its bound")
            for (path, private), expected in zip(self._trust, self._trust_hashes):
                if _sha(read_file(path, owner=os.geteuid(), private=private, bound=65536)) != expected:
                    raise TransportError("SSH trust file changed")
            timeout = self.timeout_seconds
            if mutating:
                left = self.absolute_deadline.timestamp() - self._clock()
                if left <= 0:
                    raise TransportError("original execution allowance expired")
                timeout = min(timeout, left)
            body = self._transport(
                self._command + [remote_command], payload, timeout=timeout, max_output_bytes=output_bound
            )
            if type(body) is not bytes or len(body) > output_bound:
                raise TransportError("SSH response exceeds its bound or is invalid")
            return body
        except TransportError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            raise TransportError("SSH transport failed; reconcile the same attempt") from None

    def stage_helper(self, local_path, expected_sha256):
        try:
            TypeAdapter(SHA256).validate_python(expected_sha256)
            body = _read_regular(Path(local_path), MAX_JSON)
            if not body or _sha(body) != expected_sha256:
                raise TransportError("pinned helper checksum mismatch")
            payload = _json({"sha256": expected_sha256, "length": len(body)}) + body
            response = decode(
                self._execute(
                    shlex.join([PYTHON, "-I", "-c", STAGE_HELPER]), payload, output_bound=65536, mutating=True
                )
            )
            expected = {"path": HELPER, "sha256": expected_sha256, "bytes": len(body), "uid": 0, "mode": 0o444}
            if canonical_json(response) != canonical_json(expected):
                raise TransportError("pinned helper readback mismatch")
            return {"path": HELPER, "sha256": expected_sha256}
        except (OSError, ValueError, TypeError):
            raise TransportError("pinned helper staging failed") from None

    def _receipt(self, operation, payload, *, request=None, job_id=None, attempt_id=None, mutating=False):
        try:
            receipt = ExecutionReceipt.model_validate(decode(self._call(operation, _json(payload), mutating=mutating)))
            if receipt.attempt_id != attempt_id or (job_id is not None and receipt.job_id != job_id):
                raise TransportError("worker returned a different execution identity")
            if self._job_id is not None and receipt.job_id != self._job_id:
                raise TransportError("worker changed its execution identity")
            if request is not None and receipt.manifest is not None:
                if receipt.manifest.model != request.spec.model or receipt.manifest.run.run_id != request.job_id:
                    raise TransportError("worker returned a different manifest identity")
            self._job_id = receipt.job_id
            return receipt
        except (ValueError, TypeError, KeyError):
            raise TransportError("worker returned an invalid receipt") from None

    def submit(self, request):
        try:
            request = ExecutionRequest.model_validate(request)
            if request.spec.model.model_dump(mode="json") != self.config["model"]:
                raise TransportError("execution model differs from the pinned public config")
            if not self._clock() < request.deadline.timestamp() <= self.absolute_deadline.timestamp():
                raise TransportError("execution deadline exceeds its original allowance or expired")
            self._bind_attempt(request.attempt_id)
            if self._job_id is not None and request.job_id != self._job_id:
                raise TransportError("an execution attempt cannot be rebound")
            previous = self._requests.get(request.attempt_id)
            if previous is not None and previous != request:
                raise TransportError("an execution attempt cannot be rebound")
            self._requests[request.attempt_id] = request  # Retain even when the response is lost.
            return self._receipt(
                "start",
                {
                    "config": self.config,
                    "request": request.model_dump(mode="json"),
                    "absolute_deadline": self.absolute_deadline.isoformat(),
                },
                request=request,
                job_id=request.job_id,
                attempt_id=request.attempt_id,
                mutating=True,
            )
        except (ValueError, TypeError):
            raise TransportError("invalid execution request") from None

    def status(self, attempt_id):
        try:
            TypeAdapter(Identifier).validate_python(attempt_id)
        except ValueError:
            raise TransportError("invalid execution attempt identifier") from None
        self._bind_attempt(attempt_id)
        request = self._requests.get(attempt_id)
        return self._receipt(
            "status",
            {"attempt_id": attempt_id},
            request=request,
            job_id=request.job_id if request else None,
            attempt_id=attempt_id,
        )

    def _bind_attempt(self, attempt_id):
        if self._attempt_id is not None and self._attempt_id != attempt_id:
            raise TransportError("one Pod cannot execute a fresh attempt")
        self._attempt_id = attempt_id

    def cancel(self, attempt_id):
        previous = self.status(attempt_id)
        if previous.process_stopped:
            return previous
        return self._receipt(
            "cancel",
            {"attempt_id": attempt_id},
            request=self._requests.get(attempt_id),
            job_id=previous.job_id,
            attempt_id=attempt_id,
        )

    def inspect(self, attempt_id):
        """Read fixed lifecycle evidence; orchestration verifies action-specific proof."""
        try:
            TypeAdapter(Identifier).validate_python(attempt_id)
            self._bind_attempt(attempt_id)
            body = decode(self._call("inspect", _json({"attempt_id": attempt_id})))
            required = {
                "receipt",
                "request_sha256",
                "config_sha256",
                "child_identity",
                "monitor_identity",
                "original_deadline",
                "effective_job_deadline",
                "execution_started",
                "cancellation",
            }
            if type(body) is not dict or set(body) != required:
                raise TransportError("worker returned invalid inspection fields")
            receipt = ExecutionReceipt.model_validate(body["receipt"])
            if receipt.attempt_id != attempt_id:
                raise TransportError("worker returned a different inspection identity")
            for name in ("request_sha256", "config_sha256"):
                TypeAdapter(SHA256).validate_python(body[name])
            absolute = TypeAdapter(UTCTimestamp).validate_python(body["original_deadline"])
            effective = TypeAdapter(UTCTimestamp).validate_python(body["effective_job_deadline"])
            if (
                absolute != self.absolute_deadline
                or effective > absolute
                or body["config_sha256"] != _sha(canonical_json(self.config).encode())
            ):
                raise TransportError("worker inspection configuration or deadline mismatch")
            request = self._requests.get(attempt_id)
            if request is not None and (
                receipt.job_id != request.job_id
                or body["request_sha256"] != _sha(canonical_json(request.model_dump(mode="json")).encode())
                or effective > request.deadline
            ):
                raise TransportError("worker inspection request mismatch")
            for name in ("child_identity", "monitor_identity"):
                identity = body[name]
                if identity is not None and (
                    type(identity) is not dict
                    or set(identity) != {"pid", "identity", "boot_id"}
                    or type(identity["pid"]) is not int
                    or not 0 < identity["pid"] <= 2**31 - 1
                    or type(identity["identity"]) is not str
                    or not re.fullmatch(r"[0-9]{1,32}", identity["identity"])
                    or type(identity["boot_id"]) is not str
                    or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity["boot_id"])
                ):
                    raise TransportError("worker inspection process identity invalid")
            for name in ("execution_started", "cancellation"):
                if body[name] is not None and type(body[name]) is not dict:
                    raise TransportError("worker inspection proof invalid")
            return body
        except (ValueError, TypeError, KeyError):
            raise TransportError("worker returned invalid inspection evidence") from None

    def upload_tensor(self, source, *, expected_sha256):
        """Return the actual upload receipt only after independent byte readback."""
        try:
            TypeAdapter(SHA256).validate_python(expected_sha256)
            body = _read_regular(Path(source), MAX_TENSOR)
            if _sha(body) != expected_sha256:
                raise TransportError("registered input tensor checksum mismatch")
            descriptions = _tensor_descriptions(body)
            payload = _json({"sha256": expected_sha256, "length": len(body)}) + body
            receipt = decode(self._call("upload", payload, output_bound=65536, mutating=True))
            expected = {
                "path": expected_sha256.removeprefix("sha256:") + "/tensor.safetensors",
                "sha256": expected_sha256,
                "tensors": descriptions,
            }
            if canonical_json(receipt) != canonical_json(expected):
                raise TransportError("worker tensor upload receipt mismatch")
            readback = self._call("read-tensor", _json({"sha256": expected_sha256}), output_bound=len(body))
            if len(readback) != len(body) or _sha(readback) != expected_sha256:
                raise TransportError("worker tensor byte readback mismatch")
            return receipt
        except (OSError, ValueError, TypeError, KeyError):
            raise TransportError("tensor transfer failed; no execution was submitted") from None

    def download(self, receipt, destination, *, max_bytes):
        try:
            receipt = ExecutionReceipt.model_validate(receipt)
            if not receipt.process_stopped or receipt.state != WorkerState.SUCCEEDED or receipt.manifest is None:
                raise TransportError("artifact download requires stopped successful execution")
            if type(max_bytes) is not int or not 0 < max_bytes <= MAX_TENSOR:
                raise TransportError("artifact byte budget must fit the fixed public profile")
            if receipt.manifest.cost.bytes_persisted > max_bytes:
                raise TransportError("manifest output size exceeds the dispatch budget")
            destination = _private_directory(destination)
            total = 0
            for artifact in receipt.manifest.artifacts:
                target = destination / artifact.path
                parent = _private_directory(target.parent)
                # A verified existing file permits collection to resume after a lost response.
                if target.exists() or target.is_symlink():
                    body = _read_regular(target, max_bytes - total)
                else:
                    body = self._call(
                        "artifact",
                        _json({"attempt_id": receipt.attempt_id, "path": artifact.path}),
                        output_bound=max_bytes - total,
                    )
                total += len(body)
                if total > max_bytes or hashlib.sha256(body).hexdigest() != artifact.sha256:
                    raise TransportError("worker artifact checksum or byte budget mismatch")
                if not target.exists():
                    fd, name = tempfile.mkstemp(prefix=".download-", dir=parent)
                    temporary = Path(name)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(body)
                            stream.flush()
                            os.fsync(stream.fileno())
                        # Never overwrite a replaced path or an existing artifact.
                        os.link(temporary, target)
                        temporary.unlink()
                        folder = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            os.fsync(folder)
                        finally:
                            os.close(folder)
                    finally:
                        temporary.unlink(missing_ok=True)
            if total != receipt.manifest.cost.bytes_persisted:
                raise TransportError("downloaded bytes differ from the retained manifest")
            return destination
        except (OSError, ValueError, TypeError, KeyError):
            raise TransportError("artifact collection failed") from None
