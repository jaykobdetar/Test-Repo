import hashlib
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile

import pytest

from probe_core.artifact_store import ArtifactStore
from probe_core.backup import BackupError, create_snapshot, restore_snapshot, upload_snapshot, upload_pending, prune_verified_outbox, publish_snapshot, verify_snapshot
from probe_core.ledger import ArtifactError, Ledger
from test_ledger import clock, manifest_data, job_factory, approve, prepare_manifest


@pytest.fixture
def snapshot(tmp_path, clock, job_factory, manifest_data):
    store = ArtifactStore(tmp_path / "inputs")
    source = tmp_path / "analysis.txt"
    source.write_bytes(b"retained CPU analysis")
    retained = store.register(source)
    with Ledger(tmp_path / "original.sqlite", clock=clock) as ledger:
        job = ledger.submit_job(job_factory())
        grant = approve(ledger, clock, [job])
        job = ledger.dispatch_next("worker-1", approval_id=grant.approval_id)
        ledger.start_job(job.job_id, job.attempt_id, "worker-1")
        ledger.begin_finalization(job.job_id, job.attempt_id, "worker-1")
        ledger.confirm_stopped(job.job_id, job.attempt_id)
        manifest = prepare_manifest(manifest_data, job, tmp_path / "result", clock)
        ledger.complete_job(job.job_id, job.attempt_id, "worker-1", manifest, artifact_root=tmp_path / "result")
        original_records = ledger.audit_records()
        receipt = create_snapshot(ledger, tmp_path / "snapshot.tar", input_store=store.root, source_commit="a" * 40)
        # Changes after the snapshot do not alter its audit or database.
        ledger.record_event("tool_call", {"tool": "after_snapshot"})
    return receipt, job, manifest, retained, original_records


def test_live_snapshot_restores_artifacts_inputs_and_audit_independently(snapshot, tmp_path):
    receipt, job, manifest, retained, records = snapshot
    assert verify_snapshot(receipt["archive"], expected_sha256=receipt["archive_sha256"])["verified"]
    restored = tmp_path / "recovered"
    result = restore_snapshot(receipt["archive"], restored, expected_sha256=receipt["archive_sha256"])
    assert result["services_started"] is False
    with Ledger(restored / "research.sqlite") as ledger:
        assert ledger.get_manifest(job.job_id) == manifest
        assert ledger.audit_records()[:-1] == records
        assert ledger.audit_records()[-1]["payload"]["tool"] == "backup_restore"
        path = ledger.get_artifact_root(job.job_id)
        assert path.is_relative_to(restored)
        assert (path / "artifacts/result.txt").read_bytes() == b"validated result\n"
        assert (path / "artifacts/result.txt").stat().st_mode & 0o222 == 0
        with ledger.read_connection() as connection:
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='manifest_no_update'").fetchone()
    assert ArtifactStore(restored / "inputs").read(retained["artifact_id"], max_bytes=100)[1] == b"retained CPU analysis"
    assert (restored / "RESTORE-OFFLINE.txt").is_file()
    with pytest.raises(BackupError, match="new"):
        restore_snapshot(receipt["archive"], restored, expected_sha256=receipt["archive_sha256"])


def test_snapshot_excludes_credentials_and_partial_inputs(tmp_path):
    store = ArtifactStore(tmp_path / "inputs")
    (store.root / ".stage-unpublished").mkdir()
    (store.root / ".stage-unpublished/credential").write_text("should never enter backup")
    (tmp_path / "rclone.conf").write_text("private configuration")
    with Ledger(tmp_path / "research.sqlite") as ledger:
        result = create_snapshot(ledger, tmp_path / "snapshot.tar", input_store=store.root, source_commit="b" * 40)
    with tarfile.open(result["archive"]) as archive:
        assert set(archive.getnames()) == {"snapshot.json", "state/research.sqlite", "state/research.audit.jsonl"}
    assert Path(result["archive"]).stat().st_mode & 0o077 == 0


def test_archive_tamper_and_oversize_fail(snapshot, tmp_path):
    receipt, *_ = snapshot
    with pytest.raises(BackupError, match="checksum"):
        verify_snapshot(receipt["archive"], expected_sha256="0" * 64)
    with pytest.raises(BackupError, match="limit|oversized"):
        verify_snapshot(receipt["archive"], max_bytes=1)


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE), ("snapshot.json", tarfile.SYMTYPE)])
def test_untrusted_archive_paths_and_links_are_rejected(tmp_path, name, kind):
    target = tmp_path / "unsafe.tar"
    with tarfile.open(target, "w") as archive:
        info = tarfile.TarInfo(name)
        info.type, info.linkname = kind, "outside"
        archive.addfile(info, io.BytesIO())
    with pytest.raises(BackupError):
        verify_snapshot(target)
    assert not (tmp_path / "escape").exists()


