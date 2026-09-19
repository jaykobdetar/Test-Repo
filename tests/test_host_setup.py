from dataclasses import replace
from pathlib import Path
import hashlib
import json
import shutil
import stat
import subprocess

import pytest

from probe_core.host_setup import Identities, render_configuration
from probe_core.research_service import ServiceConfig


def identities():
    return Identities(trusted_uid=991, research_uid=992, watchdog_uid=993, backup_uid=994,
                      human_uid=1000, ipc_gid=981, research_gid=992, stop_gid=982, backup_read_gid=983)


def render(tmp_path, **changes):
    return render_configuration(tmp_path / "rendered", identities=identities(),
                                templates=Path(__file__).resolve().parents[1] / "deploy/live",
                                source_commit="a" * 40, drive_folder_id="pinnedDriveFolder123", **changes)


def test_generated_facade_policy_and_paths_are_consistent(tmp_path):
    report = render(tmp_path)
    root = tmp_path / "rendered"
    config = ServiceConfig.model_validate_json((root / "research.json").read_bytes())
    dispatcher = json.loads((root / "dispatcher.json.pending").read_text())
    assert config.service_uid == 991 and config.research_uid == 992 and config.admin_uid == 1000
    assert config.input_artifact_root == dispatcher["input_artifact_root"]
    assert dispatcher["ssh"]["user"] == "root"  # Trusted SSH bootstrap; numerical worker drops UID.
    assert config.policy.discovery_datasets == () and config.policy.allow_calibration is False
    assert config.sandbox_image is None and not report["services_started"]
    assert "XDG_RUNTIME_DIR=/run/user/991\n" in (root / "research-runtime.env").read_text()
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/991/bus" in (root / "research-runtime.env").read_text()


def test_watchdog_has_stop_socket_and_no_provider_credential_path(tmp_path):
    render(tmp_path)
    root = tmp_path / "rendered"
    watchdog = (root / "probe-watchdog.service").read_text()
    assert "--stop-socket /run/probe-provider/stop.sock --stop-server-uid 991" in watchdog
    assert "--provider-config" not in watchdog and "runpod.json" not in watchdog
    assert "\nRequires=" not in watchdog and "\nPartOf=" not in watchdog and "\nBindsTo=" not in watchdog
    broker = (root / "probe-provider-stop.service").read_text()
    assert "--watchdog-uid 993 --socket-gid 982" in broker
    research = (root / "probe-research.service").read_text()
    assert "/run/user/991" in research and "ProtectHome=read-only" in research
    assert "%U" not in research and "@USER_RUNTIME@" not in research
    backup = (root / "probe-backup.service").read_text()
    assert "User=probe-backup" in backup and "runpod" not in backup.lower()
    assert "/var/lib/probe-core" not in backup
    controller = (root / "probe-controller.service").read_text()
    assert "setfacl --modify u:1000:r-x,d:u:1000:rw- /run/probe-controller" in controller
    assert (root / "probe-backup.timer").is_file()


def test_cpu_acceptance_uses_the_actual_research_service_security_profile(tmp_path):
    render(tmp_path)
    root = tmp_path / "rendered"
    research = (root / "probe-research.service").read_text().splitlines()
    acceptance = (root / "probe-sandbox-acceptance.service").read_text().splitlines()
    profile_keys = {"User", "Group", "SupplementaryGroups", "WorkingDirectory", "Environment",
                    "ProtectSystem", "ProtectHome", "ReadWritePaths", "PrivateTmp", "NoNewPrivileges",
                    "KillMode", "Delegate", "UMask", "RuntimeDirectory", "RuntimeDirectoryMode"}
    profile = lambda lines: [line for line in lines if line.split("=", 1)[0] in profile_keys]
    assert profile(research) == profile(acceptance)
    assert "Type=oneshot" in acceptance
    assert "EnvironmentFile=/etc/probe-core/research-runtime.env" in acceptance
    assert "EnvironmentFile=/etc/probe-core/sandbox-acceptance.env" in acceptance
    assert any("-I -m probe_core.sandbox_acceptance --image ${SANDBOX_IMAGE}" in line for line in acceptance)


