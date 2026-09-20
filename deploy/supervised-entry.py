"""Load the pinned SSH adapter beside this script without replacing the installed wheel."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from probe_core.gpu_acceptance_runner import read_file


def load():
    root = Path(__file__).resolve().parent
    release = json.loads(read_file(root / 'files.json', owner=0))
    expected = {'ssh_job_client.py', 'supervised_runner.py', 'public-job.py'}
    if set(release) != expected:
        raise ValueError('unexpected supervised release inventory')
    for name in sorted(expected):
        raw = read_file(root / name, owner=0, bound=256 * 1024)
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != release[name]:
            raise ValueError('supervised release file changed')
    for name in ('ssh_job_client', 'supervised_runner'):
        spec = importlib.util.spec_from_file_location('probe_core.' + name, root / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules['probe_core.supervised_runner']


def main():
    return load().main()


if __name__ == '__main__':
    raise SystemExit(main())
