"""Exercise delegated cgroup-v2 limits with fixed, short, non-GPU workloads.

Run as UID/GID 10001 inside JOBS_ROOT/supervisor. Only newly created private
probe-resource-* children are written or killed. JSON is evidence, not compute
authorization. The caller must independently bound the paid Pod lifetime.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import time
import uuid

UID = 10001
RAM = 128 * 1024 * 1024
WORK_SECONDS = 28
TOTAL_SECONDS = 35


class AcceptanceError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise AcceptanceError(code)


def directory(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "InvalidDirectory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def filesystem(fd):
    buffer = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(fd, ctypes.byref(buffer)):
        raise OSError(ctypes.get_errno(), "fstatfs failed")
    return ctypes.c_long.from_buffer(buffer).value


def read(fd, name, limit=8192):
    control = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    try:
        require(stat.S_ISREG(os.fstat(control).st_mode), "NonregularControl")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(control, limit + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        require(len(data) <= limit, "ControlReadLimit")
        return data.decode("ascii").strip()
    finally:
        os.close(control)


def write(fd, name, value):
    control = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    try:
        require(stat.S_ISREG(os.fstat(control).st_mode), "NonregularControl")
        data = str(value).encode("ascii")
        require(os.write(control, data) == len(data), "ControlShortWrite")
    finally:
        os.close(control)


def counters(fd, name):
    return {key: int(value) for line in read(fd, name).splitlines() for key, value in [line.split()]}


def pidfd_open(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid, 0)
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "pidfd_open", None)
    require(function is not None, "PidfdUnavailable")
    function.argtypes = (ctypes.c_int, ctypes.c_uint)
    function.restype = ctypes.c_int
    fd = function(pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return fd


def enable_subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(36, 1, 0, 0, 0) == 0, "ChildSubreaperUnavailable")


def reap_descendants(deadline):
    # This standalone process creates only these acceptance children. Becoming
    # their subreaper avoids leaving killed grandchildren for a Python PID1.
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            require(time.monotonic() < deadline, "DescendantReapingIncomplete")
            time.sleep(0.01)


def memory_envelope(path):
    """Refuse known insufficient ancestor headroom before the OOM workload."""
    result = []
    for ancestor in (Path(path), *Path(path).parents):
        fd = directory(ancestor)
        try:
            if filesystem(fd) != 0x63677270:
                break
            try:
                maximum, current = read(fd, "memory.max"), int(read(fd, "memory.current"))
            except FileNotFoundError:
                continue  # Actual hierarchy root has no resource limit files.
            result.append({"path":str(ancestor), "memory_max":maximum, "memory_current":current})
            if maximum != "max":
                require(int(maximum)-current >= RAM+32*1024*1024, "InsufficientOuterMemoryHeadroom")
        finally:
            os.close(fd)
    return result


def validate_root(path):
    require(os.getresuid() == (UID,)*3 and os.getresgid() == (UID,)*3 and os.getgroups() == [], "WorkerIdentityRequired")
    proc = directory(f"/proc/{os.getpid()}")
    try:
        status = dict(line.split(":", 1) for line in read(proc, "status").splitlines() if ":" in line)
        require(all(status.get(key, "").strip() == "0000000000000000"
                    for key in ("CapInh", "CapPrm", "CapEff", "CapAmb")), "WorkerCapabilitiesPresent")
    finally:
        os.close(proc)
    root = directory(path)
    try:
        info = os.fstat(root)
        require(filesystem(root) == 0x63677270, "RealCgroupV2Required")
        require(info.st_uid == UID and info.st_gid == UID and not stat.S_IMODE(info.st_mode) & 0o022,
                "PrivateWorkerDelegationRequired")
        require(not os.fstatvfs(root).f_flag & os.ST_RDONLY, "ReadOnlyDelegation")
        require({"cpu", "memory", "pids"} <= set(read(root, "cgroup.subtree_control").split()), "ControllersNotEnabled")
        require(not read(root, "cgroup.procs"), "DelegatedParentPopulated")
        leaf = os.open("supervisor", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root)
        try:
            require(filesystem(leaf) == 0x63677270 and str(os.getpid()) in read(leaf, "cgroup.procs").splitlines(),
                    "SupervisorMembershipRequired")
        finally:
            os.close(leaf)
        return root
    except BaseException:
        os.close(root)
        raise


def wait_until(predicate, deadline, code):
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    require(predicate(), code)


class Scope:
    def __init__(self, root, kind, hard_deadline):
        self.root, self.kind = root, kind
        self.hard_deadline = hard_deadline
        self.name = "probe-resource-" + kind + "-" + uuid.uuid4().hex
        self.fd = None
        self.pid = None
        self.pipe = None
        self.status = None
        self.created = False
        self.started = None

    def prepare(self):
        os.mkdir(self.name, 0o700, dir_fd=self.root)
        self.created = True
        self.fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.root)
        self.inode = os.fstat(self.fd).st_ino
        require(filesystem(self.fd) == 0x63677270, "ChildFilesystemMismatch")
        expected = {"memory.max": str(RAM), "memory.swap.max": "0", "pids.max": "16", "cpu.max": "50000 100000"}
        for name, value in expected.items():
            write(self.fd, name, value)
        require({name:read(self.fd, name) for name in expected} == expected, "LimitReadbackMismatch")
        require(not read(self.fd, "cgroup.procs"), "NewScopeNotEmpty")
        self.limits = expected

    def launch(self):
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
        output_read, output_write = os.pipe2(os.O_CLOEXEC)
        parent = os.getpid()
        try:
            pid = os.fork()
        except BaseException:
            for fd in (ready_read, ready_write, output_read, output_write):
                os.close(fd)
            raise
        if pid == 0:
            os.close(ready_write); os.close(output_read)
            os.close(self.fd); os.close(self.root)
            try:
                child_guard(parent, self.hard_deadline)
                os.setsid()
                require(os.read(ready_read, 1) == b"1", "LaunchBarrierClosed")
                os.close(ready_read)
                workload(self.kind, output_write, self.hard_deadline)
                os._exit(0)
            except BaseException:
                os._exit(91)
        self.pid, self.pipe = pid, output_read
        os.close(ready_read); os.close(output_write)
        try:
            write(self.fd, "cgroup.procs", pid)
            require(str(pid) in read(self.fd, "cgroup.procs").splitlines(), "ChildMembershipMismatch")
            self.started = time.monotonic()
            require(os.write(ready_write, b"1") == 1, "LaunchBarrierFailed")
        finally:
            os.close(ready_write)

    def exited(self):
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = status
        return self.status is not None

    def message(self, deadline):
        data = bytearray()
        while time.monotonic() < deadline:
            if select.select([self.pipe], [], [], max(0, min(0.1, deadline-time.monotonic())))[0]:
                chunk = os.read(self.pipe, 4097-len(data))
                require(chunk, "ChildMessageMissing")
                data.extend(chunk)
                require(len(data) <= 4096, "ChildMessageLimit")
                if b"\n" in data:
                    return json.loads(data.split(b"\n", 1)[0])
        raise AcceptanceError("ChildMessageTimeout")

    def cleanup(self, deadline):
        if self.fd is not None:
            write(self.fd, "cgroup.kill", "1")
            wait_until(lambda:counters(self.fd, "cgroup.events").get("populated") == 0, deadline, "ScopeStillPopulated")
        if self.pid is not None:
            # If attachment failed, the barrier never released a workload.
            # This PID is still our unreaped direct child, hence cannot be reused.
            if not self.exited():
                os.kill(self.pid, signal.SIGKILL)
                wait_until(self.exited, deadline, "ChildStillAlive")
            reap_descendants(deadline)
        if self.created:
            if self.fd is not None:
                require(os.stat(self.name, dir_fd=self.root, follow_symlinks=False).st_ino == self.inode, "ScopeIdentityChanged")
            os.rmdir(self.name, dir_fd=self.root)
            self.created = False

    def close(self):
        for fd in (self.pipe, self.fd):
            if fd is not None:
                os.close(fd)
        self.pipe = self.fd = None


def child_guard(parent, deadline, *, kill_with_parent=True):
    # Both a finite child alarm and parent-death SIGKILL survive loss of the
    # SSH acceptance launcher. Every forked descendant installs its own guard.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    require(deadline > time.monotonic(), "ChildGuardDeadlineExpired")
    signal.setitimer(signal.ITIMER_REAL, deadline-time.monotonic())
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(1, signal.SIGKILL if kill_with_parent else 0, 0, 0, 0) == 0 and os.getppid() == parent,
            "ParentDeathGuardFailed")


def emit(fd, value):
    data = json.dumps(value).encode()+b"\n"
    require(len(data) <= 4096 and os.write(fd, data) == len(data), "ChildMessageWriteFailed")


def workload(kind, output, deadline):
    if kind == "cpu":
        until = time.monotonic()+3
        value = 0
        while time.monotonic() < until:
            value = (value+1) % 1000003
    elif kind == "memory":
        blocks = []
        for _ in range(20):
            block = bytearray(16*1024*1024)
            for page in range(0, len(block), 4096):
                block[page] = 1
            blocks.append(block)
        raise AcceptanceError("MemoryLimitNotEnforced")
    elif kind == "pids":
        children = []
        failure = None
        parent = os.getpid()
        for _ in range(32):
            try:
                pid = os.fork()
            except OSError as exc:
                failure = exc.errno
                break
            if pid == 0:
                os.close(output)
                child_guard(parent, deadline)
                while True:
                    signal.pause()
            children.append(pid)
        emit(output, {"fork_errno":failure, "children":children})
        while True:
            signal.pause()
    elif kind == "descendants":
        leader = os.getpid()
        pid = os.fork()
        if pid == 0:
            # Do not let parent-death cleanup masquerade as cgroup.kill proof.
            # This detached child retains only its independent absolute alarm.
            child_guard(leader, deadline, kill_with_parent=False)
            os.setsid()
            emit(output, {"leader":leader, "descendant":os.getpid(), "descendant_session":os.getsid(0),
                          "descendant_group":os.getpgrp()})
        while True:
            signal.pause()
    else:
        raise AcceptanceError("UnknownWorkload")


def exercise(scope, deadline):
    if scope.kind == "cpu":
        before = counters(scope.fd, "cpu.stat")
        scope.launch()
        wait_until(scope.exited, min(deadline, time.monotonic()+5), "CpuChildTimeout")
        elapsed = time.monotonic()-scope.started
        after = counters(scope.fd, "cpu.stat")
        usage = after["usage_usec"]-before["usage_usec"]
        throttled = after["nr_throttled"]-before["nr_throttled"]
        require(os.waitstatus_to_exitcode(scope.status) == 0 and 2.9 <= elapsed <= 5.1, "CpuWorkloadIncomplete")
        require(throttled > 0 and 0 < usage <= (elapsed*0.5+0.15)*1000000, "CpuThrottleNotObserved")
        return {"elapsed_seconds":elapsed, "usage_usec":usage, "nr_throttled_delta":throttled,
                "cpu_allowance":0.5, "accounting_tolerance_seconds":0.15}
    if scope.kind == "memory":
        before = counters(scope.fd, "memory.events")
        scope.launch()
        wait_until(scope.exited, min(deadline, time.monotonic()+6), "MemoryChildTimeout")
        after = counters(scope.fd, "memory.events")
        killed = after["oom_kill"]-before["oom_kill"]
        require(os.waitstatus_to_exitcode(scope.status) == -signal.SIGKILL and killed > 0, "CgroupOomKillNotObserved")
        return {"memory_limit_bytes":RAM, "oom_kill_delta":killed, "child_exit_signal":"SIGKILL"}
    if scope.kind == "pids":
        before = counters(scope.fd, "pids.events")
        scope.launch()
        message = scope.message(min(deadline, time.monotonic()+4))
        current = int(read(scope.fd, "pids.current"))
        delta = counters(scope.fd, "pids.events")["max"]-before["max"]
        require(message.get("fork_errno") == errno.EAGAIN and len(message.get("children", [])) == 15
                and current == 16 and delta > 0, "PidLimitNotObserved")
        return {"fork_errno":errno.EAGAIN, "children_started":15, "pids_current":current, "max_event_delta":delta}
    scope.launch()
    message = scope.message(min(deadline, time.monotonic()+4))
    leader, descendant = message.get("leader"), message.get("descendant")
    require(leader == scope.pid and type(descendant) is int and descendant not in {os.getpid(), leader}
            and message.get("descendant_session") == descendant and message.get("descendant_group") == descendant,
            "DetachedDescendantNotObserved")
    members = set(read(scope.fd, "cgroup.procs").splitlines())
    require({str(leader), str(descendant)} <= members and str(os.getpid()) not in members, "KillScopeMembershipMismatch")
    descriptors = []
    try:
        for pid in (leader, descendant):
            descriptors.append(pidfd_open(pid))
        require(not select.select(descriptors, [], [], 0)[0], "ChildExitedBeforeKill")
        require(time.monotonic()+5 < scope.hard_deadline, "CleanupGuardTooClose")
        write(scope.fd, "cgroup.kill", "1")
        wait_until(lambda:len(select.select(descriptors, [], [], 0)[0]) == 2
                   and counters(scope.fd, "cgroup.events").get("populated") == 0,
                   min(deadline, time.monotonic()+5), "WholeScopeKillNotObserved")
        return {"leader_pid":leader, "descendant_pid":descendant, "descendant_used_setsid":True,
                "both_pidfds_exited":True, "populated":0, "signal_scope":"cgroup.kill",
                "backup_alarm_excluded":True, "descendant_parent_death_signal_disabled":True}
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def run(path):
    started = time.monotonic()
    deadline = started+WORK_SECONDS
    report = {"schema_version":1, "kind":"resource_acceptance", "scientific_evidence":False,
              "passed":False, "jobs_root":str(path), "worker_uid":os.geteuid(), "checks":[],
              "observed_at":datetime.now(timezone.utc).isoformat(),
              "script_sha256":"sha256:"+hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    root = None
    try:
        root = validate_root(path)
        enable_subreaper()
        report["visible_outer_memory"] = memory_envelope(path)
        for kind in ("cpu", "memory", "pids", "descendants"):
            require(time.monotonic() < deadline, "AcceptanceDeadline")
            scope = Scope(root, kind, started+TOTAL_SECONDS-1)
            result = {"kind":kind, "passed":False, "cleanup_confirmed":False, "cgroup_name":scope.name}
            try:
                scope.prepare()
                result["limits"] = scope.limits
                if kind == "memory":
                    result["outer_memory_before_oom"] = memory_envelope(path)
                result["observations"] = exercise(scope, deadline)
                result["passed"] = True
            except (AcceptanceError, OSError, ValueError, KeyError) as exc:
                result["error_code"] = str(exc) if isinstance(exc, AcceptanceError) else type(exc).__name__
            finally:
                try:
                    scope.cleanup(min(started+TOTAL_SECONDS-1, time.monotonic()+5))
                    result["cleanup_confirmed"] = True
                except (AcceptanceError, OSError, ValueError) as exc:
                    result.update(passed=False, cleanup_error=str(exc) if isinstance(exc, AcceptanceError) else type(exc).__name__)
                scope.close()
                report["checks"].append(result)
            if not result["cleanup_confirmed"]:
                break
        report["passed"] = len(report["checks"]) == 4 and all(item["passed"] and item["cleanup_confirmed"] for item in report["checks"])
    except (AcceptanceError, OSError, ValueError) as exc:
        report["error_code"] = str(exc) if isinstance(exc, AcceptanceError) else type(exc).__name__
    finally:
        if root is not None:
            os.close(root)
        report["elapsed_seconds"] = time.monotonic()-started
        if report["elapsed_seconds"] >= TOTAL_SECONDS:
            report["passed"] = False
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-root", "--cgroup-root", dest="jobs_root", type=Path, default=Path("/sys/fs/cgroup/probe-jobs"))
    args = parser.parse_args()
    def expired(*_):
        raise AcceptanceError("AcceptanceDeadline")
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(TOTAL_SECONDS)
    try:
        report = run(args.jobs_root)
    finally:
        signal.alarm(0)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 2)


if __name__ == "__main__":
    main()
