"""Consistent, credential-free research snapshots and verified Drive transport.

Snapshots never include arbitrary directories, service configuration, credentials,
model caches, or evaluator corpora. Restoration is offline into a new directory;
it never starts services, consumes an approval, or changes provider state.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import uuid

from .artifact_store import ArtifactStore
from .audit import GENESIS_HASH, _verify_records, canonical_json, validate_audit_payload
from .ledger import Ledger
from .schemas import RunManifest

MAX_SNAPSHOT_BYTES = 100 * 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class BackupError(ValueError):
    pass


def _relative(value: str) -> Path:
    if (not isinstance(value, str) or not value or len(value) > 4096 or
            "\\" in value or "\x00" in value or Path(value).is_absolute() or
            any(part in {"", ".", ".."} for part in value.split("/"))):
        raise BackupError("unsafe snapshot member")
    return Path(value)


def _new_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise BackupError("destination must be a new private directory")
    with Ledger._directory(path.parent):
        path.mkdir(mode=0o700)


def _read_regular(path: Path, limit: int) -> bytes:
    with Ledger._directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise BackupError("snapshot input is not a bounded unshared regular file")
        data = stream.read(limit + 1)
        if len(data) != info.st_size:
            raise BackupError("snapshot input changed while reading")
        return data


def _copy(source: Path, destination: Path, limit: int, expected: str | None = None) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest, total = hashlib.sha256(), 0
    with Ledger._directory(source.parent) as parent:
        fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as incoming, destination.open("xb") as outgoing:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise BackupError("snapshot input must be bounded and unshared")
        while block := incoming.read(1024**2):
            total += len(block)
            if total > limit:
                raise BackupError("snapshot exceeds byte limit")
            digest.update(block)
            outgoing.write(block)
        after = os.fstat(incoming.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise BackupError("snapshot input changed while copying")
        outgoing.flush()
        os.fchmod(outgoing.fileno(), 0o600)
        os.fsync(outgoing.fileno())
    checksum = digest.hexdigest()
    if expected is not None and checksum != expected:
        raise BackupError("snapshot input checksum mismatch")
    return {"bytes": total, "sha256": checksum}


def _inventory(path: Path, limit: int) -> dict:
    with Ledger._directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise BackupError("snapshot file exceeds its limit or is unsafe")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise BackupError("snapshot file changed while hashing")
        return {"bytes": info.st_size, "sha256": digest}


def _database(path: Path):
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.row_factory = sqlite3.Row
    return closing(connection)


def _audit(connection) -> tuple[list[dict], dict]:
    records = [json.loads(row[0]) for row in connection.execute("SELECT record FROM audit_events ORDER BY sequence")]
    _verify_records(records)
    return records, {"sequence": len(records), "hash": records[-1]["hash"] if records else GENESIS_HASH}


def _write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(canonical_json(value))
        stream.flush()
        os.fchmod(stream.fileno(), 0o600)
        os.fsync(stream.fileno())


def _verify_provider_state(path: Path) -> None:
    with _database(path) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("provider SQLite integrity failed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != {"runpod_intents"}:
            raise BackupError("provider snapshot contains an unsupported table; review its credential policy")
        for row in connection.execute("SELECT * FROM runpod_intents"):
            values = dict(row)
            for field in ("configuration", "launch_configuration"):
                if field in values:
                    values[field] = json.loads(values[field])
            validate_audit_payload(values)


def create_snapshot(ledger: Ledger, destination: str | Path, *, input_store: str | Path,
                    source_commit: str, provider_database: str | Path | None = None,
                    max_bytes: int = MAX_SNAPSHOT_BYTES) -> dict:
    """Back up live WAL state first, then exactly its immutable referenced files."""
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit) is None:
        raise BackupError("reviewed full source commit is required")
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError("archive destination must be new")
    with Ledger._directory(destination.parent):
        pass
    store = ArtifactStore(input_store)
    with tempfile.TemporaryDirectory(prefix=".probe-snapshot-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        (stage / "state").mkdir(mode=0o700)
        database = ledger.backup(stage / "state/research.sqlite")
        provider_state = None
        if provider_database is not None:
            source = Path(provider_database).absolute()
            with Ledger._directory(source.parent) as parent:
                fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                info = os.fstat(fd)
                os.close(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
                    raise BackupError("unsafe provider database")
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as incoming:
                with closing(sqlite3.connect(stage / "state/provider.sqlite")) as outgoing:
                    incoming.backup(outgoing)
            _verify_provider_state(stage / "state/provider.sqlite")
            provider_state = {"path": "state/provider.sqlite", "snapshot_at": datetime.now(timezone.utc).isoformat(),
                              "cross_database_atomic": False, "reconciliation_required": True}
        files, bundles, total = [], [], 0

        def retain(source, name, expected=None):
            nonlocal total
            target = stage / _relative(name)
            item = _copy(Path(source), target, max_bytes - total, expected)
            total += item["bytes"]
            files.append({"path": name, **item})

        with _database(database) as connection:
            records, tip = _audit(connection)
            for row in connection.execute("SELECT m.*, j.attempt_id FROM manifests m JOIN jobs j USING(job_id) ORDER BY m.job_id"):
                manifest = RunManifest.model_validate_json(row["document"])
                if manifest.run.experiment_stage.value in {"confirmatory", "replication"}:
                    raise BackupError("private evaluator results require a separately supported backup policy")
                original = Path(row["artifact_root"])
                archive_root = f"artifacts/{row['job_id']}/{row['attempt_id']}"
                _relative(archive_root)
                for artifact in manifest.artifacts:
                    retain(original / artifact.path, f"{archive_root}/{artifact.path}", artifact.sha256)
                retain(original / ".probe-bundle.json", f"{archive_root}/.probe-bundle.json")
                bundles.append({"job_id": row["job_id"], "attempt_id": row["attempt_id"],
                                "original_root": str(original), "archive_root": archive_root})
        # Published input-store directories are immutable. Concurrent new inputs
        # may be included; no partially registered .stage directory is included.
        for directory in sorted(store.root.iterdir()):
            if directory.name.startswith(".stage-"):
                continue
            if not _HASH.fullmatch(directory.name):
                raise BackupError("unexpected input-store entry")
            record = store.describe(directory.name)
            retain(store.root / record["path"], "inputs/" + record["path"], directory.name)
            retain(directory / "record.json", f"inputs/{directory.name}/record.json")
        audit_path = stage / "state/research.audit.jsonl"
        with audit_path.open("x", encoding="utf-8") as stream:
            for record in records:
                stream.write(canonical_json(record) + "\n")
        for path in [database, audit_path, *([stage / "state/provider.sqlite"] if provider_state else [])]:
            item = _inventory(path, max_bytes - total)
            files.append({"path": str(path.relative_to(stage)), **item})
            total += item["bytes"]
        body = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "source_commit": source_commit, "audit_tip": tip, "files": files,
                "bundles": bundles, "total_bytes": total,
                "provider_state": provider_state,
                "excluded": ["credentials", "service_configuration", "model_caches", "evaluator_data"]}
        body["snapshot_id"] = hashlib.sha256(canonical_json(body).encode()).hexdigest()
        _write_json(stage / "snapshot.json", body)
        partial = stage / "archive.tar"
        with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as archive:
            for name in ["snapshot.json", *[entry["path"] for entry in files]]:
                info = archive.gettarinfo(str(stage / name), arcname=name)
                info.uid, info.gid, info.uname, info.gname, info.mode = 0, 0, "", "", 0o600
                with (stage / name).open("rb") as stream:
                    archive.addfile(info, stream)
        partial.chmod(0o600)
        with partial.open("rb") as stream:
            os.fsync(stream.fileno())
            archive_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        # Refuse concurrent replacement of an existing output.
        os.link(partial, destination)
        partial.unlink()
        with Ledger._directory(destination.parent) as parent:
            os.fsync(parent)
    return {"snapshot_id": body["snapshot_id"], "archive": str(destination),
            "archive_sha256": archive_sha256, "archive_bytes": destination.stat().st_size,
            "audit_tip": tip, "files": len(files)}


def _unpack(archive_path: Path, stage: Path, max_bytes: int) -> dict:
    seen, total, metadata = set(), 0, None
    with tarfile.open(archive_path, "r|*") as archive:
        for member in archive:
            _relative(member.name)
            if not member.isfile() or member.name in seen:
                raise BackupError("archive contains a duplicate or nonregular member")
            seen.add(member.name)
            if metadata is None:
                if member.name != "snapshot.json" or member.size > MAX_MANIFEST_BYTES:
                    raise BackupError("snapshot manifest must be the first bounded member")
                with archive.extractfile(member) as stream:
                    metadata = json.load(stream)
                if metadata.get("schema_version") != 1:
                    raise BackupError("unsupported snapshot schema")
                body = {key: value for key, value in metadata.items() if key != "snapshot_id"}
                if hashlib.sha256(canonical_json(body).encode()).hexdigest() != metadata.get("snapshot_id"):
                    raise BackupError("snapshot manifest hash mismatch")
                expected = {}
                for entry in metadata["files"]:
                    _relative(entry["path"])
                    if entry["path"] == "snapshot.json" or entry["path"] in expected:
                        raise BackupError("duplicate snapshot inventory")
                    if type(entry["bytes"]) is not int or entry["bytes"] < 0 or not _HASH.fullmatch(entry["sha256"]):
                        raise BackupError("invalid snapshot inventory")
                    expected[entry["path"]] = entry
                continue
            entry = expected.get(member.name)
            if entry is None or member.size != entry["bytes"] or total + member.size > max_bytes:
                raise BackupError("unexpected or oversized archive member")
            target = stage / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256()
            with archive.extractfile(member) as incoming, target.open("xb") as outgoing:
                while block := incoming.read(1024**2):
                    digest.update(block)
                    total += len(block)
                    if total > max_bytes:
                        raise BackupError("snapshot exceeds byte limit")
                    outgoing.write(block)
                outgoing.flush()
                os.fchmod(outgoing.fileno(), 0o600)
                os.fsync(outgoing.fileno())
            if digest.hexdigest() != entry["sha256"]:
                raise BackupError("archive member checksum mismatch")
    if metadata is None or seen != {"snapshot.json", *expected} or total != metadata["total_bytes"]:
        raise BackupError("incomplete snapshot")
    return metadata


def _verify_stage(stage: Path, metadata: dict) -> None:
    if metadata.get("provider_state") is not None:
        if metadata["provider_state"]["path"] != "state/provider.sqlite":
            raise BackupError("unexpected provider snapshot path")
        _verify_provider_state(stage / "state/provider.sqlite")
    with _database(stage / "state/research.sqlite") as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("restored SQLite integrity failed")
        records, tip = _audit(connection)
        if tip != metadata["audit_tip"]:
            raise BackupError("audit tip mismatch")
        exported = [json.loads(line) for line in (stage / "state/research.audit.jsonl").read_text().splitlines()]
        if exported != records:
            raise BackupError("audit projection differs from the database")
        bundles = {entry["job_id"]: entry for entry in metadata["bundles"]}
        rows = connection.execute("SELECT m.*, j.attempt_id FROM manifests m JOIN jobs j USING(job_id)").fetchall()
        if {row["job_id"] for row in rows} != set(bundles) or len(rows) != len(metadata["bundles"]):
            raise BackupError("manifest bundle inventory mismatch")
        for row in rows:
            manifest = RunManifest.model_validate_json(row["document"])
            digest = "sha256:" + hashlib.sha256(canonical_json(manifest.model_dump(mode="json")).encode()).hexdigest()
            entry = bundles[row["job_id"]]
            canonical_root = f"artifacts/{row['job_id']}/{row['attempt_id']}"
            if row["digest"] != digest or entry["archive_root"] != canonical_root or entry["original_root"] != row["artifact_root"]:
                raise BackupError("accepted manifest provenance mismatch")
            bundle = stage / _relative(canonical_root)
            marker = json.loads(_read_regular(bundle / ".probe-bundle.json", 4096))
            if marker != {"job_id": row["job_id"], "attempt_id": row["attempt_id"], "manifest_hash": digest}:
                raise BackupError("accepted bundle seal mismatch")
            Ledger._copy_artifacts(manifest, bundle, None, manifest.cost.bytes_persisted)
    inputs = stage / "inputs"
    inputs.mkdir(mode=0o700, exist_ok=True)
    inputs.chmod(0o700)
    store = ArtifactStore(inputs)
    for directory in inputs.iterdir():
        record = store.describe(directory.name)
        item = _inventory(store.root / record["path"], MAX_SNAPSHOT_BYTES)
        if item != {"bytes": record["bytes"], "sha256": directory.name} or record["sha256"] != "sha256:" + directory.name:
            raise BackupError("retained input identity mismatch")


def verify_snapshot(archive: str | Path, *, expected_sha256: str | None = None,
                    max_bytes: int = MAX_SNAPSHOT_BYTES, scratch_directory: str | Path | None = None) -> dict:
    archive = Path(archive).absolute()
    with tempfile.TemporaryDirectory(prefix=".probe-verify-", dir=scratch_directory) as temporary:
        # Pin once into a newly created private directory. Every parser consumes
        # these checked bytes, never a caller-controlled path reopened later.
        pinned = Path(temporary) / "archive.tar"
        digest = _copy(archive, pinned, max_bytes + MAX_MANIFEST_BYTES + 1024**3, expected_sha256)["sha256"]
        stage = Path(temporary) / "extracted"
        stage.mkdir(mode=0o700)
        metadata = _unpack(pinned, stage, max_bytes)
        _verify_stage(stage, metadata)
    return {"snapshot_id": metadata["snapshot_id"], "archive_sha256": digest,
            "audit_tip": metadata["audit_tip"], "files": len(metadata["files"]), "verified": True}


def restore_snapshot(archive: str | Path, destination: str | Path, *, expected_sha256: str,
                     max_bytes: int = MAX_SNAPSHOT_BYTES) -> dict:
    """Materialize a verified isolated restore. Never overwrite an installation."""
    archive, destination = Path(archive).absolute(), Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError("restore destination must be new")
    with Ledger._directory(destination.parent):
        pass
    with tempfile.TemporaryDirectory(prefix=".probe-restore-", dir=destination.parent) as temporary:
        pinned = Path(temporary) / "archive.tar"
        digest = _copy(archive, pinned, max_bytes + MAX_MANIFEST_BYTES + 1024**3, expected_sha256)["sha256"]
        stage = Path(temporary) / "restored"
        stage.mkdir(mode=0o700)
        metadata = _unpack(pinned, stage, max_bytes)
        _verify_stage(stage, metadata)
        verified = {"snapshot_id": metadata["snapshot_id"], "archive_sha256": digest,
                    "audit_tip": metadata["audit_tip"], "files": len(metadata["files"]), "verified": True}
        database = stage / "research.sqlite"
        os.rename(stage / "state/research.sqlite", database)
        os.rename(stage / "state/research.audit.jsonl", stage / "research.audit.jsonl")
        if metadata.get("provider_state") is not None:
            os.rename(stage / "state/provider.sqlite", stage / "provider.sqlite")
        (stage / "state").rmdir()
        if (stage / "artifacts").exists():
            os.rename(stage / "artifacts", stage / "research.artifacts")
            for directory, _, names in os.walk(stage / "research.artifacts", topdown=False):
                for name in names:
                    (Path(directory) / name).chmod(0o400)
                Path(directory).chmod(0o500)
        for directory, _, names in os.walk(stage / "inputs"):
            for name in names:
                (Path(directory) / name).chmod(0o400)
        # This is a new offline database, not a mutation of accepted scientific
        # manifests. Preserve their documents/digests; relocate only storage paths.
        with closing(sqlite3.connect(database)) as connection:
            trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='manifest_no_update'").fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TRIGGER manifest_no_update")
            for entry in metadata["bundles"]:
                root = destination / "research.artifacts" / entry["job_id"] / entry["attempt_id"]
                connection.execute("UPDATE manifests SET artifact_root=? WHERE job_id=?", (str(root), entry["job_id"]))
            connection.execute(trigger)
            connection.commit()
        _write_json(stage / "restore-receipt.json", {**verified, "restored_at": datetime.now(timezone.utc).isoformat(),
                    "services_started": False, "provider_reconciliation_required": True})
        (stage / "RESTORE-OFFLINE.txt").write_text("Offline restore only. Reconcile provider state and unresolved attempts before service startup. Credentials and evaluator data are absent.\n")
        os.rename(stage, destination)
        with Ledger._directory(destination.parent) as parent:
            os.fsync(parent)
    with Ledger(destination / "research.sqlite") as ledger:
        ledger.record_event("tool_call", {"tool": "backup_restore", "snapshot_id": metadata["snapshot_id"],
                             "source_audit_hash": metadata["audit_tip"]["hash"], "offline": True})
    return {**verified, "destination": str(destination), "restored": True, "services_started": False}


def upload_snapshot(archive: str | Path, *, rclone_config: str | Path, drive_folder_id: str,
                    receipt_directory: str | Path, rclone: str = "/usr/bin/rclone") -> dict:
    """Upload only to a pinned Drive root, then download and verify the full copy."""
    archive, config, receipts = Path(archive).absolute(), Path(rclone_config).absolute(), Path(receipt_directory).absolute()
    info = config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise BackupError("rclone credential file must be private and backup-service-owned")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,256}", drive_folder_id):
        raise BackupError("a pinned Drive folder ID is required")
    import configparser
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.read(config)
    if parsed.sections() != ["gdrive"] or parsed["gdrive"].get("type") != "drive":
        raise BackupError("backup credentials must contain only the gdrive Drive remote")
    receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = [rclone, "--config", str(config), "--drive-root-folder-id", drive_folder_id,
            "--ask-password=false", "--retries", "1", "--low-level-retries", "1"]

    def run(arguments, *, output=None):
        result = subprocess.run([*base, *arguments], stdin=subprocess.DEVNULL,
                                stdout=output if output is not None else subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=3600, check=False,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(config.parent)})
        if result.returncode:
            # Provider errors can contain OAuth context. Never persist/display them.
            if b"invalid_grant" in result.stderr:
                raise BackupError("Drive authorization expired or was revoked; reconnect the backup account")
            raise BackupError("Drive backup transfer failed; credential/provider details withheld")

    with tempfile.TemporaryDirectory(prefix=".readback-", dir=receipts) as temporary:
        pinned = Path(temporary) / "source.tar"
        identity = _copy(archive, pinned, MAX_SNAPSHOT_BYTES + MAX_MANIFEST_BYTES + 1024**3)
        checked = verify_snapshot(pinned, expected_sha256=identity["sha256"], scratch_directory=temporary)
        remote = "gdrive:probe-" + checked["snapshot_id"] + ".tar"
        run(["copyto", str(pinned), remote, "--immutable", "--no-traverse"])
        downloaded = Path(temporary) / "download.tar"
        with downloaded.open("xb") as stream:
            run(["cat", remote], output=stream)
        remote_verified = verify_snapshot(downloaded, expected_sha256=checked["archive_sha256"])
        restored = restore_snapshot(downloaded, Path(temporary) / "restored", expected_sha256=checked["archive_sha256"])
    receipt = {**remote_verified, "drive_folder_id": drive_folder_id, "remote": remote,
               "readback_verified": True, "restore_verified": restored["restored"],
               "uploaded_at": datetime.now(timezone.utc).isoformat()}
    receipt_path = receipts / (checked["snapshot_id"] + ".json")
    if receipt_path.exists():
        previous = json.loads(_read_regular(receipt_path, 1024**2))
        if any(previous.get(key) != receipt[key] for key in ("snapshot_id", "archive_sha256", "drive_folder_id", "remote", "readback_verified", "restore_verified")):
            raise BackupError("existing remote receipt has different identity")
        return previous
    _write_json(receipt_path, receipt)
    receipt_path.chmod(0o640)
    return receipt


def publish_snapshot(ledger: Ledger, *, outbox: str | Path, input_store: str | Path, source_commit: str,
                     provider_database: str | Path | None = None,
                     receipt_directory: str | Path | None = None, drive_folder_id: str | None = None) -> dict:
    outbox = Path(outbox).absolute()
    if receipt_directory is not None:
        confirmed = set()
        for path in Path(receipt_directory).glob("*.json"):
            receipt = json.loads(_read_regular(path, 1024**2))
            if all(receipt.get(key) is True for key in ("verified", "readback_verified", "restore_verified")) and receipt.get("drive_folder_id") == drive_folder_id:
                confirmed.add(receipt["archive_sha256"])
        pending = []
        for archive in outbox.glob("probe-*.tar"):
            item = _inventory(archive, MAX_SNAPSHOT_BYTES + MAX_MANIFEST_BYTES + 1024**3)
            if item["sha256"] not in confirmed:
                pending.append(archive)
        if pending:
            return {"snapshot_created": False, "retry_pending_archives": len(pending)}
    name = "probe-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex + ".tar"
    receipt = create_snapshot(ledger, outbox / name, input_store=input_store, source_commit=source_commit, provider_database=provider_database)
    # Service's primary group is the read-only handoff group; only the producer
    # may write the outbox. Uploader never receives access to live state.
    (outbox / name).chmod(0o640)
    return receipt


def upload_pending(*, outbox: str | Path, rclone_config: str | Path, drive_folder_id: str,
                   receipt_directory: str | Path, rclone: str = "/usr/bin/rclone") -> list[dict]:
    results, receipts = [], []
    for path in Path(receipt_directory).glob("*.json"):
        receipt = json.loads(_read_regular(path, 1024**2))
        if (receipt.get("verified") is True and receipt.get("readback_verified") is True and
                receipt.get("restore_verified") is True and receipt.get("drive_folder_id") == drive_folder_id):
            receipts.append(receipt)
    for archive in sorted(Path(outbox).glob("probe-*.tar")):
        digest = _inventory(archive, MAX_SNAPSHOT_BYTES + MAX_MANIFEST_BYTES + 1024**3)["sha256"]
        previous = next((receipt for receipt in receipts if receipt.get("archive_sha256") == digest), None)
        if previous is not None:
            results.append({**previous, "transfer_skipped": True})
            continue
        results.append(upload_snapshot(archive, rclone_config=rclone_config, drive_folder_id=drive_folder_id,
                                       receipt_directory=receipt_directory, rclone=rclone))
    return results


def prune_verified_outbox(*, outbox: str | Path, receipt_directory: str | Path,
                          drive_folder_id: str, keep: int = 2) -> list[str]:
    """Producer-only local retention; never delete unverified or unrelated files."""
    if type(keep) is not int or keep < 1:
        raise BackupError("retain at least one verified archive")
    verified = set()
    for path in Path(receipt_directory).glob("*.json"):
        receipt = json.loads(_read_regular(path, 1024**2))
        if all(receipt.get(key) is True for key in ("verified", "readback_verified", "restore_verified")) and receipt.get("drive_folder_id") == drive_folder_id:
            verified.add(receipt["archive_sha256"])
    candidates = []
    for path in Path(outbox).glob("probe-*.tar"):
        item = _inventory(path, MAX_SNAPSHOT_BYTES + MAX_MANIFEST_BYTES + 1024**3)
        if item["sha256"] in verified:
            candidates.append(path)
    removed = []
    for path in sorted(candidates, key=lambda item: item.name, reverse=True)[keep:]:
        path.unlink()
        removed.append(path.name)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--ledger", type=Path, required=True)
    create.add_argument("--inputs", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--provider-state", type=Path)
    publish = sub.add_parser("publish")
    publish.add_argument("--ledger", type=Path, required=True)
    publish.add_argument("--inputs", type=Path, required=True)
    publish.add_argument("--outbox", type=Path, required=True)
    publish.add_argument("--source-commit", required=True)
    publish.add_argument("--provider-state", type=Path)
    publish.add_argument("--receipts", type=Path)
    publish.add_argument("--drive-folder-id")
    verify = sub.add_parser("verify")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--sha256")
    restore = sub.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)
    restore.add_argument("--sha256", required=True)
    upload = sub.add_parser("upload")
    upload.add_argument("archive", type=Path)
    upload.add_argument("--config", type=Path, required=True)
    upload.add_argument("--drive-folder-id", required=True)
    upload.add_argument("--receipts", type=Path, required=True)
    pending = sub.add_parser("upload-pending")
    pending.add_argument("--outbox", type=Path, required=True)
    pending.add_argument("--config", type=Path, required=True)
    pending.add_argument("--drive-folder-id", required=True)
    pending.add_argument("--receipts", type=Path, required=True)
    prune = sub.add_parser("prune-verified")
    prune.add_argument("--outbox", type=Path, required=True)
    prune.add_argument("--receipts", type=Path, required=True)
    prune.add_argument("--drive-folder-id", required=True)
    prune.add_argument("--keep", type=int, default=2)
    args = parser.parse_args()
    if args.command == "create":
        with Ledger(args.ledger) as ledger:
            result = create_snapshot(ledger, args.output, input_store=args.inputs, source_commit=args.source_commit, provider_database=args.provider_state)
    elif args.command == "publish":
        with Ledger(args.ledger) as ledger:
            result = publish_snapshot(ledger, outbox=args.outbox, input_store=args.inputs, source_commit=args.source_commit, provider_database=args.provider_state, receipt_directory=args.receipts, drive_folder_id=args.drive_folder_id)
    elif args.command == "upload-pending":
        result = upload_pending(outbox=args.outbox, rclone_config=args.config, drive_folder_id=args.drive_folder_id, receipt_directory=args.receipts)
    elif args.command == "prune-verified":
        result = prune_verified_outbox(outbox=args.outbox, receipt_directory=args.receipts, drive_folder_id=args.drive_folder_id, keep=args.keep)
    elif args.command == "verify":
        result = verify_snapshot(args.archive, expected_sha256=args.sha256)
    elif args.command == "restore":
        result = restore_snapshot(args.archive, args.destination, expected_sha256=args.sha256)
    else:
        result = upload_snapshot(args.archive, rclone_config=args.config, drive_folder_id=args.drive_folder_id, receipt_directory=args.receipts)
    print(canonical_json(result))


if __name__ == "__main__":
    main()
