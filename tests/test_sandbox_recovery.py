import hashlib
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "deploy/resume-controller-sandbox.py"
spec = importlib.util.spec_from_file_location("sandbox_recovery", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(mode)


@pytest.fixture
def installed(tmp_path):
    root, fs = tmp_path / "installed", tmp_path / "filesystem"
    root.mkdir(mode=0o755)
    users = {name: os.getuid() for name in (*recovery.ACCOUNTS, "human")}
    groups = {name: os.getgid() for name in (*recovery.ACCOUNTS, "probe-ipc", "probe-ledger-read", "probe-stop", "probe-backup-read")}
    executable = root / "python/bin/python3.13"
    executable.parent.mkdir(parents=True)
    shutil.copyfile("/usr/bin/true", executable)
    executable.chmod(0o755)
    write(root / "python/lib/python3.13/example.py", "value = 1\n")
    cache = root / "python/lib/python3.13/__pycache__/example.cpython-313.pyc"
    write(cache, "original generated cache")
    for path in (PROJECT / "deploy/live").iterdir():
        text = path.read_text()
        if path.name == "probe-research.service":
            text = text.replace("# Rootless mapping helpers require the account's own primary group.\n", "")
            text = text.replace("\nGroup=probe-trusted\n", "\nGroup=probe-research\n")
            text = text.replace("SupplementaryGroups=probe-research probe-ipc probe-ledger-read", "SupplementaryGroups=probe-trusted probe-ipc probe-ledger-read")
            text = text.replace("ExecStartPre=/usr/bin/chgrp probe-research /run/probe-research\n", "")
            # This fixture reconstructs the original historical unit, before
            # both the primary-group repair and the main-command prologue.
            text = text.replace("# Set the socket directory group after systemd prepares this command's runtime\n"
                                "# directory. A separate pre-start command is undone by the next command's setup.\n", "")
            text = text.replace("ExecStart=/bin/sh -ec '/usr/bin/chgrp probe-research /run/probe-research; exec ", "ExecStart=")
            text = text.replace("--config /etc/probe-core/research.json'\n", "--config /etc/probe-core/research.json\n")
        write(root / "deploy/live" / path.name, text)
    installer = (PROJECT / "deploy/install-controller.sh").read_text().replace(
        "runuser -u probe-trusted -g probe-trusted -- env HOME=", "runuser -u probe-trusted -g probe-research -- env HOME=")
    write(root / "deploy/install-controller.sh", installer)
    write(root / "deployment.json", json.dumps({"sandbox_image": "sha256:" + "a" * 64}))
    write(root / "deploy/sandbox/seccomp.json", "{}")
    entries = [{"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
               for p in sorted(root.rglob("*")) if p.is_file()]
    write(root / "release-manifest.json", json.dumps({"schema_version": 1, "files": entries}))
    digest = hashlib.sha256((root / "release-manifest.json").read_bytes()).hexdigest()
    # Model actual execution-generated extras: contained links and changed pyc.
    write(cache, "regenerated after relocation")
    write(root / "venv/pyvenv.cfg", "home = " + str(executable.parent))
    (root / "venv/bin").mkdir()
    (root / "venv/bin/python").symlink_to(executable)
    (root / "venv/lib").mkdir()
    (root / "venv/lib64").symlink_to("lib")
    write(root / "seccomp.json", "{}")
    units = recovery.expected_units(root, users, groups, "human")
    for name, text in units.items():
        write(root / "rendered" / name, text)
        write(fs / "etc/systemd/system" / name, text, 0o644)
    u, g = os.getuid(), os.getgid()
    research = {"ledger_path": "/var/lib/probe-core/research.sqlite", "socket_path": "/run/probe-research/research.sock",
        "service_uid": u, "research_uid": u, "admin_uid": u, "socket_gid": g,
        "policy": {"discovery_datasets": [], "allow_calibration": False},
        "controller_socket": "/run/probe-controller/research.sock", "controller_uid": u,
        "input_artifact_root": "/var/lib/probe-core/input-artifacts", "sandbox_image": None,
        "sandbox_workspace": "/var/lib/probe-sandbox/runs", "sandbox_seccomp_profile": "/opt/probe-core/seccomp.json", "podman_path": "/usr/bin/podman"}
    values = {"research.json": json.dumps(research), "dispatcher.json.pending": "{}",
        "research-runtime.env": f"XDG_RUNTIME_DIR=/run/user/{u}\nDBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{u}/bus\n",
        "backup.env": "SOURCE_COMMIT=" + "a" * 40 + "\n", "installation-plan.json": "{}",
        "sandbox-acceptance.env": "SANDBOX_IMAGE=sha256:" + "a" * 64 + "\n"}
    for name, text in values.items():
        write(root / "rendered" / name, text)
        write(fs / "etc/probe-core" / name, text)
    write(fs / "etc/probe-core/runpod.json", json.dumps({"state_path": "/var/lib/probe-provider/runpod.sqlite", "api_key_file": "/etc/probe-core/runpod-api-key"}))
    write(fs / "etc/probe-core/runpod-api-key", "synthetic fixture")
    write(fs / "var/lib/probe-backup/rclone.conf", "synthetic fixture")
    for relative, tables in (("var/lib/probe-core/research.sqlite", recovery.LEDGER_TABLES), ("var/lib/probe-provider/runpod.sqlite", {"runpod_intents"})):
        path = fs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            for table in tables:
                connection.execute(f'CREATE TABLE "{table}"(value TEXT)')
            connection.commit()
        path.chmod(0o600)
    write(fs / "var/lib/probe-core/research.audit.jsonl", "")
    for relative in ("var/lib/probe-core/input-artifacts", "var/lib/probe-core/worker-transfers", "var/lib/probe-watchdog",
        "var/lib/probe-backups/outbox", "var/lib/probe-backups/receipts", "var/lib/probe-research", "var/lib/probe-sandbox/.local"):
        (fs / relative).mkdir(parents=True, exist_ok=True)
    for base in (root, fs):
        for path in base.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
    return root, fs, digest, users, groups, units


def inactive(command, **kwargs):
    if command[1] == "list-units":
        return subprocess.CompletedProcess(command, 0, b"", b"")
    state = "static" if "acceptance" in command[2] or "snapshot" in command[2] else "disabled"
    return subprocess.CompletedProcess(command, 0, f"LoadState=loaded\nActiveState=inactive\nSubState=dead\nUnitFileState={state}\nDropInPaths=\n".encode(), b"")


def test_real_shaped_post_venv_state_including_regenerated_caches_and_links(installed):
    root, fs, digest, users, groups, units = installed
    original = recovery.verify_runtime(root, digest, owner=os.getuid())
    recovery.verify_units(root, units, filesystem_root=fs, owner=os.getuid(), run=inactive)
    recovery.verify_state(root, users, groups, "human", filesystem_root=fs, owner=os.getuid())
    assert original == (root / "deploy/install-controller.sh").read_bytes()


@pytest.mark.parametrize("kind", ["source", "native", "extra", "escaping_link", "writable_venv", "cache_without_source"])
def test_runtime_refuses_changes_outside_recognized_generated_files(installed, kind):
    root, _, digest, *_ = installed
    if kind == "source":
        write(root / "python/lib/python3.13/example.py", "tampered")
    elif kind == "native":
        write(root / "python/bin/python3.13", "tampered", 0o755)
    elif kind == "extra":
        write(root / "unexpected", "unreviewed")
    elif kind == "escaping_link":
        (root / "venv/bin/python").unlink()
        (root / "venv/bin/python").symlink_to("/usr/bin/python3")
    elif kind == "writable_venv":
        (root / "venv/pyvenv.cfg").chmod(0o622)
    else:
        write(root / "python/lib/python3.13/__pycache__/unknown.cpython-313.pyc", "unknown")
    with pytest.raises(recovery.RecoveryError):
        recovery.verify_runtime(root, digest, owner=os.getuid())


@pytest.mark.parametrize("kind", ["active", "enabled", "dropin", "changed_unit"])
def test_service_state_must_be_inactive_disabled_and_exact(installed, kind):
    root, fs, _, _, _, units = installed
    if kind == "changed_unit":
        write(fs / "etc/systemd/system/probe-research.service", "unexpected")

    def reply(command, **kwargs):
        result = inactive(command)
        if kind == "active": result.stdout = result.stdout.replace(b"ActiveState=inactive", b"ActiveState=active")
        if kind == "enabled": result.stdout = result.stdout.replace(b"UnitFileState=disabled", b"UnitFileState=enabled")
        if kind == "dropin": result.stdout = result.stdout.replace(b"DropInPaths=", b"DropInPaths=/etc/override.conf")
        return result

    with pytest.raises(recovery.RecoveryError):
        recovery.verify_units(root, units, filesystem_root=fs, owner=os.getuid(), run=reply)


@pytest.mark.parametrize("kind", ["job", "provider_intent", "wal", "backup", "sandbox_acceptance", "enabled_sandbox"])
def test_started_work_or_completed_gate_is_preserved_and_refused(installed, kind):
    root, fs, _, users, groups, _ = installed
    if kind in {"job", "provider_intent"}:
        relative, table = ("var/lib/probe-core/research.sqlite", "jobs") if kind == "job" else ("var/lib/probe-provider/runpod.sqlite", "runpod_intents")
        with closing(sqlite3.connect(fs / relative)) as connection:
            connection.execute(f'INSERT INTO "{table}" VALUES (?)', ("existing work",))
            connection.commit()
    elif kind == "wal": write(fs / "var/lib/probe-core/research.sqlite-wal", "uncheckpointed")
    elif kind == "backup": write(fs / "var/lib/probe-backups/outbox/retained.tar", "retained data")
    elif kind == "sandbox_acceptance": write(fs / "var/lib/probe-sandbox/acceptance-report.json", '{"status":"passed"}')
    else:
        path = fs / "etc/probe-core/research.json"
        data = json.loads(path.read_text())
        data["sandbox_image"] = "sha256:" + "a" * 64
        path.write_text(json.dumps(data))
    before = {str(p): p.read_bytes() for p in fs.rglob("*") if p.is_file()}
    with pytest.raises(recovery.RecoveryError):
        recovery.verify_state(root, users, groups, "human", filesystem_root=fs, owner=os.getuid())
    assert before == {str(p): p.read_bytes() for p in fs.rglob("*") if p.is_file()}


def test_only_two_installed_service_files_change_and_tail_stays_syntax_valid(installed, tmp_path):
    root, fs, _, users, _, units = installed
    before = {p: p.read_bytes() for p in fs.rglob("*") if p.is_file()}
    recovery.apply_unit_patches(units, filesystem_root=fs, owner=os.getuid())
    changed = {p.name for p, data in before.items() if p.read_bytes() != data}
    assert changed == {"probe-research.service", "probe-sandbox-acceptance.service"}
    for name in changed:
        text = (fs / "etc/systemd/system" / name).read_text()
        assert "\nGroup=probe-trusted\n" in text
        assert "SupplementaryGroups=probe-research probe-ipc probe-ledger-read" in text
        assert text.index("ExecStartPre=/usr/bin/chgrp") < text.index("ExecStartPre=/usr/bin/test -S")
        assert (root / "rendered" / name).read_text() == units[name]
    original = (root / "deploy/install-controller.sh").read_bytes()
    script = recovery.continuation(original, users["probe-trusted"])
    assert "apt-get" not in script and "usermod" not in script and "copy_gdrive_only" not in script
    assert script.count("runuser -u probe-trusted -g probe-trusted --") == 2
    assert "runuser -u probe-trusted -g probe-research --" not in script
    assert script.index("systemctl daemon-reload") < script.index("PROBE_SANDBOX_IMAGE=")
    assert "journalctl --unit probe-sandbox-acceptance.service" in script
    path = tmp_path / "continue.sh"
    path.write_text(script)
    subprocess.run(["/bin/bash", "-n", str(path)], check=True)


def test_real_nonroot_invocation_cannot_enter_recovery():
    if os.geteuid() == 0:
        pytest.skip("requires a genuinely nonroot process")
    result = subprocess.run(["/usr/bin/python3", "-I", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 1 and "requires the administrator" in result.stderr


def test_real_initialized_ledger_matches_the_recovery_empty_state(tmp_path):
    from probe_core.ledger import Ledger

    database = tmp_path / "research.sqlite"
    with Ledger(database):
        pass
    recovery.empty_database(database, recovery.LEDGER_TABLES, owner=os.getuid())
    assert (tmp_path / "research.audit.jsonl").read_bytes() == b""
    assert not any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
