"""Installed, fail-closed CPU containment acceptance; no host ML/test dependencies.

Run as the trusted service UID, using its actual Podman store, delegated cgroup
and service restrictions. Exit status, not a report from an earlier invocation,
is the installation gate. This checks local CPU containment, not GPU acceptance.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from .sandbox import PodmanSandbox, SandboxLimits, SandboxResult


class AcceptanceError(RuntimeError):
    pass


_DIAGNOSTIC_BYTES = 8192
_STAGES = {"configuration", "runtime", "cpu_and_isolation", "pid_and_output", "memory", "wall_time", "complete"}
_PUBLIC_REASONS = {
    "report parent must be a trusted, owned directory without symlinks": "unsafe_report_directory",
    "report must be an owned regular file": "unsafe_report_file",
    "acceptance workspace must be private, owned and without symlinks": "unsafe_workspace",
    "acceptance requires the packaged reviewed seccomp profile": "seccomp_profile_mismatch",
    "the requested immutable image is not present in the service Podman store": "immutable_image_unavailable",
    "check lacks exactly one accepted runtime attestation": "runtime_attestation_missing_or_duplicated",
    "contained check did not finish successfully": "contained_check_failed",
    "contained check did not return its exact result artifact": "result_artifact_mismatch",
    "contained result exceeds its bound": "result_artifact_too_large",
    "contained check did not prove every required condition": "required_condition_unproven",
    "memory pressure was not refused after verified startup": "memory_limit_unproven",
    "wall time did not terminate a verified running job within the cleanup bound": "wall_time_limit_unproven",
}


def _safe_reason(error: BaseException) -> str:
    # Never make arbitrary exception or subprocess text part of public output.
    return (_PUBLIC_REASONS.get(str(error), "acceptance_check_failed")
            if isinstance(error, AcceptanceError) else "runtime_error")


def _bounded_diagnostic(value: str | bytes) -> dict:
    raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
    return {"tail": raw[-_DIAGNOSTIC_BYTES:].decode("utf-8", errors="ignore"),
            "bytes": len(raw), "truncated": len(raw) > _DIAGNOSTIC_BYTES}


def _exception_diagnostics(error: BaseException) -> dict:
    diagnostics = {"exception": _bounded_diagnostic(str(error))}
    for name in ("stdout", "stderr"):
        value = getattr(error, name, None)
        if isinstance(value, (str, bytes)):
            diagnostics[name] = _bounded_diagnostic(value)
    if isinstance(error, AcceptanceError):
        diagnostics.update(getattr(error, "private_diagnostics", {}))
    return diagnostics


_ATTESTATION_FIELDS = (
    "uid", "cap_eff", "seccomp", "no_new_privs", "memory_max", "pids_max",
    "cpu_max", "interfaces", "socket_denied", "input_readonly", "root_readonly",
)


class _RecordedSandbox(PodmanSandbox):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.attestations: list[dict] = []

    def _validate_attestation(self, report: dict, limits: SandboxLimits):
        super()._validate_attestation(report, limits)
        # This hook runs before the pinned entrypoint releases experiment code.
        self.attestations.append({key: report[key] for key in _ATTESTATION_FIELDS})


_CPU_CODE = '''import json, os, pathlib, socket
import numpy as np
import torch
from scipy.stats import pearsonr
from sklearn.linear_model import Ridge
from safetensors.torch import save_file, load_file
assert torch.version.cuda is None and not torch.cuda.is_available()
assert not any(name in os.environ for name in ("RUNPOD_API_KEY", "HF_TOKEN", "AWS_SECRET_ACCESS_KEY"))
for path in ["/run/podman/podman.sock", "/var/run/docker.sock", "/root/.ssh", "/dev/nvidia0", "/dev/nvidiactl", "/dev/dri"]:
    try: exists = pathlib.Path(path).exists()
    except PermissionError: exists = False
    assert not exists, path
sentinel = json.loads(pathlib.Path("/input/host-sentinel.json").read_text())
try: pathlib.Path(sentinel["path"]).read_bytes()
except (FileNotFoundError, PermissionError): pass
else: raise AssertionError("host sentinel was mounted")
for path in ["/input/forbidden", "/etc/forbidden"]:
    try: pathlib.Path(path).write_text("escape")
    except OSError: pass
    else: raise AssertionError("writable trusted filesystem")
for family in [socket.AF_INET, socket.AF_INET6, socket.AF_UNIX]:
    try: connection = socket.socket(family, socket.SOCK_STREAM)
    except PermissionError: pass
    else:
        connection.close()
        raise AssertionError("socket creation allowed")
x = np.arange(20, dtype=np.float32).reshape(-1, 1)
y = 2 * x[:, 0] + 3
fit = Ridge(alpha=0.01).fit(x, y)
assert abs(float(fit.coef_[0]) - 2) < 0.001
assert float(pearsonr(x[:, 0], y).statistic) > 0.999
value = torch.arange(8, dtype=torch.float32)
save_file({"value": value}, "cpu.safetensors")
assert torch.equal(load_file("cpu.safetensors")["value"], value)
pathlib.Path("cpu.safetensors").unlink()
pathlib.Path("result.json").write_text(json.dumps({
    "cpu_job": True, "network_denied": True, "gpu_unavailable": True,
    "host_path_denied": True, "trusted_paths_readonly": True,
    "credentials_absent": True, "numpy_version": np.__version__, "torch_version": torch.__version__,
}))
'''

_BOUNDS_CODE = '''import errno, json, pathlib, subprocess, sys
events = pathlib.Path("/sys/fs/cgroup/pids.events")
def maximum_events():
    return int(dict(line.split() for line in events.read_text().splitlines())["max"])
before = maximum_events()
children = []
try:
    try:
        for _ in range(100):
            children.append(subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(20)"]))
    except OSError as error:
        assert error.errno == errno.EAGAIN
        assert maximum_events() > before
    else: raise AssertionError("PID limit ignored")
finally:
    for child in children:
        if child.poll() is None: child.kill()
    for child in children: child.wait()
denied = False
with open("pressure.bin", "wb") as stream:
    try:
        for _ in range(100):
            stream.write(b"x" * 65536)
            stream.flush()
    except OSError as error:
        assert error.errno in (errno.ENOSPC, errno.EFBIG)
        denied = True
assert denied and pathlib.Path("pressure.bin").stat().st_size <= 1048576
pathlib.Path("pressure.bin").unlink()
pathlib.Path("result.json").write_text(json.dumps({"pid_limit_enforced": True, "output_limit_enforced": True}))
'''

_MEMORY_CODE = '''print("PROBE_MEMORY_STARTED", flush=True)
try: value = bytearray(1024 * 1024 * 1024)
except MemoryError:
    print("PROBE_MEMORY_REFUSED", flush=True)
    raise SystemExit(42)
print("PROBE_MEMORY_UNBOUNDED", flush=True)
'''

_TIME_CODE = '''print("PROBE_TIME_STARTED", flush=True)
while True: pass
'''


def _write_report(path: Path, report: dict):
    parent = path.parent
    metadata = parent.stat()
    if parent.resolve() != parent or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise AcceptanceError("report parent must be a trusted, owned directory without symlinks")
    if path.exists() or path.is_symlink():
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid() or current.st_nlink != 1:
            raise AcceptanceError("report must be an owned regular file")
    descriptor, temporary = tempfile.mkstemp(prefix=".acceptance-", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _image_identity(sandbox: PodmanSandbox) -> str:
    result = subprocess.run(
        sandbox._command("image", "inspect", "--format", "{{.Id}}", sandbox.image),
        capture_output=True, env=sandbox._environment(), timeout=15, check=False,
    )
    identity = result.stdout.decode("ascii").strip()
    if not identity.startswith("sha256:"):
        identity = "sha256:" + identity
    if result.returncode or identity != sandbox.image:
        error = AcceptanceError("the requested immutable image is not present in the service Podman store")
        error.private_diagnostics = {"returncode": result.returncode,
                                     "stdout": _bounded_diagnostic(result.stdout),
                                     "stderr": _bounded_diagnostic(result.stderr)}
        raise error
    return identity


def _json_result(result: SandboxResult, expected: set[str]) -> dict:
    if result.returncode != 0 or result.termination_reason is not None:
        raise AcceptanceError("contained check did not finish successfully")
    if len(result.artifacts) != 1 or result.artifacts[0].name != "result.json":
        raise AcceptanceError("contained check did not return its exact result artifact")
    if result.artifacts[0].stat().st_size > 4096:
        raise AcceptanceError("contained result exceeds its bound")
    payload = json.loads(result.artifacts[0].read_bytes())
    if not isinstance(payload, dict) or any(payload.get(name) is not True for name in expected):
        raise AcceptanceError("contained check did not prove every required condition")
    return payload


def run_acceptance(*, image: str, workspace: Path, output: Path,
                   podman: str = "podman", seccomp_profile: Path | None = None) -> dict:
    """Run actual containers and durably replace the report, failing on any gap."""
    output = Path(output).absolute()
    report = {"schema_version": 1, "status": "in_progress", "image": image,
              "service_uid": os.getuid(), "started_at": datetime.now(timezone.utc).isoformat(),
              "stage": "configuration", "checks": {}}
    _write_report(output, report)
    scratch: Path | None = None
    try:
        root = Path(workspace).absolute()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.resolve() != root or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise AcceptanceError("acceptance workspace must be private, owned and without symlinks")
        scratch = Path(tempfile.mkdtemp(prefix="acceptance-", dir=root))
        sandbox = _RecordedSandbox(image=image, workspace=scratch, podman=podman, seccomp_profile=seccomp_profile)
        profile = sandbox.seccomp_profile.read_bytes()
        if profile != files("probe_core").joinpath("resources/seccomp.json").read_bytes():
            raise AcceptanceError("acceptance requires the packaged reviewed seccomp profile")
        report["seccomp_sha256"] = "sha256:" + hashlib.sha256(profile).hexdigest()
        report["stage"] = "runtime"
        sandbox.check_runtime()
        report["image"] = _image_identity(sandbox)
        report["checks"]["immutable_image_present"] = True

        def run(stage: str, code: str, limits: SandboxLimits, *, inputs=None):
            report["stage"] = stage
            before = len(sandbox.attestations)
            started = time.monotonic()
            info = {"limits": asdict(limits), "returncode": None, "termination_reason": None,
                    "elapsed_seconds": None, "attestation_count": 0}
            report.setdefault("runs", {})[stage] = info
            try:
                result = sandbox.run(code, limits=limits, inputs=inputs)
                info.update(returncode=result.returncode, termination_reason=result.termination_reason,
                            private_diagnostics={"stdout": _bounded_diagnostic(result.stdout),
                                                 "stderr": _bounded_diagnostic(result.stderr)})
            except BaseException as error:
                info["private_diagnostics"] = _exception_diagnostics(error)
                raise
            finally:
                info["elapsed_seconds"] = round(time.monotonic() - started, 3)
                info["attestation_count"] = len(sandbox.attestations) - before
                if info["attestation_count"] == 1:
                    info["attestation"] = sandbox.attestations[-1]
            if info["attestation_count"] != 1:
                raise AcceptanceError("check lacks exactly one accepted runtime attestation")
            return result

        sentinel = scratch / "unmounted-host-sentinel"
        sentinel.write_text("this file must not enter the container\n")
        sentinel.chmod(0o600)
        cpu_checks = {"cpu_job", "network_denied", "gpu_unavailable", "host_path_denied",
                      "trusted_paths_readonly", "credentials_absent"}
        result = run("cpu_and_isolation", _CPU_CODE,
                     SandboxLimits(wall_seconds=60, max_output_bytes=1048576, max_broker_requests=0),
                     inputs={"host-sentinel.json": json.dumps({"path": str(sentinel)}).encode()})
        cpu = _json_result(result, cpu_checks)
        report["checks"].update({name: True for name in cpu_checks})
        report["cpu_libraries"] = {name: cpu[name] for name in ("numpy_version", "torch_version")}

        bounds_checks = {"pid_limit_enforced", "output_limit_enforced"}
        result = run("pid_and_output", _BOUNDS_CODE, SandboxLimits(
            wall_seconds=20, memory_bytes=256 * 1024**2, pids=16,
            max_output_bytes=1048576, max_broker_requests=0))
        _json_result(result, bounds_checks)
        report["checks"].update({name: True for name in bounds_checks})

        result = run("memory", _MEMORY_CODE, SandboxLimits(
            wall_seconds=15, memory_bytes=128 * 1024**2, max_broker_requests=0))
        if (result.termination_reason is not None or result.artifacts
                or "PROBE_MEMORY_STARTED" not in result.stdout
                or "PROBE_MEMORY_UNBOUNDED" in result.stdout
                or not (result.returncode == 137 or
                        result.returncode == 42 and "PROBE_MEMORY_REFUSED" in result.stdout)):
            raise AcceptanceError("memory pressure was not refused after verified startup")
        report["checks"]["memory_limit_enforced"] = True

        # The core budget includes container startup. Give a cold rootless
        # runtime enough time to attest and release the deliberately busy job.
        result = run("wall_time", _TIME_CODE, SandboxLimits(wall_seconds=10, max_broker_requests=0))
        if (result.termination_reason != "wall_time_limit" or result.artifacts
                or "PROBE_TIME_STARTED" not in result.stdout
                or report["runs"]["wall_time"]["elapsed_seconds"] >= 45):
            raise AcceptanceError("wall time did not terminate a verified running job within the cleanup bound")
        report["checks"].update({"wall_time_enforced": True, "runtime_attestation": True,
                                 "cpu_cgroup_limit_attested": True, "container_removal_confirmed": True})
        report["stage"] = "complete"
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        report["reason"] = _safe_reason(error)
        # Only the fixed synthetic acceptance programs run here. Keep bounded
        # launch diagnostics in this owned 0600 receipt, never in public output.
        report["private_diagnostics"] = _exception_diagnostics(error)
        error.acceptance_stage = report["stage"]
        raise
    finally:
        if scratch is not None:
            shutil.rmtree(scratch)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(output, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Exact immutable local sha256 image ID")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--podman", default="podman")
    parser.add_argument("--seccomp-profile", type=Path)
    parser.add_argument("--output", required=True, type=Path, help="Private durable JSON result file")
    args = parser.parse_args(argv)
    try:
        report = run_acceptance(**vars(args))
    except Exception as error:
        stage = getattr(error, "acceptance_stage", "configuration")
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                          "stage": stage if stage in _STAGES else "configuration",
                          "reason": _safe_reason(error)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": report["status"], "image": report["image"], "checks": report["checks"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
