#!/usr/bin/python3
"""Administrator-run, bounded acceptance of installed OS identity boundaries.

No services, jobs, approvals, provider resources or credentials are modified.
Harmless research RPC reads append their normal audit records. Only the private
JSON report is written. Run with /usr/bin/python3 -I, --human and --output.
Completed history is permitted; active or unresolved execution is refused.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import subprocess
import sys
import uuid


ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
ACCOUNTS = ("probe-trusted", "probe-research", "probe-watchdog", "probe-backup")
GROUPS = (*ACCOUNTS, "probe-ipc", "probe-ledger-read", "probe-watch-read", "probe-stop", "probe-backup-read")
LEDGER = Path("/var/lib/probe-core/research.sqlite")
PROVIDER = Path("/var/lib/probe-provider/runpod.sqlite")
PROVIDER_KEY = Path("/etc/probe-core/runpod-api-key")
BACKUP_KEY = Path("/var/lib/probe-backup/rclone.conf")
RESEARCH_SOCKET = Path("/run/probe-research/research.sock")
ADMIN_SOCKET = Path("/run/probe-controller/admin.sock")
STOP_SOCKET = Path("/run/probe-provider/stop.sock")
UNITS = {
    "probe-controller.service": ("probe-trusted", "probe-ipc", {"probe-trusted", "probe-ledger-read", "probe-watch-read"}, "probe_core.controller", "serve"),
    "probe-research.service": ("probe-trusted", "probe-trusted", {"probe-research", "probe-ipc", "probe-ledger-read"}, "probe_core.research_service", None),
    "probe-provider-stop.service": ("probe-trusted", "probe-stop", {"probe-trusted"}, "probe_core.runpod_provider", "serve-stop"),
    "probe-watchdog.service": ("probe-watchdog", "probe-watch-read", {"probe-ledger-read", "probe-stop"}, "probe_core.controller", "watchdog"),
    "probe-backup.service": ("probe-backup", "probe-backup-read", {"probe-backup-read"}, None, None),
}
PROPERTIES = ("LoadState", "ActiveState", "SubState", "MainPID", "User", "Group", "SupplementaryGroups",
              "FragmentPath", "DropInPaths", "UnitFileState", "ExecMainStatus", "NoNewPrivileges",
              "ProtectSystem", "ProtectHome", "PrivateTmp")


class CheckFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise CheckFailure(code)


def read_regular(path, limit=1024 * 1024, *, owner=0, private=False):
    path = Path(path)
    require(path.resolve() == path.absolute(), "UNSAFE_PATH")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == owner
                and not info.st_mode & (0o077 if private else 0o022) and info.st_size <= limit, "UNSAFE_FILE")
        result = stream.read(limit + 1)
        require(len(result) == info.st_size, "FILE_CHANGED")
        return result


def discover(human):
    require(human not in ACCOUNTS, "IDENTITIES_NOT_DISTINCT")
    users = {name: pwd.getpwnam(name) for name in (*ACCOUNTS, human)}
    groups = {name: grp.getgrnam(name).gr_gid for name in GROUPS}
    require(len({entry.pw_uid for entry in users.values()}) == 5
            and all(entry.pw_uid > 0 for entry in users.values()), "IDENTITIES_NOT_DISTINCT")
    require(all(users[name].pw_gid == groups[name] for name in ACCOUNTS), "ACCOUNT_PRIMARY_GROUP")
    memberships = {name: {entry.gr_gid for entry in grp.getgrall() if name in entry.gr_mem} for name in users}
    require(memberships["probe-research"] <= {groups["probe-research"]}, "RESEARCH_EXTRA_GROUPS")
    return users, groups, memberships


def unit_values(name, *, run=subprocess.run):
    result = run(["/usr/bin/systemctl", "show", name, "--property=" + ",".join(PROPERTIES)],
                 cwd="/", env=ENV, stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 32768, "UNIT_QUERY_FAILED")
    values = {}
    for line in result.stdout.decode("utf-8", errors="strict").splitlines():
        key, separator, value = line.partition("=")
        require(separator and key in PROPERTIES and key not in values, "UNIT_RESPONSE_INVALID")
        values[key] = value
    require(set(values) == set(PROPERTIES), "UNIT_RESPONSE_INVALID")
    return values


def inspect_units(users, groups, memberships, *, run=subprocess.run, proc_root=Path("/proc"), unit_root=Path("/etc/systemd/system"), owner=0):
    roles, pids = {}, {}
    for name, (account, primary, supplements, module, command) in UNITS.items():
        values = unit_values(name, run=run)
        read_regular(unit_root / name, 65536, owner=owner)
        require(values["LoadState"] == "loaded" and values["DropInPaths"] == ""
                and values["FragmentPath"] == str(unit_root / name), "UNIT_SOURCE_CHANGED")
        require(values["User"] == account and values["Group"] == primary
                and set(values["SupplementaryGroups"].split()) == supplements, "UNIT_IDENTITY_CHANGED")
        research = name == "probe-research.service"
        require(values["ProtectSystem"] == "strict" and values["PrivateTmp"] == "yes"
                and values["ProtectHome"] == ("read-only" if research else "yes")
                and values["NoNewPrivileges"] == ("no" if research else "yes"), "UNIT_RESTRICTIONS_CHANGED")
        effective_groups = memberships[account] | {groups[primary]} | {groups[item] for item in supplements}
        role = {"account": account, "uid": users[account].pw_uid, "gid": groups[primary], "groups": sorted(effective_groups)}
        require(values["MainPID"].isdecimal(), "UNIT_PID_INVALID")
        pid = int(values["MainPID"])
        if module is None:
            require(values["ActiveState"] == "inactive" and values["SubState"] == "dead"
                    and pid == 0 and values["ExecMainStatus"] == "0", "BACKUP_NOT_COMPLETED")
        else:
            require(values["ActiveState"] == "active" and values["SubState"] == "running"
                    and values["UnitFileState"] == "enabled" and pid > 1, "SERVICE_NOT_RUNNING")
            status = (proc_root / str(pid) / "status").read_text()
            fields = {line.split(":", 1)[0]: line.split(":", 1)[1].split() for line in status.splitlines() if ":" in line}
            require(fields.get("Uid") == [str(role["uid"])] * 4
                    and fields.get("Gid") == [str(role["gid"])] * 4
                    and {int(item) for item in fields.get("Groups", [])} == effective_groups, "PROCESS_IDENTITY_CHANGED")
            raw = (proc_root / str(pid) / "cmdline").read_bytes()
            require(len(raw) <= 32768, "PROCESS_COMMAND_CHANGED")
            argv = raw.rstrip(b"\0").decode().split("\0")
            expected = ["/opt/probe-core/venv/bin/python", "-I", "-m", module]
            if command:
                expected.append(command)
            require(argv[:len(expected)] == expected, "PROCESS_COMMAND_CHANGED")
            pids[name] = pid
        roles[name] = role
    return roles, pids


def validate_config(users, groups, human, *, config_root=Path("/etc/probe-core"), owner=0):
    research = json.loads(read_regular(config_root / "research.json", owner=owner))
    expected = {"ledger_path": str(LEDGER), "socket_path": str(RESEARCH_SOCKET),
                "service_uid": users["probe-trusted"].pw_uid, "research_uid": users["probe-research"].pw_uid,
                "admin_uid": users[human].pw_uid, "socket_gid": groups["probe-research"],
                "controller_uid": users["probe-trusted"].pw_uid,
                "controller_socket": "/run/probe-controller/research.sock"}
    require(all(type(research.get(key)) is type(value) and research[key] == value for key, value in expected.items()), "SERVICE_CONFIG_IDENTITY")
    provider = json.loads(read_regular(config_root / "runpod.json", owner=owner))
    require(provider.get("state_path") == str(PROVIDER) and provider.get("api_key_file") == str(PROVIDER_KEY), "PROVIDER_CONFIG_PATH")
    for path, uid in ((LEDGER, users["probe-trusted"].pw_uid), (PROVIDER_KEY, users["probe-trusted"].pw_uid),
                      (BACKUP_KEY, users["probe-backup"].pw_uid)):
        require(path.resolve() == path.absolute(), "UNSAFE_PATH")
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_size > 0
                and info.st_nlink == 1 and not info.st_mode & (0o077 if path != LEDGER else 0o027), "PROTECTED_FILE_METADATA")
    for path, group in ((RESEARCH_SOCKET, "probe-research"), (ADMIN_SOCKET, "probe-ipc"), (STOP_SOCKET, "probe-stop")):
        require(path.resolve() == path.absolute(), "UNSAFE_PATH")
        info = path.lstat()
        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == users["probe-trusted"].pw_uid
                and info.st_gid == groups[group] and stat.S_IMODE(info.st_mode) == 0o660, "SOCKET_METADATA")


# The upgrader extracts this literal from the independently hash-verified checker
# bytes. Both gates therefore use the same reader, even before a new wheel is
# installed. It imports no application code and never opens SQLite for writing.
STATE_READER = r'''
import base64, hashlib, json, re, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path

def _history_require(condition, code):
    if not condition:
        raise ValueError(code)

def _history_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            _history_require(key not in result, "HISTORY_DUPLICATE_JSON_KEY")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("HISTORY_NONFINITE_JSON")))

def _history_canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def _history_database(path):
    connection = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    until = time.monotonic() + 10
    connection.set_progress_handler(lambda: int(time.monotonic() > until), 10000)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        _history_require([row[0] for row in connection.execute("PRAGMA integrity_check")] == ["ok"], "DATABASE_INTEGRITY_FAILED")
        _history_require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "DATABASE_FOREIGN_KEY_FAILED")
        schema = [list(row) for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        tables = {}
        for kind, name, _table, _sql in schema:
            if kind != "table":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            rows = [dict(row) for row in connection.execute("SELECT * FROM " + quoted).fetchmany(100001)]
            _history_require(len(rows) <= 100000, "HISTORY_TOO_LARGE")
            tables[name] = rows
        # These are separate database snapshots, not an atomic multi-DB backup.
        return schema, tables
    finally:
        connection.close()

def idle_history_snapshot(ledger, provider, *, worker_id=None, expected=None):
    schema, tables = _history_database(ledger)
    provider_schema, provider_tables = _history_database(provider)
    _history_require({"jobs", "attempts", "approvals", "approval_jobs", "hypotheses", "manifests", "compute_requests", "audit_events"} <= set(tables)
                     and "runpod_intents" in provider_tables, "HISTORY_SCHEMA_MISSING")
    jobs, attempts, approvals, requests = (tables[key] for key in ("jobs", "attempts", "approvals", "compute_requests"))
    intents = provider_tables["runpod_intents"]
    if worker_id is not None:
        _history_require(not any(row["worker_id"] == worker_id for row in intents), "PROBE_WORKER_ALREADY_REGISTERED")
    _history_require(all(row["state"] in {"COMPLETED", "FAILED"} for row in jobs), "NONTERMINAL_JOB_EXISTS")
    _history_require(all(row["stopped_at"] is not None for row in attempts), "UNSTOPPED_ATTEMPT_EXISTS")
    approval_by_id = {row["approval_id"]: row for row in approvals}
    request_by_id = {row["request_id"]: row for row in requests}
    now = datetime.now(timezone.utc).timestamp()
    for row in approvals:
        document = _history_json(row["document"])
        _history_require(document.get("approval_id") == row["approval_id"], "APPROVAL_DOCUMENT_MISMATCH")
        if row["consumed_at"] is not None:
            _history_require(row["ended_at"] is not None and row["deadline"] is not None
                             and row["ended_at"] >= row["consumed_at"], "UNCLOSED_APPROVAL_EXISTS")
        else:
            expiry = datetime.fromisoformat(document["expires_at"].replace("Z", "+00:00"))
            _history_require(expiry.tzinfo is not None and expiry.timestamp() <= now
                             and row["deadline"] is None, "UNCONSUMED_AUTHORITY_EXISTS")
    for row in attempts:
        _history_require(row["approval_id"] in approval_by_id, "ATTEMPT_APPROVAL_MISSING")
    for row in requests:
        state = row["state"]
        approval = approval_by_id.get(row["approval_id"])
        linked = [item for item in intents if item["request_key"] == row["request_id"]]
        if state == "PENDING":
            # A proposal without a nonce or provider intent is not execution
            # authority. Preserve it; changing the launch config cannot approve it.
            _history_require(approval is None and not linked and row["deadline"] is None
                             and row["observed_provider_id"] is None, "PENDING_REQUEST_HAS_AUTHORITY")
        else:
            _history_require(state in {"STOPPED", "REJECTED"}, "UNRESOLVED_COMPUTE_REQUEST")
            if state == "STOPPED":
                _history_require(approval is not None and approval["consumed_at"] is not None
                                 and approval["ended_at"] is not None, "STOPPED_REQUEST_APPROVAL_MISSING")
        if approval is not None:
            _history_require(_history_json(approval["document"]).get("pod_id") == row["worker_id"], "REQUEST_APPROVAL_WORKER_MISMATCH")
    for row in intents:
        request = request_by_id.get(row["request_key"])
        _history_require(request is not None and request["state"] == "STOPPED"
                         and request["worker_id"] == row["worker_id"] and row["provider_id"] is not None
                         and request["observed_provider_id"] == row["provider_id"]
                         and request["configuration_hash"] == row["configuration_hash"]
                         and request["deadline"] == row["deadline"], "UNRESOLVED_PROVIDER_INTENT")
        configuration = _history_json(row["configuration"])
        _history_require(configuration == {key: value for key, value in _history_json(request["configuration"]).items() if value is not None}
                         and "sha256:" + hashlib.sha256(_history_canonical(configuration).encode()).hexdigest() == row["configuration_hash"],
                         "PROVIDER_CONFIGURATION_MISMATCH")
    previous = "0" * 64
    audit = sorted(tables["audit_events"], key=lambda row: row["sequence"])
    prefix_sequence = expected["audit_events"] if expected is not None else len(audit)
    prefix = previous if prefix_sequence == 0 else None
    for sequence, row in enumerate(audit, 1):
        record = _history_json(row["record"])
        _history_require(set(record) == {"sequence", "timestamp", "event_type", "payload", "previous_hash", "hash"}
                         and type(record["sequence"]) is int and row["sequence"] == record["sequence"] == sequence
                         and record["previous_hash"] == previous and type(record["payload"]) is dict
                         and type(record["event_type"]) is str
                         and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", record["event_type"]), "AUDIT_CHAIN_INVALID")
        timestamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
        _history_require(timestamp.tzinfo is not None and record["timestamp"] == timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), "AUDIT_TIMESTAMP_INVALID")
        digest = hashlib.sha256(_history_canonical({key: value for key, value in record.items() if key != "hash"}).encode()).hexdigest()
        _history_require(row["hash"] == record["hash"] == digest, "AUDIT_HASH_INVALID")
        previous = digest
        if sequence == prefix_sequence:
            prefix = digest
    def serial(value):
        if isinstance(value, bytes):
            return {"sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
        if isinstance(value, dict):
            return {key: serial(item) for key, item in value.items()}
        return value
    def content(source, *, exclude_audit=False):
        return {name: sorted(_history_canonical(serial(row)) for row in rows)
                for name, rows in source.items() if not (exclude_audit and name == "audit_events")}
    history_hash = hashlib.sha256(_history_canonical({"ledger_schema": schema, "ledger": content(tables, exclude_audit=True),
                        "provider_schema": provider_schema, "provider": content(provider_tables)}).encode()).hexdigest()
    result = {key: len(tables[key]) for key in ("jobs", "attempts", "approvals", "compute_requests", "audit_events")}
    result.update(runpod_intents=len(intents), history_sha256=history_hash, audit_tip=previous,
                  audit_prefix_sha256=prefix, idle=True, audit_valid=True)
    if expected is not None:
        _history_require(result["history_sha256"] == expected["history_sha256"]
                         and len(audit) >= expected["audit_events"] and prefix == expected["audit_tip"], "HISTORICAL_STATE_CHANGED")
    return result
'''
exec(STATE_READER)


def state_counts(*, ledger=LEDGER, provider=PROVIDER, worker_id=None, expected=None):
    try:
        return idle_history_snapshot(ledger, provider, worker_id=worker_id, expected=expected)
    except (ValueError, KeyError, TypeError, sqlite3.Error) as error:
        code = str(error)
        raise CheckFailure(code if re.fullmatch(r"[A-Z_]{1,80}", code) else "HISTORY_VERIFICATION_FAILED") from None


def completed_archive(*, outbox=Path("/var/lib/probe-backups/outbox"), receipts=Path("/var/lib/probe-backups/receipts"), trusted_uid, backup_uid):
    receipt_paths, archives = sorted(receipts.glob("*.json")), sorted(outbox.glob("probe-*.tar"))
    require(0 < len(receipt_paths) <= 100 and 0 < len(archives) <= 10, "BACKUP_RECEIPT_OR_OUTBOX_MISSING")
    confirmed = set()
    for path in receipt_paths:
        value = json.loads(read_regular(path, 65536, owner=backup_uid))
        require(all(value.get(key) is True for key in ("verified", "readback_verified", "restore_verified")), "BACKUP_NOT_VERIFIED")
        checksum = value.get("archive_sha256")
        require(isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum), "BACKUP_RECEIPT_INVALID")
        confirmed.add(checksum)
    remaining = 64 * 1024 * 1024
    for path in reversed(archives):
        raw = read_regular(path, remaining, owner=trusted_uid)
        remaining -= len(raw)
        if hashlib.sha256(raw).hexdigest() in confirmed:
            return path
    raise CheckFailure("NO_VERIFIED_LOCAL_ARCHIVE")


# This constant is sent via -c: restricted accounts never read the administrator's
# private copy of this script. The stdin plan contains only public paths/IDs.
CHILD = r'''
import errno, json, os, socket, stat, struct, sys

def rpc(check):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        try:
            connection.connect(check["path"])
        except OSError as error:
            return check["kind"] == "deny_connect" and error.errno in (errno.EACCES, errno.EPERM)
        if check["kind"] == "deny_connect":
            return False
        uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if uid != check["server_uid"]:
            return False
        kind = check["kind"]
        methods = {"lab_status": ("lab_status", {}), "discovery": ("query_runs", {"limit": 1}),
                   "controller_status": ("status", {}), "stop_status": ("status", {"worker_id": check.get("worker_id")})}
        method, params = methods[kind]
        connection.sendall(json.dumps({"method": method, "params": params}).encode() + b"\n")
        with connection.makefile("rb") as stream:
            raw = stream.readline(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
            return False
        body = json.loads(raw)
        if type(body) is not dict or body.get("ok") is not True:
            return False
        result = body.get("result")
        if kind == "lab_status":
            return (type(result) is dict and result.get("gpu_start_authority") is False
                    and result.get("evaluation_authority") is False and type(result.get("jobs")) is list)
        if kind == "discovery":
            return type(result) is list and all(type(row) is dict and row.get("state") in ("COMPLETED", "FAILED") for row in result)
        if kind == "controller_status":
            return type(result) is list and all(type(row) is dict and row.get("state") in ("PENDING", "STOPPED", "REJECTED") for row in result)
        return (type(result) is dict and result.get("worker_id") == check["worker_id"]
                and result.get("state") == "ABSENT" and result.get("provider_id") is None)

def check_one(check):
    try:
        if check["kind"] in ("read_file", "deny_read"):
            try:
                fd = os.open(check["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError as error:
                return check["kind"] == "deny_read" and error.errno in (errno.EACCES, errno.EPERM)
            try:
                if check["kind"] == "deny_read":
                    return False
                return stat.S_ISREG(os.fstat(fd).st_mode) and len(os.read(fd, 1)) == 1
            finally:
                os.close(fd)
        return rpc(check)
    except Exception:
        return False

try:
    plan = json.loads(sys.stdin.buffer.read(65537))
    exact = (os.getuid() == os.geteuid() == plan["uid"] and os.getgid() == os.getegid() == plan["gid"]
             and set(os.getgroups()) == set(plan["groups"]))
    checks = plan["checks"]
    if type(checks) is not list or len(checks) > 16:
        raise ValueError()
    result = {"identity": exact, "checks": {item["code"]: check_one(item) if exact else False for item in checks}}
    print(json.dumps(result, sort_keys=True))
except Exception:
    print('{"identity":false,"checks":{}}')
    raise SystemExit(1)
'''


def plans(users, groups, memberships, roles, human, archive, worker_id):
    trusted = users["probe-trusted"].pw_uid

    def file_check(code, path, allowed=False):
        return {"code": code, "kind": "read_file" if allowed else "deny_read", "path": str(path)}

    def endpoint(code, path, kind):
        return {"code": code, "kind": kind, "path": str(path), "server_uid": trusted, "worker_id": worker_id}

    research = {"account": "probe-research", "uid": users["probe-research"].pw_uid,
                "gid": groups["probe-research"], "groups": [groups["probe-research"]]}
    research["checks"] = [file_check("research_ledger_denied", LEDGER), file_check("research_provider_key_denied", PROVIDER_KEY),
        file_check("research_backup_key_denied", BACKUP_KEY), endpoint("research_admin_connect_denied", ADMIN_SOCKET, "deny_connect"),
        endpoint("research_stop_connect_denied", STOP_SOCKET, "deny_connect"), endpoint("research_status_allowed", RESEARCH_SOCKET, "lab_status"),
        endpoint("research_discovery_allowed", RESEARCH_SOCKET, "discovery")]
    watchdog = dict(roles["probe-watchdog.service"])
    watchdog["checks"] = [file_check("watchdog_ledger_allowed", LEDGER, True), file_check("watchdog_provider_key_denied", PROVIDER_KEY),
        file_check("watchdog_backup_key_denied", BACKUP_KEY), endpoint("watchdog_stop_status_allowed", STOP_SOCKET, "stop_status")]
    backup = dict(roles["probe-backup.service"])
    backup["checks"] = [file_check("backup_ledger_denied", LEDGER), file_check("backup_provider_key_denied", PROVIDER_KEY),
                        file_check("backup_credentials_allowed", BACKUP_KEY, True), file_check("backup_verified_outbox_allowed", archive, True)]
    human_role = {"account": human, "uid": users[human].pw_uid, "gid": users[human].pw_gid,
                  "groups": sorted(memberships[human] | {users[human].pw_gid}),
                  "checks": [endpoint("human_controller_status_allowed", ADMIN_SOCKET, "controller_status")]}
    result = [research, watchdog, backup, human_role]
    for role, plan in zip(("research", "watchdog", "backup", "human"), result):
        plan["role"] = role
    return result


def run_probe(plan, *, run=subprocess.run):
    command = ["/usr/sbin/runuser", "-u", plan["account"], "-g", grp.getgrgid(plan["gid"]).gr_name]
    for gid in plan["groups"]:
        command.extend(["-G", grp.getgrgid(gid).gr_name])
    command.extend(["--", "/usr/bin/python3", "-I", "-c", CHILD])
    result = run(command, input=json.dumps(plan).encode(), cwd="/", env=ENV, capture_output=True, timeout=30, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 8192, "ROLE_PROBE_FAILED")
    value = json.loads(result.stdout)
    expected = {check["code"] for check in plan["checks"]}
    require(type(value) is dict and set(value) == {"identity", "checks"} and type(value["identity"]) is bool
            and type(value["checks"]) is dict and set(value["checks"]) == expected
            and all(type(item) is bool for item in value["checks"].values()), "ROLE_RESPONSE_INVALID")
    return {plan["role"] + "_identity": value["identity"], **value["checks"]}


def acceptance(human):
    users, groups, memberships = discover(human)
    roles, pids = inspect_units(users, groups, memberships)
    validate_config(users, groups, human)
    worker_id = "identity-check-" + uuid.uuid4().hex
    before = state_counts(worker_id=worker_id)
    archive = completed_archive(trusted_uid=users["probe-trusted"].pw_uid, backup_uid=users["probe-backup"].pw_uid)
    checks = {"unit_and_process_identities": True, "installed_config_metadata": True, "completed_backup_present": True}
    for plan in plans(users, groups, memberships, roles, human, archive, worker_id):
        checks.update(run_probe(plan))
    after = state_counts(worker_id=worker_id, expected=before)
    checks["jobs_approvals_provider_intents_unchanged"] = (before["history_sha256"] == after["history_sha256"]
        and after["audit_prefix_sha256"] == before["audit_tip"] and after["audit_events"] >= before["audit_events"])
    checks["normal_research_reads_audited"] = after["audit_events"] >= before["audit_events"] + 2
    checks["service_processes_unchanged"] = all(int(unit_values(name)["MainPID"]) == pid for name, pid in pids.items())
    return checks, after["audit_events"] - before["audit_events"]


def write_report(path, report):
    path = Path(path)
    require(path.is_absolute() and path.parent.resolve() == path.parent.absolute(), "REPORT_PATH_UNSAFE")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    info = path.parent.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o077, "REPORT_DIRECTORY_NOT_PRIVATE")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    if os.geteuid() != 0:
        print("IDENTITY_ACCEPTANCE_REQUIRES_ADMINISTRATOR", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--human", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    checks, audit_count, errors = {}, 0, []
    try:
        checks, audit_count = acceptance(args.human)
    except CheckFailure as error:
        errors.append(error.code)
    except Exception:
        errors.append("IDENTITY_ACCEPTANCE_UNAVAILABLE")
    errors.extend("CHECK_FAILED_" + code.upper() for code, passed in checks.items() if not passed)
    report = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
              "passed": not errors, "checks": checks, "check_count": len(checks), "passed_count": sum(checks.values()),
              "failure_codes": errors, "normal_research_audit_events": audit_count, "paid_actions_performed": False}
    try:
        write_report(args.output, report)
    except Exception:
        print("IDENTITY_REPORT_WRITE_FAILED", file=sys.stderr)
        return 1
    print(json.dumps({key: report[key] for key in ("passed", "check_count", "passed_count", "failure_codes")}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
