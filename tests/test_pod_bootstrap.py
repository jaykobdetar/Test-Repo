"""Fault-injected kernel observations; real enforcement is a separate Pod gate."""

from contextlib import contextmanager
import ctypes
from dataclasses import replace
import errno
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from probe_core import pod_bootstrap as bootstrap


MOUNT = "1020 100 0:28 / /sys/fs/cgroup rw,nosuid,nodev,noexec - cgroup2 cgroup rw,nsdelegate\n"
NS = tuple((name, name + ":[4026534001]") for name in bootstrap._NAMESPACES)
LIMITS = {"cpu.max": "1020000 100000\n", "memory.max": "61999996928\n", "memory.swap.max": "0\n", "pids.max": "6656\n"}


class KernelScope:
    """Explicit cgroup filesystem model, never a claim of real kernel enforcement."""

    def __init__(self):
        self.root = "/sys/fs/cgroup"
        self.fd = 123456
        self.members = {"": {1, 5, 41}}
        self.files = {
            **LIMITS,
            "cgroup.type": "domain\n",
            "cgroup.controllers": "cpu memory pids io\n",
            "cgroup.subtree_control": "",
        }
        self.owners = {}
        self.outer_writable = False
        self.marker = b"1"
        self.events = []
        self.closed = False
        self.after_write = lambda *_: None

    def read(self, name):
        if name.endswith("cgroup.procs"):
            return "".join(str(pid) + "\n" for pid in sorted(self.members[name.rpartition("/")[0]]))
        return self.files[name]

    def info(self, name=""):
        uid = self.owners.get(name, 65534 if name in LIMITS else 0)
        return SimpleNamespace(st_uid=uid, st_gid=65534, st_mode=(stat.S_IFREG if name else stat.S_IFDIR) | 0o644)

    @contextmanager
    def file(self, name, flags=os.O_RDONLY):
        if flags & os.O_WRONLY and name in LIMITS and not self.outer_writable:
            raise PermissionError("provider outer limit")
        yield 123457

    def children(self):
        return list(set(self.members) - {""})

    def directory_identity(self, name=""):
        return (28, {"": 1, "probe-jobs/supervisor": 4}.get(name, 2))

    def mkdir(self, name):
        if name in self.members:
            raise FileExistsError(name)
        self.events.append(("mkdir", name))
        self.members[name] = set()
        self.files.update(
            {
                name + "/cgroup.type": "domain\n",
                name + "/cgroup.controllers": "cpu memory pids\n",
                name + "/cgroup.subtree_control": "",
            }
        )

    def write(self, name, value):
        self.events.append(("write", name, value))
        if name.endswith("cgroup.procs"):
            pid = 41 if value == "0" else int(value)
            for members in self.members.values():
                members.discard(pid)
            self.members[name.rpartition("/")[0]].add(pid)
        else:
            self.files[name] = " ".join(word.lstrip("+") for word in value.split()) + "\n"
        self.after_write(name, value)

    def delegate(self, name, uid, gid):
        self.events.append(("delegate", name, uid, gid))
        for file in ("", *bootstrap._DELEGATE_FILES):
            self.owners[name + ("/" + file if file else "")] = uid

    def close(self):
        self.closed = True


@pytest.fixture
def kernel(monkeypatch):
    scope = KernelScope()
    observations = {
        "/proc/self/mountinfo": MOUNT,
        **{f"/proc/{pid}/{name}": "0 100000 65536\n" for pid in ("self", "1") for name in ("uid_map", "gid_map")},
    }
    monkeypatch.setattr(bootstrap, "_Scope", lambda _root: scope)
    monkeypatch.setattr(bootstrap, "_proc_read", observations.__getitem__)
    monkeypatch.setattr(bootstrap, "_namespaces", lambda _pid: NS)
    monkeypatch.setattr(bootstrap.os, "getpid", lambda: 41)
    monkeypatch.setattr(bootstrap.os, "getresuid", lambda: (0, 0, 0))
    monkeypatch.setattr(bootstrap.os, "getresgid", lambda: (0, 0, 0))
    monkeypatch.setattr(bootstrap.os, "getxattr", lambda *_: scope.marker)

    def fstatfs(_fd, pointer):
        ctypes.c_long.from_buffer(pointer._obj).value = 0x63677270
        return 0

    monkeypatch.setattr(bootstrap.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(fstatfs=fstatfs))

    def process(pid, namespaces, membership):
        bootstrap._require(namespaces == NS, "foreign namespace")
        bootstrap._require(pid in scope.members.get(membership.lstrip("/"), set()), "membership changed")
        return 1000 + pid

    monkeypatch.setattr(bootstrap, "_process", process)
    return scope, observations


