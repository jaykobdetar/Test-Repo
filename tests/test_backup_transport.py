"""Bounded transport recovery, exercised without any Drive connection."""

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from probe_core import backup
from probe_core.artifact_store import ArtifactStore
from probe_core.ledger import Ledger


QUOTA = (
    b"googleapi: Error 403: Quota exceeded for quota metric Queries and limit "
    b"Previous quota: Requests per minute, reason: RATE_LIMIT_EXCEEDED"
)
SECRET = "private-token-and-remote-location"


@pytest.fixture
def timer(monkeypatch):
    class Timer:
        now = 100.0

        def __init__(self):
            self.delays = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.delays.append(seconds)
            self.now += seconds

    value = Timer()
    monkeypatch.setattr(backup, "time", value)
    return value


@pytest.fixture
def transport(tmp_path, timer):
    store = ArtifactStore(tmp_path / "inputs")
    with Ledger(tmp_path / "research.sqlite") as ledger:
        receipt = backup.create_snapshot(
            ledger, tmp_path / "snapshot.tar", input_store=store.root, source_commit="a" * 40
        )
    config = tmp_path / "rclone.conf"
    config.write_text("[gdrive]\ntype = drive\ntoken = test-only\n")
    config.chmod(0o600)
    executable = tmp_path / "rclone"
    executable.write_text("""#!/usr/bin/python3
import hashlib, json, os, pathlib, shutil, sys
root = pathlib.Path(__file__).parent
scenario = json.loads((root / "scenario.json").read_text())
statefile = root / "calls.json"
calls = json.loads(statefile.read_text()) if statefile.exists() else []
args = sys.argv[1:]
operation = "copyto" if "copyto" in args else "cat"
index = args.index(operation)
assert args[args.index("--drive-root-folder-id") + 1] == "trustedFolder12345"
assert args[args.index("--low-level-retries") + 1] == "10"
assert args[args.index("--retries") + 1] == "1"
call = {"operation": operation, "args": args}
remote = root / "remote.tar"
if operation == "copyto":
    assert "--immutable" in args and "--checksum" in args and "--no-traverse" in args
    source = pathlib.Path(args[index + 1])
    call["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    call["source"] = str(source)
    if remote.exists() and remote.read_bytes() != source.read_bytes():
        raise SystemExit("immutable destination differs")
    # A completed upload may still return an uncertain failure. Its retry must
    # compare content, regardless of the newly pinned source's modification time.
    shutil.copyfile(source, remote)
    os.utime(remote, (1, 1))
attempt = sum(item["operation"] == operation for item in calls)
calls.append(call)
statefile.write_text(json.dumps(calls))
failures = scenario.get(operation, [])
if attempt < len(failures):
    if operation == "cat" and scenario.get("partial"):
        sys.stdout.buffer.write(remote.read_bytes() + b"incomplete trailing bytes")
        sys.stdout.buffer.flush()
    print(failures[attempt], file=sys.stderr)
    sys.exit(3)
if operation == "cat":
    sys.stdout.buffer.write(b"wrong content" if scenario.get("corrupt") else remote.read_bytes())
""")
    executable.chmod(0o700)

    def run(scenario):
        (tmp_path / "scenario.json").write_text(json.dumps(scenario))
        return backup.upload_snapshot(
            receipt["archive"],
            rclone_config=config,
            drive_folder_id="trustedFolder12345",
            receipt_directory=tmp_path / "receipts",
            rclone=str(executable),
        )

    return SimpleNamespace(
        run=run, source=receipt, root=tmp_path, calls=lambda: json.loads((tmp_path / "calls.json").read_text())
    )


