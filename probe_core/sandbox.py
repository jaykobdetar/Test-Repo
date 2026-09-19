"""Fail-closed CPU Python sandbox using a local rootless Podman runtime.

No host socket, credentials, GPU device, or writable host directory is mounted.
The pinned image's entrypoint attests the actual cgroup/security settings before
the host releases the experiment. All subsequent output is untrusted data.
"""
from __future__ import annotations

import base64
import hashlib
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError
from typing import Literal

from .schemas import Identifier, JobSpec


class SandboxError(RuntimeError):
    pass


class SandboxUnavailable(SandboxError):
    pass


class SandboxProtocolError(SandboxError):
    pass


@dataclass(frozen=True)
class SandboxLimits:
    wall_seconds: int = 30
    cpu_cores: float = 1.0
    memory_bytes: int = 1024**3
    pids: int = 32
    max_output_bytes: int = 8 * 1024**2
    max_log_bytes: int = 1024**2
    max_broker_requests: int = 16

    def __post_init__(self):
        bounds = {
            "wall_seconds": (1, 3600), "memory_bytes": (64 * 1024**2, 8 * 1024**3),
            "pids": (8, 128), "max_output_bytes": (4096, 64 * 1024**2),
            "max_log_bytes": (1024, 4 * 1024**2), "max_broker_requests": (0, 64),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}]")
        if isinstance(self.cpu_cores, bool) or not isinstance(self.cpu_cores, (float, int)) or not math.isfinite(self.cpu_cores) or not 0.1 <= self.cpu_cores <= 4:
            raise ValueError("cpu_cores must be finite and between 0.1 and 4")
        if self.max_output_bytes >= self.memory_bytes // 2:
            raise ValueError("output tmpfs must leave at least half of the memory budget available")


class BrokerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["gpu_request"]
    request_id: Identifier
    job_key: Identifier


class GPURequestBroker:
    """Controller-owned exact job allowlist; researcher-supplied JobSpecs are forbidden.

    submit must perform the controller's usual authorization/budget checks and
    return a non-secret job ID. This broker grants no cloud-start capability.
    """
    def __init__(self, approved_jobs: Mapping[str, JobSpec], submit: Callable[[JobSpec], str]):
        self._jobs = {key: JobSpec.model_validate(value.model_dump()) for key, value in approved_jobs.items()}
        self._submit = submit
        self._seen: dict[str, tuple[str, dict]] = {}
        self._lock = threading.Lock()

    def handle(self, data: bytes) -> dict:
        try:
            request = BrokerRequest.model_validate_json(data)
        except (ValidationError, ValueError):
            return {"status": "denied", "reason": "invalid_request"}
        with self._lock:
            if request.request_id in self._seen:
                key, response = self._seen[request.request_id]
                if key == request.job_key:
                    return dict(response)
                return {"request_id": request.request_id, "status": "denied", "reason": "request_id_reused"}
            if request.job_key not in self._jobs:
                response = {"request_id": request.request_id, "status": "denied", "reason": "job_not_approved"}
            else:
                try:
                    job_id = self._submit(self._jobs[request.job_key])
                    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", job_id):
                        raise ValueError("invalid job ID")
                    response = {"request_id": request.request_id, "status": "submitted", "job_id": job_id}
                except Exception:
                    # Arbitrary controller exception text may contain credentials.
                    response = {"request_id": request.request_id, "status": "denied", "reason": "controller_denied"}
            self._seen[request.request_id] = (request.job_key, response)
            return dict(response)


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    artifacts: tuple[Path, ...]
    termination_reason: str | None


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 240:
        raise SandboxProtocolError("invalid relative output path")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value):
        raise SandboxProtocolError("invalid relative output path")
    if any(part in {".", ".."} for part in value.split("/")):
        raise SandboxProtocolError("output path traversal")
    return value


