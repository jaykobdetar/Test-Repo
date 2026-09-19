import importlib.util
import errno
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


def module():
    source=Path(__file__).resolve().parents[1]/"deploy/gpu/diagnose.py"
    spec=importlib.util.spec_from_file_location("probe_gpu_diagnostic",source)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_ordinary_directory_cannot_fake_delegated_cgroup(tmp_path):
    for filename in ("cgroup.controllers","cgroup.subtree_control"):
        (tmp_path/filename).write_text("cpu memory pids")
    result=module().cgroup_probe(tmp_path)
    assert not result["passed"]
    assert "not a real cgroup-v2" in result["reason"]
    assert not list(tmp_path.glob("probe-preflight-*"))


def test_diagnostic_rejects_incompatible_driver_and_never_claims_stop(tmp_path,monkeypatch):
    implementation=module()
    monkeypatch.setattr(implementation,"_command",lambda _: {"returncode":0,"stdout":"NVIDIA GeForce RTX 4090, GPU-test, 570.1, 24564, 8.9\n","stderr":""})
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":True})
    result=implementation.diagnose(tmp_path)
    assert not result["cuda13_native_bf16_hardware_passed"]
    assert not result["worker_prerequisites_passed"]
    assert not result["provider_stop_verified"]
    assert not result["scientific_evidence"]


def test_diagnostic_accepts_one_compatible_gpu_only_after_cgroup_probe(tmp_path,monkeypatch):
    implementation=module()
    monkeypatch.setattr(implementation,"_command",lambda _: {"returncode":0,"stdout":"NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, 8.9\n","stderr":""})
    monkeypatch.setattr(implementation,"inspect_cgroup",lambda _: {"worker_identity_verified":True})
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":True})
    assert implementation.diagnose(tmp_path)["worker_prerequisites_passed"]
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":False})
    assert not implementation.diagnose(tmp_path)["worker_prerequisites_passed"]


def worker_identity():
    return {"resuid": [10001]*3, "resgid": [10001]*3, "groups": [], "status_complete": True,
            "process_status": {key: "0000000000000000" for key in ("CapInh", "CapPrm", "CapEff", "CapAmb")}}


def simulated_controls(tmp_path):
    values = {"cgroup.controllers": "cpu memory pids\n", "cgroup.subtree_control": "",
              "cgroup.procs": "123\n", "cgroup.threads": "123\n", "cgroup.type": "domain\n"}
    for name, value in values.items():
        (tmp_path/name).write_text(value)
    return values


def test_permission_inspection_collects_empty_controller_metadata_without_changes(tmp_path, monkeypatch):
    implementation = module()
    values = simulated_controls(tmp_path)
    monkeypatch.setattr(implementation, "_filesystem_type", lambda _: 0x63677270)
    monkeypatch.setattr(implementation.os, "write", lambda *_: pytest.fail("inspection wrote control bytes"))
    result = implementation.inspect_cgroup(tmp_path)
    assert result["files"]["cgroup.subtree_control"]["text"] == ""
    assert all(item["opened"] for item in result["open_for_write_without_write"].values())
    assert result["control_bytes_written"] == 0
    assert result["exclusive_ownership_verified"] is False
    assert {name: (tmp_path/name).read_text() for name in values} == values
    assert set(p.name for p in tmp_path.iterdir()) == set(values)


@pytest.mark.parametrize("code", [errno.EACCES, errno.EROFS])
def test_write_open_reports_kernel_denial_not_mode_bits(tmp_path, monkeypatch, code):
    implementation = module()
    simulated_controls(tmp_path)
    monkeypatch.setattr(implementation, "_filesystem_type", lambda _: 0x63677270)
    actual_open = implementation.os.open
    def denied(path, flags, *args, **kwargs):
        if flags & os.O_WRONLY:
            raise OSError(code, os.strerror(code))
        return actual_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(implementation.os, "open", denied)
    result = implementation.inspect_cgroup(tmp_path)
    assert all(not item["opened"] and item["errno"] == code
               for item in result["open_for_write_without_write"].values())
    assert result["exclusive_ownership_verified"] is False


def test_inspection_refuses_parent_symlink_and_non_cgroup_filesystem(tmp_path):
    implementation = module()
    real = tmp_path/"real"; real.mkdir(); (real/"subdir").mkdir()
    link = tmp_path/"link"; link.symlink_to(real, target_is_directory=True)
    result = implementation.inspect_cgroup(link/"subdir")
    assert "error_type" in result and "files" not in result
    result = implementation.inspect_cgroup(real)
    assert "not a real cgroup-v2" in result["reason"] and "files" not in result


