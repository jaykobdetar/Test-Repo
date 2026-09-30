"""A persistent task board for agents sharing one trusted local workspace.

Instructions and results are data: this module never executes them. Agent names
are coordination labels, not authentication principals. Ownership does not expire.
Cancelling or releasing a claim cannot terminate an agent or its external work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from .data import canonical_json

MAX_INSTRUCTION_BYTES = 16 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_REASON_BYTES = 4096
MAX_DEPENDENCIES = 64
_SCHEMA_VERSION = 1
_SCHEMA = (
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
        instruction TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed','cancelled')),
        agent_id TEXT, claim_hash TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        claimed_at TEXT, finished_at TEXT, result_json TEXT, failure_reason TEXT
    )""",
    """CREATE TABLE dependencies (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        dependency_id TEXT NOT NULL REFERENCES tasks(task_id),
        PRIMARY KEY(task_id, dependency_id), CHECK(task_id != dependency_id)
    )""",
)


def _text(value: str, name: str, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{name} must be nonempty text of at most {maximum} UTF-8 bytes")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_type(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise PermissionError("task state must be private, owned, and free of symlinks or hard links")


class AgentWorkspace:
    """Share tasks across processes using one private directory on local disk.

    Each operation uses a short SQLite transaction. A task is claimed once until
    its owner reports an outcome, someone cancels it, or the workspace operator
    explicitly releases it. Claim tokens are returned only by ``claim`` and are
    stored as hashes. Losing a token requires operator recovery with ``release``.
    After an uncertain completion response, use ``show``: completion tokens are
    invalidated, and repeating ``complete`` is deliberately rejected.
    """

    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir).expanduser().absolute()
        for path in (*reversed(self.root.parents), self.root):
            if path.is_symlink():
                raise PermissionError("task state path must not contain symlinks")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private(self.root, directory=True)
        self.database = self.root / "tasks.sqlite3"
        try:
            descriptor = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self._connection(write=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            if version == 0 and not tables:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            else:
                expected = sorted(" ".join(statement.split()) for statement in _SCHEMA)
                actual = sorted(" ".join(row[0].split()) for row in tables)
                if version != _SCHEMA_VERSION or actual != expected:
                    raise ValueError("unsupported or inconsistent task database schema")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Connections close after every operation; supplied for context-manager use."""

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        _private(self.root, directory=True)
        _private(self.database)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.database.with_name(self.database.name + suffix)
            try:
                _private(sidecar)
            except FileNotFoundError:
                # Another connection may remove its rollback journal as it commits.
                pass
        conn = sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        _text(task_id, "task_id", 128)
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("task does not exist")
        return row

    @staticmethod
    def _view(conn: sqlite3.Connection, row: sqlite3.Row, *, summary: bool = False) -> dict[str, Any]:
        hidden = {"claim_hash", "result_json"}
        if summary:
            hidden.update({"instruction", "failure_reason"})
        task = {key: value for key, value in dict(row).items() if key not in hidden}
        dependencies = conn.execute(
            """SELECT d.dependency_id,t.state FROM dependencies d JOIN tasks t ON t.task_id=d.dependency_id
            WHERE d.task_id=? ORDER BY d.dependency_id""",
            (row["task_id"],),
        ).fetchall()
        task["depends_on"] = [item["dependency_id"] for item in dependencies]
        task["blocked_by"] = [item["dependency_id"] for item in dependencies if item["state"] != "completed"]
        if not summary:
            task["result"] = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return task

    def submit(self, idempotency_key: str, instruction: str, depends_on=()) -> dict[str, Any]:
        """Create a task once. Dependencies must exist already, preventing cycles."""
        _text(idempotency_key, "idempotency_key", 128)
        _text(instruction, "instruction", MAX_INSTRUCTION_BYTES)
        if type(depends_on) not in (list, tuple) or len(depends_on) > MAX_DEPENDENCIES:
            raise ValueError(f"depends_on must be a list or tuple of at most {MAX_DEPENDENCIES} task IDs")
        dependencies = sorted(_text(item, "dependency", 128) for item in depends_on)
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("dependencies must be unique")
        with self._connection(write=True) as conn:
            existing = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                task = self._view(conn, existing)
                if task["instruction"] != instruction or task["depends_on"] != dependencies:
                    raise ValueError("idempotency key already identifies a different task")
                return task
            for task_id in dependencies:
                self._row(conn, task_id)
            task_id, now = uuid.uuid4().hex, _now()
            conn.execute(
                """INSERT INTO tasks(task_id,idempotency_key,instruction,state,created_at,updated_at)
                VALUES(?,?,?,'pending',?,?)""",
                (task_id, idempotency_key, instruction, now, now),
            )
            conn.executemany(
                "INSERT INTO dependencies(task_id,dependency_id) VALUES(?,?)",
                [(task_id, dependency) for dependency in dependencies],
            )
            return self._view(conn, self._row(conn, task_id))

    def claim(self, agent_id: str) -> dict[str, Any] | None:
        """Atomically claim the oldest ready task. Ownership has no expiry timer."""
        _text(agent_id, "agent_id", 128)
        with self._connection(write=True) as conn:
            row = conn.execute(
                """SELECT t.* FROM tasks t WHERE state='pending' AND NOT EXISTS (
                    SELECT 1 FROM dependencies d JOIN tasks p ON p.task_id=d.dependency_id
                    WHERE d.task_id=t.task_id AND p.state!='completed'
                ) ORDER BY created_at,task_id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            token, now = secrets.token_urlsafe(32), _now()
            conn.execute(
                """UPDATE tasks SET state='running',agent_id=?,claim_hash=?,attempt_count=attempt_count+1,
                claimed_at=?,updated_at=? WHERE task_id=?""",
                (agent_id, hashlib.sha256(token.encode()).hexdigest(), now, now, row["task_id"]),
            )
            task = self._view(conn, self._row(conn, row["task_id"]))
            task["claim_token"] = token
            return task

    @staticmethod
    def _owned(conn: sqlite3.Connection, task_id: str, claim_token: str) -> sqlite3.Row:
        _text(claim_token, "claim_token", 256)
        row = AgentWorkspace._row(conn, task_id)
        digest = hashlib.sha256(claim_token.encode()).hexdigest()
        if row["state"] != "running" or not hmac.compare_digest(row["claim_hash"] or "", digest):
            raise PermissionError("claim no longer owns this task")
        return row

    def complete(self, task_id: str, claim_token: str, result: Any) -> dict[str, Any]:
        """Persist a bounded JSON result; completed tasks and results are immutable."""
        encoded = canonical_json(result)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError(f"result exceeds {MAX_RESULT_BYTES} UTF-8 bytes")
        return self._finish(task_id, claim_token, "completed", encoded, None)

    def fail(self, task_id: str, claim_token: str, reason: str) -> dict[str, Any]:
        """Record a visible failure; dependent tasks remain blocked."""
        return self._finish(task_id, claim_token, "failed", None, _text(reason, "reason", MAX_REASON_BYTES))

    def _finish(self, task_id, claim_token, state, result, reason) -> dict[str, Any]:
        with self._connection(write=True) as conn:
            self._owned(conn, task_id, claim_token)
            now = _now()
            conn.execute(
                """UPDATE tasks SET state=?,claim_hash=NULL,result_json=?,failure_reason=?,
                updated_at=?,finished_at=? WHERE task_id=?""",
                (state, result, reason, now, now, task_id),
            )
            return self._view(conn, self._row(conn, task_id))

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Invalidate a claim and mark cancelled. This does not stop external work."""
        with self._connection(write=True) as conn:
            row = self._row(conn, task_id)
            if row["state"] == "completed":
                raise ValueError("completed tasks are immutable")
            if row["state"] != "cancelled":
                now = _now()
                conn.execute(
                    "UPDATE tasks SET state='cancelled',claim_hash=NULL,updated_at=?,finished_at=? WHERE task_id=?",
                    (now, now, task_id),
                )
            return self._view(conn, self._row(conn, task_id))

    def release(self, task_id: str) -> dict[str, Any]:
        """Owner recovery: reset unfinished work and invalidate its previous claim.

        Keep this off the agent-facing RPC surface. The operator must coordinate
        any external work before reassigning; releasing cannot stop that work.
        """
        with self._connection(write=True) as conn:
            row = self._row(conn, task_id)
            if row["state"] == "completed":
                raise ValueError("completed tasks are immutable")
            conn.execute(
                """UPDATE tasks SET state='pending',agent_id=NULL,claim_hash=NULL,claimed_at=NULL,
                finished_at=NULL,result_json=NULL,failure_reason=NULL,updated_at=? WHERE task_id=?""",
                (_now(), task_id),
            )
            return self._view(conn, self._row(conn, task_id))

    def show(self, task_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            return self._view(conn, self._row(conn, task_id))

    def list(self, *, offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        if type(offset) is not int or not 0 <= offset <= 1_000_000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("offset must be 0..1000000 and limit must be 1..100")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at,task_id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            return [self._view(conn, row, summary=True) for row in rows]
