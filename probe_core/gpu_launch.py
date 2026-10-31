"""Trusted image bootstrap; refuses execution without actual delegated cgroups.

Root supervises SSH. The numerical supervisor runs as the fixed unprivileged
worker UID. No provider credential or paid action exists in this entry point.
"""

import json
from datetime import datetime, timezone
from functools import partial
import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

from .worker_contracts import WorkerConfig
from .audit import canonical_json

ROOT_UID = 0
WORKER_UID = 10001
BOOTSTRAP_STATE = Path("/run/probe-bootstrap-state.json")
CONFIGURED = Path("/run/probe-worker-configured.json")
CONFIG_BUNDLE = Path("/run/probe-worker-bootstrap.json")
CONFIG_ROOT = Path("/workspace/probe/config")
BAKED_ROOT = Path("/opt/probe-assets")
BAKED_MANIFEST = Path("/opt/probe-core/public-assets.json")
PROVENANCE = Path("/opt/probe-core/build-provenance.json")
SUPERVISOR_PID = Path("/run/probe-worker-supervisor.pid")
TRUSTED_PYTHON = "/opt/probe-core/venv/bin/python"
FIXED_PATH = (
    "/opt/probe-core/venv/bin:/usr/local/nvidia/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
GPU_LIBRARY_DIRECTORIES = (
    "/usr/local/nvidia/lib",
    "/usr/local/nvidia/lib64",
    "/usr/local/cuda/lib64",
    "/usr/local/cuda/compat",
    "/usr/lib/x86_64-linux-gnu",
)
BOOTSTRAP_BINDINGS = (
    "PROBE_ABSOLUTE_DEADLINE",
    "PROBE_WORKER_ID",
    "PROBE_REQUEST_ID",
    "PROBE_CONFIGURATION_HASH",
    "PUBLIC_KEY",
)


def _mode(arguments):
    if not arguments:
        return "bootstrap"
    if len(arguments) == 2 and arguments[0] == "--configure":
        return "configure"
    if len(arguments) == 3 and arguments[0] == "--check-paths":
        return "check-paths"
    raise ValueError("unsupported bootstrap command")


def _gpu_environment(source):
    result = {}
    visible = source.get("CUDA_VISIBLE_DEVICES")
    if visible is not None:
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,3}|GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", visible):
            raise ValueError("invalid single-GPU device selector")
        result["CUDA_VISIBLE_DEVICES"] = visible
    # NVIDIA's runtime has already mounted the devices and driver libraries.
    # Preserve only standard root-controlled library locations, never an
    # inherited preload/search path or pre-start NVIDIA runtime directives.
    libraries = []
    for value in GPU_LIBRARY_DIRECTORIES:
        path = Path(value)
        if not path.exists():
            continue
        resolved = path.resolve(strict=True)
        for parent in (resolved, *resolved.parents):
            info = parent.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o022:
                raise ValueError("GPU library directory is not root controlled")
        libraries.append(str(resolved))
    if libraries:
        result["LD_LIBRARY_PATH"] = ":".join(dict.fromkeys(libraries))
    return result


def _environment(mode, source=None):
    source = os.environ if source is None else source
    worker = mode == "check-paths" or mode == "worker"
    result = {
        "PATH": FIXED_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/home/probe-worker" if worker else "/root",
        "USER": "probe-worker" if worker else "root",
        "LOGNAME": "probe-worker" if worker else "root",
    }
    if mode != "ssh":
        result.update(_gpu_environment(source))
    if mode == "bootstrap":
        result.update({key: source[key] for key in BOOTSTRAP_BINDINGS if key in source})
    if worker:
        result.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            TOKENIZERS_PARALLELISM="false",
            WANDB_MODE="disabled",
            CUBLAS_WORKSPACE_CONFIG=":4096:8",
        )
    return result


def _initial_environment():
    with open("/proc/self/environ", "rb") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError("initial process environment exceeds bound")
    fields = [entry.split(b"=", 1) for entry in raw.split(b"\0") if entry]
    if any(len(entry) != 2 for entry in fields):
        raise ValueError("invalid initial process environment")
    result = {os.fsdecode(key): os.fsdecode(value) for key, value in fields}
    if len(result) != len(fields):
        raise ValueError("duplicate initial process environment key")
    return result


