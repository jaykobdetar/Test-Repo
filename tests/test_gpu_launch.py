from types import SimpleNamespace

import pytest

from probe_core.gpu_launch import _prepare_worker_cgroups


@pytest.mark.parametrize('path', ['/sys/fs/cgroup', '/sys/fs/cgroup/another-service',
                                 '/sys/fs/cgroup/probe-jobs/../probe-jobs', None])
def test_runpod_launch_rejects_other_cgroup_scopes_before_mutation(path, monkeypatch):
    import probe_core.pod_bootstrap as bootstrap
    calls = []
    monkeypatch.setattr(bootstrap, 'prepare', lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match='must be /sys/fs/cgroup/probe-jobs'):
        _prepare_worker_cgroups(SimpleNamespace(cgroup_directory=path))
    assert calls == []
