import hashlib
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from test_sandbox_recovery import installed, inactive, recovery as verifier, write


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "deploy/resume-controller-runtime.py"
spec = importlib.util.spec_from_file_location("runtime_recovery", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.fixture
def failed_install(installed):
    root, fs, digest, users, groups, units = installed
    verifier.apply_unit_patches(units, filesystem_root=fs, owner=os.getuid())
    sandbox = fs / "var/lib/probe-sandbox"
    (sandbox / "acceptance").mkdir(mode=0o700)
    report = {"schema_version": 1, "status": "failed", "image": "sha256:" + "a" * 64,
              "service_uid": os.getuid(), "stage": "cpu_and_isolation",
              "checks": {"immutable_image_present": True}, "error_type": "AcceptanceError",
              "seccomp_sha256": "sha256:" + hashlib.sha256((root / "seccomp.json").read_bytes()).hexdigest(),
              "started_at": "2026-09-19T18:06:52.835057+00:00", "finished_at": "2026-09-19T18:07:01.762376+00:00"}
    write(sandbox / "acceptance-report.json", json.dumps(report))
    return installed


def failed_acceptance(command, **kwargs):
    result = inactive(command)
    if command[1] == "show":
        if command[2] == "probe-sandbox-acceptance.service":
            result.stdout = result.stdout.replace(b"ActiveState=inactive\nSubState=dead", b"ActiveState=failed\nSubState=failed")
            result.stdout += b"ExecMainStatus=1\nResult=exit-code\n"
        else:
            result.stdout += b"ExecMainStatus=0\nResult=success\n"
    return result


def validate(fixture):
    root, fs, digest, users, groups, units = fixture
    verifier.verify_runtime(root, digest, owner=os.getuid())
    verifier.verify_units(root, units, filesystem_root=fs, owner=os.getuid(), run=failed_acceptance,
                          repaired_groups=True, failed_acceptance=True)
    verifier.verify_state(root, users, groups, "human", filesystem_root=fs, owner=os.getuid(), allow_failed_acceptance=True)


def test_only_known_failed_state_is_allowed_by_explicit_opt_in(failed_install):
    root, fs, _, users, groups, units = failed_install
    before = {p: p.read_bytes() for p in fs.rglob("*") if p.is_file()}
    validate(failed_install)
    with pytest.raises(verifier.RecoveryError):
        verifier.verify_units(root, units, filesystem_root=fs, owner=os.getuid(), run=failed_acceptance)
    with pytest.raises(verifier.RecoveryError):
        verifier.verify_state(root, users, groups, "human", filesystem_root=fs, owner=os.getuid())
    assert before == {p: p.read_bytes() for p in fs.rglob("*") if p.is_file()}


@pytest.mark.parametrize("change", ["passed", "later_stage", "runs", "other_image", "other_uid", "other_profile", "checks", "time", "workspace", "job", "unit"])
def test_recovery_refuses_any_other_failure_or_started_work(failed_install, change):
    root, fs, *_ = failed_install
    path = fs / "var/lib/probe-sandbox/acceptance-report.json"
    report = json.loads(path.read_bytes())
    changes = {"passed": ("status", "passed"), "later_stage": ("stage", "memory"), "runs": ("runs", {}),
               "other_image": ("image", "sha256:" + "b" * 64), "other_uid": ("service_uid", os.getuid() + 1),
               "other_profile": ("seccomp_sha256", "sha256:" + "b" * 64), "checks": ("checks", {}),
               "time": ("finished_at", "2026-09-18T00:00:00+00:00")}
    if change in changes:
        key, value = changes[change]
        report[key] = value
        write(path, json.dumps(report))
    elif change == "workspace":
        write(path.parent / "acceptance/uncollected-output", "must preserve")
    elif change == "job":
        import sqlite3
        with closing(sqlite3.connect(fs / "var/lib/probe-core/research.sqlite")) as connection:
            connection.execute("INSERT INTO jobs VALUES ('must preserve')")
            connection.commit()
    else:
        unit = fs / "etc/systemd/system/probe-research.service"
        unit.write_text(unit.read_text().replace("ProtectSystem=strict", "ProtectSystem=no"))
    before = {p: p.read_bytes() for p in fs.rglob("*") if p.is_file()}
    with pytest.raises(verifier.RecoveryError):
        validate(failed_install)
    assert before == {p: p.read_bytes() for p in fs.rglob("*") if p.is_file()}


@pytest.mark.parametrize("change", ["inactive_gate", "different_exit", "different_result", "active_other", "override"])
def test_failed_service_state_is_exact(failed_install, change):
    root, fs, _, _, _, units = failed_install

    def reply(command, **kwargs):
        result = failed_acceptance(command)
        if command[1] == "show":
            if command[2] == "probe-sandbox-acceptance.service":
                replacements = {"inactive_gate": (b"ActiveState=failed\nSubState=failed", b"ActiveState=inactive\nSubState=dead"),
                                "different_exit": (b"ExecMainStatus=1", b"ExecMainStatus=2"),
                                "different_result": (b"Result=exit-code", b"Result=signal")}
                if change in replacements:
                    result.stdout = result.stdout.replace(*replacements[change])
            elif change == "active_other":
                result.stdout = result.stdout.replace(b"ActiveState=inactive", b"ActiveState=active")
            if change == "override":
                result.stdout = result.stdout.replace(b"DropInPaths=", b"DropInPaths=/etc/unreviewed")
        return result

    with pytest.raises(verifier.RecoveryError):
        verifier.verify_units(root, units, filesystem_root=fs, owner=os.getuid(), run=reply,
                              repaired_groups=True, failed_acceptance=True)


def test_pinned_verifier_executes_checked_bytes_and_refuses_bad_hash_or_link(tmp_path):
    path = tmp_path / "checks.py"
    write(path, "VALUE = 'verified'\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert recovery.load_verifier(path, digest, owner=os.getuid()).VALUE == "verified"
    write(path, "raise AssertionError('must not execute')\n")
    with pytest.raises(RuntimeError, match="checksum"):
        recovery.load_verifier(path, digest, owner=os.getuid())
    path.unlink()
    target = tmp_path / "target.py"
    write(target, "VALUE = 'verified'\n")
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match="regular file"):
        recovery.load_verifier(path, digest, owner=os.getuid())


def test_runtime_config_matches_source_and_preserves_unrelated_files(tmp_path):
    home = tmp_path / "home"
    retained = home / ".config/containers/retained-data"
    write(retained, "existing private data")
    for directory in (home, home / ".config", retained.parent):
        directory.chmod(0o700)
    recovery.install_runtime_config(home, os.getuid(), os.getgid(), owner=os.getuid())
    config = retained.parent / "containers.conf"
    assert config.read_bytes() == recovery.RUNTIME_CONFIG == (PROJECT / "deploy/sandbox/containers.conf").read_bytes()
    assert config.stat().st_mode & 0o777 == 0o640
    assert all(directory.stat().st_mode & 0o777 == 0o750 for directory in (home / ".config", retained.parent))
    assert retained.read_text() == "existing private data"
    before = config.read_bytes()
    with pytest.raises(RuntimeError, match="not be overwritten"):
        recovery.install_runtime_config(home, os.getuid(), os.getgid(), owner=os.getuid())
    assert config.read_bytes() == before and retained.read_text() == "existing private data"


@pytest.mark.parametrize("unsafe", ["symlink_parent", "writable_parent", "existing_config", "symlink_config"])
def test_runtime_configuration_location_fails_closed(tmp_path, unsafe):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    if unsafe == "symlink_parent":
        (tmp_path / "other").mkdir()
        (home / ".config").symlink_to(tmp_path / "other", target_is_directory=True)
    elif unsafe == "writable_parent":
        (home / ".config").mkdir(mode=0o777)
        (home / ".config").chmod(0o777)
    else:
        config = home / ".config/containers/containers.conf"
        config.parent.mkdir(parents=True)
        config.parent.chmod(0o750)
        config.parent.parent.chmod(0o750)
        if unsafe == "existing_config":
            write(config, "must preserve")
        else:
            config.symlink_to(tmp_path / "missing")
    with pytest.raises(RuntimeError):
        recovery.verify_config_location(home, os.getuid(), owner=os.getuid())


@pytest.mark.parametrize("path,code,passes", [(b"/usr/bin/crun\n", 0, True), (b"/usr/bin/runc\n", 0, False), (b"/usr/bin/crun\n", 1, False)])
def test_actual_runtime_selection_uses_trusted_identity_store_and_bus(path, code, passes):
    def run(command, **kwargs):
        assert command[:7] == ["/usr/sbin/runuser", "-u", "probe-trusted", "-g", "probe-trusted", "--", "env"]
        assert "HOME=/var/lib/probe-sandbox" in command and "XDG_RUNTIME_DIR=/run/user/994" in command
        assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/994/bus" in command
        assert command[-5:] == ["/usr/bin/podman", "--remote=false", "info", "--format", "{{.Host.OCIRuntime.Path}}"]
        assert kwargs["env"] == recovery.ENV and kwargs["timeout"] == 30
        return subprocess.CompletedProcess(command, code, path, b"")
    if passes:
        recovery.verify_selected_runtime(994, run=run)
    else:
        with pytest.raises(RuntimeError, match="did not select"):
            recovery.verify_selected_runtime(994, run=run)


@pytest.mark.parametrize("gate_succeeds", [False, True])
def test_continuation_preserves_gate_order_and_never_reimports(installed, tmp_path, gate_succeeds):
    root, *_ = installed
    script = recovery.continuation((root / "deploy/install-controller.sh").read_bytes(), 994)
    assert "podman --remote=false load" not in script and "apt-get" not in script and "usermod" not in script
    assert script.index("systemctl start probe-sandbox-acceptance.service") < script.index("data['sandbox_image']") < script.index("systemctl enable --now")
    assert "PROBE_RECOVERY_SINCE=$(/usr/bin/date +%s)" in script
    assert '--since "@$PROBE_RECOVERY_SINCE" "_UID=$PROBE_TRUSTED_UID" _COMM=conmon' in script
    file = tmp_path / "continue.sh"
    file.write_text(script)
    subprocess.run(["/bin/bash", "-n", str(file)], check=True)
    # Execute the real extracted control flow with harmless local command
    # stand-ins. Failed acceptance must prevent configuration and services.
    log = tmp_path / "events"
    python = tmp_path / "python"
    write(python, "#!/bin/bash\nif [ \"$2\" = -c ]; then printf 'sha256:" + "a" * 64 + "\\n'; else cat >/dev/null; echo configure >> " + shlex.quote(str(log)) + "; fi\n", 0o755)
    functions = ("systemctl() { echo \"$*\" >> " + shlex.quote(str(log)) + "; "
                 + ("return 0;" if gate_succeeds else '[ "$*" != "start probe-sandbox-acceptance.service" ];') + " }\n"
                 + "runuser() { printf 'sha256:" + "a" * 64 + "\\n'; }\n")
    harmless = script.replace("/opt/probe-core/venv/bin/python", str(python)).replace("/usr/bin/systemctl", "systemctl").replace("/usr/bin/journalctl", "/bin/true")
    harmless = harmless.replace("PROBE_RECOVERY_SINCE=", functions + "PROBE_RECOVERY_SINCE=", 1)
    file.write_text(harmless)
    result = subprocess.run(["/bin/bash", str(file)], capture_output=True, text=True)
    events = log.read_text().splitlines()
    assert result.returncode == (0 if gate_succeeds else 1)
    if gate_succeeds:
        assert events.index("start probe-sandbox-acceptance.service") < events.index("configure")
        assert "enable --now probe-backup.timer" in events and "start probe-backup.service" in events
    else:
        assert events == ["daemon-reload", "start probe-sandbox-acceptance.service"]


def test_main_root_guard_is_real():
    if os.geteuid() == 0:
        pytest.skip("requires an actual non-root process")
    result = subprocess.run(["/usr/bin/python3", "-I", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 1 and "requires the administrator" in result.stderr
