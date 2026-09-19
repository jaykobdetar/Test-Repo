from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
import os

import pytest

from probe_core.audit import (
    GENESIS_HASH,
    AuditIntegrityError,
    AuditLog,
    AuditPathError,
    SecretDetectedError,
    canonical_json,
    make_record,
    validate_audit_payload,
)


NOW = datetime(2026, 9, 19, 12, 30, 15, 123456, tzinfo=timezone.utc)


def chain(count):
    records = []
    previous = GENESIS_HASH
    for sequence in range(1, count + 1):
        record = make_record(sequence, previous, "job.created", {"job_id": f"job-{sequence}"}, NOW)
        records.append(record)
        previous = record["hash"]
    return records


def write_records(path, records):
    path.write_text("".join(canonical_json(record) + "\n" for record in records), encoding="utf-8")
    path.chmod(0o600)


def process_append(path, worker, count):
    log = AuditLog(path)
    for number in range(count):
        log.append("worker.event", {"worker": worker, "number": number}, NOW)


def test_canonical_json_is_deterministic_utf8():
    assert canonical_json({"z": [None, True, 1.5], "a": "café"}) == '{"a":"café","z":[null,true,1.5]}'
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), {1: "value"}, (1, 2), {"x": object()}, "\ud800"])
def test_canonical_json_rejects_non_json(value):
    with pytest.raises((ValueError, TypeError, UnicodeError)):
        canonical_json(value)


def test_canonical_json_rejects_cycles_but_allows_shared_children():
    circular = []
    circular.append(circular)
    with pytest.raises(ValueError, match="Cyclic"):
        canonical_json(circular)
    shared = {"a": 1}
    assert canonical_json([shared, shared]) == '[{"a":1},{"a":1}]'


@pytest.mark.parametrize("key", [
    "token", "TOKEN", "tokens", "access_token", "refreshToken", "rawApprovalToken",
    "approval_token_hash", "api_key", "APIKey", "runpodApiKey", "X-API-Key",
    "password", "dbPassword", "passwd", "clientSecret", "credentials", "credential",
    "private_key", "privateKey", "authorization", "AuthorizationHeader", "authorisation",
    "ＦＵＬＬ＿ＡＰＩ＿ＫＥＹ", "accesstoken", "tokenValue",
])
def test_sensitive_keys_rejected_recursively_without_echo(key):
    with pytest.raises(SecretDetectedError) as raised:
        validate_audit_payload({"safe": [{"nested": {key: "do-not-echo-this"}}]})
    assert "do-not-echo-this" not in str(raised.value)


def test_rejection_happens_before_any_file_creation(tmp_path):
    path = tmp_path / "not-created" / "audit.jsonl"
    with pytest.raises(SecretDetectedError):
        AuditLog(path).append("approval.issued", {"approvalToken": "sensitive"})
    assert not path.parent.exists()


def test_model_token_metadata_remains_allowed():
    validate_audit_payload({
        "token_generation": {"max_new_tokens": 42, "minNewTokens": 1},
        "maxNewTokens": 128,
        "tokenizer": {"name": "example", "revision": "abc", "eos_token_id": 3},
        "tokenizer_config": {"padding_side": "left"},
        "input_tokens": 10,
        "token_ids": [1, 2, 3],
    })


def test_allowed_model_metadata_still_rejects_nested_secrets():
    with pytest.raises(SecretDetectedError):
        validate_audit_payload({"token_generation": {"apiKey": "hidden"}})


def test_record_hash_utc_timestamp_and_detached_payload():
    payload = {"nested": ["café"]}
    record = make_record(1, GENESIS_HASH, "job.created", payload, NOW.astimezone(timezone(timedelta(hours=-4))))
    assert record["timestamp"] == "2026-09-19T12:30:15.123456Z"
    unhashed = {key: value for key, value in record.items() if key != "hash"}
    assert record["hash"] == hashlib.sha256(canonical_json(unhashed).encode("utf-8")).hexdigest()
    payload["nested"].append("later")
    assert record["payload"] == {"nested": ["café"]}


