"""Durable, secret-rejecting, SHA-256 chained audit records.

The log is append-only through this API, not an authentication mechanism against
someone who can rewrite its files. Keep its directory private and retain a trusted
chain tip (or the authoritative database records) to detect complete suffix loss.
Database users should commit events first, then call ``sync_records``. An export
error means the projection needs reconciliation; it cannot undo a database commit.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterable, Iterator
import unicodedata


GENESIS_HASH = "0" * 64
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EVENT = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_FIELDS = {"sequence", "timestamp", "event_type", "payload", "previous_hash", "hash"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_SAFE_TOKEN_KEYS = {
    "token_generation", "max_new_tokens", "min_new_tokens", "max_tokens",
    "min_tokens", "num_tokens", "token_count", "token_counts", "token_ids",
    "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
    "total_tokens", "generated_tokens", "generation_tokens", "tokens_per_second",
    "eos_token_id", "bos_token_id", "pad_token_id", "decoder_start_token_id",
}


class AuditError(ValueError):
    """Base class for rejected audit data or unsafe audit files."""


class SecretDetectedError(AuditError):
    """A payload contains a sensitive field name and was not written."""


class AuditIntegrityError(AuditError):
    """A chain, file, or authoritative projection does not match."""


class AuditPathError(AuditError):
    """An audit path uses a symlink or is not a private regular file."""


def _check_json(value: Any, ancestors: set[int]) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if type(value) not in (dict, list):
        raise TypeError("Only JSON objects, arrays, strings, numbers, booleans and null are allowed")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("Cyclic JSON values are not allowed")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise TypeError("JSON object keys must be strings")
            children = value.values()
        else:
            children = value
        for child in children:
            _check_json(child, ancestors)
    finally:
        ancestors.remove(identity)


def canonical_json(value: Any) -> str:
    """Encode strict JSON deterministically, without ASCII escaping or NaN.

    This is the project's stable Python JSON encoding, not an RFC 8785 claim.
    Lone Unicode surrogates, non-string keys, tuples and custom types are rejected.
    """
    _check_json(value, set())
    result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False)
    result.encode("utf-8", errors="strict")
    return result


def _key_words(key: str) -> tuple[str, str]:
    key = unicodedata.normalize("NFKC", key)
    key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    words = re.findall(r"[a-z0-9]+", key.casefold())
    return "_".join(words), "".join(words)


def _sensitive_key(key: str) -> bool:
    normalized, compact = _key_words(key)
    if any(part in compact for part in (
        "apikey", "password", "passwd", "secret", "credential", "privatekey",
        "authorization", "authorisation",
    )):
        return True
    if normalized in _SAFE_TOKEN_KEYS:
        return False
    words = normalized.split("_")
    return ("token" in words or "tokens" in words or
            compact.endswith(("token", "tokens")) or
            (compact.startswith("token") and not compact.startswith(("tokenizer", "tokeniser", "tokenization", "tokenisation"))))


def validate_audit_payload(payload: dict[str, Any]) -> None:
    """Reject sensitive field names recursively, including camelCase variants.

    Values must also be strict JSON. This field-name check cannot discover a
    secret deliberately hidden in an innocuously named string; callers must only
    pass explicit, reviewed audit fields, never arbitrary environment dumps.
    """
    if type(payload) is not dict:
        raise TypeError("An audit payload must be a JSON object")
    canonical_json(payload)

    def visit(value: Any) -> None:
        if type(value) is dict:
            for key, child in value.items():
                if _sensitive_key(key):
                    # Do not echo a potentially sensitive value or field name.
                    raise SecretDetectedError("Sensitive audit field rejected")
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)

    visit(payload)


def _timestamp(timestamp: datetime) -> str:
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Audit timestamps must be timezone-aware datetimes")
    return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def make_record(sequence: int, previous_hash: str, event_type: str,
                payload: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    """Build a detached record; the first record has sequence 1 and GENESIS_HASH."""
    if type(sequence) is not int or sequence < 1:
        raise ValueError("Audit sequence must be a positive integer")
    if type(previous_hash) is not str or _HASH.fullmatch(previous_hash) is None:
        raise ValueError("Previous hash must be a lowercase SHA-256 digest")
    if type(event_type) is not str or _EVENT.fullmatch(event_type) is None:
        raise ValueError("Event type must be a bounded symbolic identifier")
    validate_audit_payload(payload)
    record = {
        "sequence": sequence,
        "timestamp": _timestamp(timestamp),
        "event_type": event_type,
        "payload": json.loads(canonical_json(payload)),
        "previous_hash": previous_hash,
    }
    record["hash"] = hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
    return record


def _verify_records(records: list[dict[str, Any]]) -> None:
    previous_hash = GENESIS_HASH
    for sequence, record in enumerate(records, start=1):
        try:
            if type(record) is not dict or set(record) != _FIELDS:
                raise ValueError("Unexpected record fields")
            if type(record["sequence"]) is not int or record["sequence"] != sequence:
                raise ValueError("Non-contiguous sequence")
            if record["previous_hash"] != previous_hash:
                raise ValueError("Previous hash mismatch")
            if type(record["timestamp"]) is not str:
                raise ValueError("Invalid timestamp")
            timestamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            expected = make_record(sequence, previous_hash, record["event_type"], record["payload"], timestamp)
            if record["timestamp"] != expected["timestamp"]:
                raise ValueError("Timestamp is not canonical UTC")
            if type(record["hash"]) is not str or _HASH.fullmatch(record["hash"]) is None:
                raise ValueError("Invalid hash")
            if not hmac.compare_digest(record["hash"], expected["hash"]):
                raise ValueError("Record hash mismatch")
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise AuditIntegrityError(f"Invalid audit record at sequence {sequence}") from exc
        previous_hash = record["hash"]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant")


class AuditLog:
    """A Linux process/thread-safe JSONL log that never repairs corrupt tails.

    The API does not rotate or replace files. All writers must use this protocol.
    Complete suffix deletion needs a trusted expected tip or the database for
    detection; a hash chain alone cannot reveal it.
    """

    def __init__(self, path: str | os.PathLike[str]):
        candidate = Path(path)
        if ".." in candidate.parts:
            raise AuditPathError("Parent traversal is not allowed in audit paths")
        self.path = candidate.absolute()
        with _LOCKS_GUARD:
            self._thread_lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    def _open_parent(self, create: bool) -> int:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for component in self.path.parts[1:-1]:
                try:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, 0o700, dir_fd=current)
                        os.fsync(current)
                    except FileExistsError:
                        pass
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
                os.close(current)
                current = child
            return current
        except BaseException:
            os.close(current)
            raise

    @staticmethod
    def _check_file(fd: int, parent: int, name: str) -> None:
        opened = os.fstat(fd)
        linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                opened.st_uid != os.geteuid() or opened.st_mode & 0o022 or
                (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)):
            raise AuditPathError("Audit path must reference an owned, unshared regular file without group/world write permission")

    @contextmanager
    def _locked(self, create: bool) -> Iterator[tuple[int, int] | None]:
        with self._thread_lock:
            parent = None
            fd = None
            try:
                try:
                    parent = self._open_parent(create)
                    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                    if create:
                        flags |= os.O_CREAT
                    fd = os.open(self.path.name, flags, 0o600, dir_fd=parent)
                except FileNotFoundError:
                    if create:
                        raise
                    yield None
                    return
                except OSError as exc:
                    if exc.errno in (20, 40):  # ENOTDIR / ELOOP, including parent symlinks.
                        raise AuditPathError("Audit paths must not contain symlinks") from exc
                    raise
                self._check_file(fd, parent, self.path.name)
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._check_file(fd, parent, self.path.name)
                yield fd, parent
            finally:
                if fd is not None:
                    os.close(fd)  # Closing also releases flock, including on errors.
                if parent is not None:
                    os.close(parent)

    @staticmethod
    def _read(fd: int) -> list[dict[str, Any]]:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content:
            return []
        if not content.endswith(b"\n"):
            raise AuditIntegrityError("Audit log has an incomplete final JSONL line")
        records = []
        for number, line in enumerate(content[:-1].split(b"\n"), start=1):
            try:
                text = line.decode("utf-8", errors="strict")
                record = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
                if canonical_json(record) != text:
                    raise ValueError("Non-canonical JSON encoding")
            except (ValueError, TypeError, UnicodeError) as exc:
                raise AuditIntegrityError(f"Invalid audit JSONL at line {number}") from exc
            records.append(record)
        _verify_records(records)
        return records

    def _write(self, fd: int, parent: int, records: list[dict[str, Any]]) -> None:
        self._check_file(fd, parent, self.path.name)
        for record in records:
            remaining = memoryview((canonical_json(record) + "\n").encode("utf-8"))
            while remaining:
                written = os.write(fd, remaining)
                if written == 0:
                    raise OSError("Audit append made no progress")
                remaining = remaining[written:]
        os.fsync(fd)
        os.fsync(parent)

    def verify(self, *, expected_sequence: int | None = None,
               expected_hash: str | None = None) -> list[dict[str, Any]]:
        """Return verified records; optionally compare a trusted external chain tip."""
        with self._locked(create=False) as opened:
            records = [] if opened is None else self._read(opened[0])
        if expected_sequence is not None and (type(expected_sequence) is not int or expected_sequence < 0):
            raise ValueError("Expected sequence must be a nonnegative integer")
        if expected_hash is not None and (type(expected_hash) is not str or _HASH.fullmatch(expected_hash) is None):
            raise ValueError("Expected hash must be a lowercase SHA-256 digest")
        tip = records[-1]["hash"] if records else GENESIS_HASH
        if ((expected_sequence is not None and len(records) != expected_sequence) or
                (expected_hash is not None and not hmac.compare_digest(tip, expected_hash))):
            raise AuditIntegrityError("Audit chain does not match the trusted expected tip")
        return records

    def append(self, event_type: str, payload: dict[str, Any],
               timestamp: datetime | None = None) -> dict[str, Any]:
        """Verify the complete file, append one event, and fsync before returning."""
        # Validate and detach before creating/opening files or acquiring their locks.
        draft = make_record(1, GENESIS_HASH, event_type, payload,
                            timestamp if timestamp is not None else datetime.now(timezone.utc))
        with self._locked(create=True) as opened:
            assert opened is not None
            fd, parent = opened
            records = self._read(fd)
            record = make_record(len(records) + 1, records[-1]["hash"] if records else GENESIS_HASH,
                                 draft["event_type"], draft["payload"],
                                 datetime.fromisoformat(draft["timestamp"].replace("Z", "+00:00")))
            self._write(fd, parent, [record])
            return record

    def sync_records(self, records: Iterable[dict[str, Any]]) -> int:
        """Project a complete authoritative chain, returning the appended count.

        Existing contents must be its exact prefix. A stale shorter authority,
        divergent chain or partial final line raises without rewriting anything.
        Retrying a complete matching projection is idempotent. I/O failures may
        leave a complete prefix or a partial line; the latter needs explicit
        operator recovery, never automatic truncation.
        """
        authoritative = json.loads(canonical_json(list(records)))
        _verify_records(authoritative)
        with self._locked(create=True) as opened:
            assert opened is not None
            fd, parent = opened
            existing = self._read(fd)
            if len(existing) > len(authoritative):
                raise AuditIntegrityError("Authoritative snapshot is older than the existing audit projection")
            for existing_record, source_record in zip(existing, authoritative):
                if canonical_json(existing_record) != canonical_json(source_record):
                    raise AuditIntegrityError("Audit projection conflicts with the authoritative chain")
            missing = authoritative[len(existing):]
            # Also fsync idempotent retries: a previous fsync failure can leave
            # complete bytes visible without their durability being established.
            self._write(fd, parent, missing)
            return len(missing)

    export = sync_records