def test_snapshot_refuses_symlink_inputs_and_existing_archive(tmp_path):
    store = ArtifactStore(tmp_path / "inputs")
    source = tmp_path / "source"
    source.write_text("observation")
    record = store.register(source)
    data = store.root / record["path"]
    data.unlink()
    data.symlink_to(source)
    with Ledger(tmp_path / "research.sqlite") as ledger:
        with pytest.raises((BackupError, OSError, ArtifactError)):
            create_snapshot(ledger, tmp_path / "snapshot.tar", input_store=store.root, source_commit="c" * 40)
        (tmp_path / "existing.tar").write_text("keep")
        with pytest.raises(BackupError, match="new"):
            create_snapshot(ledger, tmp_path / "existing.tar", input_store=store.root, source_commit="c" * 40)
    assert (tmp_path / "existing.tar").read_text() == "keep"


def fake_rclone(tmp_path, *, failure=False, corrupt=False):
    executable = tmp_path / "rclone"
    executable.write_text(f'''#!/usr/bin/python3
import pathlib, shutil, sys
args = sys.argv[1:]
assert args[args.index('--drive-root-folder-id') + 1] == 'trustedFolder12345'
remote = pathlib.Path({str(tmp_path / "remote.tar")!r})
if {failure!r}:
    print('invalid_grant: sensitive-token-value', file=sys.stderr)
    sys.exit(1)
if 'copyto' in args:
    shutil.copyfile(args[args.index('copyto') + 1], remote)
elif 'cat' in args:
    sys.stdout.buffer.write(b'corrupt' if {corrupt!r} else remote.read_bytes())
else:
    sys.exit(2)
''')
    executable.chmod(0o700)
    config = tmp_path / "rclone.conf"
    config.write_text('[gdrive]\ntype = drive\ntoken = test-secret\n')
    config.chmod(0o600)
    return executable, config


def test_drive_upload_download_and_restore_are_all_verified(snapshot, tmp_path):
    receipt, *_ = snapshot
    executable, config = fake_rclone(tmp_path)
    result = upload_snapshot(receipt["archive"], rclone_config=config, drive_folder_id="trustedFolder12345", receipt_directory=tmp_path / "receipts", rclone=str(executable))
    assert result["archive_sha256"] == receipt["archive_sha256"]
    assert result["readback_verified"] and result["restore_verified"]
    assert list((tmp_path / "receipts").glob("*.json"))


@pytest.mark.parametrize("operation", ["verify", "restore", "upload"])
def test_archive_replacement_after_pinning_cannot_change_verified_bytes(snapshot, tmp_path, monkeypatch, operation):
    import probe_core.backup as backup

    receipt, job, _, _, records = snapshot
    original = Path(receipt["archive"])
    empty_inputs = ArtifactStore(tmp_path / "replacement-inputs")
    with Ledger(tmp_path / "replacement.sqlite") as ledger:
        replacement = create_snapshot(ledger, tmp_path / "replacement.tar", input_store=empty_inputs.root, source_commit="d" * 40)
    unpack, replaced = backup._unpack, False

    def replace_source_then_unpack(archive, stage, max_bytes):
        nonlocal replaced
        if not replaced:
            os.replace(replacement["archive"], original)
            replaced = True
        assert Path(archive) != original
        return unpack(archive, stage, max_bytes)

    monkeypatch.setattr(backup, "_unpack", replace_source_then_unpack)
    if operation == "restore":
        result = restore_snapshot(original, tmp_path / "recovered", expected_sha256=receipt["archive_sha256"])
        with Ledger(tmp_path / "recovered/research.sqlite") as ledger:
            assert ledger.get_manifest(job.job_id)
            assert ledger.audit_records()[:-1] == records
        assert not (tmp_path / "recovered/archive.tar").exists()
    elif operation == "upload":
        executable, config = fake_rclone(tmp_path)
        result = upload_snapshot(original, rclone_config=config, drive_folder_id="trustedFolder12345", receipt_directory=tmp_path / "receipts", rclone=str(executable))
        assert hashlib.sha256((tmp_path / "remote.tar").read_bytes()).hexdigest() == receipt["archive_sha256"]
    else:
        result = verify_snapshot(original, expected_sha256=receipt["archive_sha256"])
    assert replaced and result["snapshot_id"] == receipt["snapshot_id"]
    assert result["archive_sha256"] == receipt["archive_sha256"]
    assert hashlib.sha256(original.read_bytes()).hexdigest() == replacement["archive_sha256"]


@pytest.mark.parametrize("failure,corrupt", [(True, False), (False, True)])
def test_drive_auth_or_readback_failure_never_claims_backup(snapshot, tmp_path, failure, corrupt):
    receipt, *_ = snapshot
    executable, config = fake_rclone(tmp_path, failure=failure, corrupt=corrupt)
    with pytest.raises(BackupError) as error:
        upload_snapshot(receipt["archive"], rclone_config=config, drive_folder_id="trustedFolder12345", receipt_directory=tmp_path / "receipts", rclone=str(executable))
    assert "sensitive-token-value" not in str(error.value)
    assert not list((tmp_path / "receipts").glob("*.json"))


