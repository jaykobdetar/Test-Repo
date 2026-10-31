"""One trusted public BF16 parity run; not managed-worker lifecycle acceptance.

The root launcher supplies a clean UID10001 process and an independent hard
deadline. This command never starts a supervisor, opens a ledger, or requests
compute. The provider Pod is the disposable resource boundary.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import traceback
from typing import Annotated

from pydantic import Field, TypeAdapter, model_validator

from probe_core.audit import canonical_json
from probe_core.gpu_acceptance import fixed_plan
from probe_core.gpu_launch import _environment, _initial_environment
from probe_core.model_assets import canonical_locks
from probe_core.rpc import decode
from probe_core.schemas import FrozenModel, GitSHA, Identifier, ModelIdentity, SHA256, UTCTimestamp
from probe_core.worker import WorkerEngine, _json_write, prompt_set_hash, sha256_file
from probe_core.worker_contracts import DatasetAsset, ExecutionRequest, FileDigest, PromptDataset, ScienceMetadata

UID = 10001
ASSETS = Path('/opt/probe-assets')
BAKED_MANIFEST = Path('/opt/probe-core/public-assets.json')
PROVENANCE = Path('/opt/probe-core/build-provenance.json')
WORKSPACE = Path('/tmp/public-calibration')
DATASET_SHA = 'sha256:6867f3b38b8587c8f7b71955bbb597eac27445ee69b2aa033904619df4df5bbe'
PROMPTS = ('public-short', 'public-long')
UTC = timezone.utc


class CalibrationRefused(ValueError):
    """Fixed public reason codes only."""


def require(condition, code):
    if not condition:
        raise CalibrationRefused(code)


class PublicCalibrationConfig(FrozenModel):
    """Numerical configuration without a fictitious delegated cgroup claim."""
    model: ModelIdentity
    assets: Annotated[tuple[FileDigest, ...], Field(min_length=1, max_length=128)]
    datasets: Annotated[tuple[DatasetAsset, ...], Field(min_length=1, max_length=1)]
    code_git_commit: GitSHA
    container_image_digest: SHA256
    region: Identifier
    live_price_usd_per_hour: Annotated[float, Field(strict=True, gt=0, lt=1.50, allow_inf_nan=False)]

    @model_validator(mode='after')
    def fixed_public_identity(self):
        lock = next((item for item in canonical_locks() if item.repo == self.model.repo), None)
        require(lock is not None and self.model.revision_sha == self.model.tokenizer_revision == lock.revision
                and self.model.dtype == 'bfloat16' and self.model.quantized is False,
                'CANONICAL_BF16_MODEL_REQUIRED')
        require(self.model.local_weight_hashes == tuple(item.sha256 for item in lock.files
                                                       if item.path.endswith('.safetensors')),
                'CANONICAL_WEIGHT_HASHES_REQUIRED')
        require(len({item.path for item in self.assets}) == len(self.assets)
                and {item.path for item in self.assets} == {item.path for item in lock.files},
                'EXACT_MODEL_INVENTORY_REQUIRED')
        if not self.model.repo.endswith('-Base'):
            require(type(self.model.thinking_mode) is bool and self.model.chat_template_hash is not None,
                    'POSTTRAINED_TEMPLATE_MODE_REQUIRED')
        require(self.datasets[0].path == str(ASSETS / 'datasets/public-calibration-prompts.json')
                and self.datasets[0].sha256 == DATASET_SHA, 'FIXED_PUBLIC_DATASET_REQUIRED')
        return self

    @property
    def model_directory(self):
        return str(ASSETS / 'models' / self.model.repo.split('/')[-1] / self.model.revision_sha)

    @property
    def tensor_directory(self):
        return str(WORKSPACE / 'tensors')

    backend = property(lambda self: 'nnsight')
    device = property(lambda self: 'cuda:0')
    provider_backend = property(lambda self: 'runpod')
    environment_lock_path = property(lambda self: '/opt/probe-core/uv.lock')
    max_tensor_bytes = property(lambda self: 32 * 1024**2)


def immutable_file(path, *, maximum=1024*1024):
    path = Path(path)
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'INPUT_PARENT_NOT_IMMUTABLE')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022 and info.st_size <= maximum, 'INPUT_NOT_IMMUTABLE')
        raw = stream.read(maximum + 1)
        require(len(raw) == info.st_size and len(raw) <= maximum, 'INPUT_CHANGED')
        return raw


def validate_assets(config):
    provenance = decode(immutable_file(PROVENANCE))
    require(provenance.get('source_commit') == config.code_git_commit, 'IMAGE_SOURCE_MISMATCH')
    require(provenance.get('environment_lock_hash') == sha256_file(Path(config.environment_lock_path)),
            'ENVIRONMENT_LOCK_MISMATCH')
    baked = decode(immutable_file(BAKED_MANIFEST))
    require(type(baked) is dict and baked.get('schema_version') == 1
            and type(baked.get('assets')) is list, 'BAKED_MANIFEST_INVALID')
    actual = {item['path']: item['sha256'] for item in baked['assets']}
    expected = {str((Path(config.model_directory) / item.path).relative_to(ASSETS)): item.sha256
                for item in config.assets}
    expected['datasets/public-calibration-prompts.json'] = DATASET_SHA
    require(len(actual) == len(baked['assets']) and actual == expected, 'BAKED_ASSET_HASHES_MISMATCH')
    # Inspect permissions without copying the multi-GB weights. The unchanged
    # numerical engine hashes every configured file before loading the model.
    for relative in actual:
        path = ASSETS / relative
        for parent in path.parents:
            info = parent.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                    'ASSET_PARENT_NOT_IMMUTABLE')
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022, 'ASSET_NOT_IMMUTABLE')
    dataset_raw = immutable_file(config.datasets[0].path)
    require('sha256:' + hashlib.sha256(dataset_raw).hexdigest() == DATASET_SHA, 'PUBLIC_DATASET_HASH_MISMATCH')
    dataset = PromptDataset.model_validate_json(dataset_raw)
    require(tuple(item.prompt_id for item in dataset.prompts) == PROMPTS, 'PUBLIC_PROMPT_SET_MISMATCH')
    return dataset


class PublicCalibrationEngine(WorkerEngine):
    """Reuse numerical methods unchanged, with a separate honest entry contract."""
    def __init__(self, config):
        self.config = PublicCalibrationConfig.model_validate_json(config.model_dump_json())
        self.model = self.tokenizer = None
        self._verify_model_bundle()


def process_boundary():
    require(os.getresuid() == (UID, UID, UID) and os.getresgid() == (UID, UID, UID)
            and set(os.getgroups()) <= {UID}, 'DEDICATED_NUMERICAL_IDENTITY_REQUIRED')
    require(sys.flags.isolated and dict(os.environ) == _environment('worker')
            and _initial_environment() == dict(os.environ), 'CLEAN_EXEC_ENVIRONMENT_REQUIRED')


def execution_request(config, dataset, run_id, absolute_deadline, now):
    run_id = TypeAdapter(Identifier).validate_python(run_id)
    absolute_deadline = TypeAdapter(UTCTimestamp).validate_python(absolute_deadline)
    require(0 < (absolute_deadline - now).total_seconds() <= 900, 'ABSOLUTE_DEADLINE_INVALID')
    case = fixed_plan(config.model, run_id, DATASET_SHA, prompt_set_hash(dataset, PROMPTS), PROMPTS).cases[0]
    return ExecutionRequest(job_id=run_id, attempt_id=run_id, worker_id='standalone-public-pod',
        approval_id='standalone-no-ledger-approval', deadline=min(absolute_deadline, now + timedelta(seconds=240)),
        spec=case.spec, science=ScienceMetadata(session_id='standalone-public-calibration'))


def private_output(output):
    output = Path(output)
    require(output == WORKSPACE / 'result', 'FIXED_OUTPUT_PATH_REQUIRED')
    info = WORKSPACE.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == UID and not info.st_mode & 0o077,
            'PRIVATE_WORKSPACE_REQUIRED')
    require(not output.exists() and not output.is_symlink() and not (WORKSPACE / 'started.json').exists(),
            'CALIBRATION_ALREADY_STARTED')
    return output


def run(config, *, absolute_deadline, run_id, output):
    process_boundary()
    output = private_output(output)
    now = datetime.now(UTC)
    # Publish an irreversible per-Pod claim before checking/hashing model bytes.
    request = execution_request(config, PromptDataset.model_validate_json(immutable_file(config.datasets[0].path)),
                                run_id, absolute_deadline, now)
    claim = WORKSPACE / 'started.json'
    with claim.open('x') as stream:
        stream.write(canonical_json({'run_id': run_id, 'absolute_deadline': absolute_deadline.isoformat(),
                                    'job_deadline': request.deadline.isoformat()}))
        stream.flush()
        os.fsync(stream.fileno())
    def expired(*_):
        raise TimeoutError('public calibration job deadline reached')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(.001, (request.deadline - datetime.now(UTC)).total_seconds()))
    try:
        dataset = validate_assets(config)
        require(request == execution_request(config, dataset, run_id, absolute_deadline, now),
                'PUBLIC_INPUTS_CHANGED')
        receipt = PublicCalibrationEngine(config).execute(request, output)
        summary = decode((output / 'summary.json').read_bytes())
        require(summary.get('suite') == 'backend_parity_v1' and summary.get('passed') is True
                and summary.get('scientific_evidence') is False and len(summary.get('checks', [])) == 29
                and all(row.get('passed') is True for row in summary['checks']), 'PARITY_NOT_COMPLETE')
        require(datetime.now(UTC) < request.deadline, 'JOB_DEADLINE_EXCEEDED')
        manifest = receipt.manifest
        report = {'schema_version': 1, 'kind': 'standalone_public_calibration', 'status': 'passed',
            'suite': 'backend_parity_v1', 'run_id': run_id, 'scientific_evidence': False,
            'heldout_data_used': False, 'installed_ledger_used': False, 'lifecycle_acceptance': False,
            'nested_cgroup_limits_enforced': False, 'resource_boundary': 'disposable_provider_pod',
            'hard_deadline_enforcer': 'external_root_launcher',
            'config_sha256': 'sha256:' + hashlib.sha256(canonical_json(config.model_dump(mode='json')).encode()).hexdigest(),
            'requested_limits': request.spec.limits.model_dump(mode='json'),
            'absolute_deadline': absolute_deadline.isoformat(), 'job_deadline': request.deadline.isoformat(),
            'started_at': receipt.started_at.isoformat(), 'finished_at': receipt.finished_at.isoformat(),
            'model': config.model.model_dump(mode='json'), 'inputs': request.spec.inputs.model_dump(mode='json'),
            'operation': request.spec.operation.model_dump(mode='json'),
            'software': manifest.software.model_dump(mode='json'), 'hardware': manifest.hardware.model_dump(mode='json'),
            'cost': manifest.cost.model_dump(mode='json'), 'artifacts': [item.model_dump(mode='json') for item in manifest.artifacts],
            'checks_passed': len(summary['checks']), 'process_exit_and_pod_deletion_verified': False,
            'calibration_script_sha256': sha256_file(Path(__file__))}
        _json_write(output / 'standalone-manifest.json', report)
        return report
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--absolute-deadline', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = PublicCalibrationConfig.model_validate_json(immutable_file(args.config))
        deadline = TypeAdapter(UTCTimestamp).validate_python(args.absolute_deadline)
        result = run(config, absolute_deadline=deadline, run_id=args.run_id, output=args.output)
        print(canonical_json({'status': result['status'], 'kind': result['kind'], 'checks_passed': result['checks_passed'],
                              'manifest_sha256': sha256_file(args.output / 'standalone-manifest.json')}), flush=True)
        return 0
    except Exception as error:
        # This fixed public-input process has a clean credential-free environment.
        # Keep actionable numerical diagnostics in the private collected log.
        traceback.print_exc(limit=20, file=sys.stderr)
        print(canonical_json({'status': 'failed', 'kind': 'standalone_public_calibration',
            'reason': str(error) if isinstance(error, CalibrationRefused) else 'CALIBRATION_FAILED',
            'error_type': type(error).__name__, 'scientific_evidence': False}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
