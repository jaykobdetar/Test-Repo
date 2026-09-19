"""Bounded, peer-authenticated Unix RPC between local OS identities.

This is a private service transport, not MCP. MCP runs under the untrusted
research identity and can reach only the explicitly permitted research socket.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
import struct
import threading
from typing import Any, Callable

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024


class RPCError(Exception):
    """Public RPC errors contain fixed messages, never raw service exceptions."""


def peer_uid(sock: socket.socket) -> int:
    return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def decode(data: bytes) -> Any:
    def invalid(_: str) -> None:
        raise ValueError("non-finite JSON")
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=invalid)


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode() + b"\n"


class UnixRPCServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, path: str | Path, dispatch: Callable[[str, dict[str, Any]], Any],
                 *, allowed_uids: set[int], socket_gid: int | None = None,
                 allow_service_uid: bool = False, timeout_seconds: float = 30):
        if not allowed_uids or any(type(uid) is not int or uid < 0 for uid in allowed_uids):
            raise ValueError("explicit client OS identities are required")
        if os.geteuid() in allowed_uids and not allow_service_uid:
            raise ValueError("research clients must use a different OS identity")
        self.path = Path(path).absolute()
        parent = self.path.parent
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("socket parent must be service-owned and not group/world writable")
        self._lock_fd: int | None = None
        self._socket_identity: tuple[int, int] | None = None
        self.dispatch = dispatch
        self.allowed_uids = frozenset(allowed_uids)
        self.timeout_seconds = timeout_seconds
        self._request_slots = threading.BoundedSemaphore(16)
        # Keep this inode permanently: unlinking a lock file can split the
        # lifetime lock between an old opener and a newly created inode.
        lock_path = self.path.with_name("." + self.path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
                    or opened.st_nlink != 1 or opened.st_mode & 0o077):
                raise PermissionError("service lock must be a private, owned, unshared regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise FileExistsError("service socket is already owned by a running instance") from None
            self._lock_fd = fd
        except BaseException:
            os.close(fd)
            raise
        try:
            self._remove_stale_socket()
            super().__init__(str(self.path), _Handler)
            opened = self.path.lstat()
            self._socket_identity = (opened.st_dev, opened.st_ino)
            os.chmod(self.path, 0o660 if socket_gid is not None else 0o600)
            if socket_gid is not None:
                os.chown(self.path, -1, socket_gid)
        except BaseException:
            if hasattr(self, "socket"):
                self.server_close()
            else:
                self._release_lock()
            raise

    def _remove_stale_socket(self) -> None:
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid()
                or existing.st_nlink != 1):
            raise FileExistsError("refusing to replace a file, symlink, or unowned socket")
        # The lifetime lock excludes current servers. Also refuse a live socket
        # from an older service version that did not yet use this lock protocol.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            try:
                probe.connect(str(self.path))
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise FileExistsError("existing service socket cannot be proven stale") from None
            else:
                raise FileExistsError("service socket is still accepting connections")
        current = self.path.lstat()
        if (current.st_dev, current.st_ino) != (existing.st_dev, existing.st_ino):
            raise FileExistsError("service socket changed during recovery")
        self.path.unlink()

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            fd, self._lock_fd = self._lock_fd, None
            os.close(fd)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def server_close(self) -> None:
        try:
            super().server_close()
            try:
                existing = self.path.lstat()
                if (existing.st_dev, existing.st_ino) == self._socket_identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
        finally:
            self._release_lock()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(self.server.timeout_seconds)
        response: dict[str, Any]
        try:
            if peer_uid(self.request) not in self.server.allowed_uids:
                raise PermissionError
            data = self.rfile.readline(MAX_REQUEST + 1)
            if len(data) > MAX_REQUEST or not data.endswith(b"\n"):
                raise ValueError
            message = decode(data)
            if (type(message) is not dict or set(message) != {"method", "params"}
                    or type(message["method"]) is not str
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", message["method"]) is None
                    or type(message["params"]) is not dict):
                raise ValueError
            result = self.server.dispatch(message["method"], message["params"])
            response = {"ok": True, "result": result}
        except PermissionError:
            response = {"ok": False, "error": {"code": "forbidden", "message": "request denied"}}
        except (ValueError, TypeError, KeyError):
            response = {"ok": False, "error": {"code": "invalid_request", "message": "invalid request"}}
        except Exception:
            response = {"ok": False, "error": {"code": "service_error", "message": "service could not complete request"}}
        try:
            data = encode(response)
            if len(data) > MAX_RESPONSE:
                data = encode({"ok": False, "error": {"code": "response_limit", "message": "response exceeds limit"}})
            self.wfile.write(data)
        except (OSError, ValueError, TypeError):
            return


class UnixRPCClient:
    def __init__(self, path: str | Path, *, expected_server_uid: int,
                 timeout_seconds: float = 30):
        self.path = str(path)
        self.expected_server_uid = expected_server_uid
        self.timeout_seconds = timeout_seconds

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        data = encode({"method": method, "params": params or {}})
        if len(data) > MAX_REQUEST:
            raise RPCError("request exceeds limit")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout_seconds)
            connection.connect(self.path)
            if peer_uid(connection) != self.expected_server_uid:
                raise RPCError("untrusted service identity")
            connection.sendall(data)
            with connection.makefile("rb") as stream:
                raw = stream.readline(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
                raise RPCError("invalid service response")
            result = decode(raw)
            if type(result) is not dict or type(result.get("ok")) is not bool:
                raise RPCError("invalid service response")
            if not result["ok"]:
                # Do not propagate arbitrary peer error strings into model context.
                raise RPCError("request rejected by trusted service")
            return result["result"]
