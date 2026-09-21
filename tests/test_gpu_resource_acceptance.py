"""Local protocol tests only: no real cgroup writes or exhaustion workloads."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import time
from types import SimpleNamespace

import pytest


def module():
    source = Path(__file__).resolve().parents[1]/"deploy/gpu/accept-resources.py"
    spec = importlib.util.spec_from_file_location("gpu_resource_acceptance", source)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_wrong_identity_rejects_before_any_cgroup_write(tmp_path, monkeypatch):
    implementation = module()
    monkeypatch.setattr(implementation.os, "getresuid", lambda:(123,)*3)
    monkeypatch.setattr(implementation, "directory", lambda *_:pytest.fail("wrong identity inspected cgroup"))
    monkeypatch.setattr(implementation, "write", lambda *_:pytest.fail("wrong identity wrote cgroup"))
    result = implementation.run(tmp_path)
    assert result["passed"] is False and result["error_code"] == "WorkerIdentityRequired" and not result["checks"]


def test_private_scope_limits_cleanup_and_unrelated_files_are_preserved(tmp_path, monkeypatch):
    implementation = module()
    unrelated = tmp_path/"other-job"; unrelated.mkdir(); (unrelated/"data").write_text("unchanged")
    (tmp_path/"memory.max").write_text("outer-limit-unchanged")
    actual_mkdir, actual_rmdir = os.mkdir, os.rmdir
    created = []
    def make(name, mode=0o777, *, dir_fd=None):
        actual_mkdir(name, mode, dir_fd=dir_fd)
        path = tmp_path/name; created.append(path)
        for filename in ("memory.max", "memory.swap.max", "pids.max", "cpu.max", "cgroup.procs", "cgroup.kill"):
            (path/filename).write_text("")
        (path/"cgroup.events").write_text("populated 0\n")
    def remove(name, *, dir_fd=None):
        path = tmp_path/name
        assert path in created and path.name.startswith("probe-resource-cpu-")
        for item in path.iterdir(): item.unlink()
        actual_rmdir(name, dir_fd=dir_fd)
    monkeypatch.setattr(implementation.os, "mkdir", make)
    monkeypatch.setattr(implementation.os, "rmdir", remove)
    monkeypatch.setattr(implementation, "filesystem", lambda _:0x63677270)
    root = implementation.directory(tmp_path)
    scope = implementation.Scope(root, "cpu", time.monotonic()+30)
    try:
        scope.prepare()
        assert (created[0]/"memory.max").read_text() == str(128*1024*1024)
        assert (created[0]/"memory.swap.max").read_text() == "0"
        assert (created[0]/"cpu.max").read_text() == "50000 100000"
        assert (created[0]/"pids.max").read_text() == "16"
        scope.cleanup(time.monotonic()+1)
        assert not created[0].exists()
    finally:
        scope.close(); os.close(root)
    assert (tmp_path/"memory.max").read_text() == "outer-limit-unchanged"
    assert (unrelated/"data").read_text() == "unchanged"


def test_control_symlink_and_fifo_are_rejected_without_writes(tmp_path):
    implementation = module()
    outside = tmp_path/"outside"; outside.write_text("unchanged")
    (tmp_path/"link").symlink_to(outside)
    os.mkfifo(tmp_path/"pipe")
    root = implementation.directory(tmp_path)
    try:
        for name in ("link", "pipe"):
            with pytest.raises((OSError, implementation.AcceptanceError)):
                implementation.write(root, name, "1")
            with pytest.raises((OSError, implementation.AcceptanceError)):
                implementation.read(root, name)
    finally:
        os.close(root)
    assert outside.read_text() == "unchanged"


def fake_scope(kind, status=0, message=None):
    return SimpleNamespace(kind=kind, fd=12, status=status, started=time.monotonic()-3, pid=12345,
                           hard_deadline=time.monotonic()+30,
                           launch=lambda:None, exited=lambda:True, message=lambda _deadline:message)


@pytest.mark.parametrize("usage,throttled,passed", [(1400000,20,True), (1400000,0,False), (2900000,20,False), (0,1,False)])
def test_cpu_requires_actual_throttling_and_bounded_accounted_usage(monkeypatch, usage, throttled, passed):
    implementation = module(); values = iter([{"usage_usec":0,"nr_throttled":0}, {"usage_usec":usage,"nr_throttled":throttled}])
    monkeypatch.setattr(implementation, "counters", lambda *_:next(values))
    scope = fake_scope("cpu")
    if passed:
        assert implementation.exercise(scope, time.monotonic()+10)["nr_throttled_delta"] == 20
    else:
        with pytest.raises(implementation.AcceptanceError, match="CpuThrottleNotObserved"):
            implementation.exercise(scope, time.monotonic()+10)


@pytest.mark.parametrize("status,oom_kills,passed", [(signal.SIGKILL,1,True), (signal.SIGKILL,0,False), (0,1,False)])
def test_memory_requires_both_kernel_oom_counter_and_sigkill(monkeypatch, status, oom_kills, passed):
    implementation = module(); values = iter([{"oom_kill":0}, {"oom_kill":oom_kills}])
    monkeypatch.setattr(implementation, "counters", lambda *_:next(values))
    scope = fake_scope("memory", status)
    if passed:
        assert implementation.exercise(scope, time.monotonic()+10)["oom_kill_delta"] == 1
    else:
        with pytest.raises(implementation.AcceptanceError, match="CgroupOomKillNotObserved"):
            implementation.exercise(scope, time.monotonic()+10)


@pytest.mark.parametrize("count,current,events,error", [(15,16,1,errno.EAGAIN), (12,13,1,errno.EAGAIN), (15,16,0,errno.EAGAIN), (15,16,1,errno.ENOMEM)])
def test_pids_rejects_outer_limit_and_unrelated_fork_failures(monkeypatch, count, current, events, error):
    implementation = module(); values = iter([{"max":0}, {"max":events}])
    monkeypatch.setattr(implementation, "counters", lambda *_:next(values))
    monkeypatch.setattr(implementation, "read", lambda *_:str(current))
    scope = fake_scope("pids", message={"children":list(range(count)), "fork_errno":error})
    if (count,current,events,error) == (15,16,1,errno.EAGAIN):
        assert implementation.exercise(scope, time.monotonic()+10)["pids_current"] == 16
    else:
        with pytest.raises(implementation.AcceptanceError, match="PidLimitNotObserved"):
            implementation.exercise(scope, time.monotonic()+10)


def test_detached_descendants_require_both_pidfd_exit_and_empty_scope(monkeypatch):
    implementation = module(); writes = []; closed = []
    scope = fake_scope("descendants", message={"leader":12345,"descendant":23456,"descendant_session":23456,"descendant_group":23456})
    monkeypatch.setattr(implementation, "read", lambda *_:"12345\n23456")
    monkeypatch.setattr(implementation, "write", lambda *args:writes.append(args))
    monkeypatch.setattr(implementation, "pidfd_open", lambda pid:pid+100)
    monkeypatch.setattr(implementation.os, "close", closed.append)
    monkeypatch.setattr(implementation.select, "select", lambda descriptors,*_: (descriptors if writes else [],[],[]))
    monkeypatch.setattr(implementation, "counters", lambda *_:{"populated":0 if writes else 1})
    result = implementation.exercise(scope, time.monotonic()+10)
    assert result["both_pidfds_exited"] and result["descendant_used_setsid"]
    assert writes == [(12,"cgroup.kill","1")] and len(closed) == 2


def test_cleanup_cannot_pass_if_remaining_descendants_exist(tmp_path, monkeypatch):
    implementation = module(); removed = []
    scope = implementation.Scope(10,"descendants",time.monotonic()+30)
    scope.fd=11; scope.created=True
    monkeypatch.setattr(implementation,"write",lambda *_:None)
    monkeypatch.setattr(implementation,"counters",lambda *_:{"populated":1})
    monkeypatch.setattr(implementation.os,"rmdir",lambda *_a,**_k:removed.append(True))
    with pytest.raises(implementation.AcceptanceError, match="ScopeStillPopulated"):
        scope.cleanup(time.monotonic()-1)
    assert not removed


def test_child_guard_disables_parent_death_for_detached_proof_but_keeps_deadline(monkeypatch):
    implementation=module(); calls=[]; timers=[]
    monkeypatch.setattr(implementation.signal,"signal",lambda *_:None)
    monkeypatch.setattr(implementation.signal,"setitimer",lambda *args:timers.append(args))
    monkeypatch.setattr(implementation.ctypes,"CDLL",lambda *_a,**_k:SimpleNamespace(prctl=lambda *args:calls.append(args) or 0))
    monkeypatch.setattr(implementation.os,"getppid",lambda:123)
    implementation.child_guard(123,time.monotonic()+5,kill_with_parent=False)
    assert calls == [(1,0,0,0,0)]
    assert len(timers)==1 and 4 < timers[0][1] <= 5


def test_cleanup_failure_prevents_following_tests_and_overall_success(monkeypatch):
    implementation=module(); instances=[]
    class FailedScope:
        def __init__(self,_root,kind,_deadline):
            self.name="private-test"; self.limits={}; instances.append(kind)
        def prepare(self): pass
        def cleanup(self,_deadline): raise implementation.AcceptanceError("ScopeStillPopulated")
        def close(self): pass
    monkeypatch.setattr(implementation,"validate_root",lambda _:123)
    monkeypatch.setattr(implementation,"enable_subreaper",lambda:None)
    monkeypatch.setattr(implementation,"memory_envelope",lambda _:[])
    monkeypatch.setattr(implementation.os,"close",lambda _:None)
    monkeypatch.setattr(implementation,"Scope",FailedScope)
    monkeypatch.setattr(implementation,"exercise",lambda *_:{"observed":True})
    result=implementation.run(Path("/synthetic-test"))
    assert not result["passed"] and instances == ["cpu"]
    assert result["checks"][0]["cleanup_confirmed"] is False


@pytest.mark.parametrize("maximum,current,passed", [(256*1024*1024,32*1024*1024,True), (128*1024*1024,0,False)])
def test_known_outer_memory_headroom_is_checked_without_changing_outer_limit(tmp_path, monkeypatch, maximum, current, passed):
    implementation=module()
    (tmp_path/"memory.max").write_text(str(maximum)); (tmp_path/"memory.current").write_text(str(current))
    monkeypatch.setattr(implementation,"filesystem",lambda fd:0x63677270 if os.fstat(fd).st_ino==tmp_path.stat().st_ino else 0)
    if passed:
        assert implementation.memory_envelope(tmp_path)[0]["memory_max"] == str(maximum)
    else:
        with pytest.raises(implementation.AcceptanceError, match="InsufficientOuterMemoryHeadroom"):
            implementation.memory_envelope(tmp_path)
    assert (tmp_path/"memory.max").read_text() == str(maximum)


def test_adopted_descendants_are_reaped_until_no_children_remain(monkeypatch):
    implementation=module(); outcomes=iter([(123,9),(456,9),None]); seen=[]
    def waiting(pid, options):
        seen.append((pid,options)); value=next(outcomes)
        if value is None: raise ChildProcessError()
        return value
    monkeypatch.setattr(implementation.os,"waitpid",waiting)
    implementation.reap_descendants(time.monotonic()+1)
    assert seen == [(-1,os.WNOHANG)]*3
