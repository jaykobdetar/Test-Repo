import os
import io
import multiprocessing
from pathlib import Path
import socket
import threading

import pytest

from probe_core.rpc import RPCError, UnixRPCClient, UnixRPCServer
from probe_core import rpc as rpc_module


@pytest.fixture
def rpc(tmp_path):
    directory = tmp_path / "rpc"
    directory.mkdir(mode=0o700)
    server = UnixRPCServer(
        directory / "api.sock",
        lambda method, params: {"method": method, "params": params},
        allowed_uids={os.geteuid()},
        allow_service_uid=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def test_actual_peer_credentials_and_roundtrip(rpc):
    client = UnixRPCClient(rpc.path, expected_server_uid=os.geteuid())
    assert client.call("hello", {"x": 1}) == {"method": "hello", "params": {"x": 1}}
    with pytest.raises(RPCError, match="untrusted service"):
        UnixRPCClient(rpc.path, expected_server_uid=os.geteuid() + 1).call("hello")


def test_reject_wrong_peer_and_duplicate_keys(rpc):
    rpc.allowed_uids = frozenset({os.geteuid() + 1})
    with pytest.raises(RPCError):
        UnixRPCClient(rpc.path, expected_server_uid=os.geteuid()).call("hello")
    rpc.allowed_uids = frozenset({os.geteuid()})
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(rpc.path))
        connection.sendall(b'{"method":"hello","method":"approve","params":{}}\n')
        assert b'"ok":false' in connection.recv(4096)


def test_default_refuses_same_identity(tmp_path):
    with pytest.raises(ValueError, match="different OS identity"):
        UnixRPCServer(tmp_path / "api.sock", lambda m, p: None, allowed_uids={os.geteuid()})


def test_exceptions_do_not_leak_service_secrets(rpc):
    def broken(method, params):
        raise RuntimeError("secret-value-do-not-expose")

    rpc.dispatch = broken
    with pytest.raises(RPCError) as error:
        UnixRPCClient(rpc.path, expected_server_uid=os.geteuid()).call("hello")
    assert "secret-value" not in str(error.value)


def test_concurrent_instance_cannot_replace_live_socket(rpc):
    original = rpc.path.stat()
    with pytest.raises(FileExistsError, match="running instance"):
        UnixRPCServer(rpc.path, lambda m, p: None, allowed_uids={os.geteuid()}, allow_service_uid=True)
    assert rpc.path.stat().st_ino == original.st_ino
    assert UnixRPCClient(rpc.path, expected_server_uid=os.geteuid()).call("still_live")["method"] == "still_live"


def _run_crashable_server(path, ready):
    with UnixRPCServer(
        path, lambda m, p: {"running": True}, allowed_uids={os.geteuid()}, allow_service_uid=True
    ) as server:
        ready.set()
        server.serve_forever()


def test_sigkill_stale_socket_is_recovered_on_restart(tmp_path):
    path = tmp_path / "crash.sock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_run_crashable_server, args=(path, ready))
    process.start()
    try:
        assert ready.wait(10)
        assert UnixRPCClient(path, expected_server_uid=os.geteuid()).call("status") == {"running": True}
        process.kill()  # SIGKILL cannot execute the old server's cleanup.
        process.join(timeout=10)
        assert not process.is_alive()
        assert path.is_socket()
        with UnixRPCServer(
            path, lambda m, p: {"restarted": True}, allowed_uids={os.geteuid()}, allow_service_uid=True
        ) as restarted:
            thread = threading.Thread(target=restarted.serve_forever, daemon=True)
            thread.start()
            try:
                assert UnixRPCClient(path, expected_server_uid=os.geteuid()).call("status") == {"restarted": True}
            finally:
                restarted.shutdown()
                thread.join(timeout=5)
        assert not path.exists()
        assert path.with_name(".crash.sock.lock").is_file()
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        process.close()


@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_recovery_never_replaces_nonsocket_paths(tmp_path, kind):
    path = tmp_path / "api.sock"
    if kind == "file":
        path.write_text("preserve")
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.mkdir()
    with pytest.raises(FileExistsError):
        UnixRPCServer(path, lambda m, p: None, allowed_uids={os.geteuid()}, allow_service_uid=True)
    assert path.exists() or path.is_symlink()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "directory"])
