"""No-model, no-inference GPU/cgroup measurement. Never starts/stops cloud resources."""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import uuid

WORKER_UID = 10001
READ_LIMIT = 8192


def _error(exc):
    return {"error_type": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "reason": str(exc)[:1024]}


def _directory(path):
    """Open each path component separately; never follow a parent symlink."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("cgroup path must be absolute without parent traversal")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                      dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _filesystem_type(descriptor):
    buffer = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(descriptor, ctypes.byref(buffer)) != 0:
        raise OSError(ctypes.get_errno(), "fstatfs failed")
    return ctypes.c_long.from_buffer(buffer).value


def _metadata(info):
    return {"uid": info.st_uid, "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode)),
            "regular_file": stat.S_ISREG(info.st_mode), "directory": stat.S_ISDIR(info.st_mode)}


def _read_at(descriptor, name, limit=READ_LIMIT):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=descriptor)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("inspection requires a regular control file")
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        return {**_metadata(info), "text": content[:limit].decode("utf-8", errors="replace"),
                "truncated": len(content) > limit}
    finally:
        os.close(fd)


def _proc_file(name, limit=READ_LIMIT):
    fd = None
    try:
        fd = _directory(Path("/proc") / str(os.getpid()))
        return _read_at(fd, name, limit)
    except (OSError, ValueError) as exc:
        return _error(exc)
    finally:
        if fd is not None:
            os.close(fd)


def _mounts(text, path):
    """Keep only cgroup mounts containing the proposed path, not other mounts."""
    result = []
    unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        fields, after = left.split(), right.split()
        if not separator or len(fields) < 6 or len(after) < 3 or after[0] != "cgroup2":
            continue
        mountpoint = unescape(fields[4])
        if path != mountpoint and not path.startswith(mountpoint.rstrip("/") + "/"):
            continue
        result.append({"mount_id": fields[0], "root": unescape(fields[3]), "mountpoint": mountpoint,
                       "mount_options": fields[5].split(","), "optional_fields": fields[6:],
                       "super_options": after[2].split(",")})
    return result


def _identity():
    status = _proc_file("status")
    keys = {"CapInh", "CapPrm", "CapEff", "CapAmb", "NoNewPrivs", "Seccomp", "NSpid"}
    values = {key: value.strip() for line in status.get("text", "").splitlines()
              for key, colon, value in [line.partition(":")] if colon and key in keys}
    return {"resuid": list(os.getresuid()), "resgid": list(os.getresgid()),
            "groups": os.getgroups(), "process_status": values,
            "status_complete": "text" in status and not status["truncated"]}


def _is_worker(identity):
    return (type(identity) is dict and type(identity.get("process_status")) is dict
            and identity.get("resuid") == [WORKER_UID]*3 and identity.get("resgid") == [WORKER_UID]*3
            and identity.get("groups") == [] and identity.get("status_complete") is True
            and all(identity["process_status"].get(key) == "0000000000000000"
                    for key in ("CapInh", "CapPrm", "CapEff", "CapAmb")))


def inspect_cgroup(root: Path, *, require_worker=False):
    """Observe permissions only. Opening for write never writes or truncates."""
    identity = _identity()
    result = {"root": str(root), "identity": identity, "worker_identity_verified": _is_worker(identity),
              "inspection_only": True, "control_bytes_written": 0, "exclusive_ownership_verified": False}
    if require_worker and not result["worker_identity_verified"]:
        return {**result, "error_type": "IdentityMismatch", "reason": "inspection did not run as the unprivileged worker"}
    result["process_cgroup"] = _proc_file("cgroup")
    result["uid_map"] = _proc_file("uid_map")
    result["gid_map"] = _proc_file("gid_map")
    mountinfo = _proc_file("mountinfo", 1024*1024)
    result["mounts"] = _mounts(mountinfo.get("text", ""), str(root))
    result["mountinfo_complete"] = "text" in mountinfo and not mountinfo["truncated"]
    result["namespaces"] = {}
    for who, pid in (("self", os.getpid()), ("pid1", 1)):
        result["namespaces"][who] = {}
        for namespace in ("cgroup", "mnt", "pid", "user"):
            try:
                value = os.readlink(f"/proc/{pid}/ns/{namespace}")
            except OSError as exc:
                value = _error(exc)
            result["namespaces"][who][namespace] = value
    descriptor = None
    try:
        descriptor = _directory(root)
        result["directory"] = _metadata(os.fstat(descriptor))
        result["filesystem_type"] = hex(_filesystem_type(descriptor))
        if result["filesystem_type"] != "0x63677270":
            raise ValueError("path is not a real cgroup-v2 filesystem")
        result["mount_readonly"] = bool(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY)
        try:
            result["user_delegate"] = os.getxattr(descriptor, "user.delegate").decode(errors="replace")[:256]
        except OSError as exc:
            result["user_delegate"] = _error(exc)
        result["files"] = {}
        for name in ("cgroup.type", "cgroup.controllers", "cgroup.subtree_control", "cgroup.procs",
                     "cgroup.threads", "cgroup.events", "memory.max", "memory.swap.max", "cpu.max", "pids.max"):
            try:
                result["files"][name] = _read_at(descriptor, name)
            except (OSError, ValueError) as exc:
                result["files"][name] = _error(exc)
        result["open_for_write_without_write"] = {}
        for name in ("cgroup.subtree_control", "cgroup.procs", "cgroup.threads"):
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("control path is not a regular file")
                fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=descriptor)
                os.close(fd)
                result["open_for_write_without_write"][name] = {"opened": True}
            except (OSError, ValueError) as exc:
                result["open_for_write_without_write"][name] = {"opened": False, **_error(exc)}
    except (OSError, ValueError) as exc:
        result.update(_error(exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


def worker_inspection(root: Path, current):
    if current["worker_identity_verified"]:
        return current
    if os.geteuid() != 0:
        return {"worker_identity_verified": False, "inspection_only": True, "error_type": "WorkerIdentityUnavailable"}
    try:
        child = subprocess.run([sys.executable, "-I", str(Path(__file__).absolute()),
                                "--inspect-cgroup-only", "--cgroup-root", str(root)],
                               user=WORKER_UID, group=WORKER_UID, extra_groups=(), cwd="/",
                               env={"PATH": "/usr/bin:/bin", "LANG": "C"}, stdin=subprocess.DEVNULL,
                               close_fds=True, capture_output=True, text=True, timeout=10, check=False)
        if child.returncode != 0 or len(child.stdout) > 65536:
            raise ValueError("worker inspection failed or exceeded its output limit")
        result = json.loads(child.stdout)
        if not _is_worker(result["identity"]) or result.get("control_bytes_written") != 0 or result.get("inspection_only") is not True:
            raise ValueError("worker inspection did not confirm its exact identity and read-only scope")
        return result
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        return {"worker_identity_verified": False, "inspection_only": True, **_error(exc)}


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
    created = False
    try:
        if root.is_symlink():
            raise ValueError("cgroup root is a symlink")
        descriptor = _directory(root)
        try:
            filesystem_type = _filesystem_type(descriptor)
        finally:
            os.close(descriptor)
        result["filesystem_type"] = hex(filesystem_type)
        if filesystem_type != 0x63677270:
            raise ValueError("path is not a real cgroup-v2 filesystem")
        result["controllers"] = (root / "cgroup.controllers").read_text().split()
        result["subtree_control"] = (root / "cgroup.subtree_control").read_text().split()
        if not {"cpu", "memory", "pids"} <= set(result["subtree_control"]):
            raise ValueError("cpu, memory, and pids are not delegated to children")
        child = root / ("probe-preflight-" + uuid.uuid4().hex)
        child.mkdir(mode=0o700)
        created = True
        expected = {"memory.max": str(1024**3), "memory.swap.max": "0", "pids.max": "16", "cpu.max": "50000 100000"}
        for name, value in expected.items():
            (child / name).write_text(value)
        actual = {name: (child / name).read_text().strip() for name in expected}
        result["limits_read_back"] = actual
        if actual != expected or (child / "cgroup.procs").read_text().strip():
            raise ValueError("cgroup readback mismatch or unexpected process membership")
        result["kill_interface"] = (child / "cgroup.kill").exists()
        if not result["kill_interface"]:
            raise ValueError("cgroup.kill is required for attempt cleanup")
        result["passed"] = True
    except (OSError, ValueError) as exc:
        result["error_type"] = type(exc).__name__
        result["reason"] = str(exc)[:1024]
    finally:
        if created:
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
            row = gpu_rows[0]
            capability = float(row[4])
            gpu_passed = (row[0] == "NVIDIA GeForce RTX 4090" and int(row[2].split(".")[0]) >= 580
                          and 24000 <= int(row[3]) <= 25000 and math.isfinite(capability) and capability == 8.9)
        except ValueError:
            gpu_passed = False
    inspection = inspect_cgroup(cgroup_root)
    worker = worker_inspection(cgroup_root, inspection)
    cgroup = cgroup_probe(cgroup_root)
    return {"schema_version": 1, "kind": "infrastructure_preflight", "scientific_evidence": False,
            "observed_at": datetime.now(timezone.utc).isoformat(), "kernel": platform.release(),
            "machine": platform.machine(), "uid": os.geteuid(), "pid": os.getpid(),
            "script_sha256": "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "nvidia_smi": smi, "cuda13_native_bf16_hardware_passed": gpu_passed,
            "cgroup_inspection": inspection, "worker_cgroup_inspection": worker,
            "cgroup": cgroup, "worker_prerequisites_passed": bool(gpu_passed and cgroup["passed"]
                and inspection["worker_identity_verified"]),
            "prerequisite_scope": "current process identity only; root access does not certify worker access",
            "provider_stop_verified": False,
            "stop_note": "Only the separate controller/provider readback can verify paid compute stopped."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect-cgroup-only", action="store_true", help="Internal worker-identity inspection; no control writes")
    args = parser.parse_args()
    if args.inspect_cgroup_only:
        print(json.dumps(inspect_cgroup(args.cgroup_root, require_worker=True)))
        return
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
