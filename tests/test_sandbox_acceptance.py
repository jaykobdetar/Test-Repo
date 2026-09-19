"""Acceptance orchestration contracts; the optional final test uses real Podman."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from probe_core import sandbox_acceptance as acceptance
from probe_core.sandbox import SandboxResult, SandboxUnavailable


IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def fake_runtime(monkeypatch):
    class FakeSandbox(acceptance._RecordedSandbox):
        calls = []
        failure = None

        def check_runtime(self):
            return {}

        def run(self, code, *, limits, inputs=None):
            type(self).calls.append((code, limits, inputs))
            attestation = {"uid": 1000, "cap_eff": "0000000000000000", "seccomp": "2",
                           "no_new_privs": "1", "memory_max": str(limits.memory_bytes),
                           "pids_max": str(limits.pids), "cpu_max": "100000 100000",
                           "interfaces": ["lo"], "socket_denied": True,
                           "input_readonly": True, "root_readonly": True}
            if self.failure != "missing_attestation":
                self._validate_attestation(attestation, limits)
            if self.failure == "invalid_attestation":
                self._validate_attestation({**attestation, "memory_max": "max"}, limits)
            if self.failure == "duplicate_attestation":
                self._validate_attestation(attestation, limits)
            if code == acceptance._CPU_CODE:
                payload = {name: True for name in ("cpu_job", "network_denied", "gpu_unavailable",
                           "host_path_denied", "trusted_paths_readonly", "credentials_absent")}
                payload.update(numpy_version="test", torch_version="test")
                if self.failure == "network_allowed":
                    payload["network_denied"] = False
            elif code == acceptance._BOUNDS_CODE:
                payload = {"pid_limit_enforced": True, "output_limit_enforced": True}
                if self.failure == "output_unbounded":
                    payload["output_limit_enforced"] = False
            elif code == acceptance._MEMORY_CODE:
                if self.failure == "memory_startup_failure":
                    return SandboxResult(137, "", "", (), None)
                if self.failure == "memory_other_error":
                    return SandboxResult(1, "PROBE_MEMORY_STARTED", "", (), None)
                if self.failure == "memory_unbounded":
                    return SandboxResult(0, "PROBE_MEMORY_STARTED\nPROBE_MEMORY_UNBOUNDED", "", (), None)
                return SandboxResult(137, "PROBE_MEMORY_STARTED", "", (), None)
            else:
                assert code == acceptance._TIME_CODE
                if self.failure == "time_startup_failure":
                    return SandboxResult(-9, "", "", (), "wall_time_limit")
                if self.failure == "time_other_failure":
                    return SandboxResult(-9, "PROBE_TIME_STARTED", "", (), "containment_or_output_failure")
                return SandboxResult(-9, "PROBE_TIME_STARTED", "", (), "wall_time_limit")
            directory = self.workspace / str(len(self.calls))
            directory.mkdir()
            path = directory / "result.json"
            path.write_text(json.dumps(payload))
            return SandboxResult(0, "", "", (path,), None)

    monkeypatch.setattr(acceptance, "_RecordedSandbox", FakeSandbox)
    monkeypatch.setattr(acceptance, "_image_identity", lambda sandbox: sandbox.image)
    return FakeSandbox


def invoke(tmp_path):
    return acceptance.run_acceptance(image=IMAGE, workspace=tmp_path / "workspace", output=tmp_path / "report.json")


def test_success_records_every_attestation_limits_and_exact_identity(tmp_path, fake_runtime):
    report = invoke(tmp_path)
    assert report == json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "passed" and report["stage"] == "complete"
    assert report["image"] == IMAGE
    assert report["service_uid"] == os.getuid()
    assert report["seccomp_sha256"].startswith("sha256:")
    assert len(fake_runtime.calls) == len(report["runs"]) == 4
    assert all(report["checks"].values())
    assert report["runs"]["memory"]["limits"]["memory_bytes"] == 128 * 1024**2
    assert report["runs"]["memory"]["attestation"]["memory_max"] == str(128 * 1024**2)
    assert report["runs"]["wall_time"]["termination_reason"] == "wall_time_limit"
    assert report["runs"]["wall_time"]["limits"]["wall_seconds"] == 10
    assert all(call[1].max_broker_requests == 0 for call in fake_runtime.calls)
    assert list((tmp_path / "workspace").iterdir()) == []
    assert (tmp_path / "report.json").stat().st_mode & 0o777 == 0o600
    sentinel = json.loads(fake_runtime.calls[0][2]["host-sentinel.json"])["path"]
    assert not Path(sentinel).exists()


@pytest.mark.parametrize("failure", ["missing_attestation", "invalid_attestation", "duplicate_attestation",
    "network_allowed", "output_unbounded", "memory_startup_failure", "memory_other_error",
    "memory_unbounded", "time_startup_failure", "time_other_failure"])
def test_failure_replaces_stale_pass_and_never_becomes_acceptance(tmp_path, fake_runtime, failure):
    (tmp_path / "report.json").write_text('{"status":"passed"}')
    fake_runtime.failure = failure
    with pytest.raises((acceptance.AcceptanceError, SandboxUnavailable)):
        invoke(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["stage"] != "complete"
    assert report["error_type"]
    assert list((tmp_path / "workspace").iterdir()) == []


def test_stale_pass_is_invalidated_before_any_runtime_action(tmp_path, fake_runtime, monkeypatch):
    (tmp_path / "report.json").write_text('{"status":"passed"}')

    def fail(sandbox):
        assert json.loads((tmp_path / "report.json").read_text())["status"] == "in_progress"
        raise SandboxUnavailable("a hypothetical secret in arbitrary runtime stderr")

    monkeypatch.setattr(fake_runtime, "check_runtime", fail)
    assert acceptance.main(["--image", IMAGE, "--workspace", str(tmp_path / "workspace"),
                            "--output", str(tmp_path / "report.json")]) == 1
    report = (tmp_path / "report.json").read_text()
    assert "secret" not in report
    assert json.loads(report)["status"] == "failed"
    assert fake_runtime.calls == []


def test_wall_time_includes_a_bounded_cleanup_window(tmp_path, fake_runtime, monkeypatch):
    times = iter([0, 1, 0, 1, 0, 1, 0, 46])
    monkeypatch.setattr(acceptance.time, "monotonic", lambda: next(times))
    with pytest.raises(acceptance.AcceptanceError, match="cleanup bound"):
        invoke(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "failed" and report["stage"] == "wall_time"


def test_modified_profile_is_rejected_before_containers(tmp_path, fake_runtime):
    profile = tmp_path / "profile.json"
    profile.write_text('{"defaultAction":"SCMP_ACT_ALLOW"}')
    with pytest.raises(acceptance.AcceptanceError, match="reviewed seccomp"):
        acceptance.run_acceptance(image=IMAGE, workspace=tmp_path / "workspace",
                                  output=tmp_path / "report.json", seccomp_profile=profile)
    assert fake_runtime.calls == []


@pytest.mark.parametrize("stdout,code,success", [(IMAGE.encode(), 0, True), (IMAGE[7:].encode(), 0, True),
    (("sha256:" + "b" * 64).encode(), 0, False), (IMAGE.encode(), 1, False), (b"", 1, False)])
def test_identity_requires_exact_locally_inspected_image(tmp_path, monkeypatch, stdout, code, success):
    sandbox = acceptance._RecordedSandbox(image=IMAGE, workspace=tmp_path)

    def inspect(command, **kwargs):
        assert command == ["podman", "--remote=false", "image", "inspect", "--format", "{{.Id}}", IMAGE]
        assert kwargs["timeout"] == 15
        return subprocess.CompletedProcess(command, code, stdout, b"")

    monkeypatch.setattr(acceptance.subprocess, "run", inspect)
    if success:
        assert acceptance._image_identity(sandbox) == IMAGE
    else:
        with pytest.raises(acceptance.AcceptanceError):
            acceptance._image_identity(sandbox)


def test_symlink_report_is_not_followed(tmp_path, fake_runtime):
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    (tmp_path / "report.json").symlink_to(target)
    with pytest.raises(acceptance.AcceptanceError):
        invoke(tmp_path)
    assert target.read_text() == "unchanged"
    assert fake_runtime.calls == []


def test_real_installed_acceptance_contract(tmp_path):
    image = os.environ.get("PROBE_SANDBOX_IMAGE")
    if not image:
        if os.environ.get("PROBE_SANDBOX_REQUIRED") == "1":
            pytest.fail("real acceptance requires the exact PROBE_SANDBOX_IMAGE")
        pytest.skip("real Podman acceptance is not configured")
    report = acceptance.run_acceptance(image=image, workspace=tmp_path / "workspace",
        output=tmp_path / "report.json", podman=os.environ.get("PROBE_SANDBOX_PODMAN", "podman"))
    assert report["status"] == "passed"
    assert report["image"] == image
    assert len(report["runs"]) == 4
