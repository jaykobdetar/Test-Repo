#!/usr/bin/python3
"""Administrator-only, offline application-wheel upgrade before the first job.

Run with /usr/bin/python3 -I. The independently pinned release manifest binds
the wheel, source commit, and identity checker. Dependencies, credentials,
databases, service identities, container images and runtime settings are kept.
Failure leaves research disabled and a persistent service guard in place.
This helper deliberately does not resume a failed upgrade automatically.
"""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timezone
from email.parser import BytesParser
import grp
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path("/opt/probe-core")
CONFIG = Path("/etc/probe-core")
UNITS = Path("/etc/systemd/system")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
SERVICES = ("probe-provider-stop.service", "probe-watchdog.service", "probe-controller.service", "probe-research.service")
GUARDED = ("probe-controller.service", "probe-research.service")
OTHER_UNITS = ("probe-backup.timer", "probe-backup.service", "probe-snapshot.service", "probe-backup-prune.service", "probe-sandbox-acceptance.service")
GUARD_NAME = "50-probe-upgrade.conf"
GUARD = b"[Unit]\nConditionPathExists=!/etc/probe-core/upgrade-blocked\n"
HEX = re.compile(r"[0-9a-f]{64}\Z")
SANDBOX_CHECKS = {"immutable_image_present", "cpu_job", "network_denied", "gpu_unavailable", "host_path_denied",
                  "trusted_paths_readonly", "credentials_absent", "pid_limit_enforced", "output_limit_enforced",
                  "memory_limit_enforced", "wall_time_enforced", "runtime_attestation", "cpu_cgroup_limit_attested",
                  "container_removal_confirmed", "crash_deadline_enforced", "crash_removal_confirmed"}


class UpgradeError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise UpgradeError(code)


def read_file(path, *, owner=0, limit=256 * 1024 * 1024):
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit,
                "UNSAFE_FILE")
        if owner is not None:
            require(info.st_uid == owner and not info.st_mode & 0o022, "UNTRUSTED_FILE")
        data = stream.read(limit + 1)
        require(len(data) == info.st_size, "FILE_CHANGED_DURING_READ")
        return data


def trusted_directory(path, *, owner=0):
    path = Path(path)
    info = path.lstat()
    require(path.resolve() == path.absolute() and stat.S_ISDIR(info.st_mode)
            and info.st_uid == owner and not info.st_mode & 0o022, "UNTRUSTED_DIRECTORY")


def atomic_file(path, data, *, uid, gid, mode):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".probe-upgrade-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_member(name):
    return (type(name) is str and name and not name.startswith("/") and "\\" not in name
            and "\x00" not in name and all(part not in {"", ".", ".."} for part in name.split("/")))


