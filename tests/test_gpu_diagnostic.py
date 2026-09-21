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
    assert implementation._mounts(text, "/sys/fs/cgroup/worker") == [{"mount_id":"1", "root":"/", "mountpoint":"/sys/fs/cgroup", "filesystem":"cgroup2", "mount_options":["rw"], "optional_fields":[], "super_options":["rw", "nsdelegate"]}]
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
        data = {"malformed":"{", "oversized":"x"*(implementation.MAX_RESULT_BYTES+1), "wrong_identity":json.dumps(value), "failed":"{}"}[outcome]
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


@pytest.mark.parametrize("row", ["NVIDIA RTX 3090, GPU-test, 580.65.06, 24564, 8.9", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 123, 8.9", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 22999, 8.9", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, inf", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, nan", "NVIDIA GeForce RTX 4090, GPU-test, 580.65.06, 24564, invalid", "NVIDIA GeForce RTX 4090, GPU-test, 570.1, 24564, 8.9"])
def test_gpu_identity_and_finite_capability_are_required(tmp_path, monkeypatch, row):
    implementation = module()
    monkeypatch.setattr(implementation,"_command",lambda _: {"returncode":0,"stdout":row+"\n"})
    monkeypatch.setattr(implementation,"inspect_cgroup",lambda _: {"worker_identity_verified":True})
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":True})
    assert implementation.diagnose(tmp_path)["worker_prerequisites_passed"] is False


@pytest.mark.parametrize("memory_mib,passed", [(23000, True), (24564, True), (49140, True), (50000, True),
                                             (22999, False), (20480, False), ("nan", False), ("inf", False), ("49140.5", False)])
def test_vram_is_a_minimum_capacity_check_including_measured_48gib_device(tmp_path, monkeypatch, memory_mib, passed):
    implementation = module()
    row = f"NVIDIA GeForce RTX 4090, GPU-test, 595.71.05, {memory_mib}, 8.9\n"
    monkeypatch.setattr(implementation, "_command", lambda _: {"returncode":0, "stdout":row})
    monkeypatch.setattr(implementation, "inspect_cgroup", lambda _: {"worker_identity_verified":True})
    monkeypatch.setattr(implementation, "cgroup_probe", lambda _: {"passed":True})
    report = implementation.diagnose(tmp_path)
    assert report["cuda13_native_bf16_hardware_passed"] is passed
    assert report["worker_prerequisites_passed"] is passed
    assert report["nvidia_smi"]["stdout"] == row  # Preserve observed capacity; never rewrite it to 24 GiB.
    assert report["scientific_evidence"] is False and report["provider_stop_verified"] is False


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


def test_inventory_includes_legacy_and_hybrid_mounts_below_tmpfs_root():
    implementation = module()
    data = ("10 1 0:1 / /sys/fs/cgroup ro - tmpfs tmpfs ro\n"
            "11 10 0:2 /docker/pod /sys/fs/cgroup/cpu,cpuacct rw - cgroup cgroup rw,cpu,cpuacct\n"
            "12 10 0:3 /docker/pod /sys/fs/cgroup/memory ro - cgroup cgroup rw,memory\n"
            "13 10 0:4 /docker/pod /sys/fs/cgroup/unified rw - cgroup2 cgroup rw,nsdelegate\n")
    mounts = implementation._mounts(data)
    assert [item["filesystem"] for item in mounts] == ["cgroup", "cgroup", "cgroup2"]
    assert mounts[1]["mount_options"] == ["ro"]
    assert mounts[0]["root"] == "/docker/pod"
    assert implementation._mounts(data, "/sys/fs/cgroup") == []  # old query missed all three


def fixture_mount(path, controllers="memory", root="/"):
    return {"mount_id":"11", "root":root, "mountpoint":str(path),
            "filesystem":"cgroup2" if controllers == "" else "cgroup",
            "mount_options":["rw"], "super_options":["rw", *controllers.split(",")], "optional_fields":[]}


