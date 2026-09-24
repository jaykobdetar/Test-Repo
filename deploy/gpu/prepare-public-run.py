"""Prepare one supervised public run directory from an earlier run's templates.

It copies ``run.json`` and ``calibration.json`` from a previous run directory,
binds the new immutable image, source commit and registered suite, and sets a
fresh deadline. It never reads the provider key: ``run.json`` refers to it only
by path. Prepare immediately before starting the guard, because the deadline
(at most 900 seconds) starts now.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from probe_core.recipe_registry import registered
from probe_core.runpod_provider import RunPodConfig
from probe_core.provider import DeploymentSpec

ROOT = Path(__file__).resolve().parent
ASSETS = Path('/opt/probe-assets')


def prepare(template, output, *, suite_name, image_digest, code_commit, budget_ledger=None,
            live_price=None, seconds=900):
    suite = registered(suite_name)
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_digest) or not re.fullmatch(r'[0-9a-f]{40}', code_commit):
        raise ValueError('an immutable image digest and a full source commit are required')
    if not 60 <= seconds <= 900:
        raise ValueError('the deadline must be 60 to 900 seconds away')
    record = json.loads((template / 'run.json').read_text())
    config = json.loads((template / 'calibration.json').read_text())
    if config['model']['repo'] not in suite.models:
        raise ValueError('the template model is not registered for this suite')
    if suite.kind == 'recipe' and budget_ledger is None:
        raise ValueError('recipe runs require a budget ledger')
    deployment = DeploymentSpec.model_validate({**record['deployment'], 'image_digest': image_digest})
    RunPodConfig.model_validate_json(json.dumps(record['provider']))
    config.update(container_image_digest=image_digest, code_git_commit=code_commit,
                  datasets=[{'path': str(ASSETS / suite.dataset_path), 'sha256': suite.dataset_sha256}])
    if live_price is not None:
        config['live_price_usd_per_hour'] = live_price
    label = uuid.uuid4().hex
    record.update(deployment=deployment.model_dump(mode='json', exclude_none=True), suite=suite.name,
                  worker_id='public-' + label[:16], request_id='request-' + label,
                  deadline=time.time() + seconds,
                  calibration_script_sha256='sha256:' + hashlib.sha256(
                      (ROOT / 'public-calibration.py').read_bytes()).hexdigest())
    if budget_ledger is not None:
        record['budget_ledger'] = str(Path(budget_ledger).expanduser().resolve())
    output.mkdir(mode=0o700)
    (output / 'calibration.json').write_text(json.dumps(config, indent=2) + '\n')
    (output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
    return {'directory': str(output), 'suite': suite.name, 'worker_id': record['worker_id'],
            'deadline': datetime.fromtimestamp(record['deadline'], timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', required=True, type=Path, help='an earlier run directory for the same model')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--suite', required=True)
    parser.add_argument('--image-digest', required=True)
    parser.add_argument('--code-commit', required=True)
    parser.add_argument('--budget-ledger')
    parser.add_argument('--live-price', type=float)
    args = parser.parse_args()
    print(json.dumps(prepare(args.template, args.output, suite_name=args.suite, image_digest=args.image_digest,
                             code_commit=args.code_commit, budget_ledger=args.budget_ledger,
                             live_price=args.live_price), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
