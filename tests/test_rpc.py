import os
import multiprocessing
from pathlib import Path
import socket
import threading

import pytest

from probe_core.rpc import RPCError, UnixRPCClient, UnixRPCServer


@pytest.fixture
def rpc(tmp_path):
    directory = tmp_path / "rpc"
    directory.mkdir(mode=0o700)
    server = UnixRPCServer(directory / "api.sock", lambda method, params: {"method": method, "params": params},
                           allowed_uids={os.geteuid()}, allow_service_uid=True)
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
        UnixRPCServer(rpc.path, lambda m, p: None,
                      allowed_uids={os.geteuid()}, allow_service_uid=True)
    assert rpc.path.stat().st_ino == original.st_ino
    assert UnixRPCClient(rpc.path, expected_server_uid=os.geteuid()).call("still_live")["method"] == "still_live"


def _run_crashable_server(path, ready):
    with UnixRPCServer(path, lambda m, p: {"running": True},
                       allowed_uids={os.geteuid()}, allow_service_uid=True) as server:
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
        with UnixRPCServer(path, lambda m, p: {"restarted": True},
                           allowed_uids={os.geteuid()}, allow_service_uid=True) as restarted:
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
        UnixRPCServer(path, lambda m, p: None,
                      allowed_uids={os.geteuid()}, allow_service_uid=True)
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
        UnixRPCServer(path, lambda m, p: None,
                      allowed_uids={os.geteuid()}, allow_service_uid=True)
    assert not path.exists()
    assert original.read_text() == "preserve"


def test_live_legacy_socket_without_lock_is_not_replaced(tmp_path):
    path = tmp_path / "legacy.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as legacy:
        legacy.bind(str(path))
        legacy.listen(1)
        identity = path.stat().st_ino
        with pytest.raises(FileExistsError, match="accepting connections"):
            UnixRPCServer(path, lambda m, p: None,
                          allowed_uids={os.geteuid()}, allow_service_uid=True)
        assert path.stat().st_ino == identity