def test_backup_identity_rejects_multi_remote_credentials(snapshot, tmp_path):
    receipt, *_ = snapshot
    executable, config = fake_rclone(tmp_path)
    with config.open("a") as stream:
        stream.write('[other]\ntype = s3\n')
    with pytest.raises(BackupError, match="only"):
        upload_snapshot(receipt["archive"], rclone_config=config, drive_folder_id="trustedFolder12345", receipt_directory=tmp_path / "receipts", rclone=str(executable))


def test_already_verified_unchanged_archive_skips_all_transfers(snapshot, tmp_path):
    receipt, *_ = snapshot
    executable, config = fake_rclone(tmp_path)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    target = outbox / "probe-20260101.tar"
    target.write_bytes(Path(receipt["archive"]).read_bytes())
    arguments = dict(outbox=outbox, rclone_config=config, drive_folder_id="trustedFolder12345", receipt_directory=tmp_path / "receipts", rclone=str(executable))
    first = upload_pending(**arguments)
    assert len(first) == 1 and first[0]["restore_verified"]
    executable.unlink()  # No subprocess may be needed for an unchanged receipt.
    second = upload_pending(**arguments)
    assert len(second) == 1 and second[0]["transfer_skipped"]
    target.write_bytes(target.read_bytes() + b"changed")
    with pytest.raises((BackupError, OSError)):
        upload_pending(**arguments)


def test_local_retention_deletes_only_older_exact_verified_archives(tmp_path):
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    outbox.mkdir()
    receipts.mkdir()
    for index in range(5):
        data = str(index).encode()
        (outbox / f"probe-{index}.tar").write_bytes(data)
        if index < 4:
            (receipts / f"{index}.json").write_text(json.dumps({"verified": True, "readback_verified": True,
                "restore_verified": True, "drive_folder_id": "trustedFolder12345", "archive_sha256": hashlib.sha256(data).hexdigest()}))
    (outbox / "unrelated.txt").write_text("retain")
    deleted = prune_verified_outbox(outbox=outbox, receipt_directory=receipts, drive_folder_id="trustedFolder12345")
    assert set(deleted) == {"probe-0.tar", "probe-1.tar"}
    assert {path.name for path in outbox.iterdir()} == {"probe-2.tar", "probe-3.tar", "probe-4.tar", "unrelated.txt"}


def test_failed_upload_does_not_accumulate_daily_snapshots(tmp_path):
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    outbox.mkdir()
    receipts.mkdir()
    (outbox / "probe-pending.tar").write_bytes(b"pending archive")
    with Ledger(tmp_path / "research.sqlite") as ledger:
        report = publish_snapshot(ledger, outbox=outbox, input_store=tmp_path / "inputs", source_commit="a" * 40,
                                  receipt_directory=receipts, drive_folder_id="trustedFolder12345")
    assert report == {"snapshot_created": False, "retry_pending_archives": 1}
    assert len(list(outbox.iterdir())) == 1


def test_provider_mapping_snapshot_is_restorable_but_not_cross_database_atomic(tmp_path):
    provider = tmp_path / "provider.sqlite"
    with closing(sqlite3.connect(provider)) as connection:
        connection.execute("CREATE TABLE runpod_intents(worker_id TEXT, provider_id TEXT, configuration TEXT, launch_configuration TEXT)")
        connection.execute("INSERT INTO runpod_intents VALUES(?,?,?,?)", ("logical", "physical", '{}', '{"environment":{"PUBLIC_KEY":"public-only"}}'))
        connection.commit()
    with Ledger(tmp_path / "research.sqlite") as ledger:
        report = create_snapshot(ledger, tmp_path / "snapshot.tar", input_store=tmp_path / "inputs", source_commit="a" * 40, provider_database=provider)
    with tarfile.open(report["archive"]) as archive:
        metadata = json.load(archive.extractfile("snapshot.json"))
    assert metadata["provider_state"]["cross_database_atomic"] is False
    assert metadata["provider_state"]["reconciliation_required"] is True
    restore_snapshot(report["archive"], tmp_path / "restore", expected_sha256=report["archive_sha256"])
    with closing(sqlite3.connect(tmp_path / "restore/provider.sqlite")) as connection:
        assert connection.execute("SELECT worker_id, provider_id FROM runpod_intents").fetchall() == [("logical", "physical")]


def test_provider_backup_rejects_unreviewed_secret_tables(tmp_path):
    provider = tmp_path / "provider.sqlite"
    with closing(sqlite3.connect(provider)) as connection:
        connection.execute("CREATE TABLE credentials(api_key TEXT)")
    with Ledger(tmp_path / "research.sqlite") as ledger:
        with pytest.raises(BackupError, match="unsupported table"):
            create_snapshot(ledger, tmp_path / "snapshot.tar", input_store=tmp_path / "inputs", source_commit="a" * 40, provider_database=provider)