class _ArtifactReceiver:
    """Accept a bounded, non-executable file stream into a private empty directory."""
    def __init__(self, root: Path, limit: int):
        self.root, self.limit = root, limit
        self.total = 0
        self.files: dict[str, tuple[object, object, int]] = {}
        self.finished: list[Path] = []

    def receive(self, frame: dict):
        if not isinstance(frame, dict) or set(frame) - {"kind", "path", "data", "sha256"}:
            raise SandboxProtocolError("invalid artifact frame")
        path = _relative_path(frame.get("path"))
        kind = frame.get("kind")
        if kind == "begin":
            if path in self.files or len(self.files) >= 256:
                raise SandboxProtocolError("duplicate or excessive artifacts")
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                stream = target.open("xb")
            except OSError as exc:
                raise SandboxProtocolError("conflicting artifact paths") from exc
            os.chmod(target, 0o600)
            self.files[path] = (stream, hashlib.sha256(), 0)
        elif kind == "chunk":
            if path not in self.files or self.files[path][0].closed:
                raise SandboxProtocolError("chunk outside an open artifact")
            try:
                data = base64.b64decode(frame["data"], validate=True)
            except (KeyError, ValueError, TypeError) as exc:
                raise SandboxProtocolError("invalid artifact bytes") from exc
            if len(data) > 65536 or self.total + len(data) > self.limit:
                raise SandboxProtocolError("artifact output limit exceeded")
            stream, digest, count = self.files[path]
            stream.write(data)
            digest.update(data)
            self.total += len(data)
            self.files[path] = (stream, digest, count + len(data))
        elif kind == "end":
            if path not in self.files or self.files[path][0].closed:
                raise SandboxProtocolError("end outside an open artifact")
            stream, digest, _ = self.files[path]
            if frame.get("sha256") != digest.hexdigest():
                raise SandboxProtocolError("artifact hash mismatch")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            self.finished.append(self.root / path)
        else:
            raise SandboxProtocolError("unknown artifact frame")

    def close(self):
        incomplete = False
        for stream, _, _ in self.files.values():
            if not stream.closed:
                incomplete = True
                stream.close()
        return incomplete


