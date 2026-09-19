"""Local storage, backup, and database-level append-only protections."""

from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

from probe_core import Ledger, LedgerError
from probe_core.ledger import _require_local_path


def test_backup_captures_live_wal_events(tmp_path):
    database = tmp_path / "source.sqlite"
    with Ledger(database) as ledger:
        expected = ledger.record_event("tool_call", {"tool": "lab_status", "arguments_hash": "abc"})
        destination = ledger.backup(tmp_path / "backup" / "snapshot.sqlite")
        # The source remains open with its WAL while the independent backup opens.
        with Ledger(destination) as restored:
            assert restored.audit_records() == [expected]
            with restored.read_connection() as reader:
                assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(ValueError):
            ledger.backup(destination)
        with pytest.raises(ValueError):
            ledger.backup(database)


def test_database_and_audit_paths_cannot_overlap(tmp_path):
    target = tmp_path / "state.sqlite"
    with pytest.raises(ValueError):
        Ledger(target, audit_path=target)


def test_ledger_rejects_symlink_and_in_memory_paths(tmp_path):
    target = tmp_path / "other.sqlite"
    target.touch()
    link = tmp_path / "state.sqlite"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        Ledger(link)
    with pytest.raises(ValueError):
        Ledger(":memory:")


@pytest.mark.parametrize("filesystem", ["nfs", "nfs4", "cifs", "smb3", "9p", "fuse.sshfs", "fuse.rclone"])
def test_known_network_mounts_are_rejected(tmp_path, monkeypatch, filesystem):
    original = Path.read_text

    def mountinfo(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return f"10 1 0:20 / / rw - ext4 /dev/local rw\n11 10 0:21 / {tmp_path} rw - {filesystem} server rw\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mountinfo)
    with pytest.raises(ValueError, match="local storage"):
        _require_local_path(tmp_path / "research.sqlite")


def test_existing_non_probe_database_is_not_adopted(tmp_path):
    path = tmp_path / "unrelated.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(LedgerError, match="non-Probe"):
        Ledger(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("unrelated",)]


def test_database_triggers_block_audit_mutation(tmp_path):
    path = tmp_path / "research.sqlite"
    with Ledger(path) as ledger:
        original = ledger.record_event("policy_evaluation", {"decision": "deny"})
        # Even a separate read-write SQL connection encounters append-only triggers.
        with closing(sqlite3.connect(path)) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE audit_events SET record='{}'")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM audit_events")
        assert ledger.audit_records() == [original]


def test_closed_ledger_rejects_reads_and_writes(tmp_path):
    ledger = Ledger(tmp_path / "research.sqlite")
    ledger.close()
    ledger.close()
    with pytest.raises(LedgerError, match="closed"):
        ledger.list_jobs()
    with pytest.raises(LedgerError, match="closed"):
        ledger.record_event("tool_call", {"tool": "lab_status"})