def test_prepare_migrates_only_private_members_before_enabling_and_delegates_exact_interfaces(kernel):
    scope, _ = kernel
    prepared = bootstrap.prepare()
    assert prepared.jobs_root == "/sys/fs/cgroup/probe-jobs"
    assert prepared.supervisor_leaf == prepared.jobs_root + "/supervisor"
    assert prepared.moved_pids == (1, 5, 41)
    assert scope.members == {
        "": set(),
        "probe-bootstrap": {1, 5, 41},
        "probe-jobs": set(),
        "probe-jobs/supervisor": set(),
    }
    writes = [event for event in scope.events if event[0] == "write"]
    assert writes == [("write", "probe-bootstrap/cgroup.procs", str(pid)) for pid in (1, 5, 0)] + [
        ("write", "cgroup.subtree_control", "+cpu +memory +pids"),
        ("write", "probe-jobs/cgroup.subtree_control", "+cpu +memory +pids"),
    ]
    assert set(scope.owners) == {
        path + suffix
        for path in ("probe-jobs", "probe-jobs/supervisor")
        for suffix in ("", *("/" + name for name in bootstrap._DELEGATE_FILES))
    }
    assert all(scope.files[name] == value for name, value in LIMITS.items())
    assert scope.closed
    with pytest.raises(bootstrap.BootstrapRefused, match="fresh"):
        bootstrap.prepare()


@pytest.mark.parametrize(
    "change",
    [
        lambda scope, obs: obs.update({"/proc/self/uid_map": "0 0 4294967295\n"}),
        lambda scope, obs: obs.update({"/proc/1/gid_map": "0 0 4294967295\n"}),
        lambda scope, obs: obs.update({"/proc/self/mountinfo": MOUNT.replace("cgroup2", "cgroup")}),
        lambda scope, obs: obs.update({"/proc/self/mountinfo": MOUNT.replace("rw,nosuid", "ro,nosuid")}),
        lambda scope, obs: obs.update({"/proc/self/mountinfo": MOUNT.replace(",nsdelegate", "")}),
        lambda scope, obs: obs.update(
            {"/proc/self/mountinfo": MOUNT + MOUNT.replace("1020", "1021").replace("/sys/fs/cgroup", "/other")}
        ),
        lambda scope, obs: setattr(scope, "outer_writable", True),
        lambda scope, obs: scope.owners.update({"memory.max": 0}),
        lambda scope, obs: scope.owners.update({"cgroup.procs": 10001}),
        lambda scope, obs: scope.files.update({"cgroup.controllers": "cpu memory\n"}),
        lambda scope, obs: scope.files.update({"cgroup.type": "threaded\n"}),
        lambda scope, obs: scope.files.update({"cgroup.subtree_control": "cpu\n"}),
        lambda scope, obs: scope.members[""].add(0),
        lambda scope, obs: scope.members[""].remove(1),
        lambda scope, obs: scope.members[""].remove(41),
        lambda scope, obs: scope.members.update({"preexisting": set()}),
    ],
)
def test_preflight_refuses_without_any_mutation(kernel, change):
    scope, observations = kernel
    change(scope, observations)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.prepare()
    assert not scope.events
    assert scope.closed


def test_foreign_member_is_rejected_before_mkdir(kernel, monkeypatch):
    scope, _ = kernel
    original = bootstrap._process

    def foreign(pid, *args):
        if pid == 5:
            raise bootstrap.BootstrapRefused("foreign-namespace cgroup member refused")
        return original(pid, *args)

    monkeypatch.setattr(bootstrap, "_process", foreign)
    with pytest.raises(bootstrap.BootstrapRefused, match="foreign"):
        bootstrap.prepare()
    assert not scope.events


def test_missing_optional_userspace_marker_does_not_override_kernel_delegation(kernel, monkeypatch):
    scope, _ = kernel

    def no_marker(*_args):
        raise OSError(errno.ENODATA, "optional user.delegate marker is absent")

    monkeypatch.setattr(bootstrap.os, "getxattr", no_marker)
    assert bootstrap.prepare().jobs_root == "/sys/fs/cgroup/probe-jobs"
    assert all(scope.files[name] == value for name, value in LIMITS.items())


def test_process_reuse_aborts_before_migration_of_reused_pid(kernel, monkeypatch):
    scope, _ = kernel
    original = bootstrap._process
    seen = 0

    def changed(pid, *args):
        nonlocal seen
        result = original(pid, *args)
        if pid == 5:
            seen += 1
            return result + (seen > 1)
        return result

    monkeypatch.setattr(bootstrap, "_process", changed)
    with pytest.raises(bootstrap.BootstrapRefused, match="identity changed"):
        bootstrap.prepare()
    assert 5 in scope.members[""]
    assert all(event[:2] != ("write", "cgroup.subtree_control") for event in scope.events)
    assert scope.closed


def test_late_root_member_gets_a_bounded_verified_migration_round(kernel):
    scope, _ = kernel

    def fork(name, value):
        if value == "0":
            scope.members[""].add(52)

    scope.after_write = fork
    assert bootstrap.prepare().moved_pids == (1, 5, 41, 52)


def test_continuous_new_members_never_enables_controllers(kernel):
    scope, _ = kernel

    def fork(name, value):
        if name == "probe-bootstrap/cgroup.procs":
            scope.members[""].add(100 + len(scope.events))

    scope.after_write = fork
    with pytest.raises(bootstrap.BootstrapRefused, match="bounded"):
        bootstrap.prepare()
    assert len(scope.events) < 50
    assert not scope.files["cgroup.subtree_control"]