def inspect_wheel(raw):
    """Validate wheel inventory and RECORD before pip sees any archive bytes."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(0 < len(entries) < 3000 and sum(item.file_size for item in entries) <= 128 * 1024 * 1024,
                "WHEEL_TOO_LARGE")
        names = [item.filename for item in entries]
        require(len(set(names)) == len(names) and all(safe_member(name) for name in names), "UNSAFE_WHEEL_PATH")
        require(all(not item.is_dir() and not stat.S_ISLNK(item.external_attr >> 16) for item in entries), "UNSAFE_WHEEL_MEMBER")
        members = {item.filename: archive.read(item) for item in entries}
    metadata_names = [name for name in members if re.fullmatch(r"probe_core-[^/]+\.dist-info/METADATA", name)]
    require(len(metadata_names) == 1 and "probe_core/__init__.py" in members, "WRONG_PROJECT_WHEEL")
    info = metadata_names[0].rsplit("/", 1)[0]
    require(all(name.startswith("probe_core/") or name.startswith(info + "/") for name in members), "WHEEL_WRITES_OUTSIDE_PROJECT")
    metadata = BytesParser().parsebytes(members[info + "/METADATA"])
    require(metadata["Name"] == "probe-core" and metadata["Version"], "WRONG_PROJECT_METADATA")
    wheel = BytesParser().parsebytes(members[info + "/WHEEL"])
    require(wheel["Root-Is-Purelib"] == "true" and wheel.get_all("Tag") == ["py3-none-any"], "UNSUPPORTED_WHEEL_LAYOUT")
    record_name = info + "/RECORD"
    records = list(csv.reader(io.StringIO(members[record_name].decode())))
    require(all(len(row) == 3 for row in records) and len(records) == len(members)
            and {row[0] for row in records} == set(members), "WHEEL_RECORD_INVENTORY")
    for name, digest, size in records:
        if name == record_name:
            require(digest == size == "", "WHEEL_RECORD_SELF_HASH")
        else:
            actual = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(members[name]).digest()).decode().rstrip("=")
            require(digest == actual and size == str(len(members[name])), "WHEEL_RECORD_HASH")
    return {"members": members, "dist_info": info,
            "requirements": sorted(metadata.get_all("Requires-Dist", [])),
            "requires_python": metadata["Requires-Python"]}


def verify_installed(wheel, site, *, owner=0):
    expected = {name for name in wheel["members"] if name.startswith("probe_core/")}
    observed = {str(path.relative_to(site)) for path in (site / "probe_core").rglob("*")
                if path.is_file() and "__pycache__" not in path.parts}
    require(observed == expected, "INSTALLED_PROJECT_INVENTORY_DIFFERS")
    for name in expected | {wheel["dist_info"] + "/METADATA"}:
        require(read_file(site / name, owner=owner) == wheel["members"][name], "INSTALLED_PROJECT_BYTES_DIFFER")


def verify_original(root, digest, *, owner=0):
    trusted_directory(root, owner=owner)
    raw = read_file(root / "release-manifest.json", owner=owner)
    require(HEX.fullmatch(digest) and hashlib.sha256(raw).hexdigest() == digest, "ORIGINAL_MANIFEST_HASH")
    manifest = json.loads(raw)
    require(manifest.get("schema_version") == 1 and type(manifest.get("files")) is list, "ORIGINAL_MANIFEST_SCHEMA")
    members = {}
    for item in manifest["files"]:
        require(type(item) is dict and set(item) == {"path", "sha256"} and safe_member(item["path"])
                and type(item["sha256"]) is str and HEX.fullmatch(item["sha256"])
                and item["path"] not in members, "ORIGINAL_MANIFEST_MEMBER")
        members[item["path"]] = item["sha256"]
    # The existing venv is administrator-generated. Check every dependency and
    # interpreter path before executing it; internal venv symlinks are normal.
    for path in root.rglob("*"):
        info = path.lstat()
        require(info.st_uid == owner, "RUNTIME_OWNER_CHANGED")
        if stat.S_ISLNK(info.st_mode):
            require(path.resolve(strict=True).is_relative_to(root), "RUNTIME_LINK_ESCAPES")
        else:
            require((stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                    and not info.st_mode & 0o022, "RUNTIME_MEMBER_UNSAFE")
    for name, expected in members.items():
        path = root / name
        require(path.resolve(strict=True).is_relative_to(root), "ORIGINAL_LINK_ESCAPES")
        cache = re.fullmatch(r"(.+)/__pycache__/([^/]+)\.cpython-313(?:\.opt-[12])?\.pyc", name)
        if cache and cache[1] + "/" + cache[2] + ".py" in members:
            # Relocation regenerates timestamp-based caches, under root only.
            continue
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        require(actual == expected, "ORIGINAL_FILE_HASH")
    wheels = [name for name in members if re.fullmatch(r"probe_core-[^/]+\.whl", name)]
    require(len(wheels) == 1 and "python/bin/python3.13" in members, "ORIGINAL_WHEEL_MISSING")
    old = read_file(root / wheels[0], owner=owner)
    wheel = inspect_wheel(old)
    site = root / "venv/lib/python3.13/site-packages"
    verify_installed(wheel, site, owner=owner)
    return wheels[0], old, wheel, site


def read_release(path, digest):
    raw = read_file(path, owner=None, limit=65536)
    require(HEX.fullmatch(digest) and hashlib.sha256(raw).hexdigest() == digest, "TARGET_MANIFEST_HASH")
    body = json.loads(raw)
    require(type(body) is dict and set(body) == {"schema_version", "source_commit", "wheel_filename", "wheel_sha256", "identity_checker_sha256"}
            and body["schema_version"] == 1 and re.fullmatch(r"[0-9a-f]{40}", body["source_commit"])
            and re.fullmatch(r"probe_core-[A-Za-z0-9_.-]+\.whl", body["wheel_filename"])
            and HEX.fullmatch(body["wheel_sha256"]) and HEX.fullmatch(body["identity_checker_sha256"]), "TARGET_MANIFEST_SCHEMA")
    return raw, body


def pin_bytes(path, expected):
    raw = read_file(path, owner=None)
    require(hashlib.sha256(raw).hexdigest() == expected, "TARGET_INPUT_HASH")
    return raw


def check_idle_counts(counts):
    require(type(counts) is dict and set(counts) == {"jobs", "attempts", "approvals", "compute_requests", "runpod_intents", "audit_events", "pods", "network_volumes", "local_containers"}
            and all(type(value) is int and value >= 0 for value in counts.values()), "IDLE_RESPONSE_INVALID")
    require(all(value == 0 for name, value in counts.items() if name != "audit_events"), "PRE_FIRST_JOB_STATE_REQUIRED")


READ_IDLE = r'''
import json, sqlite3
from pathlib import Path
from probe_core.runpod_provider import RunPodConfig, RunPodHTTP
counts = {}
for path, tables in (("/var/lib/probe-core/research.sqlite", ("jobs", "attempts", "approvals", "compute_requests", "audit_events")),
                     ("/var/lib/probe-provider/runpod.sqlite", ("runpod_intents",))):
    connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        for table in tables:
            counts[table] = connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
    finally:
        connection.close()
config = RunPodConfig.load("/etc/probe-core/runpod.json")
if config.state_path != "/var/lib/probe-provider/runpod.sqlite" or config.api_key_file != "/etc/probe-core/runpod-api-key":
    raise RuntimeError("provider paths changed")
transport = RunPodHTTP(config.api_key_file)
pods = transport.request("GET", "/v2/pods?includeClusterPods=true&limit=1000")
volumes = transport.request("GET", "/v2/network-volumes")
if (type(pods.get("pods")) is not list or pods.get("pagination", {}).get("hasNextPage") is not False
        or type(volumes.get("networkVolumes")) is not list):
    raise RuntimeError("provider inventory is incomplete")
counts.update(pods=len(pods["pods"]), network_volumes=len(volumes["networkVolumes"]))
print(json.dumps(counts))
'''
DEPENDENCIES = 'import importlib.metadata as m,json; print(json.dumps(sorted((d.metadata["Name"],d.version) for d in m.distributions() if d.metadata["Name"].lower().replace("_","-")!="probe-core")))'


def socket_ready(path, uid, gid):
    try:
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid or info.st_gid != gid
                or stat.S_IMODE(info.st_mode) != 0o660):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.25)
            connection.connect(str(path))
            return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] == uid
    except OSError:
        return False


def wait_ready(check, *, timeout=30, monotonic=time.monotonic, sleep=time.sleep):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if check():
            return
        sleep(0.1)
    raise UpgradeError("SERVICE_LISTENER_NOT_READY")


def discover_users(human):
    users = {name: pwd.getpwnam(name).pw_uid for name in ("probe-trusted", "probe-research", "probe-watchdog", "probe-backup", human)}
    require(len(users) == 5 and len(set(users.values())) == 5 and all(uid > 0 for uid in users.values()), "IDENTITIES_NOT_DISTINCT")
    return users


def update_commit(raw, commit):
    lines = raw.decode().splitlines(keepends=True)
    selected = [index for index, line in enumerate(lines) if line.startswith("SOURCE_COMMIT=")]
    require(len(selected) == 1 and re.fullmatch(r"SOURCE_COMMIT=[0-9a-f]{40}\n?", lines[selected[0]]), "BACKUP_PROVENANCE_FORMAT")
    lines[selected[0]] = "SOURCE_COMMIT=" + commit + "\n"
    return "".join(lines).encode()


class Upgrade:
    def __init__(self, *, root=ROOT, config=CONFIG, units=UNITS, owner=0, run=subprocess.run,
                 backup_copy=Path("/var/lib/probe-backup/backup.env"),
                 sandbox_report=Path("/var/lib/probe-sandbox/acceptance-report.json")):
        self.root, self.config, self.units, self.owner, self.run = root, config, units, owner, run
        self.backup_copy, self.sandbox_report = backup_copy, sandbox_report
        self.stage = "validation"
        self.changed = False
        self.work = None
        self.closed_after_failure = False

    def command(self, arguments, *, timeout=60):
        result = self.run(arguments, cwd=self.root, env=ENV, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=timeout, check=False, umask=0o022)
        if result.returncode:
            if self.work is not None:
                atomic_file(self.work / "last-command-stderr.txt", result.stderr[-8192:],
                            uid=self.owner, gid=os.getegid(), mode=0o600)
            raise UpgradeError("COMMAND_FAILED_" + self.stage.upper())
        return result.stdout

    def phase(self, stage, message):
        self.stage = stage
        print("Probe upgrade: " + message + ".", flush=True)

    def systemctl(self, *arguments, timeout=60):
        return self.command(["/usr/bin/systemctl", *arguments], timeout=timeout)

    def ready(self, users):
        expected = (("/run/probe-research/research.sock", "probe-research"),
                    ("/run/probe-controller/admin.sock", "probe-ipc"),
                    ("/run/probe-controller/research.sock", "probe-ipc"),
                    ("/run/probe-provider/stop.sock", "probe-stop"))
        groups = {group: grp.getgrnam(group).gr_gid for _, group in expected}
        wait_ready(lambda: all(socket_ready(Path(path), users["probe-trusted"], groups[group]) for path, group in expected))

    def idle(self):
        raw = self.command(["/usr/sbin/runuser", "-u", "probe-trusted", "-g", "probe-trusted", "--",
                            str(self.root / "venv/bin/python"), "-I", "-c", READ_IDLE], timeout=60)
        require(len(raw) < 65536, "IDLE_RESPONSE_TOO_LARGE")
        result = json.loads(raw)
        # CPU exec_code runs do not occupy the GPU job queue. Refuse any live
        # or unreconciled container in the dedicated trusted account's store.
        uid = self.trusted_uid
        containers = self.command(["/usr/sbin/runuser", "-u", "probe-trusted", "-g", "probe-trusted", "--", "env",
                                   "HOME=/var/lib/probe-sandbox", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                                   f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", "/usr/bin/podman",
                                   "--remote=false", "ps", "--all", "--quiet", "--no-trunc"], timeout=30)
        require(len(containers) < 65536, "CONTAINER_INVENTORY_TOO_LARGE")
        ids = containers.decode().splitlines()
        require(all(re.fullmatch(r"[0-9a-f]{64}", value) for value in ids), "CONTAINER_INVENTORY_INVALID")
        result["local_containers"] = len(ids)
        check_idle_counts(result)
        return result

    def guard(self, enabled):
        marker = self.config / "upgrade-blocked"
        if enabled:
            atomic_file(marker, b"Application upgrade incomplete; research stays disabled.\n", uid=self.owner, gid=os.getegid(), mode=0o600)
        for name in GUARDED:
            directory = self.units / (name + ".d")
            path = directory / GUARD_NAME
            if enabled:
                directory.mkdir(mode=0o755, exist_ok=True)
                trusted_directory(directory, owner=self.owner)
                require(set(directory.iterdir()) <= {path}, "UNEXPECTED_SERVICE_OVERRIDE")
                atomic_file(path, GUARD, uid=self.owner, gid=os.getegid(), mode=0o644)
            else:
                require(read_file(path, owner=self.owner) == GUARD, "UPGRADE_GUARD_CHANGED")
                path.unlink()
                directory.rmdir()
        # Keep the marker until success. During identity checks the durable
        # disabled sandbox configuration is the guard while drop-ins are absent.
        self.systemctl("daemon-reload")

    def write_config(self, raw):
        atomic_file(self.config / "research.json", raw, **self.config_metadata)

    def fail_closed(self):
        errors = []
        for action in (lambda: self.write_config(self.disabled_config), lambda: self.guard(True),
                       lambda: self.systemctl("stop", "probe-research.service", "probe-controller.service", timeout=120)):
            try:
                action()
            except BaseException as error:
                errors.append(error)
        self.closed_after_failure = not errors
        require(not errors, "FAIL_CLOSED_REPAIR_UNCONFIRMED")

    def execute(self, args):
        self.phase("validation", "verifying the installed release and pinned upgrade")
        original_name, old_raw, old_wheel, site = verify_original(self.root, args.original_manifest_sha256, owner=self.owner)
        manifest_raw, release = read_release(args.release_manifest, args.release_manifest_sha256)
        require(args.wheel.name == release["wheel_filename"], "TARGET_WHEEL_FILENAME")
        new_raw = pin_bytes(args.wheel, release["wheel_sha256"])
        checker_raw = pin_bytes(args.identity_checker, release["identity_checker_sha256"])
        new_wheel = inspect_wheel(new_raw)
        require(new_wheel["requirements"] == old_wheel["requirements"]
                and new_wheel["requires_python"] == old_wheel["requires_python"], "DEPENDENCY_CHANGE_REFUSED")
        require(new_raw != old_raw, "TARGET_IS_ALREADY_INSTALLED")
        trusted_directory(self.config, owner=self.owner)
        trusted_directory(self.units, owner=self.owner)
        require(not (self.config / "upgrade-blocked").exists(), "PRIOR_UPGRADE_REQUIRES_REVIEW")
        for name in (*SERVICES, *OTHER_UNITS):
            read_file(self.units / name, owner=self.owner, limit=65536)
            raw = self.systemctl("show", name, "--property=LoadState,FragmentPath,DropInPaths,ActiveState,UnitFileState")
            values = dict(line.split("=", 1) for line in raw.decode().splitlines())
            require(values.get("LoadState") == "loaded" and values.get("FragmentPath") == str(self.units / name)
                    and values.get("DropInPaths") == "", "INSTALLED_UNIT_CHANGED")
            require(not (self.units / (name + ".d")).exists(), "EXISTING_SERVICE_OVERRIDE")
            if name in SERVICES or name == "probe-backup.timer":
                require(values.get("ActiveState") == "active" and values.get("UnitFileState") == "enabled", "EXPECTED_SERVICE_NOT_RUNNING")
        users = discover_users(args.human)
        self.trusted_uid = users["probe-trusted"]
        original_config = read_file(self.config / "research.json", owner=self.owner, limit=65536)
        config = json.loads(original_config)
        image = config.get("sandbox_image")
        require(type(image) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", image), "ORIGINAL_SANDBOX_NOT_ENABLED")
        require(config.get("service_uid") == users["probe-trusted"] and config.get("research_uid") == users["probe-research"]
                and config.get("admin_uid") == users[args.human], "CONFIG_IDENTITY_CHANGED")
        metadata = (self.config / "research.json").stat()
        self.config_metadata = dict(uid=metadata.st_uid, gid=metadata.st_gid, mode=stat.S_IMODE(metadata.st_mode))
        config["sandbox_image"] = None
        self.disabled_config = (json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n").encode()
        provenance = []
        for path, uid in ((self.config / "backup.env", self.owner), (self.backup_copy, users["probe-backup"])):
            raw = read_file(path, owner=uid, limit=65536)
            metadata = path.stat()
            provenance.append((path, raw, update_commit(raw, release["source_commit"]),
                               dict(uid=metadata.st_uid, gid=metadata.st_gid, mode=stat.S_IMODE(metadata.st_mode))))
        self.idle()  # Read-only refusal before stopping any service.
        dependencies = self.command([str(self.root / "venv/bin/python"), "-I", "-c", DEPENDENCIES])
        self.phase("pre_upgrade_backup", "saving and verifying the pre-upgrade backup")
        self.systemctl("start", "probe-backup.service", timeout=360)
        self.idle()
        upgrades = self.root / "upgrades"
        upgrades.mkdir(mode=0o700, exist_ok=True)
        trusted_directory(upgrades, owner=self.owner)
        self.work = upgrades / release["wheel_sha256"]
        self.work.mkdir(mode=0o700)
        rollback = self.work / "rollback"
        rollback.mkdir(mode=0o700)
        # Each pinned input is copied exactly once from the verified bytes.
        for name, raw in ((release["wheel_filename"], new_raw), ("rollback/" + original_name, old_raw),
                          ("upgrade-release.json", manifest_raw), ("verify-installed-identities.py", checker_raw),
                          ("research-before.json", original_config), ("rollback/backup.env", provenance[0][1]),
                          ("rollback/backup-account.env", provenance[1][1])):
            atomic_file(self.work / name, raw, uid=self.owner, gid=os.getegid(), mode=0o600)
        try:
            self.changed = True
            self.stage = "close_research"
            self.write_config(self.disabled_config)
            self.guard(True)
            self.systemctl("stop", "probe-research.service", "probe-controller.service", "probe-backup.timer", timeout=120)
            self.idle()  # Watchdog/broker remain live if an action raced preflight.
            self.systemctl("stop", "probe-watchdog.service", "probe-provider-stop.service", timeout=120)
            for name in ("probe-backup.service", "probe-snapshot.service", "probe-backup-prune.service"):
                require(self.systemctl("show", name, "--property=ActiveState", "--value").strip() == b"inactive", "BACKGROUND_PACKAGE_USER_ACTIVE")
            self.phase("install_wheel", "installing the verified application wheel")
            self.command([str(self.root / "venv/bin/python"), "-I", "-m", "pip", "--isolated", "install",
                          "--no-index", "--no-deps", "--force-reinstall", str(self.work / release["wheel_filename"])], timeout=120)
            verify_installed(new_wheel, site, owner=self.owner)
            require(self.command([str(self.root / "venv/bin/python"), "-I", "-c", DEPENDENCIES]) == dependencies,
                    "INSTALLED_DEPENDENCIES_CHANGED")
            self.phase("sandbox_acceptance", "checking CPU containment and crash cleanup")
            started = datetime.now(timezone.utc)
            self.systemctl("start", "probe-sandbox-acceptance.service", timeout=260)
            report = json.loads(read_file(self.sandbox_report, owner=users["probe-trusted"]))
            validate_acceptance(report, image, users["probe-trusted"], started)
            atomic_file(self.work / "sandbox-acceptance.json", json.dumps(report).encode(), uid=self.owner, gid=os.getegid(), mode=0o600)
            self.phase("identity_acceptance", "checking the installed identity boundaries")
            self.guard(False)
            self.systemctl("start", *SERVICES, timeout=120)
            self.ready(users)
            self.command(["/usr/bin/python3", "-I", str(self.work / "verify-installed-identities.py"),
                          "--human", args.human, "--output", str(self.work / "identity-acceptance.json")], timeout=120)
            identity = json.loads(read_file(self.work / "identity-acceptance.json", owner=self.owner))
            require(identity.get("passed") is True and identity.get("paid_actions_performed") is False
                    and identity.get("check_count", 0) >= 20 and identity.get("passed_count") == identity["check_count"], "IDENTITY_GATE_FAILED")
            self.idle()
            self.stage = "restore_research"
            for path, before, after, metadata in provenance:
                require(read_file(path, owner=metadata["uid"]) == before, "BACKUP_METADATA_CHANGED_DURING_UPGRADE")
                atomic_file(path, after, **metadata)
            self.write_config(original_config)
            self.systemctl("restart", "probe-research.service", timeout=120)
            self.ready(users)
            self.systemctl("start", "probe-backup.timer")
            self.systemctl("is-active", "--quiet", *SERVICES, "probe-backup.timer")
            receipt = {"schema_version": 1, "status": "passed", "source_commit": release["source_commit"],
                       "wheel_sha256": release["wheel_sha256"], "previous_wheel_sha256": hashlib.sha256(old_raw).hexdigest(),
                       "dependencies_unchanged": True, "sandbox_checks": len(report["checks"]),
                       "identity_checks": identity["check_count"], "cloud_mutations_performed": False,
                       "finished_at": datetime.now(timezone.utc).isoformat()}
            atomic_file(self.work / "upgrade-report.json", json.dumps(receipt, indent=2).encode() + b"\n",
                        uid=self.owner, gid=os.getegid(), mode=0o600)
            (self.config / "upgrade-blocked").unlink()
            self.phase("complete", "research restored after all checks passed")
            return receipt
        except BaseException:
            try:
                self.fail_closed()
            except BaseException:
                # A reboot still sees the already-written disabled config or
                # guard. Do not pretend services recovered or roll back data.
                pass
            raise


def validate_acceptance(report, image, uid, started):
    checks = report.get("checks")
    require(report.get("status") == "passed" and report.get("stage") == "complete" and report.get("image") == image
            and report.get("service_uid") == uid and type(checks) is dict and set(checks) == SANDBOX_CHECKS
            and all(value is True for value in checks.values())
            and checks.get("crash_deadline_enforced") is True and checks.get("crash_removal_confirmed") is True,
            "SANDBOX_GATE_FAILED")
    require(datetime.fromisoformat(report["started_at"]) >= started
            and datetime.fromisoformat(report["finished_at"]) >= datetime.fromisoformat(report["started_at"]), "STALE_SANDBOX_REPORT")
    require(all(report.get("lifecycle", {}).get(key) is True for key in
                ("program_started", "host_timer_excluded", "launchers_killed", "container_processes_stopped", "container_removed")), "CRASH_CLEANUP_UNPROVEN")


def main(argv=None):
    if os.geteuid() != 0:
        print("UPGRADE_REQUIRES_ADMINISTRATOR_AND_ISOLATED_SYSTEM_PYTHON", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--original-manifest-sha256", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--identity-checker", type=Path, required=True)
    parser.add_argument("--human", required=True)
    args = parser.parse_args(argv)
    upgrade = Upgrade()
    try:
        result = upgrade.execute(args)
    except BaseException as error:
        code = str(error) if isinstance(error, UpgradeError) else type(error).__name__
        print(f"Upgrade stopped at {upgrade.stage}: {code}. "
              + (("Research remains disabled; keep the saved previous wheel for reviewed rollback." if upgrade.closed_after_failure
                  else "Could not confirm every shutdown guard; administrator inspection is required.")
                 if upgrade.changed else "No application upgrade was applied."), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