def test_v1_own_subtree_and_outer_limits_are_read_without_changes(tmp_path, monkeypatch):
    implementation = module()
    own = tmp_path/"docker"/"pod"; own.mkdir(parents=True)
    for path, limit in ((tmp_path, "4096"), (own.parent, "2048"), (own, "8192")):
        (path/"cgroup.procs").write_text(f"{os.getpid()}\n" if path == own else "")
        (path/"memory.limit_in_bytes").write_text(limit)
        (path/"memory.use_hierarchy").write_text("1")
        (path/"memory.memsw.limit_in_bytes").write_text("16384")
    before = {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr(implementation, "_filesystem_type", lambda _: 0x27e0eb)
    monkeypatch.setattr(implementation.os, "write", lambda *_:pytest.fail("read-only inventory wrote control bytes"))
    result = implementation._inventory([fixture_mount(tmp_path)], {"text":"9:memory:/docker/pod\n", "truncated":False})
    item = result["mounts"][0]
    assert item["own_membership_verified"] and item["own_path"] == str(own)
    assert item["visible_ancestors_complete"] and len(item["ancestors"]) == 2
    assert item["visible_limits"]["upper_bounds"]["ram_bytes"]["value"] == 2048
    assert item["visible_limits"]["host_ancestor_limits_verified"] is False
    assert item["candidates"][0]["view"]["open_for_write_without_write"]["memory.limit_in_bytes"]["opened"]
    assert result["control_bytes_written"] == 0 and not result["exclusive_ownership_verified"]
    assert before == {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


@pytest.mark.parametrize("membership,expected_method", [("/docker/pod", "mount_root_relative"), ("/", "namespace_relative_requires_pid_proof")])
def test_bind_mount_and_namespace_relative_resolution_require_own_pid(tmp_path, monkeypatch, membership, expected_method):
    implementation = module(); mount = fixture_mount(tmp_path, "", "/docker/pod")
    (tmp_path/"cgroup.procs").write_text(f"{os.getpid()}\n")
    (tmp_path/"cgroup.events").write_text("populated 1\nfrozen 0\n")
    (tmp_path/"cgroup.kill").write_text("")
    (tmp_path/"cpuset.cpus.effective").write_text("2-5,8\n")
    (tmp_path/"cpuset.mems.effective").write_text("0\n")
    monkeypatch.setattr(implementation, "_filesystem_type", lambda _:0x63677270)
    result = implementation._inventory([mount], {"text":f"0::{membership}\n"})["mounts"][0]
    assert result["own_membership_verified"] and result["resolution"] == expected_method
    view = result["candidates"][0]["view"]
    assert view["files"]["cgroup.events"]["text"] == "populated 1\nfrozen 0\n"
    assert view["files"]["cpuset.cpus.effective"]["text"] == "2-5,8\n"
    assert view["files"]["cpuset.mems.effective"]["text"] == "0\n"
    assert view["open_for_write_without_write"]["cgroup.kill"]["opened"]
    assert not view["child_creation_verified"]
    (tmp_path/"cgroup.procs").write_text("99999999\n")
    unresolved = implementation._inventory([mount], {"text":f"0::{membership}\n"})["mounts"][0]
    assert not unresolved["own_membership_verified"] and "ancestors" not in unresolved


@pytest.mark.parametrize("unsafe", ["/../pod", "/docker/../../pod", "/docker/./pod", "/docker//pod", "relative", "/pod\x00"])
def test_unsafe_membership_paths_never_open_candidate(tmp_path, monkeypatch, unsafe):
    implementation = module()
    monkeypatch.setattr(implementation, "_control_view", lambda *_:pytest.fail("unsafe path inspected"))
    result = implementation._inventory([fixture_mount(tmp_path)], {"text":f"9:memory:{unsafe}\n"})
    assert result["error_type"] == "ValueError" and not result["mounts"]


def test_inventory_rejects_fake_cgroup_and_symlink_parent_before_reading_controls(tmp_path, monkeypatch):
    implementation = module(); real = tmp_path/"real"; real.mkdir()
    (real/"cgroup.procs").write_text(f"{os.getpid()}\n")
    mount = fixture_mount(real)
    result = implementation._inventory([mount], {"text":"9:memory:/\n"})["mounts"][0]
    assert not result["own_membership_verified"]
    assert "files" not in result["candidates"][0]["view"]
    link = tmp_path/"link"; link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(implementation, "_filesystem_type", lambda _:0x27e0eb)
    result = implementation._inventory([fixture_mount(link)], {"text":"9:memory:/\n"})["mounts"][0]
    assert not result["own_membership_verified"]
    assert "files" not in result["candidates"][0]["view"]


def test_inventory_is_bounded_and_does_not_treat_incomplete_membership_as_proof(tmp_path, monkeypatch):
    implementation = module(); mount = fixture_mount(tmp_path)
    (tmp_path/"cgroup.procs").write_text(f"{os.getpid()}\n")
    monkeypatch.setattr(implementation, "_filesystem_type", lambda _:0x27e0eb)
    monkeypatch.setattr(implementation, "MAX_INVENTORY_BYTES", 1)
    result = implementation._inventory([mount], {"text":"9:memory:/\n"})
    assert result["truncated"] and result["encoded_mount_bytes"] == 0 and result["mounts"] == []
    result = implementation._inventory([mount], {"text":"9:memory:/\n", "truncated":True})
    assert result["error_type"] == "MembershipUnavailable" and result["mounts"] == []


def test_readonly_inventory_cli_never_runs_active_probe_or_gpu_query(tmp_path, monkeypatch, capsys):
    implementation = module(); inspection = {"worker_identity_verified":False, "inspection_only":True}
    monkeypatch.setattr(implementation, "inspect_cgroup", lambda _:inspection)
    monkeypatch.setattr(implementation, "worker_inspection", lambda *_: {"worker_identity_verified":True})
    monkeypatch.setattr(implementation, "cgroup_probe", lambda *_:pytest.fail("inventory invoked active probe"))
    monkeypatch.setattr(implementation, "_command", lambda *_:pytest.fail("inventory invoked GPU command"))
    monkeypatch.setattr(implementation.sys, "argv", ["diagnose.py", "--inventory-only", "--cgroup-root", str(tmp_path)])
    with pytest.raises(SystemExit) as stopped:
        implementation.main()
    assert stopped.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "infrastructure_inventory" and not result["worker_prerequisites_passed"]
    assert result["control_bytes_written"] == 0
