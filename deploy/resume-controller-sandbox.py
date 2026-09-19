#!/usr/bin/python3
"""Resume the known post-venv, pre-image-import installation, as administrator.

This standalone helper preserves installed data and credentials. It repairs only
the two rootless service group settings and the two image-import group arguments.
"""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile

ROOT = Path("/opt/probe-core")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
ACCOUNTS = ("probe-trusted", "probe-research", "probe-watchdog", "probe-backup")
UNITS = {"probe-research.service", "probe-controller.service", "probe-backup.service",
         "probe-provider-stop.service", "probe-watchdog.service", "probe-backup.timer",
         "probe-snapshot.service", "probe-backup-prune.service", "probe-sandbox-acceptance.service"}
CONFIGS = {"research.json", "dispatcher.json.pending", "research-runtime.env", "backup.env",
           "installation-plan.json", "sandbox-acceptance.env"}
LEDGER_TABLES = {"jobs", "attempts", "approvals", "approval_jobs", "hypotheses", "manifests", "audit_events"}


class RecoveryError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def regular(path: Path, owner: int, *, private=False):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
            and not info.st_mode & (0o077 if private else 0o022), "Unsafe or unexpected file: " + str(path))
    return info


def directory(path: Path, owner: int):
    info = path.lstat()
    require(path.resolve() == path.absolute() and stat.S_ISDIR(info.st_mode)
            and info.st_uid == owner and not info.st_mode & 0o022, "Unsafe directory: " + str(path))


def cache_source(name: str) -> str | None:
    path = PurePosixPath(name)
    match = re.fullmatch(r"(.+)\.cpython-313(?:\.opt-[12])?\.pyc", path.name)
    if path.parent.name == "__pycache__" and match:
        return str(path.parent.parent / (match[1] + ".py"))
    return None


def verify_runtime(root: Path, digest: str, *, owner=0) -> bytes:
    """Pin all base source/native bytes; validate root-generated venv/cache ownership."""
    directory(root, owner)
    regular(root / "release-manifest.json", owner)
    raw = (root / "release-manifest.json").read_bytes()
    require(re.fullmatch(r"[0-9a-f]{64}", digest) and hashlib.sha256(raw).hexdigest() == digest,
            "Pinned original manifest checksum mismatch")
    manifest = json.loads(raw)
    require(manifest.get("schema_version") == 1 and type(manifest.get("files")) is list, "Unsupported manifest")
    entries = {}
    for entry in manifest["files"]:
        require(type(entry) is dict and set(entry) == {"path", "sha256"}, "Invalid manifest member")
        name, checksum = entry["path"], entry["sha256"]
        require(isinstance(name, str) and name and not name.startswith("/") and "\\" not in name
                and "\x00" not in name and all(part not in {"", ".", ".."} for part in name.split("/")), "Unsafe manifest path")
        require(name not in entries and name != "release-manifest.json" and isinstance(checksum, str)
                and re.fullmatch(r"[0-9a-f]{64}", checksum), "Duplicate or invalid manifest digest")
        entries[name] = checksum
    require({"deploy/install-controller.sh", "python/bin/python3.13", "deployment.json", "deploy/sandbox/seccomp.json"} <= entries.keys(),
            "Pinned bundle lacks required recovery inputs")
    base_dirs = {str(parent) for name in entries for parent in PurePosixPath(name).parents if str(parent) != "."}
    seen = set()
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        info = path.lstat()
        require(info.st_uid == owner, "Installed runtime has a non-administrator-owned member")
        source = cache_source(name)
        generated_cache = name.startswith("python/") and source in entries
        venv = name == "venv" or name.startswith("venv/")
        rendered = name == "rendered" or name.startswith("rendered/")
        cache_dir = path.name == "__pycache__" and any(str(PurePosixPath(item).parent) == str(PurePosixPath(name).parent) for item in entries)
        if stat.S_ISLNK(info.st_mode):
            target = path.resolve(strict=True)
            require(target.is_relative_to(root) and (target.is_file() or venv and target.is_dir()), "Runtime symlink escapes its trusted tree")
        elif stat.S_ISDIR(info.st_mode):
            require(not info.st_mode & 0o022 and (name in base_dirs or venv or rendered or cache_dir), "Unexpected or writable runtime directory")
            continue
        else:
            regular(path, owner)
        require(name in entries or name == "release-manifest.json" or venv or rendered or name == "seccomp.json" or generated_cache,
                "Unexpected installed runtime file: " + name)
        seen.add(name)
        # Running the relocated interpreter regenerates timestamp-based caches.
        # Only caches with a pinned corresponding source receive this exemption.
        if name in entries and not generated_cache:
            with path.open("rb") as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == entries[name], "Pinned base content changed: " + name)
    require(set(entries) <= seen, "Pinned base files are missing")
    directory(root / "venv", owner)
    directory(root / "rendered", owner)
    require({p.name for p in (root / "rendered").iterdir()} == UNITS | CONFIGS, "Unexpected rendered installation inventory")
    require((root / "venv/bin/python").resolve() == root / "python/bin/python3.13", "Venv interpreter points outside the pinned runtime")
    regular(root / "python/bin/python3.13", owner)
    require(os.access(root / "python/bin/python3.13", os.X_OK), "Pinned interpreter is not executable")
    require((root / "seccomp.json").read_bytes() == (root / "deploy/sandbox/seccomp.json").read_bytes(), "Installed seccomp profile changed")
    return (root / "deploy/install-controller.sh").read_bytes()


