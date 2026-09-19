import importlib.util
from pathlib import Path


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
    monkeypatch.setattr(implementation,"_command",lambda _: {"returncode":0,"stdout":"NVIDIA RTX 4090, GPU-test, 570.1, 24564, 8.9\n","stderr":""})
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":True})
    result=implementation.diagnose(tmp_path)
    assert not result["cuda13_native_bf16_hardware_passed"]
    assert not result["worker_prerequisites_passed"]
    assert not result["provider_stop_verified"]
    assert not result["scientific_evidence"]


def test_diagnostic_accepts_one_compatible_gpu_only_after_cgroup_probe(tmp_path,monkeypatch):
    implementation=module()
    monkeypatch.setattr(implementation,"_command",lambda _: {"returncode":0,"stdout":"NVIDIA RTX 4090, GPU-test, 580.65.06, 24564, 8.9\n","stderr":""})
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":True})
    assert implementation.diagnose(tmp_path)["worker_prerequisites_passed"]
    monkeypatch.setattr(implementation,"cgroup_probe",lambda _: {"passed":False})
    assert not implementation.diagnose(tmp_path)["worker_prerequisites_passed"]
