"""SSH and a one-shot timeout for operator-supervised public calibration only."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

PYTHON = '/opt/probe-core/venv/bin/python'
SCRIPT = '/opt/probe/public-calibration.py'
ROOT = Path('/tmp/public-calibration')


def clean_environment():
    from probe_core.gpu_launch import _environment
    return _environment('worker', {'CUDA_VISIBLE_DEVICES': '0'})


def deadline_seconds(value):
    deadline = datetime.fromisoformat(value)
    if deadline.tzinfo is None or not 0 < deadline.timestamp() - time.time() <= 900:
        raise ValueError('a live absolute deadline of at most 900 seconds is required')
    return deadline.timestamp()


def drop_identity():
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def run(config, absolute_deadline):
    deadline = deadline_seconds(absolute_deadline)
    ROOT.mkdir(mode=0o700)  # One execution only; never reuse or overwrite outputs.
    os.chown(ROOT, 10001, 10001)
    source = Path(config)
    if not source.is_file() or source.is_symlink() or source.stat().st_size > 131072:
        raise ValueError('invalid calibration configuration')
    data = json.loads(source.read_text())
    configuration = Path('/run/probe-public-calibration.json')
    configuration.write_text(json.dumps(data))
    configuration.chmod(0o444)
    command = [PYTHON, '-I', SCRIPT, '--config', str(configuration),
               '--absolute-deadline', absolute_deadline,
               '--run-id', 'public-calibration', '--output', str(ROOT / 'result')]
    with (ROOT / 'stdout.log').open('wb') as out, (ROOT / 'stderr.log').open('wb') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            env=clean_environment(), preexec_fn=drop_identity, start_new_session=True, cwd=ROOT)
        try:
            code = process.wait(timeout=min(250, max(1, deadline - time.time() - 30)))
        except subprocess.TimeoutExpired:
            stop_group(process)
            code = 124
        finally:
            # Also remove unexpected surviving children in this execution group.
            stop_group(process)
    receipt = {'schema_version': 1, 'profile': 'supervised_public_calibration',
               'returncode': code, 'process_stopped': True,
               'finished_at': datetime.now(timezone.utc).isoformat(),
               'nested_cgroup_enforcement': False, 'lifecycle_acceptance_complete': False}
    (ROOT / 'process.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)
    return code


def serve():
    public_key = os.environ.get('PUBLIC_KEY', '').strip()
    deadline = deadline_seconds(os.environ['PROBE_ABSOLUTE_DEADLINE'])
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]+)?', public_key):
        raise ValueError('one dedicated Ed25519 public key is required')
    # Provider-injected environment does not reach SSH or numerical children.
    os.environ.clear()
    os.environ.update(clean_environment())
    sshroot = Path('/root/.ssh')
    sshroot.mkdir(mode=0o700, exist_ok=True)
    sshroot.chmod(0o700)
    keys = sshroot / 'authorized_keys'
    keys.write_text(public_key + '\n')
    keys.chmod(0o600)
    Path('/run/sshd').mkdir(exist_ok=True)
    subprocess.run(['/usr/bin/ssh-keygen', '-A'], check=True)
    subprocess.run(['/usr/bin/ssh-keygen', '-l', '-f', '/etc/ssh/ssh_host_ed25519_key.pub'], check=True)
    config = Path('/etc/ssh/sshd_config.public-calibration')
    config.write_text('Port 22\nHostKey /etc/ssh/ssh_host_ed25519_key\n'
        'PermitRootLogin prohibit-password\nPasswordAuthentication no\n'
        'KbdInteractiveAuthentication no\nPubkeyAuthentication yes\nUsePAM yes\n'
        'AllowTcpForwarding no\nAllowAgentForwarding no\nX11Forwarding no\n'
        'Subsystem sftp internal-sftp\n')
    from probe_core.gpu_launch import _environment
    process = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(config)],
                               env=_environment('ssh', {}), start_new_session=True)
    print(json.dumps({'status': 'ready', 'profile': 'supervised_public_calibration',
                      'nested_cgroups_required': False}), flush=True)
    try:
        process.wait(timeout=max(1, deadline - time.time()))
    except subprocess.TimeoutExpired:
        pass
    finally:
        stop_group(process)


def main():
    if os.geteuid() != 0:
        raise ValueError('entrypoint requires container root; numerical code runs as UID10001')
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['ssh', 'run'], default='ssh', nargs='?')
    parser.add_argument('--config')
    parser.add_argument('--absolute-deadline')
    args = parser.parse_args()
    if args.mode == 'run':
        if not args.config or not args.absolute_deadline:
            parser.error('run requires a configuration and deadline')
        return run(args.config, args.absolute_deadline)
    serve()
    return 0


if __name__ == '__main__':
    sys.exit(main())
