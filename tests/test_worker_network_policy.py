"""Actual syscall policy checks without importing Torch or loading a model."""

import ctypes
import errno
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from probe_core import worker
from probe_core.worker_contracts import WorkerRequestError


def test_unix_creation_and_all_other_domain_denials_in_actual_child(tmp_path):
    script = r"""
import errno, json, socket, sys
sys.path.insert(0, sys.argv[1])
from probe_core.worker import _block_network

directory = sys.argv[2]
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(directory + '/listener')
listener.listen(1)
receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
receiver.bind(directory + '/receiver')
left, right = socket.socketpair()
inherited_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_block_network()

def denied(operation):
    try:
        value = operation()
    except OSError as exc:
        assert exc.errno == errno.EPERM, (type(exc).__name__, exc.errno)
        return
    if hasattr(value, 'close'):
        value.close()
    raise AssertionError('expected EPERM')

allowed = []
blocked = []
flags = (0, socket.SOCK_CLOEXEC, socket.SOCK_NONBLOCK,
         socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK)
for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_SEQPACKET):
    for flag in flags:
        with socket.socket(socket.AF_UNIX, kind | flag):
            allowed.append([kind, flag])
for family, kind in ((socket.AF_INET, socket.SOCK_STREAM),
                     (socket.AF_INET6, socket.SOCK_DGRAM),
                     (socket.AF_NETLINK, socket.SOCK_RAW),
                     (socket.AF_PACKET, socket.SOCK_RAW),
                     # Some Linux Python builds omit this constant; the Linux
                     # socket ABI still assigns AF_VSOCK the value 40.
                     (getattr(socket, 'AF_VSOCK', 40), socket.SOCK_STREAM)):
    for flag in flags:
        denied(lambda: socket.socket(family, kind | flag))
        blocked.append([family, flag])
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    denied(lambda: client.connect(directory + '/listener'))
with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
    denied(lambda: sender.sendto(b'x', directory + '/receiver'))
denied(lambda: left.sendmsg([b'x']))
denied(lambda: inherited_udp.connect(('127.0.0.1', 9)))
denied(lambda: inherited_udp.sendto(b'x', ('127.0.0.1', 9)))
denied(lambda: inherited_udp.sendmsg([b'x'], [], 0, ('127.0.0.1', 9)))
for item in (listener, receiver, left, right, inherited_udp):
    item.close()
print(json.dumps({'allowed_unix': allowed, 'denied_domains': blocked,
                  'denied_unix_connect_sendto_sendmsg': True,
                  'denied_inherited_inet_connect_sendto_sendmsg': True}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report["allowed_unix"]) == 12
    assert len(report["denied_domains"]) == 20
    assert report["denied_unix_connect_sendto_sendmsg"] is True
    assert report["denied_inherited_inet_connect_sendto_sendmsg"] is True


class _Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class _Library:
    def __init__(self, failure=None):
        self.released = []
        self.rules = []
        self.loaded = False
        names = (b"socket", b"connect", b"sendto", b"sendmsg")
        numbers = {name: index + 40 for index, name in enumerate(names)}

        def resolve(name):
            return -1 if failure == ("resolve", name) else numbers[name]

        def add(context, action, syscall, count, comparisons):
            assert context == 1234
            assert action == 0x00050000 | errno.EPERM
            if syscall == numbers[b"socket"]:
                assert count == 1
                compare = comparisons[0]
                # Validate the actual C ABI, not a variadic ctypes argument list.
                assert ctypes.sizeof(compare) == 24
                assert type(compare).op.offset == 4
                assert type(compare).datum_a.offset == 8
                assert type(compare).datum_b.offset == 16
                assert (compare.arg, compare.op, compare.datum_a, compare.datum_b) == (0, 1, socket.AF_UNIX, 0)
            else:
                assert count == 0 and comparisons is None
            self.rules.append(syscall)
            return -errno.EINVAL if failure == ("rule", names[syscall - 40]) else 0

        def load(context):
            assert context == 1234
            self.loaded = True
            return -errno.EPERM if failure == ("load", None) else 0

        self.seccomp_init = _Function(lambda _action: None if failure == ("init", None) else 1234)
        self.seccomp_syscall_resolve_name = _Function(resolve)
        self.seccomp_rule_add_array = _Function(add)
        self.seccomp_load = _Function(load)
        self.seccomp_release = _Function(self.released.append)


def test_socket_comparison_uses_fixed_array_abi_and_only_creation_exception(monkeypatch):
    library = _Library()
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *_args, **_kwargs: library)
    worker._block_network()
    assert library.rules == [40, 41, 42, 43]
    assert library.loaded
    assert library.released == [1234]
    assert len(library.seccomp_rule_add_array.argtypes) == 5


@pytest.mark.parametrize(
    "failure",
    [
        ("init", None),
        ("load", None),
        *((kind, name) for kind in ("resolve", "rule") for name in (b"socket", b"connect", b"sendto", b"sendmsg")),
    ],
)
def test_policy_setup_failure_refuses_execution_and_releases_context(monkeypatch, failure):
    library = _Library(failure)
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *_args, **_kwargs: library)
    with pytest.raises(WorkerRequestError, match="network-denial"):
        worker._block_network()
    assert library.released == ([] if failure[0] == "init" else [1234])
    assert library.loaded is (failure[0] == "load")