def identities(human: str):
    users = {name: pwd.getpwnam(name) for name in (*ACCOUNTS, human)}
    require(human not in ACCOUNTS and len({item.pw_uid for item in users.values()}) == 5
            and all(item.pw_uid > 0 for item in users.values()), "Human and service identities must be distinct and non-root")
    groups = {name: grp.getgrnam(name).gr_gid for name in (*ACCOUNTS, "probe-ipc", "probe-ledger-read", "probe-stop", "probe-backup-read")}
    for name in ACCOUNTS:
        require(users[name].pw_gid == groups[name], "Service primary group differs from passwd account")
    return {name: item.pw_uid for name, item in users.items()}, groups


def expected_units(root: Path, users: dict, groups: dict, human: str) -> dict[str, str]:
    values = {"TRUSTED_UID": users["probe-trusted"], "HUMAN_UID": users[human], "AGENT_UID": users["probe-research"],
              "WATCHDOG_UID": users["probe-watchdog"], "IPC_GID": groups["probe-ipc"], "STOP_GID": groups["probe-stop"],
              "USER_RUNTIME": f'/run/user/{users["probe-trusted"]}'}
    result = {}
    for path in (root / "deploy/live").iterdir():
        require(path.name in UNITS - {"probe-sandbox-acceptance.service"}, "Unexpected signed service template")
        text = path.read_text()
        for key, value in values.items():
            text = text.replace("@" + key + "@", str(value))
        require(not re.search(r"@[A-Z_]+@", text), "Unresolved service template")
        result[path.name] = text
    lines = []
    for line in result["probe-research.service"].splitlines():
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
        lines.append(line)
    result["probe-sandbox-acceptance.service"] = "\n".join(lines) + "\n"
    require(set(result) == UNITS, "Incomplete signed unit inventory")
    return result


def verify_units(root: Path, expected: dict, *, filesystem_root=Path("/"), owner=0, run=subprocess.run):
    installed = filesystem_root / "etc/systemd/system"
    listed = run(["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager", "probe-*"],
                 capture_output=True, env=ENV, timeout=30, check=False)
    require(listed.returncode == 0 and all(line.split()[0] in expected for line in listed.stdout.decode().splitlines() if line.strip()),
            "Unexpected loaded Probe unit or failed systemd inspection")
    for name, text in expected.items():
        for path in (installed / name, root / "rendered" / name):
            regular(path, owner)
            require(path.read_text() == text, "Installed unit differs from its original generated template: " + name)
        reply = run(["/usr/bin/systemctl", "show", name, "--no-pager", "--property=LoadState,ActiveState,SubState,UnitFileState,DropInPaths"],
                    capture_output=True, env=ENV, timeout=30, check=False)
        require(reply.returncode == 0, "Unable to inspect installed service state")
        state = dict(line.split("=", 1) for line in reply.stdout.decode().splitlines() if "=" in line)
        require(state.get("LoadState") == "loaded" and state.get("ActiveState") == "inactive" and state.get("SubState") == "dead"
                and state.get("UnitFileState") in {"disabled", "static"} and state.get("DropInPaths") == "", "Probe service has unexpected activity, enablement or overrides: " + name)
    for relative in ("etc/systemd/system", "run/systemd/system", "usr/lib/systemd/system", "usr/local/lib/systemd/system"):
        folder = filesystem_root / relative
        if folder.exists():
            for path in folder.rglob("*"):
                if path.name.startswith("probe-"):
                    require(path.parent == installed and path.name in expected and not path.is_symlink(), "Unexpected Probe unit, enablement link or override")


