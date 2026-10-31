#!/usr/bin/python3
"""Resume only the verified initial installation interrupted before venv creation.

Invoke with /usr/bin/python3 -I as root. This is deliberately not a general
repair or upgrade command: unexpected state is preserved and causes refusal.
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
import shlex
import stat
import subprocess
import sys
import tempfile


INSTALLED_ROOT = Path("/opt/probe-core")
ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
ACCOUNTS = ("probe-trusted", "probe-research", "probe-watchdog", "probe-backup")
GROUPS = (*ACCOUNTS, "probe-ipc", "probe-ledger-read", "probe-watch-read", "probe-stop", "probe-backup-read")


class RecoveryError(RuntimeError):
    pass


def _require(condition: bool, message: str):
    if not condition:
        raise RecoveryError(message)


def _regular(path: Path, *, expected_uid: int, private: bool = False):
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == expected_uid,
             "Expected an owned, unshared regular file: " + str(path))
    _require(not info.st_mode & (0o077 if private else 0o022), "Unsafe file permissions: " + str(path))
    return info


def verify_bundle(root: Path, manifest_sha256: str, *, expected_uid: int = 0) -> bytes:
    """Return the verified installer bytes; never trust mutable source modes."""
    root = Path(root).absolute()
    _require(root.resolve() == root, "Installed bundle path must not traverse symlinks")
    info = root.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == expected_uid and not info.st_mode & 0o077,
             "Interrupted bundle must remain private and administrator-owned")
    _require(re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is not None, "Pinned manifest SHA256 required")
    manifest_path = root / "release-manifest.json"
    _regular(manifest_path, expected_uid=expected_uid)
    data = manifest_path.read_bytes()
    _require(len(data) <= 16 * 1024**2 and hashlib.sha256(data).hexdigest() == manifest_sha256,
             "Installed release manifest checksum mismatch")
    manifest = json.loads(data)
    _require(type(manifest) is dict and manifest.get("schema_version") == 1
             and type(manifest.get("files")) is list, "Unsupported release manifest")
    expected = {"release-manifest.json"}
    directories = set()
    records = {}
    for entry in manifest["files"]:
        _require(type(entry) is dict and set(entry) == {"path", "sha256"}, "Unsupported manifest member")
        name, digest = entry["path"], entry["sha256"]
        _require(isinstance(name, str) and name and not name.startswith("/") and "\\" not in name
                 and "\x00" not in name and all(part not in {"", ".", ".."} for part in name.split("/")),
                 "Unsafe release member path")
        _require(name not in expected and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
                 "Duplicate member or invalid digest")
        expected.add(name)
        records[name] = digest
        directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")

    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        _require(info.st_uid == expected_uid, "Bundle contains a non-administrator-owned member")
        if stat.S_ISLNK(info.st_mode):
            target = path.resolve(strict=True)
            _require(target.is_relative_to(root) and target.is_file(), "Bundle symlink escapes or targets a directory")
            actual.add(relative)
        elif stat.S_ISDIR(info.st_mode):
            _require(not info.st_mode & 0o022 and relative in directories, "Unexpected or writable bundle directory")
        elif stat.S_ISREG(info.st_mode):
            _regular(path, expected_uid=expected_uid)
            actual.add(relative)
        else:
            raise RecoveryError("Bundle contains a special file")
    _require(actual == expected, "Installed bundle inventory differs from the pinned release")
    for name, digest in records.items():
        with (root / name).open("rb") as stream:
            _require(hashlib.file_digest(stream, "sha256").hexdigest() == digest,
                     "Installed release member checksum mismatch: " + name)
    required = {"python/bin/python3.13", "deploy/install-controller.sh", "controller-requirements.lock", "deployment.json"}
    _require(required <= expected, "Release lacks required continuation inputs")
    _regular(root / "python/bin/python3.13", expected_uid=expected_uid)
    _regular(root / "deploy/install-controller.sh", expected_uid=expected_uid)
    return (root / "deploy/install-controller.sh").read_bytes()


def verify_identities(human: str) -> tuple[dict[str, int], dict[str, int]]:
    users = {name: pwd.getpwnam(name) for name in (*ACCOUNTS, human)}
    _require(human not in ACCOUNTS and len(users) == 5 and
             len({user.pw_uid for user in users.values()}) == 5 and
             all(user.pw_uid > 0 for user in users.values()), "Human and four service UIDs must be positive and distinct")
    groups = {name: grp.getgrnam(name).gr_gid for name in GROUPS}
    _require(all(value > 0 for value in groups.values()) and len(set(groups.values())) == len(groups),
             "Service groups must have distinct positive IDs")
    for name in ACCOUNTS:
        user = users[name]
        _require(user.pw_gid == groups[name] and user.pw_dir == "/var/lib/" + name
                 and user.pw_shell == "/usr/sbin/nologin", "Service account differs from the interrupted installer: " + name)
    memberships = {
        "probe-trusted": {"probe-ipc", "probe-ledger-read", "probe-watch-read", "probe-stop", "probe-backup-read"},
        "probe-watchdog": {"probe-ledger-read", "probe-watch-read", "probe-stop"},
        "probe-backup": {"probe-backup-read"}, human: {"probe-ipc"},
    }
    for name, required in memberships.items():
        existing = set(os.getgrouplist(name, users[name].pw_gid))
        _require({groups[group] for group in required} <= existing, "Required group membership is missing: " + name)
    return {name: user.pw_uid for name, user in users.items()}, groups


def verify_subordinate_ranges(text: str, *, owner: str = "probe-trusted"):
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(":")
        _require(len(parts) == 3 and parts[0] and parts[1].isdigit() and parts[2].isdigit(),
                 "Malformed subordinate ID range")
        start, count = int(parts[1]), int(parts[2])
        _require(start > 0 and count > 0 and start + count <= 2**32, "Invalid subordinate ID range")
        rows.append((start, start + count, parts[0]))
    ordered = sorted(rows)
    _require(all(left[1] <= right[0] for left, right in zip(ordered, ordered[1:])),
             "Subordinate ID ranges overlap")
    _require(any(name == owner and end - start >= 65536 and start >= 65536 for start, end, name in rows),
             "Probe trusted identity lacks its nonoverlapping subordinate ID range")


def verify_partial_state(users: dict[str, int], groups: dict[str, int], *, filesystem_root: Path = Path("/"), root_uid: int = 0):
    """Require the exact empty directory layout made before the failed exec."""
    expected = {
        "etc/probe-core": (root_uid, groups["probe-trusted"], 0o750, set()),
        "var/lib/probe-core": (users["probe-trusted"], groups["probe-ledger-read"], 0o2750, {"input-artifacts", "worker-transfers"}),
        # GNU install preserves the setgid bit inherited from probe-core even
        # with -m0700, while its explicit -g selects probe-trusted as the group.
        "var/lib/probe-core/input-artifacts": (users["probe-trusted"], groups["probe-trusted"], 0o2700, set()),
        "var/lib/probe-core/worker-transfers": (users["probe-trusted"], groups["probe-trusted"], 0o2700, set()),
        "var/lib/probe-provider": (users["probe-trusted"], groups["probe-trusted"], 0o700, set()),
        "var/lib/probe-sandbox": (users["probe-trusted"], groups["probe-trusted"], 0o700, set()),
        "var/lib/probe-watchdog": (users["probe-watchdog"], groups["probe-watch-read"], 0o750, set()),
        "var/lib/probe-backups": (root_uid, groups["probe-backup-read"], 0o750, {"outbox", "receipts"}),
        "var/lib/probe-backups/outbox": (users["probe-trusted"], groups["probe-backup-read"], 0o750, set()),
        "var/lib/probe-backups/receipts": (users["probe-backup"], groups["probe-backup-read"], 0o750, set()),
        "var/lib/probe-backup": (users["probe-backup"], groups["probe-backup"], 0o700, set()),
        "var/lib/probe-research": (users["probe-research"], groups["probe-research"], 0o700, set()),
    }
    for relative, (uid, gid, mode, children) in expected.items():
        path = filesystem_root / relative
        info = path.lstat()
        _require(path.resolve() == path.absolute() and stat.S_ISDIR(info.st_mode)
                 and info.st_uid == uid and info.st_gid == gid and stat.S_IMODE(info.st_mode) == mode,
                 "Unexpected partial-state directory identity or permissions: /" + relative)
        _require({child.name for child in path.iterdir()} == children,
                 "Unexpected later installation state; nothing will be removed: /" + relative)
    for relative in ("run/probe-controller", "run/probe-provider", "run/probe-research"):
        path = filesystem_root / relative
        _require(not path.exists() and not path.is_symlink(), "Existing Probe runtime directory requires manual review")


def verify_no_units(*, run=subprocess.run, filesystem_root: Path = Path("/")):
    for relative in ("etc/systemd/system", "run/systemd/system"):
        directory = filesystem_root / relative
        if directory.exists():
            _require(not any(path.name.startswith("probe-") for path in directory.rglob("*")),
                     "Probe unit files or overrides already exist")
    for operation in ("list-unit-files", "list-units"):
        result = run(["/usr/bin/systemctl", operation, "--all", "--plain", "--no-legend", "--no-pager", "probe-*"],
                     stdin=subprocess.DEVNULL, capture_output=True, env=ENVIRONMENT, timeout=30, check=False)
        # systemctl list-unit-files reports an unmatched pattern as status 1
        # on this Ubuntu release; a connection failure also has status 1 but
        # emits diagnostics. Accept only the exact empty no-match response.
        no_matches = (operation == "list-unit-files" and result.returncode == 1
                      and not result.stdout.strip() and not result.stderr.strip())
        _require(no_matches or result.returncode == 0 and not result.stdout.strip() and not result.stderr.strip(),
                 "Installed or loaded Probe units found, or systemd inspection failed")


def verify_handoffs(rclone_config: Path, provider_directory: Path, *, human_uid: int) -> tuple[Path, Path]:
    rclone_config, provider_directory = rclone_config.absolute(), provider_directory.absolute()
    _require(rclone_config.resolve() == rclone_config and provider_directory.resolve() == provider_directory,
             "Credential handoff paths must not traverse symlinks")
    for path in (rclone_config, provider_directory / "runpod.json", provider_directory / "runpod-api-key"):
        info = path.lstat()
        _require(info.st_uid in {0, human_uid}, "Credential handoff is not owned by the administrator or selected human")
        _regular(path, expected_uid=info.st_uid, private=True)
        _require(0 < info.st_size <= 128 * 1024, "Credential handoff file has an unexpected size")
    return rclone_config, provider_directory


def continuation_script(installer: bytes, *, human: str, rclone_config: Path, provider_directory: Path,
                        installed_root: Path = INSTALLED_ROOT) -> str:
    marker = f"{installed_root}/python/bin/python3.13 -I -m venv {installed_root}/venv\n"
    lines = installer.decode("utf-8").splitlines(keepends=True)
    positions = [index for index, line in enumerate(lines) if line == marker]
    _require(len(positions) == 1, "Verified installer lacks the unique exact continuation boundary")
    prefix = "#!/bin/bash\nset -euo pipefail\nexport PATH=/usr/sbin:/usr/bin:/sbin:/bin\numask 077\n"
    for name, value in (("PROBE_HUMAN", human), ("PROBE_RCLONE_SOURCE", str(rclone_config)),
                        ("PROBE_PROVIDER_SOURCE", str(provider_directory))):
        prefix += name + "=" + shlex.quote(value) + "\n"
    return prefix + "".join(lines[positions[0]:])


def execute_continuation(root: Path, script: str, *, temporary_parent: Path = Path("/opt")) -> int:
    # This function is called only after every validation above. Remove only
    # our newly created temporary directory, never any interrupted install data.
    with tempfile.TemporaryDirectory(prefix=".probe-resume-", dir=temporary_parent) as temporary:
        path = Path(temporary) / "continue.sh"
        with path.open("x", encoding="utf-8") as stream:
            stream.write(script)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
        interpreter = root / "python/bin/python3.13"
        interpreter.chmod(0o755)
        descriptor = os.open(interpreter, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return subprocess.run(["/bin/bash", str(path)], env=ENVIRONMENT, cwd=root, check=False).returncode


def main(argv=None) -> int:
    if os.geteuid() != 0:
        print("Recovery must be run explicitly by the administrator with /usr/bin/python3 -I.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--human", required=True)
    parser.add_argument("--rclone-config", required=True, type=Path)
    parser.add_argument("--provider-directory", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        installer = verify_bundle(INSTALLED_ROOT, args.manifest_sha256)
        users, groups = verify_identities(args.human)
        for path in (Path("/etc/subuid"), Path("/etc/subgid")):
            _regular(path, expected_uid=0)
            verify_subordinate_ranges(path.read_text())
        verify_partial_state(users, groups)
        verify_no_units()
        rclone, provider = verify_handoffs(args.rclone_config, args.provider_directory, human_uid=users[args.human])
        script = continuation_script(installer, human=args.human, rclone_config=rclone, provider_directory=provider)
        print("Verified exact interrupted state. Restoring the pinned interpreter and continuing the original installer.", flush=True)
        return execute_continuation(INSTALLED_ROOT, script)
    except (RecoveryError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Recovery refused; existing state preserved: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
