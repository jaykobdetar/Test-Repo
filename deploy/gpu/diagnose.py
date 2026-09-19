"""No-model, no-inference GPU/cgroup measurement. Never starts/stops cloud resources."""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import uuid


def _command(arguments):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=15, check=False,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"})
        return {"returncode": result.returncode, "stdout": result.stdout[:16384], "stderr": result.stderr[:4096]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": type(exc).__name__}


def cgroup_probe(root: Path):
    """Create an empty temporary child; never move or reconfigure host processes."""
    result = {"root": str(root), "passed": False}
    child = None
    try:
        if root.is_symlink():
            raise ValueError("cgroup root is a symlink")
        # Linux statfs starts with f_type. Use a larger opaque buffer to avoid
        # depending on libc's remaining platform-specific structure members.
        buffer = ctypes.create_string_buffer(256)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.statfs(os.fsencode(root), ctypes.byref(buffer)) != 0:
            raise OSError(ctypes.get_errno(), "statfs failed")
        filesystem_type = ctypes.c_long.from_buffer(buffer).value
        result["filesystem_type"] = hex(filesystem_type)
        if filesystem_type != 0x63677270:
            raise ValueError("path is not a real cgroup-v2 filesystem")
        result["controllers"] = (root / "cgroup.controllers").read_text().split()
        result["subtree_control"] = (root / "cgroup.subtree_control").read_text().split()
        if not {"cpu", "memory", "pids"} <= set(result["subtree_control"]):
            raise ValueError("cpu, memory, and pids are not delegated to children")
        child = root / ("probe-preflight-" + uuid.uuid4().hex)
        child.mkdir(mode=0o700)
        expected = {"memory.max": str(1024**3), "memory.swap.max": "0", "pids.max": "16", "cpu.max": "50000 100000"}
        for name, value in expected.items():
            (child / name).write_text(value)
        actual = {name: (child / name).read_text().strip() for name in expected}
        result["limits_read_back"] = actual
        if actual != expected or (child / "cgroup.procs").read_text().strip():
            raise ValueError("cgroup readback mismatch or unexpected process membership")
        result["kill_interface"] = (child / "cgroup.kill").exists()
        result["passed"] = True
    except (OSError, ValueError) as exc:
        result["error_type"] = type(exc).__name__
        result["reason"] = str(exc)[:1024]
    finally:
        if child is not None:
            try:
                child.rmdir()
            except OSError as exc:
                result["passed"] = False
                result["cleanup_error"] = type(exc).__name__
    return result


def diagnose(cgroup_root: Path):
    smi = _command(["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total,compute_cap", "--format=csv,noheader,nounits"])
    gpu_rows = [row.strip().split(", ") for row in smi.get("stdout", "").splitlines() if row.strip()]
    gpu_passed = len(gpu_rows) == 1 and len(gpu_rows[0]) == 5 and smi.get("returncode") == 0
    if gpu_passed:
        try:
            gpu_passed = int(gpu_rows[0][2].split(".")[0]) >= 580 and float(gpu_rows[0][4]) >= 8.0
        except ValueError:
            gpu_passed = False
    cgroup = cgroup_probe(cgroup_root)
    return {"schema_version": 1, "kind": "infrastructure_preflight", "scientific_evidence": False,
            "observed_at": datetime.now(timezone.utc).isoformat(), "kernel": platform.release(),
            "machine": platform.machine(), "uid": os.geteuid(), "pid": os.getpid(),
            "script_sha256": "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "nvidia_smi": smi, "cuda13_native_bf16_hardware_passed": gpu_passed,
            "cgroup": cgroup, "worker_prerequisites_passed": gpu_passed and cgroup["passed"],
            "provider_stop_verified": False,
            "stop_note": "Only the separate controller/provider readback can verify paid compute stopped."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = diagnose(args.cgroup_root)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            stream.write(encoded)
    print(encoded, end="")
    raise SystemExit(0 if report["worker_prerequisites_passed"] else 2)


if __name__ == "__main__":
    main()