@pytest.mark.parametrize(
    "stderr,operation,code,retry",
    [
        (QUOTA, "cat", "RATE_LIMITED", True),
        (b"googleapi: Error 403: quota exceeded, rateLimitExceeded", "copyto", "RATE_LIMITED", True),
        (b"googleapi: Error 403: userRateLimitExceeded", "cat", "RATE_LIMITED", True),
        (b"HTTP/2 429 Too Many Requests", "copyto", "PROVIDER_TEMPORARY", True),
        (b"googleapi: Error 503: backendError", "cat", "PROVIDER_TEMPORARY", True),
        (b"Failed to cat: directory not found", "cat", "READBACK_NOT_VISIBLE", True),
        (b"directory not found", "copyto", "UNCLASSIFIED_FAILURE", False),
        (b"unexpected EOF", "cat", "NETWORK_TEMPORARY", True),
        (b"connection reset by peer", "copyto", "NETWORK_TEMPORARY", True),
        (b"invalid_grant; unexpected EOF", "cat", "AUTHORIZATION_REJECTED", False),
        (b"immutable file modified; connection reset", "copyto", "IMMUTABLE_CONFLICT", False),
        (b"Failed to save config; HTTP 503", "copyto", "CREDENTIAL_WRITE_FAILED", False),
        (b"permission denied; unexpected EOF", "cat", "PERMISSION_REFUSED", False),
        (b"x509: certificate signed by unknown authority", "copyto", "TLS_VERIFICATION_FAILED", False),
        (b"googleapi: Error 401: unauthorized; EOF", "cat", "AUTHORIZATION_REFUSED", False),
        (b"googleapi: Error 403: storageQuotaExceeded", "copyto", "AUTHORIZATION_REFUSED", False),
        (b"unrecognized remote failure", "cat", "UNCLASSIFIED_FAILURE", False),
        (b"x" * 65537, "copyto", "DIAGNOSTIC_TOO_LARGE", False),
    ],
)
def test_classification_requires_specific_retry_evidence(stderr, operation, code, retry):
    assert backup._transport_failure(stderr, operation=operation) == (code, retry)


def test_quota_retry_reuses_identical_immutable_content_then_proves_restore(transport, timer, capsys):
    result = transport.run({"copyto": [QUOTA.decode() + " " + SECRET]})
    calls = transport.calls()
    assert [item["operation"] for item in calls] == ["copyto", "copyto", "cat"]
    assert calls[0]["source"] == calls[1]["source"]
    assert calls[0]["sha256"] == calls[1]["sha256"] == result["archive_sha256"]
    assert result["readback_verified"] is True and result["restore_verified"] is True
    assert hashlib.sha256((transport.root / "remote.tar").read_bytes()).hexdigest() == result["archive_sha256"]
    assert timer.delays == [5]
    assert capsys.readouterr().err == "backup_transport operation=copyto attempt=1 code=RATE_LIMITED\n"


def test_partial_cat_is_truncated_before_retry_and_receipt_requires_full_restore(transport, timer, capsys):
    result = transport.run({"cat": ["unexpected EOF " + SECRET, QUOTA.decode()], "partial": True})
    assert [item["operation"] for item in transport.calls()] == ["copyto", "cat", "cat", "cat"]
    assert result["archive_sha256"] == transport.source["archive_sha256"]
    assert result["restore_verified"] is True
    assert timer.delays == [5, 30]
    assert capsys.readouterr().err.splitlines() == [
        "backup_transport operation=cat attempt=1 code=NETWORK_TEMPORARY",
        "backup_transport operation=cat attempt=2 code=RATE_LIMITED",
    ]


@pytest.mark.parametrize(
    "failure,code",
    [
        ("invalid_grant", "AUTHORIZATION_REJECTED"),
        ("immutable file modified", "IMMUTABLE_CONFLICT"),
        ("permission denied", "PERMISSION_REFUSED"),
        ("a failure without temporary evidence", "UNCLASSIFIED_FAILURE"),
    ],
)
def test_permanent_failure_never_retries_or_writes_receipt(transport, timer, capsys, failure, code):
    with pytest.raises(backup.BackupError) as error:
        transport.run({"copyto": [failure + " " + SECRET]})
    assert str(error.value) == f"BACKUP_TRANSPORT_COPYTO_{code}_AFTER_1_ATTEMPTS"
    assert len(transport.calls()) == 1 and timer.delays == []
    assert not list((transport.root / "receipts").glob("*.json"))
    assert SECRET not in capsys.readouterr().err