@pytest.mark.parametrize("arguments", [
    (0, GENESIS_HASH, "job.created", {}, NOW),
    (True, GENESIS_HASH, "job.created", {}, NOW),
    (1, "invalid", "job.created", {}, NOW),
    (1, GENESIS_HASH, "raw event text\n", {}, NOW),
    (1, GENESIS_HASH, "job.created", [], NOW),
    (1, GENESIS_HASH, "job.created", {}, NOW.replace(tzinfo=None)),
])
def test_make_record_rejects_invalid_inputs(arguments):
    with pytest.raises((ValueError, TypeError)):
        make_record(*arguments)


def test_append_and_verify_missing_and_existing_log(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    assert log.verify(expected_sequence=0, expected_hash=GENESIS_HASH) == []
    assert not path.exists()
    first = log.append("job.created", {"job_id": "one"}, NOW)
    second = AuditLog(path).append("job.succeeded", {"job_id": "one"}, NOW)
    assert first["sequence"] == 1
    assert second["previous_hash"] == first["hash"]
    assert log.verify(expected_sequence=2, expected_hash=second["hash"]) == [first, second]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["tamper", "delete_middle", "reorder", "duplicate"])
def test_tampering_deletion_reordering_prevents_further_append(tmp_path, damage):
    path = tmp_path / "audit.jsonl"
    records = chain(3)
    if damage == "tamper":
        records[1]["payload"]["job_id"] = "changed"
    elif damage == "delete_middle":
        del records[1]
    elif damage == "reorder":
        records[0], records[1] = records[1], records[0]
    else:
        records.insert(1, records[0])
    write_records(path, records)
    original = path.read_bytes()
    with pytest.raises(AuditIntegrityError):
        AuditLog(path).verify()
    with pytest.raises(AuditIntegrityError):
        AuditLog(path).append("job.created", {})
    assert path.read_bytes() == original