@pytest.mark.parametrize("fault", ["readback", "outer_change", "late_member"])
def test_post_mutation_fault_refuses_worker_authority(kernel, fault):
    scope, _ = kernel

    def corrupt(name, _value):
        if name == "cgroup.subtree_control":
            if fault == "readback":
                scope.files[name] = "cpu memory\n"
            if fault == "outer_change":
                scope.files["memory.max"] = "1\n"
            if fault == "late_member":
                scope.members[""].add(99)

    scope.after_write = corrupt
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.prepare()
    assert scope.closed
    assert "probe-bootstrap" in scope.members  # No unsafe rollback/reuse.


def test_real_proc_observation_is_read_only_and_checks_namespace():
    namespaces = bootstrap._namespaces("self")
    rows = Path("/proc/self/cgroup").read_text().splitlines()
    membership = next(line[3:] for line in rows if line.startswith("0::"))
    if len(rows) == 1:
        assert bootstrap._process(os.getpid(), namespaces, membership) > 0
    else:
        # This development host also exposes net_cls v1. The production Pod
        # profile deliberately refuses it, without performing any migration.
        with pytest.raises(bootstrap.BootstrapRefused, match="cgroup member moved"):
            bootstrap._process(os.getpid(), namespaces, membership)
    with pytest.raises(bootstrap.BootstrapRefused, match="foreign"):
        bootstrap._process(os.getpid(), tuple(), membership)


def test_control_writer_rejects_outer_limit_even_on_ordinary_fixture(tmp_path):
    (tmp_path / "memory.max").write_text("123")
    scope = bootstrap._Scope(str(tmp_path))
    try:
        with pytest.raises(bootstrap.BootstrapRefused, match="allowlist"):
            scope.write("memory.max", "999")
        assert (tmp_path / "memory.max").read_text() == "123"
    finally:
        scope.close()


def test_scope_rejects_symlinked_ancestor(tmp_path):
    (tmp_path / "actual").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "actual", target_is_directory=True)
    with pytest.raises(OSError):
        bootstrap._Scope(str(tmp_path / "alias"))


def test_worker_enters_only_prepared_leaf_before_privilege_drop(kernel, monkeypatch):
    scope, _ = kernel
    prepared = bootstrap.prepare()
    order = []
    identity = {"uid": (0, 0, 0), "gid": (0, 0, 0), "groups": [0]}
    monkeypatch.setattr(bootstrap.os, "getresuid", lambda: identity["uid"])
    monkeypatch.setattr(bootstrap.os, "getresgid", lambda: identity["gid"])
    monkeypatch.setattr(bootstrap.os, "getgroups", lambda: identity["groups"])

    def set_identity(key, value):
        assert scope.members["probe-jobs/supervisor"] == {41}
        order.append(key)
        identity[key] = value

    monkeypatch.setattr(bootstrap.os, "setgroups", lambda groups: set_identity("groups", groups))
    monkeypatch.setattr(bootstrap.os, "setresgid", lambda *ids: set_identity("gid", ids))
    monkeypatch.setattr(bootstrap.os, "setresuid", lambda *ids: set_identity("uid", ids))
    monkeypatch.setattr(bootstrap.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(prctl=lambda *args: 0))
    monkeypatch.setattr(
        bootstrap, "_proc_read", lambda _: "NoNewPrivs:\t1\nCapEff:\t0\nCapPrm:\t0\nCapInh:\t0\nCapAmb:\t0\n"
    )
    bootstrap.enter_supervisor_and_drop(prepared)
    assert order == ["groups", "gid", "uid"]
    assert identity == {"uid": (10001,) * 3, "gid": (10001,) * 3, "groups": []}


def test_worker_rejects_replaced_scope_without_moving(kernel):
    scope, _ = kernel
    prepared = replace(bootstrap.prepare(), supervisor_identity=(28, 999))
    before = list(scope.events)
    with pytest.raises(bootstrap.BootstrapRefused, match="identity changed"):
        bootstrap.enter_supervisor_and_drop(prepared)
    assert scope.events == before


def test_cli_exec_has_allowlisted_environment_and_no_provider_key(monkeypatch):
    prepared = SimpleNamespace(root="/sys/fs/cgroup")
    monkeypatch.setattr(bootstrap, "prepare", lambda _: prepared)
    monkeypatch.setattr(bootstrap, "asdict", lambda _: {"root": prepared.root})
    monkeypatch.setattr(bootstrap, "enter_supervisor_and_drop", lambda _: None)
    monkeypatch.setenv("RUNPOD_API_KEY", "synthetic-forbidden-value")
    monkeypatch.setattr("sys.argv", ["pod_bootstrap.py", "--", "/usr/bin/python3", "-I", "check.py"])
    seen = []
    monkeypatch.setattr(bootstrap.os, "execve", lambda *args: seen.append(args))
    bootstrap.main()
    assert seen[0][0] == "/usr/bin/python3"
    assert set(seen[0][2]) == {"PATH", "HOME", "USER", "LOGNAME", "LANG"}
