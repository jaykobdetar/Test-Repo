#!/usr/bin/python3
"""Recover the reviewed pre-attestation installation failures, as administrator.

Run as administrator with /usr/bin/python3 -I. The original release, generated
state, service restrictions and credentials remain intact. An unchanged real
acceptance gate must pass before the original installer enables any service.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types

ROOT = Path("/opt/probe-core")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
RUNTIME_CONFIG = (b"# Installed only under the trusted Probe account's dedicated HOME.\n"
                  b'[engine]\nruntime = "crun"\n\n[engine.runtimes]\ncrun = ["/usr/bin/crun"]\n')
STORAGE_ANCESTORS = (".local", ".local/share", ".local/share/containers", ".local/share/containers/storage")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def load_verifier(path: Path, digest: str, *, owner=0):
    """Hash and execute the same bytes, never re-open a verified import path."""
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
            and not info.st_mode & 0o022, "Verification helper must be an administrator-owned regular file")
    raw = path.read_bytes()
    require(re.fullmatch(r"[0-9a-f]{64}", digest) and hashlib.sha256(raw).hexdigest() == digest,
            "Verification helper checksum mismatch")
    module = types.ModuleType("probe_pinned_runtime_recovery_verifier")
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def runtime_command(uid: int, *arguments: str) -> list[str]:
    require(type(uid) is int and uid > 0, "Trusted service UID required")
    return ["/usr/sbin/runuser", "-u", "probe-trusted", "-g", "probe-trusted", "--", "env",
            "HOME=/var/lib/probe-sandbox", f"XDG_RUNTIME_DIR=/run/user/{uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", "/usr/bin/podman", "--remote=false", *arguments]


def runtime_output(uid: int, *arguments: str, operation: str, run=subprocess.run) -> str:
    # runuser retains cwd. The human's terminal may be inside a private home
    # that the service account cannot traverse after dropping privileges.
    result = run(runtime_command(uid, *arguments), cwd=ROOT, stdin=subprocess.DEVNULL,
                 capture_output=True, env=ENV, timeout=30, check=False)
    if result.returncode:
        diagnostic = result.stderr[-4096:].decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{operation} failed (exit {result.returncode}) from {ROOT}: "
                           + (diagnostic or "Podman returned no stderr diagnostic"))
    return result.stdout.decode("utf-8", errors="replace").strip()


def verify_image(uid: int, image: str, *, run=subprocess.run):
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image), "Immutable sandbox image required")
    identity = runtime_output(uid, "image", "inspect", "--format", "{{.Id}}", image,
                              operation="Podman image inspection", run=run)
    require(identity.removeprefix("sha256:") == image[7:],
            "Podman returned an unexpected CPU image identity: " + repr(identity[:256])
            + "; refusing any import or reset")


def verify_config_location(home: Path, uid: int, *, owner=0):
    for directory in (home, home / ".config", home / ".config/containers"):
        if directory.exists() or directory.is_symlink():
            info = directory.lstat()
            require(stat.S_ISDIR(info.st_mode) and directory.resolve() == directory.absolute()
                    and info.st_uid in {owner, uid} and not info.st_mode & 0o022,
                    "Unsafe per-Probe configuration directory")
        elif directory == home:
            raise RuntimeError("Existing Probe home is required")
    config = home / ".config/containers/containers.conf"
    require(not config.exists() and not config.is_symlink(), "Existing container runtime configuration will not be overwritten")


def install_runtime_config(home: Path, uid: int, gid: int, *, owner=0):
    verify_config_location(home, uid, owner=owner)
    for directory in (home / ".config", home / ".config/containers"):
        directory.mkdir(exist_ok=True, mode=0o750)
        os.chown(directory, owner, gid)
        directory.chmod(0o750)
    config = home / ".config/containers/containers.conf"
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchown(stream.fileno(), owner, gid)
        os.fchmod(stream.fileno(), 0o640)
        stream.write(RUNTIME_CONFIG)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(config.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_selected_runtime(uid: int, *, run=subprocess.run):
    runtime = runtime_output(uid, "info", "--format", "{{.Host.OCIRuntime.Path}}",
                             operation="Podman runtime inspection", run=run)
    require(runtime == "/usr/bin/crun",
            "The actual Probe Podman store did not select /usr/bin/crun; observed " + repr(runtime[:256]))


def verify_installed_runtime_config(home: Path, gid: int, *, owner=0):
    """Accept only the exact configuration written by the previous repair."""
    for relative in (".config", ".config/containers"):
        path = home / relative
        info = path.lstat()
        require(path.resolve() == path.absolute() and stat.S_ISDIR(info.st_mode)
                and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (owner, gid, 0o750),
                "Unexpected installed runtime configuration directory: " + str(path))
    path = home / ".config/containers/containers.conf"
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (owner, gid, 0o640)
            and path.read_bytes() == RUNTIME_CONFIG, "Installed runtime configuration differs from the prior repair")


def verify_storage_runtime(uid: int, gid: int, stale_gid: int, *, run=subprocess.run):
    require(gid != stale_gid, "The old and current storage groups must differ")
    mapping = runtime_output(uid, "unshare", "/usr/bin/cat", "/proc/self/gid_map",
                             operation="Podman group mapping inspection", run=run)
    rows = [line.split() for line in mapping.splitlines()]
    require(rows and all(len(row) == 3 and all(item.isdecimal() for item in row) for row in rows),
            "Unexpected rootless group mapping")
    ranges = [(int(row[1]), int(row[2])) for row in rows]
    require(all(count > 0 for start, count in ranges), "Empty rootless group mapping")
    mapped = lambda group: any(start <= group < start + count for start, count in ranges)
    require(mapped(gid) and not mapped(stale_gid), "Observed group mapping does not match the known storage failure")
    containers = runtime_output(uid, "ps", "--all", "--quiet", operation="Podman container inspection", run=run)
    require(not containers, "Existing containers must not be modified by installation recovery")


def repair_storage_groups(home: Path, uid: int, gid: int, stale_gid: int) -> int:
    """Correct four known ancestors only; retain owner, 0700 mode and contents.

    Validate every directory before the first mutation and retain no-follow
    descriptors so fchown cannot follow a replaced path. An interrupted repair
    may have corrected a prefix already; both exact group states are accepted.
    """
    require(gid != stale_gid, "The old and current storage groups must differ")
    with ExitStack() as stack:
        require(home.resolve() == home.absolute(), "Sandbox home must not contain symlinks")
        descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (uid, gid, 0o700),
                "Sandbox home ownership or private permissions changed")
        pending = []
        for relative in STORAGE_ANCESTORS:
            descriptor = os.open(Path(relative).name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            require(info.st_uid == uid and info.st_gid in {gid, stale_gid} and stat.S_IMODE(info.st_mode) == 0o700,
                    "Unexpected storage ancestor ownership or mode: " + relative)
            pending.append((descriptor, info, relative))
        overlay = os.open("overlay", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        stack.callback(os.close, overlay)
        info = os.fstat(overlay)
        require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (uid, gid, 0o700),
                "Overlay directory does not match the successfully imported store")
        changed = 0
        for descriptor, original, relative in pending:
            info = os.fstat(descriptor)
            require((info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode)
                    == (original.st_dev, original.st_ino, original.st_uid, original.st_gid, original.st_mode),
                    "Storage ancestor changed during validation: " + relative)
            if info.st_gid == stale_gid:
                os.fchown(descriptor, -1, gid)
                os.fsync(descriptor)
                changed += 1
            info = os.fstat(descriptor)
            require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (uid, gid, 0o700),
                    "Storage group repair did not preserve its private permissions")
        return changed


def continuation(installer: bytes, trusted_uid: int) -> str:
    require(type(trusted_uid) is int and trusted_uid > 0, "Trusted service UID required")
    text = installer.decode()
    marker = "PROBE_SANDBOX_IMAGE=$(/opt/probe-core/venv/bin/python -I -c 'import json; print(json.load(open(\"/opt/probe-core/deployment.json\")).get(\"sandbox_image\") or \"\")')\n"
    require(text.count(marker) == 1, "Original installer lacks the exact continuation boundary")
    tail = marker + text.split(marker, 1)[1]
    load = '  runuser -u probe-trusted -g probe-research -- env HOME=/var/lib/probe-sandbox XDG_RUNTIME_DIR="/run/user/$PROBE_TRUSTED_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$PROBE_TRUSTED_UID/bus" /usr/bin/podman --remote=false load < /opt/probe-core/images/cpu-sandbox.tar\n'
    require(tail.count(load) == 1 and tail.count("runuser -u probe-trusted -g probe-research --") == 2
            and tail.count("  systemctl start probe-sandbox-acceptance.service\n") == 1,
            "Original continuation differs from the reviewed image and acceptance sequence")
    tail = tail.replace(load, "", 1).replace("runuser -u probe-trusted -g probe-research --", "runuser -u probe-trusted -g probe-trusted --")
    prefix = ("#!/bin/bash\nset -euo pipefail\nexport PATH=/usr/sbin:/usr/bin:/sbin:/bin\numask 077\n"
              + f"PROBE_TRUSTED_UID={trusted_uid}\n"
              + "PROBE_RECOVERY_SINCE=$(/usr/bin/date +%s)\n"
              + "probe_failure() {\n  probe_status=$?\n"
              + '  /usr/bin/journalctl --unit probe-sandbox-acceptance.service --since "@$PROBE_RECOVERY_SINCE" --no-pager -n 80 >&2 || true\n'
              + '  /usr/bin/journalctl --since "@$PROBE_RECOVERY_SINCE" "_UID=$PROBE_TRUSTED_UID" _COMM=conmon --no-pager -n 80 >&2 || true\n'
              + '  exit "$probe_status"\n}\ntrap probe_failure ERR\n/usr/bin/systemctl daemon-reload\n')
    return prefix + tail


def main(argv=None) -> int:
    if os.geteuid() != 0:
        print("Runtime recovery requires the administrator and /usr/bin/python3 -I.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification-helper", required=True, type=Path)
    parser.add_argument("--verification-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--human", required=True)
    parser.add_argument("--repair-stale-storage-groups", action="store_true",
                        help="Correct only the four stale storage groups after the reviewed crun retry")
    args = parser.parse_args(argv)
    try:
        verifier = load_verifier(args.verification_helper, args.verification_sha256)
        installer = verifier.verify_runtime(ROOT, args.manifest_sha256)
        users, groups = verifier.identities(args.human)
        expected = verifier.expected_units(ROOT, users, groups, args.human)
        verifier.verify_units(ROOT, expected, repaired_groups=True, failed_acceptance=True)
        verifier.verify_state(ROOT, users, groups, args.human, allow_failed_acceptance=True)
        uid, gid = users["probe-trusted"], groups["probe-trusted"]
        home = Path("/var/lib/probe-sandbox")
        if args.repair_stale_storage_groups:
            verify_installed_runtime_config(home, gid)
            verify_selected_runtime(uid)
            verify_storage_runtime(uid, gid, groups["probe-research"])
        else:
            verify_config_location(home, uid)
        verify_image(uid, json.loads((ROOT / "deployment.json").read_bytes())["sandbox_image"])
        script = continuation(installer, uid)
        with tempfile.TemporaryDirectory(prefix=".probe-runtime-resume-", dir="/opt") as temporary:
            path = Path(temporary) / "continue.sh"
            path.write_text(script)
            path.chmod(0o600)
            subprocess.run(["/bin/bash", "-n", str(path)], cwd=ROOT, check=True, env=ENV)
            if args.repair_stale_storage_groups:
                changed = repair_storage_groups(home, uid, gid, groups["probe-research"])
                print(f"Corrected {changed} private storage directory groups; permissions remain 0700. Retrying the unchanged acceptance gate.", flush=True)
            else:
                print("Verified the known pre-attestation failure. Installing crun and retrying the unchanged acceptance gate.", flush=True)
                subprocess.run(["/usr/bin/apt-get", "install", "-y", "--no-install-recommends", "crun"], cwd="/", check=True, env=ENV)
                verifier.regular(Path("/usr/bin/crun"), 0)
                require(os.access("/usr/bin/crun", os.X_OK), "Installed crun is not executable")
                install_runtime_config(home, uid, gid)
                verify_selected_runtime(uid)
            return subprocess.run(["/bin/bash", str(path)], cwd=ROOT, env=ENV, check=False).returncode
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        print("Runtime recovery stopped; existing data and credentials preserved: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