@pytest.mark.parametrize("field", ["trusted_uid", "research_uid", "watchdog_uid", "backup_uid"])
def test_human_or_service_identity_reuse_is_rejected(field):
    with pytest.raises(ValueError, match="differ"):
        replace(identities(), **{field: 1000})


@pytest.mark.parametrize("value", [0, -1, True])
def test_root_or_ambiguous_identity_is_rejected(value):
    with pytest.raises(ValueError):
        replace(identities(), watchdog_uid=value)


def test_render_never_overwrites_existing_configuration(tmp_path):
    render(tmp_path)
    path = tmp_path / "rendered/research.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="new"):
        render(tmp_path)
    assert path.read_bytes() == before


def test_sandbox_requires_immutable_image(tmp_path):
    with pytest.raises(ValueError, match="immutable"):
        render(tmp_path, sandbox_image="python:latest")


def test_first_backup_has_provider_state_before_async_service_start():
    installer = (Path(__file__).resolve().parents[1] / "deploy/install-controller.sh").read_text()
    initialization = ('runuser -u probe-trusted -g probe-trusted -- /opt/probe-core/venv/bin/python -I -c '
                      '\'from probe_core.runpod_provider import RunPodConfig, RunPodProvider; '
                      'RunPodProvider(RunPodConfig.load("/etc/probe-core/runpod.json"))\'')
    assert initialization in installer
    assert installer.index('install -o probe-trusted -g probe-trusted -m 0600') < installer.index(initialization)
    assert installer.index(initialization) < installer.index('systemctl enable --now probe-provider-stop.service')
    assert installer.index(initialization) < installer.index('systemctl start probe-backup.service')


def test_staged_runtime_restores_only_verified_interpreter_execution(tmp_path):
    installer = (Path(__file__).resolve().parents[1] / "deploy/install-controller.sh").read_text()
    copy_command = next(line for line in installer.splitlines()
                        if line.startswith("cp -R --no-preserve=all --no-dereference -- "))
    repair_command = 'chmod 0755 "$PROBE_STAGE/python/bin/python3.13"'
    assert repair_command in installer
    assert installer.index('chown -hR root:root "$PROBE_STAGE"') < installer.index(repair_command)
    assert installer.index(repair_command) < installer.index('mv -- "$PROBE_STAGE" /opt/probe-core')

    bundle = tmp_path / "bundle"
    source = bundle / "python/bin/python3.13"
    source.parent.mkdir(parents=True)
    # Execute a real harmless ELF binary; no administrator or system changes.
    shutil.copyfile("/usr/bin/true", source)
    source.chmod(0o755)
    (source.parent / "python3").symlink_to("python3.13")
    (bundle / "data.json").write_text('{}\n')
    (bundle / "data.json").chmod(0o644)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    subprocess.run([
        "/bin/bash", "-c",
        'set -euo pipefail\numask 077\nPROBE_BUNDLE=$1\nPROBE_STAGE=$2\n' + copy_command,
        "installer-copy-test", str(bundle), str(stage),
    ], check=True)
    copied = stage / "python/bin/python3.13"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert hashlib.sha256(copied.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
    with pytest.raises(PermissionError):
        subprocess.run([str(copied)], check=True)

    subprocess.run([
        "/bin/bash", "-c", 'set -euo pipefail\nPROBE_STAGE=$1\n' + repair_command,
        "installer-permission-test", str(stage),
    ], check=True)
    subprocess.run([str(copied)], check=True)
    alias = stage / "python/bin/python3"
    assert alias.is_symlink()
    subprocess.run([str(alias)], check=True)
    assert stat.S_IMODE(copied.stat().st_mode) == 0o755
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    assert stat.S_IMODE((stage / "data.json").stat().st_mode) == 0o600