def test_inspection_refuses_control_symlink_and_fifo_without_hanging(tmp_path, monkeypatch):
    implementation = module()
    simulated_controls(tmp_path)
    target = tmp_path/"unrelated"; target.write_text("do not read or modify")
    (tmp_path/"cgroup.procs").unlink(); (tmp_path/"cgroup.procs").symlink_to(target)
    (tmp_path/"cgroup.threads").unlink(); os.mkfifo(tmp_path/"cgroup.threads")
    monkeypatch.setattr(implementation, "_filesystem_type", lambda _: 0x63677270)
    result = implementation.inspect_cgroup(tmp_path)
    for name in ("cgroup.procs", "cgroup.threads"):
        assert "error_type" in result["files"][name]
        assert result["open_for_write_without_write"][name]["opened"] is False
    assert target.read_text() == "do not read or modify"


def test_bounded_reads_do_not_trust_zero_proc_file_size(tmp_path):
    implementation = module()
    actual = implementation._proc_file("cgroup", 1)
    assert actual["text"] and actual["truncated"]
    (tmp_path/"control").write_text("x"*50)
    fd = implementation._directory(tmp_path)
    try:
        actual = implementation._read_at(fd, "control", 10)
    finally:
        os.close(fd)
    assert actual["text"] == "x"*10 and actual["truncated"]


def test_proc_short_reads_are_not_mistaken_for_eof(tmp_path, monkeypatch):
    implementation = module()
    (tmp_path/"control").write_text("placeholder")
    chunks = iter([b"first ", b"second", b""])
    monkeypatch.setattr(implementation.os, "read", lambda *_: next(chunks))
    fd = implementation._directory(tmp_path)
    try:
        actual = implementation._read_at(fd, "control", 100)
    finally:
        os.close(fd)
    assert actual["text"] == "first second" and not actual["truncated"]


def test_mount_parser_selects_containing_mount_and_decodes_escapes():
    implementation = module()
    text = "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw,nsdelegate\n2 0 0:2 / /private rw - tmpfs secret rw\n3 0 0:3 /foo /sys/fs/cgroup\\040space ro - cgroup2 cgroup rw\n"
    assert implementation._mounts(text, "/sys/fs/cgroup/worker") == [{"mount_id":"1", "root":"/", "mountpoint":"/sys/fs/cgroup", "mount_options":["rw"], "optional_fields":[], "super_options":["rw", "nsdelegate"]}]
    assert implementation._mounts(text, "/sys/fs/cgroup space")[0]["mount_options"] == ["ro"]
    assert implementation._mounts(text, "/sys/fs/cgroups") == []


@pytest.mark.parametrize("field,value", [("resuid",[0,10001,0]), ("resgid",[10001,0,10001]),
                                         ("groups",[0]), ("status_complete",False),
                                         ("process_status",[]),
                                         ("process_status",{"CapEff":"0000000000000000"})])
def test_worker_identity_rejected_before_any_cgroup_access(tmp_path, monkeypatch, field, value):
    implementation = module(); identity = worker_identity(); identity[field] = value
    monkeypatch.setattr(implementation, "_identity", lambda: identity)
    monkeypatch.setattr(implementation, "_directory", lambda *_: pytest.fail("invalid worker opened cgroup"))
    result = implementation.inspect_cgroup(tmp_path, require_worker=True)
    assert result["error_type"] == "IdentityMismatch"