class PodmanSandbox:
    def __init__(self, *, image: str, workspace: Path, podman: str = "podman", seccomp_profile: Path | None = None):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("sandbox image must be an immutable local sha256 image ID")
        self.image = image
        self.workspace = Path(workspace).absolute()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.workspace.is_symlink() or self.workspace.stat().st_uid != os.getuid():
            raise ValueError("workspace must be a private directory owned by the controller")
        os.chmod(self.workspace, 0o700)
        self.podman = str(podman)
        self.seccomp_profile = Path(seccomp_profile or str(files("probe_core").joinpath("resources/seccomp.json"))).absolute()
        if not self.seccomp_profile.is_file() or self.seccomp_profile.is_symlink():
            raise ValueError("a regular trusted seccomp profile is required")

    def _command(self, *args: str) -> list[str]:
        return [self.podman, "--remote=false", *args]

    def _environment(self) -> dict[str, str]:
        # Only Podman's local runtime needs these host settings; --unsetenv-all
        # ensures they are never inherited by the experiment container.
        keys = ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
        return {key: os.environ[key] for key in keys if key in os.environ}

    def check_runtime(self) -> dict:
        if os.geteuid() == 0:
            raise SandboxUnavailable("sandbox must be launched by a non-root controller identity")
        try:
            result = subprocess.run(self._command("info", "--format=json"), capture_output=True, env=self._environment(), timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailable("rootless Podman runtime is unavailable") from exc
        if result.returncode:
            diagnostic = result.stderr.decode(errors="replace")[-2000:]
            raise SandboxUnavailable("rootless Podman runtime probe failed: " + diagnostic)
        try:
            info = json.loads(result.stdout)
            host = info["host"]
            if not host["security"]["rootless"] or not host["security"]["seccompEnabled"] or host["cgroupVersion"] != "v2":
                raise ValueError("missing rootless/seccomp/cgroup-v2 capability")
            if not {"cpu", "memory", "pids"} <= set(host.get("cgroupControllers", [])):
                raise ValueError("CPU, memory and PID cgroup controllers must be delegated")
        except (ValueError, KeyError, TypeError) as exc:
            raise SandboxUnavailable("rootless Podman requires seccomp and delegated cgroup-v2 CPU, memory and PID controllers") from exc
        return info

    def _run_command(self, name: str, inputs: Path, limits: SandboxLimits) -> list[str]:
        return self._command(
            "run", "--name", name, "--pull=never", "--interactive", "--log-driver=none",
            # Conmon enforces this deadline even if the facade and attached
            # Podman client die. Keep the host timer for startup-inclusive limits.
            "--timeout=" + str(limits.wall_seconds), "--rm",
            "--network=none", "--pid=private", "--ipc=private", "--uts=private", "--cgroupns=private",
            "--userns=keep-id:uid=1000,gid=1000", "--user=1000:1000", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--security-opt=seccomp=" + str(self.seccomp_profile),
            "--read-only", "--read-only-tmpfs=false", "--unsetenv-all", "--http-proxy=false",
            "--env=PATH=/usr/local/bin:/usr/bin:/bin", "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTHONUNBUFFERED=1", "--env=HOME=/output", "--env=TMPDIR=/output",
            "--env=OPENBLAS_NUM_THREADS=1", "--env=OMP_NUM_THREADS=1", "--env=MKL_NUM_THREADS=1",
            "--env=NUMEXPR_NUM_THREADS=1", "--env=TOKENIZERS_PARALLELISM=false",
            "--env=HF_HUB_OFFLINE=1", "--env=TRANSFORMERS_OFFLINE=1", "--env=HF_HUB_DISABLE_TELEMETRY=1",
            "--env=PROBE_OUTPUT_LIMIT=" + str(limits.max_output_bytes),
            "--cpus=" + str(limits.cpu_cores), "--memory=" + str(limits.memory_bytes),
            "--memory-swap=" + str(limits.memory_bytes), "--pids-limit=" + str(limits.pids),
            "--ulimit=core=0:0", "--ulimit=nofile=128:128",
            "--ulimit=fsize=" + str(limits.max_output_bytes) + ":" + str(limits.max_output_bytes),
            "--mount=type=bind,src=" + str(inputs) + ",dst=/input,ro=true,nodev,nosuid,noexec",
            "--tmpfs=/output:rw,noexec,nosuid,nodev,size=" + str(limits.max_output_bytes) + ",mode=1777",
            "--workdir=/output", "--entrypoint=/usr/local/bin/python", self.image,
            "-I", "-B", "/opt/probe/sandbox_entry.py",
        )

    @staticmethod
    def _validate_attestation(report: dict, limits: SandboxLimits):
        try:
            quota, period = report["cpu_max"].split()
            secure = (
                report["uid"] == 1000 and report["cap_eff"] == "0000000000000000"
                and report["seccomp"] == "2" and report["no_new_privs"] == "1"
                and report["socket_denied"] and report["input_readonly"] and report["root_readonly"]
                and 0 < int(report["memory_max"]) <= limits.memory_bytes
                and 0 < int(report["pids_max"]) <= limits.pids
                and 0 < int(quota) / int(period) <= limits.cpu_cores + 0.0001
                and set(report["interfaces"]) <= {"lo"}
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            secure = False
        if not secure:
            raise SandboxUnavailable("container did not attest enforced isolation and resource limits")

    def run(self, code: str, *, inputs: Mapping[str, bytes] | None = None, limits: SandboxLimits | None = None, broker: GPURequestBroker | None = None, cancel_event: threading.Event | None = None) -> SandboxResult:
        limits = limits or SandboxLimits()
        if not isinstance(code, str) or "\x00" in code or len(code.encode()) > 1024**2:
            raise ValueError("code must be UTF-8 text of at most 1 MiB")
        self.check_runtime()
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=self.workspace))
        input_dir, output_dir = run_dir / "input", run_dir / "output"
        input_dir.mkdir(mode=0o700)
        output_dir.mkdir(mode=0o700)
        supplied = dict(inputs or {})
        if len(supplied) > 256 or sum(len(value) for value in supplied.values() if isinstance(value, bytes)) > 16 * 1024**2:
            raise ValueError("input bundle exceeds its count or size limit")
        for key, value in supplied.items():
            path = _relative_path(key)
            if path == "code.py" or not isinstance(value, bytes):
                raise ValueError("input files must be bytes and cannot replace code.py")
            target = input_dir / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(value)
            target.chmod(0o400)
        (input_dir / "code.py").write_text(code)
        (input_dir / "code.py").chmod(0o400)
        name = "probe-cpu-" + run_dir.name.removeprefix("run-")
        command = self._run_command(name, input_dir, limits)
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._environment(), start_new_session=True, close_fds=True)
        receiver = _ArtifactReceiver(output_dir, limits.max_output_bytes)
        logs = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        for stream_name in buffers:
            selector.register(getattr(proc, stream_name), selectors.EVENT_READ, stream_name)
        reason = None
        attested = False
        request_count = 0
        timed_out = threading.Event()
        stop_lock = threading.Lock()

        def terminate():
            with stop_lock:
                try:
                    subprocess.run(self._command("kill", "--signal=KILL", name), capture_output=True, env=self._environment(), timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

        def timeout():
            timed_out.set()
            terminate()

        timer = threading.Timer(limits.wall_seconds, timeout)
        timer.daemon = True
        timer.start()
        try:
            while selector.get_map():
                if timed_out.is_set():
                    reason = "wall_time_limit"
                    break
                if cancel_event is not None and cancel_event.is_set():
                    reason = "cancelled"
                    terminate()
                    break
                for key, _ in selector.select(timeout=0.05):
                    chunk = os.read(key.fd, 65536)
                    channel = key.data
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if buffers[channel]:
                            logs[channel].extend(buffers[channel])
                            buffers[channel].clear()
                        continue
                    buffers[channel].extend(chunk)
                    while b"\n" in buffers[channel]:
                        line, _, tail = buffers[channel].partition(b"\n")
                        buffers[channel] = bytearray(tail)
                        if channel == "stdout" and line.startswith(b"PROBE_RUNTIME:"):
                            if attested:
                                raise SandboxProtocolError("duplicate runtime attestation")
                            self._validate_attestation(json.loads(line[14:]), limits)
                            proc.stdin.write(b"RUN\n")
                            proc.stdin.flush()
                            attested = True
                        elif channel == "stdout" and line.startswith(b"PROBE_BROKER:"):
                            if not attested:
                                raise SandboxProtocolError("broker before runtime attestation")
                            request_count += 1
                            if request_count > limits.max_broker_requests:
                                raise SandboxProtocolError("broker request limit exceeded")
                            response = broker.handle(bytes(line[13:])) if broker else {"status": "denied", "reason": "broker_disabled"}
                            proc.stdin.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                            proc.stdin.flush()
                        elif channel == "stdout" and line.startswith(b"PROBE_ARTIFACT:"):
                            if not attested:
                                raise SandboxProtocolError("artifact before runtime attestation")
                            receiver.receive(json.loads(line[15:]))
                        else:
                            logs[channel].extend(line + b"\n")
                        if sum(map(len, logs.values())) > limits.max_log_bytes:
                            raise SandboxProtocolError("log output limit exceeded")
                    if len(buffers[channel]) > 128 * 1024:
                        raise SandboxProtocolError("output frame exceeds 128 KiB")
                    if sum(map(len, logs.values())) + sum(map(len, buffers.values())) > limits.max_log_bytes + 128 * 1024:
                        raise SandboxProtocolError("log output limit exceeded")
            proc.wait(timeout=5)
            # A kill can close both streams during select(), ending the loop
            # before its next timeout check. Preserve the actual timer outcome.
            if reason is None and timed_out.is_set():
                reason = "wall_time_limit"
            if sum(map(len, logs.values())) > limits.max_log_bytes:
                raise SandboxProtocolError("log output limit exceeded")
            if reason is None and (not attested or receiver.close()):
                raise SandboxProtocolError("container exited without a complete attested result")
        except (SandboxError, json.JSONDecodeError, BrokenPipeError, subprocess.TimeoutExpired) as exc:
            reason = "containment_or_output_failure"
            terminate()
            if isinstance(exc, SandboxUnavailable):
                raise
        finally:
            timer.cancel()
            selector.close()
            receiver.close()
            terminate()
            try:
                removed = subprocess.run(self._command("rm", "--force", "--ignore", name), capture_output=True, env=self._environment(), timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SandboxUnavailable("container termination could not be confirmed") from exc
            if removed.returncode:
                raise SandboxUnavailable("container termination could not be confirmed")
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
        if reason is not None or proc.returncode != 0:
            shutil.rmtree(output_dir)
            output_dir.mkdir(mode=0o700)
            artifacts = ()
        else:
            artifacts = tuple(receiver.finished)
        return SandboxResult(proc.returncode, logs["stdout"].decode(errors="replace"), logs["stderr"].decode(errors="replace"), artifacts, reason)
