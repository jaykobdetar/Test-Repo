"""Trusted image entrypoint. No controller imports, credentials, sockets or GPU."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys


def emit(prefix, data):
    sys.stdout.write(prefix + json.dumps(data, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def readonly(path):
    try:
        with open(path, "xb"):
            pass
    except OSError as error:
        return error.errno in (13, 30)
    else:
        os.unlink(path)
        return False


def attest():
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    cgroup = Path("/sys/fs/cgroup")
    denied = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        denied = True
    else:
        sock.close()
    emit("PROBE_RUNTIME:", {
        "uid": os.getuid(), "cap_eff": status["CapEff"].strip(),
        "seccomp": status["Seccomp"].strip(), "no_new_privs": status["NoNewPrivs"].strip(),
        "memory_max": (cgroup / "memory.max").read_text().strip(),
        "pids_max": (cgroup / "pids.max").read_text().strip(),
        "cpu_max": (cgroup / "cpu.max").read_text().strip(),
        "interfaces": os.listdir("/sys/class/net"), "socket_denied": denied,
        "input_readonly": readonly("/input/.write-test"), "root_readonly": readonly("/.probe-write-test"),
    })
    if sys.stdin.buffer.readline(32) != b"RUN\n":
        raise RuntimeError("controller did not accept containment attestation")


def export_outputs(limit):
    total = 0
    count = 0
    for directory, dirs, files in os.walk("/output", followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise RuntimeError("output symlinks are forbidden")
        for name in sorted(files):
            path = Path(directory) / name
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                count += 1
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > limit or count > 256:
                    raise RuntimeError("invalid or oversized output")
                relative = str(path.relative_to("/output"))
                emit("PROBE_ARTIFACT:", {"kind": "begin", "path": relative})
                digest = hashlib.sha256()
                while chunk := stream.read(65536):
                    total += len(chunk)
                    if total > limit:
                        raise RuntimeError("output limit exceeded")
                    digest.update(chunk)
                    emit("PROBE_ARTIFACT:", {"kind": "chunk", "path": relative, "data": base64.b64encode(chunk).decode("ascii")})
                emit("PROBE_ARTIFACT:", {"kind": "end", "path": relative, "sha256": digest.hexdigest()})


def main():
    attest()
    child = subprocess.Popen([sys.executable, "-I", "-B", "/input/code.py"], start_new_session=True)
    code = child.wait()
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if code == 0:
        export_outputs(int(os.environ["PROBE_OUTPUT_LIMIT"]))
    return code if 0 <= code <= 255 else 128 + abs(code)


if __name__ == "__main__":
    raise SystemExit(main())
