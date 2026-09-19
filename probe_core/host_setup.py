"""Render reviewed controller configuration from installed account identities.

This module writes a new staging directory only. The separate administrator
installer creates accounts and installs files; neither action happens on import.
"""
from __future__ import annotations

import argparse
import configparser
from dataclasses import asdict, dataclass
import grp
import json
import os
from pathlib import Path
import pwd
import re

from .audit import canonical_json


@dataclass(frozen=True)
class Identities:
    trusted_uid: int
    research_uid: int
    watchdog_uid: int
    backup_uid: int
    human_uid: int
    ipc_gid: int
    research_gid: int
    stop_gid: int
    backup_read_gid: int

    def __post_init__(self):
        values = asdict(self)
        if any(type(value) is not int or value <= 0 for value in values.values()):
            raise ValueError("identities must be positive numeric OS IDs")
        if len({self.trusted_uid, self.research_uid, self.watchdog_uid, self.backup_uid, self.human_uid}) != 5:
            raise ValueError("human, trusted, research, watchdog and backup accounts must differ")

    @classmethod
    def discover(cls, human: str):
        return cls(pwd.getpwnam("probe-trusted").pw_uid, pwd.getpwnam("probe-research").pw_uid,
                   pwd.getpwnam("probe-watchdog").pw_uid, pwd.getpwnam("probe-backup").pw_uid,
                   pwd.getpwnam(human).pw_uid, grp.getgrnam("probe-ipc").gr_gid,
                   grp.getgrnam("probe-research").gr_gid, grp.getgrnam("probe-stop").gr_gid,
                   grp.getgrnam("probe-backup-read").gr_gid)