def test_persistent_missing_readback_fails_truthfully_after_bounded_retries(transport, timer, capsys):
    with pytest.raises(backup.BackupError, match="BACKUP_TRANSPORT_CAT_READBACK_NOT_VISIBLE_AFTER_3_ATTEMPTS"):
        transport.run({"cat": ["Failed to cat: directory not found " + SECRET] * 3})
    assert len(transport.calls()) == 4
    assert timer.delays == [5, 30]
    assert not list((transport.root / "receipts").glob("*.json"))
    assert len(capsys.readouterr().err.splitlines()) == 3


def test_success_exit_with_corrupt_content_is_not_a_transport_retry(transport, timer):
    with pytest.raises(backup.BackupError):
        transport.run({"corrupt": True})
    assert len(transport.calls()) == 2 and timer.delays == []
    assert not list((transport.root / "receipts").glob("*.json"))


def test_timeout_and_backoff_share_one_budget_across_operations(tmp_path, timer, monkeypatch, capsys):
    calls = []

    def run(args, **kwargs):
        calls.append((args[1], kwargs["timeout"]))
        if args[1] == "copyto":
            timer.now += 59
            return SimpleNamespace(returncode=0)
        timer.now += kwargs["timeout"]
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=SECRET.encode())

    monkeypatch.setattr(backup.subprocess, "run", run)
    deadline = timer.now + 240
    backup._run_transfer(["rclone"], ["copyto", "source", "remote"], config=tmp_path / "config", deadline=deadline)
    with pytest.raises(backup.BackupError, match="BACKUP_TRANSPORT_CAT_PROCESS_TIMEOUT_AFTER_3_ATTEMPTS"):
        backup._run_transfer(["rclone"], ["cat", "remote"], config=tmp_path / "config", deadline=deadline)
    assert calls == [("copyto", 60), ("cat", 60), ("cat", 60), ("cat", 26)]
    assert timer.now == deadline and timer.delays == [5, 30]
    assert SECRET not in capsys.readouterr().err
    with pytest.raises(backup.BackupError, match="BACKUP_TRANSPORT_COPYTO_DEADLINE_AFTER_0_ATTEMPTS"):
        backup._run_transfer(["rclone"], ["copyto", "source", "remote"], config=tmp_path / "config", deadline=deadline)
    assert len(calls) == 4


def test_insufficient_backoff_budget_stops_without_sleep_or_another_call(tmp_path, timer, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs["timeout"])
        timer.now += 8
        return SimpleNamespace(returncode=3, stderr=QUOTA)

    monkeypatch.setattr(backup.subprocess, "run", run)
    with pytest.raises(backup.BackupError, match="BACKUP_TRANSPORT_CAT_DEADLINE_AFTER_1_ATTEMPTS"):
        backup._run_transfer(["rclone"], ["cat", "remote"], config=tmp_path / "config", deadline=timer.now + 10)
    assert calls == [10] and timer.delays == []


def test_pending_archives_do_not_receive_separate_transfer_budgets(tmp_path, timer, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for index in range(2):
        (outbox / f"probe-{index}.tar").write_bytes(str(index).encode())
    seen = []

    def upload(archive, **kwargs):
        seen.append(kwargs["_transport_deadline"])
        timer.now += 120
        return {"verified": True}

    monkeypatch.setattr(backup, "upload_snapshot", upload)
    backup.upload_pending(
        outbox=outbox,
        rclone_config=tmp_path / "config",
        drive_folder_id="trustedFolder12345",
        receipt_directory=tmp_path / "receipts",
    )
    assert seen == [340, 340]
