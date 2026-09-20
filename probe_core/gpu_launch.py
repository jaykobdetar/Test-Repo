"""Trusted image bootstrap; refuses execution without actual delegated cgroups.

Root supervises SSH. The numerical supervisor runs as the fixed unprivileged
worker UID. No provider credential or paid action exists in this entry point.
"""
import json
from functools import partial
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from .worker_contracts import WorkerConfig

WORKER_UID = 10001


def _prepare_worker_cgroups(config: WorkerConfig):
    from .pod_bootstrap import prepare

    # This path is created inside the verified, provider-delegated namespace.
    # An arbitrary writable host cgroup is not an equivalent launch contract.
    if config.cgroup_directory != "/sys/fs/cgroup/probe-jobs":
        raise ValueError("RunPod worker cgroup_directory must be /sys/fs/cgroup/probe-jobs")
    return prepare(root="/sys/fs/cgroup", worker_uid=WORKER_UID, worker_gid=WORKER_UID)


def _private_worker_file(path):
    info = path.stat()
    if path.is_symlink() or info.st_uid != WORKER_UID or info.st_mode & 0o077:
        raise ValueError("worker config and token must be private mode0600 files owned by UID10001")


def check_worker_paths(config: WorkerConfig, *, token_path: Path):
    """Exercise kernel permissions as the worker, without importing torch/model."""
    from .worker import _path
    model_root = Path(config.model_directory)
    for path in (model_root, *model_root.parents):
        if path.is_symlink():
            raise ValueError("model parent paths cannot be symlinks")
    if model_root.stat().st_uid == os.geteuid() or os.access(model_root, os.W_OK, effective_ids=True):
        raise ValueError("worker must not own or be able to modify the model directory")
    for asset in config.assets:
        path = _path(model_root, asset.path)
        if path.stat().st_uid == os.geteuid() or os.access(path, os.W_OK, effective_ids=True):
            raise ValueError("worker must not own or be able to modify model assets")
        with path.open("rb") as stream:
            stream.read(1)
    for dataset in config.datasets:
        path = Path(dataset.path)
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ValueError("dataset paths cannot be symlinks")
        if path.stat().st_uid == os.geteuid() or os.access(path, os.W_OK, effective_ids=True):
            raise ValueError("worker must not own or be able to modify dataset assets")
        with path.open("rb") as stream:
            stream.read(1)
    with token_path.open() as stream:
        secret = stream.read(514).strip()
    if not 32 <= len(secret) <= 512:
        raise ValueError("worker token file has an invalid length")
    for value in (config.tensor_directory, config.output_directory):
        directory = Path(value)
        if any(item.is_symlink() for item in (directory, *directory.parents)):
            raise ValueError("worker writable paths cannot be symlinks")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("tensor and attempt directories must be worker-owned and private")
        descriptor, name = tempfile.mkstemp(prefix=".probe-access-", dir=directory)
        try:
            os.write(descriptor, b"permission-check\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            Path(name).unlink()
    return {"kind": "worker_path_preflight", "uid": os.geteuid(), "passed": True,
            "model_loaded": False, "model_assets_checked": len(config.assets), "datasets_checked": len(config.datasets)}


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--check-paths":
        if os.geteuid() != WORKER_UID:
            raise SystemExit("path preflight must actually run as UID10001")
        config_path, token_path = Path(sys.argv[2]), Path(sys.argv[3])
        _private_worker_file(config_path)
        _private_worker_file(token_path)
        config = WorkerConfig.model_validate_json(config_path.read_text())
        print(json.dumps(check_worker_paths(config, token_path=token_path)), flush=True)
        return
    if os.geteuid() != 0:
        raise SystemExit("the image bootstrap needs container root to separate SSH and worker identities")
    if any(os.environ.get(key) for key in ("RUNPOD_API_KEY", "HF_TOKEN", "AWS_SECRET_ACCESS_KEY")):
        raise SystemExit("management or download credentials do not belong on the execution image")
    config_path = Path(os.environ.get("PROBE_WORKER_CONFIG", "/workspace/probe/config/worker.json"))
    token_path = Path(os.environ.get("PROBE_WORKER_TOKEN_FILE", "/workspace/probe/config/worker-token"))
    _private_worker_file(config_path)
    _private_worker_file(token_path)
    config = WorkerConfig.model_validate_json(config_path.read_text())
    provenance = json.loads(Path("/opt/probe-core/build-provenance.json").read_text())
    if config.device != "cuda:0" or config.provider_backend != "runpod" or config.code_git_commit != provenance["source_commit"]:
        raise SystemExit("worker configuration must match this reviewed CUDA image and actual source commit")
    from .pod_bootstrap import enter_supervisor_and_drop
    scope = _prepare_worker_cgroups(config)
    worker_identity = partial(enter_supervisor_and_drop, scope)
    public_key = os.environ.pop("PUBLIC_KEY", "").strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]+)?", public_key):
        raise SystemExit("PUBLIC_KEY must be one trusted Ed25519 public key")
    directory = Path("/root/.ssh")
    directory.mkdir(exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    authorized = directory / "authorized_keys"
    authorized.write_text(public_key+"\n")
    authorized.chmod(0o600)
    subprocess.run(["ssh-keygen", "-l", "-f", str(authorized)], check=True)
    subprocess.run(["ssh-keygen", "-A"], check=True)
    subprocess.run(["ssh-keygen", "-l", "-f", "/etc/ssh/ssh_host_ed25519_key.pub"], check=True)
    ssh_config = Path("/etc/ssh/sshd_config.probe-worker")
    ssh_config.write_text("Port 22\nHostKey /etc/ssh/ssh_host_ed25519_key\nPermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\nUsePAM yes\nAllowTcpForwarding local\nPermitOpen 127.0.0.1:8080\nAllowAgentForwarding no\nX11Forwarding no\nSubsystem sftp internal-sftp\n")
    # Run the exact diagnostic as the worker identity, not root: root write
    # access would not prove that the numerical supervisor can enforce limits.
    worker_environment = dict(os.environ, HOME="/home/probe-worker", USER="probe-worker", LOGNAME="probe-worker")
    paths = subprocess.run([sys.executable, "-m", "probe_core.gpu_launch", "--check-paths", str(config_path), str(token_path)],
                           preexec_fn=worker_identity, env=worker_environment, timeout=30, check=False)
    if paths.returncode != 0:
        raise SystemExit("worker asset permissions failed before model loading; external controller must stop the Pod")
    preflight = subprocess.run([sys.executable, "/opt/probe/diagnose.py", "--cgroup-root", config.cgroup_directory],
                               preexec_fn=worker_identity, env=worker_environment, timeout=45, check=False)
    if preflight.returncode != 0:
        raise SystemExit("worker prerequisites failed; the external controller must stop this paid Pod")
    containment = subprocess.run([sys.executable, "/opt/probe/accept-resources.py", "--cgroup-root", config.cgroup_directory],
                                 preexec_fn=worker_identity, env=worker_environment, timeout=40, check=False)
    if containment.returncode != 0:
        raise SystemExit("worker resource enforcement failed under load; external controller must delete the Pod")
    stopped = False
    def terminate(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    ssh = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(ssh_config)], start_new_session=True)
    def start_worker():
        process = subprocess.Popen([sys.executable, "-m", "probe_core.worker", "--config", str(config_path), "--token-file", str(token_path), "--port", "8080"],
                                   preexec_fn=worker_identity, env=worker_environment, start_new_session=True)
        Path("/run/probe-worker-supervisor.pid").write_text(str(process.pid)+"\n")
        print(json.dumps({"worker_supervisor_pid": process.pid, "execution_authority_renewed": False}), flush=True)
        return process
    worker = start_worker()
    restarts = 0
    try:
        while not stopped and ssh.poll() is None:
            if worker.poll() is not None:
                if restarts >= 3:
                    break
                # Restart only the local supervisor. Its persisted exact attempt
                # is adopted; no job POST or provider start is replayed.
                restarts += 1
                worker = start_worker()
            time.sleep(0.1)
    finally:
        # Worker SIGTERM runs Supervisor.close(), which kills each execution's
        # separate process group before the supervisor exits.
        for process in (worker, ssh):
            if process.poll() is None:
                process.terminate()
        for process in (worker, ssh):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    raise SystemExit(0 if stopped else 1)


if __name__ == "__main__":
    main()