def test_worker_inspection_drops_all_identity_fields_and_inherited_descriptors(tmp_path, monkeypatch):
    implementation = module(); seen = {}
    expected = {"identity":worker_identity(), "worker_identity_verified":True, "control_bytes_written":0, "inspection_only":True}
    def spawn(args, **kwargs):
        seen.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps(expected))
    monkeypatch.setattr(implementation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(implementation.subprocess, "run", spawn)
    assert implementation.worker_inspection(tmp_path, {"worker_identity_verified":False}) == expected
    assert seen["user"] == seen["group"] == 10001 and seen["extra_groups"] == ()
    assert seen["cwd"] == "/" and seen["close_fds"] and seen["stdin"] == subprocess.DEVNULL
    assert seen["env"] == {"PATH":"/usr/bin:/bin", "LANG":"C"} and seen["timeout"] == 10
    assert "--inspect-cgroup-only" in seen["args"] and "-I" in seen["args"]
    assert "preexec_fn" not in seen and "pass_fds" not in seen


@pytest.mark.parametrize("outcome", ["malformed", "oversized", "wrong_identity", "failed", "timeout", "permission"])
def test_worker_inspection_failure_never_falls_back_to_root(tmp_path, monkeypatch, outcome):
    implementation = module()
    monkeypatch.setattr(implementation.os, "geteuid", lambda: 0)
    def fail(*args, **kwargs):
        if outcome == "timeout": raise subprocess.TimeoutExpired("inspect",10)
        if outcome == "permission": raise PermissionError(errno.EPERM,"cannot set identity")
        value = {"identity": worker_identity(), "control_bytes_written":0, "inspection_only":True}
        value["identity"]["process_status"]["CapEff"] = "0000000000000001"
        data = {"malformed":"{", "oversized":"x"*65537, "wrong_identity":json.dumps(value), "failed":"{}"}[outcome]
        return SimpleNamespace(returncode=1 if outcome=="failed" else 0, stdout=data)
    monkeypatch.setattr(implementation.subprocess, "run", fail)
    result = implementation.worker_inspection(tmp_path, {"worker_identity_verified":False})
    assert result["worker_identity_verified"] is False and "error_type" in result


def test_existing_worker_identity_is_not_switched_again(tmp_path, monkeypatch):
    implementation = module(); current = {"worker_identity_verified":True}
    monkeypatch.setattr(implementation.subprocess, "run", lambda *_a,**_k: pytest.fail("unnecessary UID switch"))
    assert implementation.worker_inspection(tmp_path, current) is current


def test_successful_root_probe_cannot_certify_worker_prerequisites(tmp_path, monkeypatch):
    implementation = module()
    monkeypatch.setattr(implementation,"_command",lambda _: {"returncode":0,"stdout":"NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, 8.9\n"})
    monkeypatch.setattr(implementation,"inspect_cgroup",lambda _: {"worker_identity_verified":False})
    monkeypatch.setattr(implementation,"worker_inspection",lambda *_: {"worker_identity_verified":True,"inspection_only":True})
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":True})
    assert implementation.diagnose(tmp_path)["worker_prerequisites_passed"] is False


@pytest.mark.parametrize("row", ["NVIDIA RTX 3090, GPU-test, 580.65.06, 24564, 8.9", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 123, 8.9", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 50000, 8.9", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, inf", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, nan", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, invalid", "NVIDIA GeForce RTX 4090, GPU-test, 570.1, 24564, 8.9"])
def test_gpu_identity_and_finite_capability_are_required(tmp_path, monkeypatch, row):
    implementation = module()
    monkeypatch.setattr(implementation,"_command",lambda _: {"returncode":0,"stdout":row+"\n"})
    monkeypatch.setattr(implementation,"inspect_cgroup",lambda _: {"worker_identity_verified":True})
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":True})
    assert implementation.diagnose(tmp_path)["worker_prerequisites_passed"] is False


@pytest.mark.parametrize("measurement", [("1e100",1,0), ("8.9",2,0), ("8.9",1,1)])
def test_impossible_capability_multiple_gpus_and_failed_query_are_rejected(tmp_path, monkeypatch, measurement):
    capability, count, returncode = measurement
    implementation = module()
    row = f"NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, {capability}\n"
    monkeypatch.setattr(implementation,"_command",lambda _: {"returncode":returncode,"stdout":row*count})
    monkeypatch.setattr(implementation,"inspect_cgroup",lambda _: {"worker_identity_verified":True})
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":True})
    assert implementation.diagnose(tmp_path)["worker_prerequisites_passed"] is False


@pytest.mark.parametrize("failure", ["missing_kill", "readback", "cleanup", "none"])
def test_active_empty_child_probe_requires_kill_readback_and_cleanup(tmp_path, monkeypatch, failure):
    implementation = module()
    (tmp_path/"cgroup.controllers").write_text("cpu memory pids")
    (tmp_path/"cgroup.subtree_control").write_text("cpu memory pids")
    monkeypatch.setattr(implementation,"_filesystem_type",lambda _:0x63677270)
    mkdir, rmdir, read_text = Path.mkdir, Path.rmdir, Path.read_text
    def create(path, *args, **kwargs):
        mkdir(path, *args, **kwargs)
        (path/"cgroup.procs").write_text("")
        if failure != "missing_kill": (path/"cgroup.kill").write_text("")
    def remove(path):
        if failure == "cleanup": raise OSError(errno.EBUSY,"synthetic cleanup refusal")
        for item in path.iterdir(): item.unlink()
        rmdir(path)
    def read(path, *args, **kwargs):
        if failure == "readback" and path.name == "cpu.max": return "max 100000"
        return read_text(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,"mkdir",create); patch.setattr(Path,"rmdir",remove); patch.setattr(Path,"read_text",read)
        result = implementation.cgroup_probe(tmp_path)
    assert result["passed"] is (failure == "none")
    if failure == "missing_kill": assert "cgroup.kill" in result["reason"]
    if failure == "readback": assert "readback mismatch" in result["reason"]
    if failure == "cleanup": assert result["cleanup_error"] == "OSError"
    else: assert not list(tmp_path.glob("probe-preflight-*"))