def _fresh_environment(mode, arguments):
    clean = _environment(mode)
    # unsetenv alone leaves the original bytes readable through /proc. A fresh
    # exec also removes them from the root bootstrap's inherited memory. There
    # is no environment marker that can bypass this equality check.
    if (
        dict(os.environ) != clean
        or _initial_environment() != clean
        or sys.executable != TRUSTED_PYTHON
        or not sys.flags.isolated
    ):
        os.execve(TRUSTED_PYTHON, [TRUSTED_PYTHON, "-I", "-m", "probe_core.gpu_launch", *arguments], clean)
        raise RuntimeError("clean bootstrap exec unexpectedly returned")
    _credential_guard(os.environ)


def _credential_guard(environment):
    if any(key.startswith(("RUNPOD_", "AWS_")) or key in {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"} for key in environment):
        raise ValueError("unexpected provider or download credential residue")


def _root_parent_environment_private():
    parent = Path("/proc") / str(os.getppid())
    if parent.stat().st_uid != ROOT_UID:
        raise ValueError("path preflight requires its root bootstrap parent")
    try:
        descriptor = os.open(parent / "environ", os.O_RDONLY | os.O_NOFOLLOW)
    except PermissionError:
        return
    else:
        os.close(descriptor)
        raise ValueError("worker can read root bootstrap environment")


def _root_file(path, *, maximum=1024 * 1024):
    path = Path(path)
    if any(p.is_symlink() for p in path.parents):
        raise ValueError("bootstrap input has a symlink parent")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ROOT_UID
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or info.st_size > maximum
        ):
            raise ValueError("bootstrap input is not a bounded root-owned file")
        return stream.read(maximum + 1)


def _publish(path, raw, *, uid=None, gid=None, mode=0o600):
    uid = ROOT_UID if uid is None else uid
    gid = ROOT_UID if gid is None else gid
    fd, temporary = tempfile.mkstemp(prefix=".probe-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _configuration_digest(value):
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def validate_baked_config(body, provenance, manifest):
    """The authenticated bootstrap may select only assets baked in this image."""
    config = WorkerConfig.model_validate_json(canonical_json(body))
    if (
        config.device != "cuda:0"
        or config.backend != "nnsight"
        or config.provider_backend != "runpod"
        or config.code_git_commit != provenance["source_commit"]
        or config.environment_lock_path != "/opt/probe-core/uv.lock"
        or config.cgroup_directory != "/sys/fs/cgroup/probe-jobs"
        or config.tensor_directory != "/workspace/probe/tensors"
        or config.output_directory != "/workspace/probe/attempts"
    ):
        raise ValueError("configuration differs from the reviewed disposable worker profile")
    assets = {item["path"]: item["sha256"] for item in manifest["assets"]}
    root = Path(config.model_directory)
    if not root.is_relative_to(BAKED_ROOT / "models") or ".." in root.parts:
        raise ValueError("model must come from the baked public asset directory")
    requested = {str((root / item.path).relative_to(BAKED_ROOT)): item.sha256 for item in config.assets}
    for dataset in config.datasets:
        path = Path(dataset.path)
        if not path.is_relative_to(BAKED_ROOT / "datasets") or ".." in path.parts:
            raise ValueError("dataset must come from the baked public asset directory")
        requested[str(path.relative_to(BAKED_ROOT))] = dataset.sha256
    if requested != assets:
        raise ValueError("configuration asset hashes differ from the immutable baked manifest")
    return config


def configure(path=CONFIG_BUNDLE):
    """Install one data-only config over authenticated SSH, without executing a job."""
    if os.geteuid() != ROOT_UID or Path(path) != CONFIG_BUNDLE:
        raise ValueError("configuration requires the fixed root-only staging file")
    raw = _root_file(path)
    if Path(path).stat().st_mode & 0o077:
        raise ValueError("configuration bundle must be private")
    body = json.loads(raw)
    if set(body) != {"schema_version", "worker_config", "bearer_token"} or body["schema_version"] != 1:
        raise ValueError("invalid configuration envelope")
    token = body["bearer_token"]
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_+=/-]{32,512}", token):
        raise ValueError("invalid worker token")
    state = json.loads(_root_file(BOOTSTRAP_STATE))
    if time.time() >= state["deadline"] - 30:
        raise ValueError("the original Pod deadline is too close or expired")
    validate_baked_config(
        body["worker_config"], json.loads(_root_file(PROVENANCE)), json.loads(_root_file(BAKED_MANIFEST))
    )
    receipt = {
        "schema_version": 1,
        "configured": True,
        "worker_config_sha256": _configuration_digest(body["worker_config"]),
        "bundle_sha256": _configuration_digest(body),
    }
    if CONFIGURED.exists():
        previous = json.loads(_root_file(CONFIGURED))
        if previous != {**receipt, "bootstrap": state}:
            raise ValueError("worker is already bound to a different configuration")
        Path(path).unlink()
        return receipt
    # Root retains the parent; only the numerical identity owns its private
    # execution files. Atomic ready publication is the sole startup signal.
    parent = CONFIG_ROOT.parent
    for directory in (parent.parent, parent):
        if not directory.exists():
            directory.mkdir(mode=0o755)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o022 or directory.is_symlink():
            raise ValueError("worker workspace parent is not trusted")
    for name in ("config", "tensors", "attempts"):
        directory = parent / name
        directory.mkdir(mode=0o700)
        os.chown(directory, WORKER_UID, WORKER_UID)
    _publish(
        CONFIG_ROOT / "worker.json", canonical_json(body["worker_config"]).encode(), uid=WORKER_UID, gid=WORKER_UID
    )
    _publish(CONFIG_ROOT / "worker-token", (token + "\n").encode(), uid=WORKER_UID, gid=WORKER_UID)
    _publish(CONFIGURED, canonical_json({**receipt, "bootstrap": state}).encode())
    Path(path).unlink()
    return receipt


def _bootstrap_deadline():
    deadline = datetime.fromisoformat(os.environ["PROBE_ABSOLUTE_DEADLINE"])
    if deadline.tzinfo is None or not 0 < deadline.timestamp() - time.time() <= 900:
        raise ValueError("bootstrap requires the original bounded controller deadline")
    state = {"deadline": deadline.timestamp()}
    for name in ("PROBE_WORKER_ID", "PROBE_REQUEST_ID", "PROBE_CONFIGURATION_HASH"):
        value = os.environ[name]
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", value):
            raise ValueError("invalid controller binding")
        state[name] = value
    if BOOTSTRAP_STATE.exists():
        raise ValueError("bootstrap was already initialized")
    _publish(BOOTSTRAP_STATE, canonical_json(state).encode())
    return deadline.timestamp()


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
    return {
        "kind": "worker_path_preflight",
        "uid": os.geteuid(),
        "passed": True,
        "model_loaded": False,
        "model_assets_checked": len(config.assets),
        "datasets_checked": len(config.datasets),
    }


def _run_numerical_worker(scope, deadline, ssh, stopped):
    from .pod_bootstrap import enter_supervisor_and_drop

    worker_identity = partial(enter_supervisor_and_drop, scope)
    while not CONFIGURED.exists():
        if stopped() or ssh.poll() is not None or time.time() >= deadline - 30:
            raise ValueError("configuration was not staged within the original allowance")
        time.sleep(0.1)
    config_path, token_path = CONFIG_ROOT / "worker.json", CONFIG_ROOT / "worker-token"
    _private_worker_file(config_path)
    _private_worker_file(token_path)
    body = json.loads(config_path.read_text())
    config = validate_baked_config(body, json.loads(_root_file(PROVENANCE)), json.loads(_root_file(BAKED_MANIFEST)))
    marker = json.loads(_root_file(CONFIGURED))
    token = token_path.read_text().strip()
    if (
        marker["worker_config_sha256"] != _configuration_digest(body)
        or marker["bundle_sha256"]
        != _configuration_digest({"schema_version": 1, "worker_config": body, "bearer_token": token})
        or marker["bootstrap"] != json.loads(_root_file(BOOTSTRAP_STATE))
    ):
        raise ValueError("staged configuration changed before worker startup")
    worker_environment = _environment("worker")
    _credential_guard(worker_environment)
    gates = [
        ([TRUSTED_PYTHON, "-I", "-m", "probe_core.gpu_launch", "--check-paths", str(config_path), str(token_path)], 30),
        ([TRUSTED_PYTHON, "-I", "/opt/probe/diagnose.py", "--cgroup-root", config.cgroup_directory], 45),
        ([TRUSTED_PYTHON, "-I", "/opt/probe/accept-resources.py", "--cgroup-root", config.cgroup_directory], 40),
    ]
    for command, maximum in gates:
        remaining = deadline - time.time() - 15
        if remaining <= 0 or stopped():
            raise ValueError("original allowance expired before resource acceptance")
        result = subprocess.run(
            command, preexec_fn=worker_identity, env=worker_environment, timeout=min(maximum, remaining), check=False
        )
        if result.returncode != 0:
            raise ValueError("worker permissions or real resource acceptance failed before inference")

    def start_worker():
        process = subprocess.Popen(
            [
                TRUSTED_PYTHON,
                "-I",
                "-m",
                "probe_core.worker",
                "--config",
                str(config_path),
                "--token-file",
                str(token_path),
                "--port",
                "8080",
            ],
            preexec_fn=worker_identity,
            env=worker_environment,
            start_new_session=True,
        )
        SUPERVISOR_PID.write_text(str(process.pid) + "\n")
        print(json.dumps({"worker_supervisor_pid": process.pid, "execution_authority_renewed": False}), flush=True)
        return process

    worker = start_worker()
    restarts = 0
    try:
        while not stopped() and ssh.poll() is None and time.time() < deadline:
            if worker.poll() is not None:
                if restarts >= 3:
                    break
                restarts += 1
                worker = start_worker()
            time.sleep(0.1)
    finally:
        if worker.poll() is None:
            worker.terminate()
        try:
            worker.wait(timeout=15)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)