def test_lifetime_lock_rejects_unsafe_files(tmp_path, kind):
    path = tmp_path / "api.sock"
    lock = tmp_path / ".api.sock.lock"
    original = tmp_path / "other"
    original.write_text("preserve")
    original.chmod(0o600)
    if kind == "symlink":
        lock.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, lock)
    elif kind == "public":
        lock.write_text("preserve")
        lock.chmod(0o644)
    else:
        lock.mkdir()
    with pytest.raises((OSError, PermissionError)):
        UnixRPCServer(path, lambda m, p: None, allowed_uids={os.geteuid()}, allow_service_uid=True)
    assert not path.exists()
    assert original.read_text() == "preserve"


def test_live_legacy_socket_without_lock_is_not_replaced(tmp_path):
    path = tmp_path / "legacy.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as legacy:
        legacy.bind(str(path))
        legacy.listen(1)
        identity = path.stat().st_ino
        with pytest.raises(FileExistsError, match="accepting connections"):
            UnixRPCServer(path, lambda m, p: None, allowed_uids={os.geteuid()}, allow_service_uid=True)
        assert path.stat().st_ino == identity


class _PeerSocket:
    """Deterministically put a disconnect at a client I/O boundary."""

    def __init__(self, *, failure=None, phase=None, response=b'{"ok":true,"result":null}\n'):
        self.failure, self.phase, self.response = failure, phase, response
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def settimeout(self, value):
        self.calls.append(("timeout", value))

    def connect(self, path):
        self.calls.append(("connect", path))

    def sendall(self, data):
        self.calls.append(("send", data))
        if self.phase == "send":
            raise self.failure("PRIVATE_SOCKET_DIAGNOSTIC")

    def makefile(self, mode):
        self.calls.append(("makefile", mode))
        peer = self

        class Reader(io.BytesIO):
            def readline(self, limit):
                peer.calls.append(("read", limit))
                if peer.phase == "read":
                    raise peer.failure("PRIVATE_SOCKET_DIAGNOSTIC")
                return super().readline(limit)

        return Reader(self.response)


@pytest.mark.parametrize("phase", ["send", "read"])
@pytest.mark.parametrize("failure", [BrokenPipeError, ConnectionResetError, TimeoutError])
def test_peer_disconnect_is_a_sanitized_rpc_error_without_replay(monkeypatch, phase, failure):
    peer = _PeerSocket(failure=failure, phase=phase)
    opened = []

    def connect_socket(*args):
        opened.append(args)
        return peer

    monkeypatch.setattr(rpc_module.socket, "socket", connect_socket)
    monkeypatch.setattr(rpc_module, "peer_uid", lambda _: os.geteuid())
    with pytest.raises(RPCError, match="^service connection failed$") as error:
        UnixRPCClient("/synthetic/private.sock", expected_server_uid=os.geteuid()).call(
            "approve", {"private_request": "PRIVATE_REQUEST_CONTENT"}
        )
    assert len(opened) == 1
    assert [name for name, _ in peer.calls].count("send") == 1
    assert [name for name, _ in peer.calls].count("read") == (phase == "read")
    assert peer.closed
    assert "PRIVATE" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__ is True


def test_wrong_server_uid_still_refuses_before_any_request_bytes(monkeypatch):
    peer = _PeerSocket(failure=BrokenPipeError, phase="send")
    monkeypatch.setattr(rpc_module.socket, "socket", lambda *_: peer)
    monkeypatch.setattr(rpc_module, "peer_uid", lambda _: os.geteuid() + 1)
    with pytest.raises(RPCError, match="^untrusted service identity$"):
        UnixRPCClient("/synthetic/private.sock", expected_server_uid=os.geteuid()).call("approve")
    assert [name for name, _ in peer.calls] == ["timeout", "connect"]
    assert peer.closed


@pytest.mark.parametrize("response", [b"", b'{"ok":true}', b"[]\n"])
def test_eof_and_malformed_envelopes_keep_existing_refusal(monkeypatch, response):
    peer = _PeerSocket(response=response)
    monkeypatch.setattr(rpc_module.socket, "socket", lambda *_: peer)
    monkeypatch.setattr(rpc_module, "peer_uid", lambda _: os.geteuid())
    with pytest.raises(RPCError, match="^invalid service response$"):
        UnixRPCClient("/synthetic/private.sock", expected_server_uid=os.geteuid()).call("status")
    assert [name for name, _ in peer.calls].count("send") == 1
    assert peer.closed
