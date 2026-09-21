"""Subdivide a provider-delegated, private Pod cgroup before worker startup.

This stdlib-only module can also run as an uploaded standalone script. It never
mounts a filesystem, changes provider resource limits, signals processes, or
reuses an existing subtree. A partial failure requires a fresh Pod; it does not
roll processes back or grant permission to begin numerical execution.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import stat


WORKER_UID = 10001
_CONTROLLERS = frozenset({"cpu", "memory", "pids"})
_DELEGATE_FILES = ("cgroup.procs", "cgroup.threads", "cgroup.subtree_control")
_LIMIT_FILES = ("cpu.max", "memory.max", "memory.swap.max", "pids.max")
_NAMESPACES = ("pid", "cgroup", "user", "mnt")
_BOOTSTRAP = "probe-bootstrap"
_JOBS = "probe-jobs"
_SUPERVISOR = "probe-jobs/supervisor"
_MAX_MEMBERS = 256


class BootstrapRefused(RuntimeError):
    """The verified private delegation required for this profile is absent."""


def _require(condition, message):
    if not condition:
        raise BootstrapRefused(message)


def _read_fd(fd, limit=65536):
    value = bytearray()
    while len(value) <= limit:
        chunk = os.read(fd, min(4096, limit + 1 - len(value)))
        if not chunk:
            return bytes(value).decode("utf-8", errors="strict")
        value.extend(chunk)
    raise BootstrapRefused("kernel observation exceeded its size limit")


def _proc_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        return _read_fd(fd, 1024 * 1024)
    finally:
        os.close(fd)


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _validate_mount(text, root):
    cgroup_mounts = []
    for line in text.splitlines():
        fields = line.split()
        _require("-" in fields, "malformed mount table")
        separator = fields.index("-")
        _require(separator >= 6 and len(fields) >= separator + 4, "malformed mount table")
        filesystem = fields[separator + 1]
        if filesystem in {"cgroup", "cgroup2"}:
            cgroup_mounts.append((fields, separator))
        mountpoint = _unescape_mount(fields[4])
        _require(not mountpoint.startswith(root + "/"), "nested mounts obscure the cgroup scope")
    _require(len(cgroup_mounts) == 1, "exactly one private unified cgroup mount is required")
    fields, separator = cgroup_mounts[0]
    _require(fields[separator + 1] == "cgroup2" and _unescape_mount(fields[3]) == "/"
             and _unescape_mount(fields[4]) == root, "the selected path is not the private cgroup2 mount root")
    _require("rw" in fields[5].split(",") and "ro" not in fields[5].split(",")
             and {"rw", "nsdelegate"} <= set(fields[separator + 3].split(",")),
             "a writable namespace-delegated cgroup2 mount is required")


def _mapped_identity(text, worker_id):
    rows = [line.split() for line in text.splitlines() if line.strip()]
    _require(len(rows) == 1 and len(rows[0]) == 3 and all(part.isdecimal() for part in rows[0]),
             "a single explicit remapped Pod identity range is required")
    inside, outside, length = map(int, rows[0])
    _require(inside == 0 and outside > 0 and worker_id < length <= 2**32 - outside,
             "host-global or insufficient identity mapping refused")


def _namespaces(pid):
    return tuple((name, os.readlink(f"/proc/{pid}/ns/{name}")) for name in _NAMESPACES)


def _process(pid, namespaces, membership):
    """Observe through one proc-directory descriptor; do not claim atomic migration."""
    fd = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        def read(name):
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                return _read_fd(child)
            finally:
                os.close(child)
        first = read("stat")
        _require(tuple((name, os.readlink("ns/" + name, dir_fd=fd)) for name in _NAMESPACES) == namespaces,
                 "foreign-namespace cgroup member refused")
        _require(read("cgroup") == "0::" + membership + "\n", "cgroup member moved during bootstrap")
        last = read("stat")
        def identity(value):
            prefix, suffix = value.rsplit(")", 1)
            _require(prefix.split(" (", 1)[0] == str(pid), "process identity mismatch")
            fields = suffix.split()
            _require(len(fields) > 19 and fields[19].isdecimal(), "invalid process start identity")
            return int(fields[19])
        _require(identity(first) == identity(last), "process identity changed during observation")
        return identity(last)
    finally:
        os.close(fd)


class _Scope:
    """All writes stay beneath the verified mount descriptor and fixed names."""
    def __init__(self, root):
        path = Path(root)
        _require(path.is_absolute() and str(path) == str(root) and ".." not in path.parts,
                 "cgroup root must be a canonical absolute path")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in path.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise
        self.root = str(path)

    def close(self):
        os.close(self.fd)

    @contextmanager
    def directory(self, relative=""):
        fd = os.dup(self.fd)
        try:
            if relative:
                _require(all(part not in {"", ".", ".."} for part in relative.split("/")), "invalid relative scope")
                for part in relative.split("/"):
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def file(self, relative, flags=os.O_RDONLY):
        parent, _, name = relative.rpartition("/")
        with self.directory(parent) as directory:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                _require(stat.S_ISREG(os.fstat(fd).st_mode), "cgroup control must be a regular kernel file")
                yield fd
            finally:
                os.close(fd)

    def read(self, relative):
        with self.file(relative) as fd:
            return _read_fd(fd)

    def write(self, relative, value):
        _require(relative in {_BOOTSTRAP + "/cgroup.procs", "cgroup.subtree_control",
                              _JOBS + "/cgroup.subtree_control", _SUPERVISOR + "/cgroup.procs"},
                 "control write is outside the fixed bootstrap allowlist")
        data = (value + "\n").encode("ascii")
        with self.file(relative, os.O_WRONLY) as fd:
            _require(os.write(fd, data) == len(data), "cgroup control write was incomplete")

    def mkdir(self, relative):
        parent, _, name = relative.rpartition("/")
        with self.directory(parent) as fd:
            os.mkdir(name, 0o755, dir_fd=fd)

    def info(self, relative=""):
        if relative:
            with self.file(relative) as fd:
                return os.fstat(fd)
        return os.fstat(self.fd)

    def directory_identity(self, relative=""):
        with self.directory(relative) as fd:
            info = os.fstat(fd)
            return (info.st_dev, info.st_ino)

    def children(self):
        return [name for name in os.listdir(self.fd)
                if stat.S_ISDIR(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)]

    def delegate(self, relative, uid, gid):
        with self.directory(relative) as fd:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, 0o755)
            info = os.fstat(fd)
            _require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (uid, gid, 0o755),
                     "delegated directory permission readback failed")
        for name in _DELEGATE_FILES:
            with self.file(relative + "/" + name) as fd:
                os.fchown(fd, uid, gid)
                os.fchmod(fd, 0o644)
                info = os.fstat(fd)
                _require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (uid, gid, 0o644),
                         "delegation interface permission readback failed")


def _members(scope):
    words = scope.read("cgroup.procs").split()
    _require(len(words) <= _MAX_MEMBERS and all(word.isdecimal() and int(word) > 0 for word in words),
             "unresolved or excessive cgroup members refused")
    return set(map(int, words))


def _validate_scope(scope, uid, gid):
    _require(os.getresuid() == (0, 0, 0) and os.getresgid() == (0, 0, 0), "Pod root is required for startup delegation")
    _require(uid == WORKER_UID and gid == WORKER_UID, "the fixed worker identity is required")
    _validate_mount(_proc_read("/proc/self/mountinfo"), scope.root)
    library = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(256)
    _require(library.fstatfs(scope.fd, ctypes.byref(buffer)) == 0
             and ctypes.c_long.from_buffer(buffer).value == 0x63677270, "actual cgroup2 filesystem required")
    namespaces = _namespaces("self")
    _require(namespaces == _namespaces(1), "Pod PID1 must share the current private namespaces")
    for filename, worker_id in (("uid_map", uid), ("gid_map", gid)):
        mapping = _proc_read("/proc/self/" + filename)
        _mapped_identity(mapping, worker_id)
        _require(mapping == _proc_read("/proc/1/" + filename), "PID1 must belong to this remapped Pod identity")
    # user.delegate is an optional userspace convention, not the kernel's
    # delegation contract. The private nsdelegate mount and the actual ownership
    # and write-denial checks below establish the boundary on this Pod.
    info = scope.info()
    _require(info.st_uid == 0 and not info.st_mode & 0o022, "delegated root ownership is unsafe")
    for name in _DELEGATE_FILES:
        info = scope.info(name)
        _require(info.st_uid == 0 and not info.st_mode & 0o022, "delegation interface ownership is unsafe")
        with scope.file(name, os.O_WRONLY):
            pass  # Test delegation access without writing any control bytes.
    _require(scope.read("cgroup.type").strip() == "domain", "domain cgroup required")
    _require(_CONTROLLERS <= set(scope.read("cgroup.controllers").split()), "required resource controllers unavailable")
    _require(not scope.read("cgroup.subtree_control").strip() and not scope.children(),
             "bootstrap requires a fresh unconfigured Pod subtree")
    limits = {}
    for name in _LIMIT_FILES:
        info = scope.info(name)
        _require(info.st_uid not in {0, uid} and not info.st_mode & 0o022, "provider outer limits must remain externally owned")
        limits[name] = scope.read(name)
        try:
            with scope.file(name, os.O_WRONLY):
                pass
        except PermissionError:
            pass
        else:
            raise BootstrapRefused("provider outer limit is writable from inside this Pod")
    members = _members(scope)
    _require({1, os.getpid()} <= members, "PID1 and bootstrap must occupy the delegated root initially")
    identities = {pid: _process(pid, namespaces, "/") for pid in members}
    return namespaces, identities, limits


@dataclass(frozen=True)
class PreparedScope:
    root: str
    bootstrap_leaf: str
    jobs_root: str
    supervisor_leaf: str
    worker_uid: int
    worker_gid: int
    namespaces: tuple[tuple[str, str], ...]
    root_identity: tuple[int, int]
    supervisor_identity: tuple[int, int]
    moved_pids: tuple[int, ...]


def prepare(root="/sys/fs/cgroup", *, worker_uid=WORKER_UID, worker_gid=WORKER_UID):
    """Prepare one fresh private Pod; never reuse or roll back a partial subtree."""
    scope = _Scope(root)
    try:
        namespaces, identities, limits = _validate_scope(scope, worker_uid, worker_gid)
        scope.mkdir(_BOOTSTRAP)
        moved = []
        # PID1 moves first so its new children inherit the bootstrap leaf. The
        # caller moves last. Numeric cgroup migration has no pidfd API: every PID
        # is checked immediately before/after; the private PID namespace bounds
        # it to this Pod. Any unresolved drift aborts instead of claiming success.
        for round_number in range(8):
            if round_number:
                members = _members(scope)
                if not members:
                    break
                identities = {pid: _process(pid, namespaces, "/") for pid in members}
            ordered = sorted(identities, key=lambda pid: (pid == os.getpid(), pid != 1, pid))
            for pid in ordered:
                _require(_process(pid, namespaces, "/") == identities[pid], "member identity changed before migration")
                scope.write(_BOOTSTRAP + "/cgroup.procs", "0" if pid == os.getpid() else str(pid))
                _require(_process(pid, namespaces, "/" + _BOOTSTRAP) == identities[pid], "member identity changed after migration")
                moved.append(pid)
        _require(not _members(scope), "root membership did not settle within the bounded migration rounds")
        scope.write("cgroup.subtree_control", "+cpu +memory +pids")
        _require(set(scope.read("cgroup.subtree_control").split()) == _CONTROLLERS, "root controller readback failed")
        scope.mkdir(_JOBS)
        _require(not scope.read(_JOBS + "/cgroup.procs").strip(), "jobs ancestor must remain empty")
        _require(_CONTROLLERS <= set(scope.read(_JOBS + "/cgroup.controllers").split()),
                 "jobs controllers were not inherited")
        scope.write(_JOBS + "/cgroup.subtree_control", "+cpu +memory +pids")
        _require(set(scope.read(_JOBS + "/cgroup.subtree_control").split()) == _CONTROLLERS, "jobs controller readback failed")
        scope.mkdir(_SUPERVISOR)
        _require(scope.read(_SUPERVISOR + "/cgroup.type").strip() == "domain"
                 and not scope.read(_SUPERVISOR + "/cgroup.subtree_control").strip()
                 and not scope.read(_SUPERVISOR + "/cgroup.procs").strip(), "supervisor must be a fresh empty leaf")
        scope.delegate(_SUPERVISOR, worker_uid, worker_gid)
        scope.delegate(_JOBS, worker_uid, worker_gid)
        _require(not _members(scope) and not scope.read(_JOBS + "/cgroup.procs").strip(),
                 "an internal process appeared during controller delegation")
        _require(all(scope.info(_JOBS + "/" + name).st_uid == 0 for name in _LIMIT_FILES),
                 "job ancestor resource limits must remain root-owned")
        _require(all(scope.read(name) == value for name, value in limits.items()), "provider outer limits changed during bootstrap")
        return PreparedScope(scope.root, scope.root + "/" + _BOOTSTRAP, scope.root + "/" + _JOBS,
                             scope.root + "/" + _SUPERVISOR, worker_uid, worker_gid, namespaces,
                             scope.directory_identity(), scope.directory_identity(_SUPERVISOR), tuple(moved))
    finally:
        scope.close()


def enter_supervisor_and_drop(prepared):
    """Call only in the freshly forked trusted child immediately before exec.

    The launcher must separately supply an allowlisted environment and private
    worker inputs. This helper does not copy secrets, extend deadlines or replay
    requests. It never moves its parent or any PID supplied by a caller.
    """
    _require(isinstance(prepared, PreparedScope) and prepared.worker_uid == WORKER_UID
             and prepared.worker_gid == WORKER_UID and os.getresuid() == (0, 0, 0), "trusted prepared worker scope required")
    _require(_namespaces("self") == prepared.namespaces, "prepared Pod namespace changed")
    scope = _Scope(prepared.root)
    try:
        _require(scope.directory_identity() == prepared.root_identity
                 and scope.directory_identity(_SUPERVISOR) == prepared.supervisor_identity,
                 "prepared cgroup identity changed")
        scope.write(_SUPERVISOR + "/cgroup.procs", "0")
        _process(os.getpid(), prepared.namespaces, "/" + _SUPERVISOR)
    finally:
        scope.close()
    os.setgroups([])
    os.setresgid(prepared.worker_gid, prepared.worker_gid, prepared.worker_gid)
    os.setresuid(prepared.worker_uid, prepared.worker_uid, prepared.worker_uid)
    library = ctypes.CDLL(None, use_errno=True)
    _require(library.prctl(38, 1, 0, 0, 0) == 0, "no_new_privs could not be set")
    _require(os.getresuid() == (WORKER_UID,) * 3 and os.getresgid() == (WORKER_UID,) * 3 and not os.getgroups(),
             "worker privilege drop failed")
    status = dict(line.split(":", 1) for line in _proc_read("/proc/self/status").splitlines() if ":" in line)
    _require(status.get("NoNewPrivs", "").strip() == "1"
             and all(int(status.get(name, "-1").strip(), 16) == 0 for name in ("CapInh", "CapPrm", "CapEff", "CapAmb")),
             "worker retained capabilities after privilege drop")


def main():
    parser = argparse.ArgumentParser(description="Prepare a fresh provider-delegated private Pod cgroup")
    parser.add_argument("--root", default="/sys/fs/cgroup")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    prepared = prepare(args.root)
    print(json.dumps({"schema_version": 1, "kind": "pod_cgroup_bootstrap", "prepared": asdict(prepared)}, sort_keys=True), flush=True)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if command:
        _require(os.path.isabs(command[0]), "an absolute trusted command is required")
        enter_supervisor_and_drop(prepared)
        environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/probe-worker",
                       "USER": "probe-worker", "LOGNAME": "probe-worker", "LANG": "C.UTF-8"}
        os.execve(command[0], command, environment)


if __name__ == "__main__":
    main()