def empty_database(path: Path, tables: set[str], *, owner: int):
    regular(path, owner)
    require(not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")), "Database has live or uncheckpointed sidecars")
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        require(connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Database integrity check failed")
        found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        require(found == tables, "Database schema differs from the untouched initialized state")
        for name in tables:
            require(connection.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] == 0, "Database already contains work; refusing installation replay")
    finally:
        connection.close()


def verify_state(root: Path, users: dict, groups: dict, human: str, *, filesystem_root=Path("/"), owner=0):
    config = filesystem_root / "etc/probe-core"
    directory(config, owner)
    require({p.name for p in config.iterdir()} == CONFIGS | {"runpod.json", "runpod-api-key"}, "Unexpected installed configuration inventory")
    for name in CONFIGS:
        regular(config / name, owner)
        require((config / name).read_bytes() == (root / "rendered" / name).read_bytes(), "Installed configuration has changed: " + name)
    research = json.loads((config / "research.json").read_bytes())
    expected = {"ledger_path": "/var/lib/probe-core/research.sqlite", "socket_path": "/run/probe-research/research.sock",
        "service_uid": users["probe-trusted"], "research_uid": users["probe-research"], "admin_uid": users[human],
        "socket_gid": groups["probe-research"], "policy": {"discovery_datasets": [], "allow_calibration": False},
        "controller_socket": "/run/probe-controller/research.sock", "controller_uid": users["probe-trusted"],
        "input_artifact_root": "/var/lib/probe-core/input-artifacts", "sandbox_image": None,
        "sandbox_workspace": "/var/lib/probe-sandbox/runs", "sandbox_seccomp_profile": "/opt/probe-core/seccomp.json", "podman_path": "/usr/bin/podman"}
    require(research == expected, "Research configuration is not the original disabled configuration")
    deployment = json.loads((root / "deployment.json").read_bytes())
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", deployment.get("sandbox_image", "")), "Pinned CPU image is absent")
    require((config / "sandbox-acceptance.env").read_text() == "SANDBOX_IMAGE=" + deployment["sandbox_image"] + "\n", "Acceptance image differs from the signed deployment")
    require((config / "research-runtime.env").read_text() == f'XDG_RUNTIME_DIR=/run/user/{users["probe-trusted"]}\nDBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{users["probe-trusted"]}/bus\n', "Unexpected user runtime configuration")
    regular(config / "runpod.json", owner)
    provider = json.loads((config / "runpod.json").read_bytes())
    require(provider.get("state_path") == "/var/lib/probe-provider/runpod.sqlite" and provider.get("api_key_file") == "/etc/probe-core/runpod-api-key", "Provider state paths have changed")
    regular(config / "runpod-api-key", users["probe-trusted"], private=True)
    regular(filesystem_root / "var/lib/probe-backup/rclone.conf", users["probe-backup"], private=True)
    empty_database(filesystem_root / "var/lib/probe-core/research.sqlite", LEDGER_TABLES, owner=users["probe-trusted"])
    empty_database(filesystem_root / "var/lib/probe-provider/runpod.sqlite", {"runpod_intents"}, owner=users["probe-trusted"])
    audit = filesystem_root / "var/lib/probe-core/research.audit.jsonl"
    require(regular(audit, users["probe-trusted"]).st_size == 0, "Audit already contains activity")
    for relative, account in (("var/lib/probe-core/input-artifacts", "probe-trusted"), ("var/lib/probe-core/worker-transfers", "probe-trusted"),
            ("var/lib/probe-watchdog", "probe-watchdog"), ("var/lib/probe-backups/outbox", "probe-trusted"),
            ("var/lib/probe-backups/receipts", "probe-backup"), ("var/lib/probe-research", "probe-research")):
        path = filesystem_root / relative
        directory(path, users[account])
        require(not any(path.iterdir()), "Existing data or service state will not be overwritten: /" + relative)
    sandbox = filesystem_root / "var/lib/probe-sandbox"
    directory(sandbox, users["probe-trusted"])
    require({p.name for p in sandbox.iterdir()} <= {".config", ".local", ".cache"}, "Unexpected sandbox artifacts or acceptance state")
    for relative in ("run/probe-controller", "run/probe-provider", "run/probe-research", "var/lib/probe-core/research.artifacts"):
        path = filesystem_root / relative
        require(not path.exists() and not path.is_symlink(), "Unexpected service runtime or accepted artifacts")


def patch_unit(text: str) -> str:
    changes = (("\nGroup=probe-research\n", "\n# Rootless mapping helpers require the account's own primary group.\nGroup=probe-trusted\n"),
               ("\nSupplementaryGroups=probe-trusted probe-ipc probe-ledger-read\n", "\nSupplementaryGroups=probe-research probe-ipc probe-ledger-read\n"),
               ("\nExecStartPre=/usr/bin/test -S ", "\nExecStartPre=/usr/bin/chgrp probe-research /run/probe-research\nExecStartPre=/usr/bin/test -S "))
    for before, after in changes:
        require(text.count(before) == 1, "Service does not match the exact previous group profile")
        text = text.replace(before, after, 1)
    return text


def apply_unit_patches(expected: dict, *, filesystem_root=Path("/"), owner=0):
    folder = filesystem_root / "etc/systemd/system"
    for name in ("probe-research.service", "probe-sandbox-acceptance.service"):
        path = folder / name
        regular(path, owner)
        require(path.read_text() == expected[name], "Unit changed after recovery validation")
        updated = patch_unit(expected[name])
        fd, temporary = tempfile.mkstemp(prefix=".probe-group-repair-", dir=folder)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(updated)
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(folder, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            Path(temporary).unlink(missing_ok=True)


def continuation(installer: bytes, trusted_uid: int) -> str:
    marker = 'PROBE_SANDBOX_IMAGE=$(/opt/probe-core/venv/bin/python -I -c \'import json; print(json.load(open("/opt/probe-core/deployment.json")).get("sandbox_image") or "")\')\n'
    lines = installer.decode().splitlines(keepends=True)
    positions = [index for index, line in enumerate(lines) if line == marker]
    require(len(positions) == 1, "Original installer lacks the unique image-import continuation boundary")
    tail = "".join(lines[positions[0]:])
    old = "runuser -u probe-trusted -g probe-research --"
    require(tail.count(old) == 2, "Image-import continuation differs from the reviewed original")
    require(type(trusted_uid) is int and trusted_uid > 0, "Trusted service UID required")
    prefix = ("#!/bin/bash\nset -euo pipefail\nexport PATH=/usr/sbin:/usr/bin:/sbin:/bin\numask 077\n"
              + f"PROBE_TRUSTED_UID={trusted_uid}\n"
              + "trap 'probe_status=$?; /usr/bin/journalctl --unit probe-sandbox-acceptance.service --no-pager -n 80 >&2 || true; exit \"$probe_status\"' ERR\n"
              + "/usr/bin/systemctl daemon-reload\n")
    return prefix + tail.replace(old, "runuser -u probe-trusted -g probe-trusted --")


def main(argv=None) -> int:
    if os.geteuid() != 0:
        print("Sandbox recovery requires the administrator and /usr/bin/python3 -I.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--human", required=True)
    args = parser.parse_args(argv)
    try:
        installer = verify_runtime(ROOT, args.manifest_sha256)
        users, groups = identities(args.human)
        expected = expected_units(ROOT, users, groups, args.human)
        verify_units(ROOT, expected)
        verify_state(ROOT, users, groups, args.human)
        script = continuation(installer, users["probe-trusted"])
        # Build and syntax-check the private continuation before any installed
        # file mutation. Never delete or recreate prior installation state.
        with tempfile.TemporaryDirectory(prefix=".probe-sandbox-resume-", dir="/opt") as temporary:
            path = Path(temporary) / "continue.sh"
            path.write_text(script)
            path.chmod(0o600)
            subprocess.run(["/bin/bash", "-n", str(path)], check=True, env=ENV)
            apply_unit_patches(expected)
            print("Verified pre-image-import state. Repairing the two service groups and resuming image acceptance.", flush=True)
            return subprocess.run(["/bin/bash", str(path)], cwd=ROOT, env=ENV, check=False).returncode
    except (RecoveryError, OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        print("Sandbox recovery refused; existing data preserved: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
