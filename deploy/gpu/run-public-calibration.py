"""One supervised public calibration: create, SSH, copy results, delete.

Run the separate `guard` user service before `run`. The installed research
controller and its ledger are intentionally not involved in this profile.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import shutil
import stat
import tempfile
import time

from types import SimpleNamespace
from probe_core.gpu_acceptance_runner import RunnerError, State, verified_endpoint
from probe_core.audit import canonical_json
from probe_core.provider import DeploymentSpec
from probe_core.runpod_provider import RunPodConfig, RunPodProvider, ProviderHTTPError


def write(directory, name, value):
    target = directory / name
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(target)


def progress(stage):
    print(json.dumps({'stage': stage}), flush=True)


def cleanup(provider, worker_id, *, seconds=60):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            intent = provider._intent(worker_id)
            if intent is None:
                return {'confirmed': True, 'created': False}
            provider.stop(worker_id)
            intent = provider._intent(worker_id)
            pod_id = intent['provider_id']
            if pod_id:
                try:
                    provider.transport.request('GET', '/v2/pods/' + pod_id)
                except ProviderHTTPError as error:
                    if error.status == 404:
                        return {'confirmed': True, 'pod_id': pod_id, 'state': 'ABSENT'}
        except Exception:
            pass
        time.sleep(2)
    return {'confirmed': False}


def guard(directory, record, provider):
    write(directory, 'guard-ready.json', {'pid': os.getpid(), 'deadline': record['deadline']})
    while time.time() < record['deadline']:
        if (directory / 'deleted.json').exists():
            return
        time.sleep(2)
    result = cleanup(provider, record['worker_id'], seconds=180)
    write(directory, 'guard-stop.json', result)
    if not result['confirmed']:
        raise RuntimeError('provider deletion remains unconfirmed')


def command(args, *, timeout, stdout=None):
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=stdout is None,
                          stdout=stdout, stderr=subprocess.PIPE if stdout is not None else None,
                          timeout=timeout, check=True)


def verify_results(directory, record):
    summary = json.loads((directory / 'summary.json').read_text())
    process = json.loads((directory / 'process.json').read_text())
    manifest = json.loads((directory / 'standalone-manifest.json').read_text())
    config = json.loads((directory/'calibration.json').read_text())
    if not (manifest.get('kind') == 'standalone_public_calibration' and manifest.get('status') == 'passed'
            and manifest.get('installed_ledger_used') is False and manifest.get('heldout_data_used') is False
            and manifest.get('lifecycle_acceptance') is False and manifest.get('nested_cgroup_limits_enforced') is False
            and manifest.get('model') == config['model'] and manifest.get('operation') == {'kind':'backend_parity'}
            and manifest.get('config_sha256') == 'sha256:'+hashlib.sha256(canonical_json(config).encode()).hexdigest()
            and manifest.get('run_id') == 'public-calibration'
            and datetime.fromisoformat(manifest['absolute_deadline']) == datetime.fromtimestamp(record['deadline'], timezone.utc)
            and manifest['software']['container_image_digest'] == config['container_image_digest']
            and manifest['software']['probe_mcp_git_commit'] == config['code_git_commit']
            and manifest['hardware']['region'] == config['region']
            and manifest['hardware']['live_price_usd_per_hour'] == config['live_price_usd_per_hour']
            and manifest['hardware']['gpu_count'] == 1
            and manifest['calibration_script_sha256'] == record['calibration_script_sha256']):
        raise ValueError('standalone result provenance mismatch')
    artifacts = manifest.get('artifacts', [])
    if {a['path'] for a in artifacts} != {'summary.json', 'tensors.safetensors'} or len(artifacts) != 2:
        raise ValueError('unexpected artifact inventory')
    for artifact in artifacts:
        raw = (directory/artifact['path']).read_bytes()
        if len(raw) > 32*1024**2 or hashlib.sha256(raw).hexdigest() != artifact['sha256']:
            raise ValueError('copied artifact hash mismatch')
    if not (process['returncode'] == 0 and process['process_stopped'] is True
            and summary['suite'] == 'backend_parity_v1' and summary['passed'] is True
            and summary['scientific_evidence'] is False and len(summary['checks']) == 29
            and all(check['passed'] is True for check in summary['checks'])
            and len(set(summary['input_lengths'])) == 2):
        raise ValueError('public parity evidence did not pass')
    exact = [c for c in summary['checks'] if 'rtol' in c]
    if len(exact) != 25 or any(c['rtol'] != 0 or c['atol'] != 0 or c['max_absolute_error'] != 0 for c in exact):
        raise ValueError('exact numerical comparisons did not pass')
    raw = (directory / 'tensors.safetensors').read_bytes()
    header_length = int.from_bytes(raw[:8], 'little')
    if not 0 < header_length <= len(raw)-8:
        raise ValueError('invalid tensor artifact')
    header = json.loads(raw[8:8+header_length])
    if len(set(header)-{'__metadata__'}) != 9:
        raise ValueError('expected nine retained tensors')
    return {'checks': 29, 'exact_comparisons': 25, 'retained_tensors': 9,
            'summary_sha256': 'sha256:'+hashlib.sha256((directory/'summary.json').read_bytes()).hexdigest(),
            'tensors_sha256': 'sha256:'+hashlib.sha256(raw).hexdigest(),
            'manifest': manifest}


def run(directory, record, provider):
    deadline = record['deadline']
    if not time.time() < deadline <= time.time()+900:
        raise ValueError('original short deadline required')
    guardian = json.loads((directory/'guard-ready.json').read_text())
    if guardian['deadline'] != deadline:
        raise ValueError('guard deadline mismatch')
    os.kill(guardian['pid'], 0)
    if provider._pods():
        raise ValueError('another Pod exists; only one calibration at a time')
    with (directory/'create-started.json').open('x') as stream:
        json.dump({'at': time.time()}, stream)
    report = {'profile':'supervised_public_calibration','status':'failed',
              'scientific_evidence':False,'lifecycle_acceptance_complete':False,
              'nested_cgroup_enforcement':False}
    ssh_state = None
    try:
        # Ordinary Documents folders may be group-writable. Keep the SSH key
        # and verified host-key state in an actual private temporary directory.
        source_key = Path(record['ssh_key'])
        info = source_key.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('SSH private key must be an owned private regular file')
        ssh_state = Path(tempfile.mkdtemp(prefix='ail-public-ssh-', dir='/tmp'))
        private_key = ssh_state / 'key'
        shutil.copyfile(source_key, private_key)
        private_key.chmod(0o600)
        deployment = DeploymentSpec.model_validate(record['deployment'])
        progress('create')
        observed = provider.create(record['worker_id'], deployment, request_key=record['request_id'],
            price_ceiling_usd_per_hour=.8, storage_ceiling_usd_per_day=1,
            absolute_deadline=datetime.fromtimestamp(deadline, timezone.utc))
        pod_id = observed.provider_id
        report['pod_id'] = pod_id
        write(directory,'created.json',{'pod_id':pod_id,'deadline':deadline})
        progress('startup')
        expected_config = json.loads((directory/'calibration.json').read_text())
        config = SimpleNamespace(deployment=deployment, ssh_identity_file=str(private_key),
            expected_worker_price_usd_per_hour=expected_config['live_price_usd_per_hour'])
        request = dict(worker_id=record['worker_id'], request_id=record['request_id'],
                       observed_provider_id=pod_id, deadline=deadline)
        endpoint = None
        with State(ssh_state) as state:
            while time.time() < deadline - 340:
                try:
                    endpoint = verified_endpoint(config, request, provider, state)
                    break
                except RunnerError as error:
                    if str(error) not in {'DIRECT_SSH_ENDPOINT_UNAVAILABLE', 'PROVIDER_ENDPOINT_UNAVAILABLE',
                            'PROVIDER_LOGS_UNAVAILABLE', 'HOST_FINGERPRINT_UNAVAILABLE',
                            'SSH_VERIFICATION_COMMAND_FAILED', 'SCANNED_HOST_KEY_AMBIGUOUS'}:
                        raise
                    time.sleep(3)
        if endpoint is None:
            raise TimeoutError('startup allowance expired')
        host, port, known = endpoint['host'], endpoint['ssh_port'], endpoint['known_hosts_file']
        options=['-i',str(private_key),'-o','IdentitiesOnly=yes','-o','BatchMode=yes',
                 '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(known),
                 '-o','ConnectTimeout=8','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2']
        target='root@'+host
        command(['scp','-q','-P',str(port),*options,str(directory/'calibration.json'),target+':/run/public-calibration.json'],timeout=20)
        absolute=datetime.fromtimestamp(deadline,timezone.utc).isoformat()
        remote='/opt/probe-core/venv/bin/python -I /opt/probe/public-calibration-entrypoint.py run --config /run/public-calibration.json --absolute-deadline '+shlex.quote(absolute)
        progress('calibration')
        result=subprocess.run(['ssh','-p',str(port),*options,target,remote],stdin=subprocess.DEVNULL,
            capture_output=True,timeout=min(280,max(1,deadline-time.time()-40)))
        (directory/'remote.stdout').write_bytes(result.stdout);(directory/'remote.stderr').write_bytes(result.stderr)
        progress('collect')
        for remote_name, local_name in [('process.json','process.json'),('stdout.log','stdout.log'),('stderr.log','stderr.log'),
                ('result/summary.json','summary.json'),('result/tensors.safetensors','tensors.safetensors'),
                ('result/standalone-manifest.json','standalone-manifest.json')]:
            copied=subprocess.run(['scp','-q','-P',str(port),*options,target+':/tmp/public-calibration/'+remote_name,str(directory/local_name)],
                stdin=subprocess.DEVNULL,capture_output=True,timeout=min(20,max(1,deadline-time.time()-10)))
            if copied.returncode and result.returncode==0:
                raise RuntimeError('required result collection failed: '+local_name)
        if result.returncode:
            raise RuntimeError('remote calibration failed; retained stderr contains the diagnostic')
        report['evidence']=verify_results(directory, record)
        report['status']='passed'
    except Exception as error:
        report.update(error_type=type(error).__name__,error=str(error)[:600])
    finally:
        progress('delete')
        report['teardown']=cleanup(provider,record['worker_id'])
        if report['teardown']['confirmed']:
            write(directory,'deleted.json',report['teardown'])
        else:
            report['status']='failed'
        write(directory,'result.json',report)
        if ssh_state is not None:
            shutil.rmtree(ssh_state)
        print(json.dumps(report),flush=True)
    return 0 if report['status']=='passed' else 1


def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['guard','run']);p.add_argument('directory',type=Path)
    a=p.parse_args();directory=a.directory.resolve();record=json.loads((directory/'run.json').read_text())
    provider=RunPodProvider(RunPodConfig.model_validate_json(json.dumps(record['provider'])))
    if a.mode=='guard':
        guard(directory,record,provider)
        return 0
    return run(directory,record,provider)


if __name__=='__main__':
    raise SystemExit(main())