def main():
    mode = _mode(sys.argv[1:])
    _fresh_environment(mode, sys.argv[1:])
    if mode == "configure":
        print(canonical_json(configure(Path(sys.argv[2]))), flush=True)
        return
    if mode == "check-paths":
        if os.geteuid() != WORKER_UID:
            raise SystemExit("path preflight must actually run as UID10001")
        _root_parent_environment_private()
        config_path, token_path = Path(sys.argv[2]), Path(sys.argv[3])
        _private_worker_file(config_path)
        _private_worker_file(token_path)
        config = WorkerConfig.model_validate_json(config_path.read_text())
        print(json.dumps(check_worker_paths(config, token_path=token_path)), flush=True)
        return
    if os.geteuid() != ROOT_UID:
        raise SystemExit("the image bootstrap needs container root to separate SSH and worker identities")
    _credential_guard(os.environ)
    deadline = _bootstrap_deadline()
    from types import SimpleNamespace

    scope = _prepare_worker_cgroups(SimpleNamespace(cgroup_directory="/sys/fs/cgroup/probe-jobs"))
    public_key = os.environ.pop("PUBLIC_KEY", "").strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]+)?", public_key):
        raise SystemExit("PUBLIC_KEY must be one trusted Ed25519 public key")
    directory = Path("/root/.ssh")
    directory.mkdir(exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    authorized = directory / "authorized_keys"
    authorized.write_text(public_key + "\n")
    authorized.chmod(0o600)
    ssh_environment = _environment("ssh")
    subprocess.run(["/usr/bin/ssh-keygen", "-l", "-f", str(authorized)], check=True, env=ssh_environment)
    subprocess.run(["/usr/bin/ssh-keygen", "-A"], check=True, env=ssh_environment)
    subprocess.run(
        ["/usr/bin/ssh-keygen", "-l", "-f", "/etc/ssh/ssh_host_ed25519_key.pub"], check=True, env=ssh_environment
    )
    ssh_config = Path("/etc/ssh/sshd_config.probe-worker")
    ssh_config.write_text(
        "Port 22\nHostKey /etc/ssh/ssh_host_ed25519_key\nPermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\nUsePAM yes\nAllowTcpForwarding local\nPermitOpen 127.0.0.1:8080\nAllowAgentForwarding no\nX11Forwarding no\nSubsystem sftp internal-sftp\n"
    )
    stopped = False

    def terminate(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    ssh = subprocess.Popen(
        ["/usr/sbin/sshd", "-D", "-e", "-f", str(ssh_config)], start_new_session=True, env=ssh_environment
    )
    try:
        _run_numerical_worker(scope, deadline, ssh, lambda: stopped)
    finally:
        if ssh.poll() is None:
            ssh.terminate()
        try:
            ssh.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ssh.kill()
            ssh.wait(timeout=5)


def _failure_receipt(error):
    line = None
    trace = error.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == __file__:
            line = trace.tb_lineno
        trace = trace.tb_next
    return {
        "status": "failed",
        "code": "WORKER_BOOTSTRAP_FAILED",
        "error_type": type(error).__name__,
        "bootstrap_line": line,
    }


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(canonical_json(_failure_receipt(error)), flush=True)
        raise SystemExit(1) from None
