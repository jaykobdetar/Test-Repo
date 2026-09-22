"""Small real subprocess checks; no SSH server, model load or cloud access."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from probe_core import dispatcher
from probe_core import gpu_acceptance_runner as runner


SECRET = "SYNTHETIC_PRIVATE_VALUE_DO_NOT_PUBLISH"
BOOTSTRAP = {
    "status": "failed",
    "code": "WORKER_BOOTSTRAP_FAILED",
    "error_type": "PermissionError",
    "bootstrap_line": 123,
}
SSH_ERRORS = [
    ("HOST_KEY_REJECTED", "Host key verification failed."),
    ("AUTHENTICATION_REJECTED", "Permission denied (publickey)."),
    ("CONNECTION_REFUSED", "ssh: connect to host PRIVATE_HOST port 40103: Connection refused"),
    ("CONNECTION_CLOSED", "/usr/bin/scp: Connection closed"),
    ("NETWORK_UNREACHABLE", "ssh: connect to host PRIVATE_HOST port 40103: Network is unreachable"),
]


def test_real_bootstrap_refusal_producer_is_accepted_without_its_private_message():
    from probe_core import gpu_launch
    from probe_core.pod_bootstrap import BootstrapRefused

    try:
        exec(
            compile("raise BootstrapRefused(secret)", gpu_launch.__file__, "exec"),
            {"BootstrapRefused": BootstrapRefused, "secret": SECRET},
        )
    except BootstrapRefused as error:
        record = gpu_launch._failure_receipt(error)
    assert record["error_type"] == "BootstrapRefused" and record["bootstrap_line"] == 1
    assert runner.bootstrap_record(json.dumps(record)) == record
    assert SECRET not in json.dumps(record)


@pytest.mark.parametrize("classification,message", SSH_ERRORS)
def test_command_classifies_bounded_stderr_without_retaining_private_text(classification, message):
    raw = SECRET + " /private/credential-file 192.0.2.9\n" + message
    code = "import sys;sys.stderr.write(" + repr(raw) + ");sys.exit(255)"
    with pytest.raises(runner.RunnerError) as caught:
        runner.command_bytes([sys.executable, "-I", "-c", code])
    expected = {"phase": "verification", "exit_status": 255, "timeout": False, "classification": classification}
    assert caught.value.diagnostic == runner.safe_command_diagnostic(caught.value.diagnostic) == expected
    assert SECRET not in str(caught.value) + json.dumps(expected)


def test_command_stderr_flood_is_bounded_and_stopped():
    code = "import sys,time;sys.stderr.write(" + repr(SECRET) + "*5000);sys.stderr.flush();time.sleep(20)"
    started = time.monotonic()
    with pytest.raises(runner.RunnerError) as caught:
        runner.command_bytes([sys.executable, "-I", "-c", code], timeout=2)
    assert time.monotonic() - started < 3
    assert caught.value.diagnostic["classification"] == "OUTPUT_BOUND"
    assert SECRET not in json.dumps(caught.value.diagnostic)


def test_nonzero_subprocess_retains_only_fixed_bootstrap_record():
    code = "import sys;print(" + repr(json.dumps(BOOTSTRAP)) + ");sys.stderr.write(" + repr(SECRET) + ");sys.exit(19)"
    with pytest.raises(runner.RunnerError) as caught:
        runner.command_bytes([sys.executable, "-I", "-c", code])
    assert str(caught.value) == "SSH_VERIFICATION_COMMAND_FAILED"
    assert caught.value.diagnostic == {
        "phase": "verification",
        "exit_status": 19,
        "timeout": False,
        "classification": "PROCESS_EXITED",
        "bootstrap": BOOTSTRAP,
    }
    assert SECRET not in json.dumps(caught.value.diagnostic)


@pytest.mark.parametrize(
    "body",
    [
        SECRET,
        json.dumps(dict(BOOTSTRAP, message=SECRET)),
        json.dumps(dict(BOOTSTRAP, error_type=SECRET)),
        json.dumps(dict(BOOTSTRAP, bootstrap_line=SECRET)),
        json.dumps(dict(BOOTSTRAP, bootstrap_line=True)),
        "prefix " + json.dumps(BOOTSTRAP),
        json.dumps(BOOTSTRAP) + "\n" + SECRET,
        '{"status":"failed","status":"failed","code":"WORKER_BOOTSTRAP_FAILED","error_type":"ValueError","bootstrap_line":12}',
    ],
)
def test_bootstrap_parser_rejects_extra_private_or_ambiguous_content(body):
    assert runner.bootstrap_record(body) is None


def test_nonzero_subprocess_does_not_extract_receipt_from_stderr():
    code = "import sys;print(" + repr(SECRET) + ");sys.stderr.write(" + repr(json.dumps(BOOTSTRAP)) + ");sys.exit(23)"
    with pytest.raises(runner.RunnerError) as caught:
        runner.command_bytes([sys.executable, "-I", "-c", code])
    assert caught.value.diagnostic == {
        "phase": "verification",
        "exit_status": 23,
        "timeout": False,
        "classification": "PROCESS_EXITED",
    }
    assert SECRET not in str(caught.value) + json.dumps(caught.value.diagnostic)


def running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] not in {"Z", "X"}
    except FileNotFoundError:
        return False


def test_command_timeout_kills_owned_group_including_child_holding_stdout(tmp_path):
    pids = tmp_path / "pids.json"
    # The original parent exits successfully, but its child holds stdout open.
    # A timeout must kill the process group even after the parent has exited.
    code = (
        "import os,sys,subprocess,json;"
        'child=subprocess.Popen([sys.executable,"-I","-c","import time;time.sleep(20)"]);'
        f'open({str(pids)!r},"w").write(json.dumps([os.getpid(),child.pid]));'
        f"print({SECRET!r},flush=True)"
    )
    started = time.monotonic()
    with pytest.raises(runner.RunnerError) as caught:
        runner.command_bytes([sys.executable, "-I", "-c", code], timeout=0.4)
    assert time.monotonic() - started < 3
    assert caught.value.diagnostic == {
        "phase": "verification",
        "exit_status": 0,
        "timeout": True,
        "classification": "COMMAND_TIMEOUT",
    }
    own, child = json.loads(pids.read_text())
    deadline = time.monotonic() + 1
    while running(child) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not running(own) and not running(child)
    assert SECRET not in json.dumps(caught.value.diagnostic)


def test_output_bound_retains_no_output_bytes():
    with pytest.raises(runner.RunnerError) as caught:
        runner.command_bytes([sys.executable, "-I", "-c", "print(" + repr(SECRET) + "*5000)"])
    assert caught.value.diagnostic["classification"] == "OUTPUT_BOUND"
    assert caught.value.diagnostic["timeout"] is False
    assert SECRET not in json.dumps(caught.value.diagnostic)


def test_diagnostic_attribute_cannot_publish_arbitrary_exception_fields():
    raw = {
        "phase": "configure",
        "exit_status": 7,
        "timeout": False,
        "classification": "PROCESS_EXITED",
        "stdout": SECRET,
        "stderr": SECRET,
        "argv": [SECRET],
        "bootstrap": dict(BOOTSTRAP, path=SECRET),
    }
    assert runner.safe_command_diagnostic(raw) == {
        "phase": "configure",
        "exit_status": 7,
        "timeout": False,
        "classification": "PROCESS_EXITED",
    }
    for key, value in [
        ("phase", SECRET),
        ("exit_status", SECRET),
        ("exit_status", True),
        ("timeout", SECRET),
        ("classification", SECRET),
        ("classification", {}),
    ]:
        assert runner.safe_command_diagnostic(dict(raw, **{key: value})) is None


def test_provider_diagnostic_publication_revalidates_records_and_drops_extra_fields():
    raw = {
        "status": "observed",
        "records": [BOOTSTRAP, dict(BOOTSTRAP, address=SECRET), SECRET],
        "url": SECRET,
        "stderr": SECRET,
    }
    assert runner.safe_bootstrap_diagnostic(raw) == {"status": "observed", "records": [BOOTSTRAP]}
    assert runner.safe_bootstrap_diagnostic(dict(raw, status=SECRET)) == {"status": "unavailable", "records": []}


@pytest.fixture
def tunnel(tmp_path, monkeypatch):
    key, hosts = tmp_path / "key", tmp_path / "hosts"
    for path in (key, hosts):
        path.write_text("synthetic fixture")
        path.chmod(0o600)
    instance = dispatcher.SSHTunnel(
        "fixture.example", user="root", identity_file=key, known_hosts_file=hosts, local_port=40000
    )

    def unavailable(*_args, **_kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(dispatcher.socket, "create_connection", unavailable)
    yield instance
    instance.close()


def test_tunnel_nonzero_exit_retains_status_without_subprocess_output(tunnel, monkeypatch):
    code = f"import sys;print({SECRET!r});sys.stderr.write({SECRET!r});sys.exit(17)"
    monkeypatch.setattr(tunnel, "command", lambda: [sys.executable, "-I", "-c", code])
    with pytest.raises(dispatcher.TransportError) as caught:
        tunnel.start(timeout_seconds=1)
    assert caught.value.diagnostic == {
        "phase": "tunnel",
        "exit_status": 17,
        "timeout": False,
        "classification": "TUNNEL_EXITED",
    }
    assert tunnel.process is None
    assert SECRET not in str(caught.value) + json.dumps(caught.value.diagnostic)


@pytest.mark.parametrize("classification,message", SSH_ERRORS)
def test_tunnel_classifies_stderr_without_leaking_private_bytes(tunnel, monkeypatch, classification, message):
    # The padding crosses a 4096-byte pipe read, exercising split-phrase handling.
    raw = SECRET + "x" * (4090 - len(SECRET)) + message
    code = "import sys;sys.stderr.write(" + repr(raw) + ");sys.exit(255)"
    monkeypatch.setattr(tunnel, "command", lambda: [sys.executable, "-I", "-c", code])
    with pytest.raises(dispatcher.TransportError) as caught:
        tunnel.start(timeout_seconds=1)
    assert caught.value.diagnostic == {
        "phase": "tunnel",
        "exit_status": 255,
        "timeout": False,
        "classification": classification,
    }
    assert tunnel.process is None and tunnel._stderr_diagnostic is None
    assert SECRET not in str(caught.value) + json.dumps(caught.value.diagnostic)


def test_tunnel_drains_beyond_diagnostic_bound_without_retaining_or_classifying_late_text(tunnel, monkeypatch):
    code = (
        "import sys;sys.stderr.write(" + repr(SECRET) + "*5000);"
        'sys.stderr.write("Host key verification failed.");sys.exit(17)'
    )
    monkeypatch.setattr(tunnel, "command", lambda: [sys.executable, "-I", "-c", code])
    with pytest.raises(dispatcher.TransportError) as caught:
        tunnel.start(timeout_seconds=2)
    assert caught.value.diagnostic == {
        "phase": "tunnel",
        "exit_status": 17,
        "timeout": False,
        "classification": "TUNNEL_EXITED",
    }
    assert tunnel.process is None and tunnel._stderr_diagnostic is None


def test_tunnel_timeout_reaps_owned_process(tunnel, monkeypatch, tmp_path):
    pidfile = tmp_path / "tunnel.pid"
    code = f'import os,time;open({str(pidfile)!r},"w").write(str(os.getpid()));time.sleep(20)'
    monkeypatch.setattr(tunnel, "command", lambda: [sys.executable, "-I", "-c", code])
    started = time.monotonic()
    with pytest.raises(dispatcher.TransportError) as caught:
        tunnel.start(timeout_seconds=0.3)
    assert time.monotonic() - started < 3
    assert caught.value.diagnostic == {
        "phase": "tunnel",
        "exit_status": None,
        "timeout": True,
        "classification": "TUNNEL_TIMEOUT",
    }
    assert tunnel.process is None and not running(int(pidfile.read_text()))


def test_provider_logs_extract_only_bounded_fixed_bootstrap_records(tmp_path, monkeypatch):
    secret = tmp_path / "provider-key"
    secret.write_text(SECRET)
    secret.chmod(0o600)
    events = [
        {"source": "container", "line": SECRET},
        {"source": "container", "line": json.dumps(dict(BOOTSTRAP, secret=SECRET))},
        {"source": "system", "line": json.dumps(BOOTSTRAP)},
        *[{"source": "container", "line": json.dumps(BOOTSTRAP), "ts": SECRET} for _ in range(10)],
    ]
    payload = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)

    class Stream(io.BytesIO):
        headers = SimpleNamespace(get_content_type=lambda: "text/event-stream")

    def open_response(request, timeout):
        assert request.get_header("Authorization") == "Bearer " + SECRET
        assert timeout == 1
        return Stream(payload)

    monkeypatch.setattr(runner, "build_opener", lambda *_: SimpleNamespace(open=open_response))
    result = runner.provider_bootstrap_diagnostic(SimpleNamespace(api_key_file=str(secret)), "owned-pod")
    assert result == {"status": "observed", "records": [BOOTSTRAP] * 4}
    assert SECRET not in json.dumps(result)


def test_stalled_provider_read_cannot_block_deletion_deadline(monkeypatch):
    release, started = threading.Event(), threading.Event()

    def stalled(*_args, **_kwargs):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr(runner, "read_provider_logs", stalled)
    before = time.monotonic()
    try:
        result = runner.provider_bootstrap_diagnostic(None, "owned-pod", timeout_seconds=0.05)
        assert started.is_set() and result == {"status": "timeout", "records": []}
        assert time.monotonic() - before < 0.5
    finally:
        release.set()