def test_trusted_tip_detects_complete_suffix_deletion(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = chain(3)
    write_records(path, records[:2])
    assert len(AuditLog(path).verify()) == 2  # A valid prefix has no internal evidence of truncation.
    with pytest.raises(AuditIntegrityError, match="trusted expected tip"):
        AuditLog(path).verify(expected_sequence=3, expected_hash=records[-1]["hash"])


@pytest.mark.parametrize("tail", [b'{"sequence":2', b'{}', b'\n', b'{"bad":NaN}\n', b'\xff\n'])
def test_partial_or_invalid_jsonl_tail_is_never_repaired(tmp_path, tail):
    path = tmp_path / "audit.jsonl"
    records = chain(2)
    write_records(path, records[:1])
    with path.open("ab") as output:
        output.write(tail)
    original = path.read_bytes()
    with pytest.raises(AuditIntegrityError):
        AuditLog(path).sync_records(records)
    assert path.read_bytes() == original


def test_missing_final_newline_is_rejected_even_for_complete_json(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(canonical_json(chain(1)[0]), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AuditIntegrityError, match="incomplete final"):
        AuditLog(path).verify()


def test_duplicate_json_keys_and_noncanonical_bytes_rejected(tmp_path):
    path = tmp_path / "audit.jsonl"
    record = chain(1)[0]
    text = canonical_json(record)
    path.write_text(text[:-1] + ',"sequence":1}\n', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AuditIntegrityError):
        AuditLog(path).verify()
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(AuditIntegrityError):
        AuditLog(path).verify()


def test_projection_appends_only_missing_records_and_is_idempotent(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = chain(4)
    log = AuditLog(path)
    assert log.sync_records(records[:2]) == 2
    prefix = path.read_bytes()
    assert log.sync_records(iter(records)) == 2
    assert path.read_bytes().startswith(prefix)
    complete = path.read_bytes()
    assert log.sync_records(records) == 0
    assert path.read_bytes() == complete
    assert log.verify() == records


def test_stale_projection_cannot_roll_back_existing_log(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = chain(3)
    log = AuditLog(path)
    log.sync_records(records)
    original = path.read_bytes()
    with pytest.raises(AuditIntegrityError, match="older"):
        log.sync_records(records[:2])
    assert path.read_bytes() == original


def test_conflicting_valid_authority_is_rejected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.sync_records(chain(1))
    alternate = [make_record(1, GENESIS_HASH, "different.event", {}, NOW)]
    original = path.read_bytes()
    with pytest.raises(AuditIntegrityError, match="conflicts"):
        log.sync_records(alternate)
    assert path.read_bytes() == original


def test_invalid_authority_is_rejected_before_file_creation(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = chain(1)
    records[0]["hash"] = GENESIS_HASH
    with pytest.raises(AuditIntegrityError):
        AuditLog(path).sync_records(records)
    assert not path.exists()


def test_thread_writers_across_instances_share_one_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(lambda number: AuditLog(path).append("thread.event", {"number": number}, NOW), range(40)))
    assert sorted(record["sequence"] for record in records) == list(range(1, 41))
    assert {record["payload"]["number"] for record in AuditLog(path).verify()} == set(range(40))


def test_process_writers_share_one_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=process_append, args=(str(path), worker, 8)) for worker in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("Concurrent audit writer did not finish")
        assert process.exitcode == 0
    records = AuditLog(path).verify()
    assert len(records) == 32
    assert {(record["payload"]["worker"], record["payload"]["number"]) for record in records} == {(worker, number) for worker in range(4) for number in range(8)}


def test_symlink_file_and_parent_are_rejected(tmp_path):
    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(AuditPathError):
        AuditLog(link).append("job.created", {})
    folder = tmp_path / "folder"
    folder.mkdir()
    linked_folder = tmp_path / "linked-folder"
    linked_folder.symlink_to(folder, target_is_directory=True)
    with pytest.raises(AuditPathError):
        AuditLog(linked_folder / "audit.jsonl").append("job.created", {})
    assert target.read_bytes() == b""


def test_hardlinks_and_shared_writable_files_rejected(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    hardlink = tmp_path / "alias.jsonl"
    os.link(path, hardlink)
    with pytest.raises(AuditPathError):
        AuditLog(path).append("job.created", {})
    hardlink.unlink()
    path.chmod(0o666)
    with pytest.raises(AuditPathError):
        AuditLog(path).append("job.created", {})


def test_fsync_before_return_and_on_idempotent_projection_retry(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    real_fsync = os.fsync
    calls = []

    def tracked_fsync(fd):
        calls.append(os.fstat(fd).st_ino)
        return real_fsync(fd)

    monkeypatch.setattr("probe_core.audit.os.fsync", tracked_fsync)
    log = AuditLog(path)
    record = log.append("job.created", {}, NOW)
    assert path.stat().st_ino in calls
    assert path.parent.stat().st_ino in calls
    calls.clear()
    assert log.sync_records([record]) == 0
    assert path.stat().st_ino in calls


def test_fsync_failure_propagates_and_complete_prefix_can_be_synced(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    records = chain(1)

    def fail_fsync(fd):
        raise OSError("simulated persistence failure")

    with monkeypatch.context() as scoped:
        scoped.setattr("probe_core.audit.os.fsync", fail_fsync)
        with pytest.raises(OSError, match="persistence failure"):
            log.sync_records(records)
    assert log.sync_records(records) == 0
    assert log.verify() == records


def test_short_writes_are_completed(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    real_write = os.write

    def short_write(fd, content):
        return real_write(fd, content[:7])

    monkeypatch.setattr("probe_core.audit.os.write", short_write)
    records = chain(2)
    assert AuditLog(path).sync_records(records) == 2
    assert AuditLog(path).verify() == records