def render_configuration(destination: str | Path, *, identities: Identities,
                         templates: str | Path, source_commit: str,
                         drive_folder_id: str, sandbox_image: str | None = None) -> dict:
    destination, templates = Path(destination), Path(templates)
    if destination.exists() or destination.is_symlink():
        raise ValueError("render destination must be new")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit) is None:
        raise ValueError("full reviewed source commit required")
    if re.fullmatch(r"[A-Za-z0-9_-]{10,256}", drive_folder_id) is None:
        raise ValueError("pinned Drive folder ID required")
    if sandbox_image is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", sandbox_image) is None:
        raise ValueError("sandbox image must be immutable")
    destination.mkdir(mode=0o700, parents=True)
    inputs = "/var/lib/probe-core/input-artifacts"
    config = {"ledger_path": "/var/lib/probe-core/research.sqlite", "socket_path": "/run/probe-research/research.sock",
              "service_uid": identities.trusted_uid, "research_uid": identities.research_uid,
              "admin_uid": identities.human_uid, "socket_gid": identities.research_gid,
              "policy": {"discovery_datasets": [], "allow_calibration": False},
              "controller_socket": "/run/probe-controller/research.sock", "controller_uid": identities.trusted_uid,
              "input_artifact_root": inputs, "sandbox_image": sandbox_image,
              "sandbox_workspace": "/var/lib/probe-sandbox/runs", "sandbox_seccomp_profile": "/opt/probe-core/seccomp.json",
              "podman_path": "/usr/bin/podman"}
    (destination / "research.json").write_text(canonical_json(config))
    # Dispatcher stays uninstalled/unstarted until a reviewed worker is bound.
    dispatcher = {"ledger_path": config["ledger_path"], "worker_id": "REQUIRES_APPROVED_WORKER_ID",
                  "transfer_directory": "/var/lib/probe-core/worker-transfers", "input_artifact_root": inputs,
                  "bearer_secret_file": "/etc/probe-core/worker-token", "lease_seconds": 30, "poll_seconds": 0.25,
                  "ssh": {"host": "REQUIRES_VERIFIED_WORKER_HOST", "user": "root", "ssh_port": 22,
                          "remote_port": 8080, "identity_file": "/etc/probe-core/worker-ssh-key",
                          "known_hosts_file": "/etc/probe-core/worker-known-hosts"}}
    (destination / "dispatcher.json.pending").write_text(canonical_json(dispatcher))
    runtime = f"/run/user/{identities.trusted_uid}"
    (destination / "research-runtime.env").write_text(f"XDG_RUNTIME_DIR={runtime}\nDBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus\n")
    (destination / "backup.env").write_text(f"SOURCE_COMMIT={source_commit}\nDRIVE_FOLDER_ID={drive_folder_id}\n")
    replacements = {"TRUSTED_UID": str(identities.trusted_uid), "HUMAN_UID": str(identities.human_uid),
                    "AGENT_UID": str(identities.research_uid), "WATCHDOG_UID": str(identities.watchdog_uid),
                    "IPC_GID": str(identities.ipc_gid), "STOP_GID": str(identities.stop_gid),
                    "USER_RUNTIME": runtime}
    for template in sorted([*templates.glob("*.service"), *templates.glob("*.timer")]):
        text = template.read_text()
        for key, value in replacements.items():
            text = text.replace("@" + key + "@", value)
        if re.search(r"@[A-Z_]+@", text):
            raise ValueError("unresolved service template value")
        (destination / template.name).write_text(text)
    # Derive the one-shot gate from the actual service profile so containment
    # cannot silently drift between a successful check and normal execution.
    research_unit = (destination / "probe-research.service").read_text()
    acceptance_lines = []
    for line in research_unit.splitlines():
        if line.startswith(("Restart=", "RestartSec=", "WantedBy=", "After=")) or line == "[Install]":
            continue
        if line.startswith("Description="):
            line = "Description=Probe mandatory CPU sandbox acceptance under the research service profile"
        elif line == "Type=simple":
            line = "Type=oneshot\nTimeoutStartSec=240\nEnvironmentFile=/etc/probe-core/sandbox-acceptance.env"
        elif line.startswith("ExecStart="):
            line = ("ExecStart=/opt/probe-core/venv/bin/python -I -m probe_core.sandbox_acceptance "
                    "--image ${SANDBOX_IMAGE} --workspace /var/lib/probe-sandbox/acceptance "
                    "--podman /usr/bin/podman --seccomp-profile /opt/probe-core/seccomp.json "
                    "--output /var/lib/probe-sandbox/acceptance-report.json")
        acceptance_lines.append(line)
    (destination / "probe-sandbox-acceptance.service").write_text("\n".join(acceptance_lines) + "\n")
    for file in destination.iterdir():
        file.chmod(0o600)
    report = {"identities": asdict(identities), "source_commit": source_commit,
              "drive_folder_id": drive_folder_id, "sandbox_enabled": sandbox_image is not None,
              "services_started": False, "dispatcher_requires_worker_configuration": True}
    (destination / "installation-plan.json").write_text(canonical_json(report))
    return report


def copy_gdrive_only(source: str | Path, destination: str | Path, *, backup_uid: int, backup_gid: int) -> None:
    """Administrator-only credential handoff. Never print or return secret values."""
    if os.geteuid() != 0:
        raise PermissionError("credential installation requires the administrator")
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file() or destination.exists() or destination.is_symlink():
        raise ValueError("credential source must be regular and destination new")
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.read(source)
    if "gdrive" not in parsed or parsed["gdrive"].get("type") != "drive":
        raise ValueError("a gdrive Drive remote is required")
    selected = configparser.ConfigParser(interpolation=None)
    selected["gdrive"] = dict(parsed["gdrive"])
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        selected.write(stream)
        stream.flush()
        os.fchown(stream.fileno(), backup_uid, backup_gid)
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--templates", type=Path, required=True)
    parser.add_argument("--human", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--drive-folder-id", required=True)
    parser.add_argument("--sandbox-image")
    args = parser.parse_args()
    print(canonical_json(render_configuration(args.output, identities=Identities.discover(args.human),
          templates=args.templates, source_commit=args.source_commit, drive_folder_id=args.drive_folder_id,
          sandbox_image=args.sandbox_image)))


if __name__ == "__main__":
    main()
