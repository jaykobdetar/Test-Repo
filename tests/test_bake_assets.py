"""Build-time public assets must be exactly the reviewed bytes, without execution."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def baker():
    path = Path(__file__).parents[1] / 'deploy/gpu/bake-assets.py'
    spec = importlib.util.spec_from_file_location('probe_test_bake_assets', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path, raw=b'{"schema_version":1,"public":true}\n', **changes):
    asset = {'path': 'datasets/public.json', 'bytes': len(raw),
             'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest(),
             'inline_base64': base64.b64encode(raw).decode()}
    asset.update(changes)
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps({'schema_version': 1, 'assets': [asset]}))
    return path, raw


def test_inline_bytes_are_verified_and_readonly_and_destination_not_reused(tmp_path, baker):
    source, raw = manifest(tmp_path)
    destination = tmp_path / 'assets'
    baker.bake(source, destination)
    target = destination / 'datasets/public.json'
    assert target.read_bytes() == raw
    assert target.stat().st_mode & 0o777 == 0o444
    with pytest.raises(FileExistsError):
        baker.bake(source, destination)
    assert target.read_bytes() == raw


@pytest.mark.parametrize('changes', [{'sha256': 'sha256:' + 'f' * 64}, {'bytes': 1}])
def test_mismatched_content_cannot_finish_bake(tmp_path, baker, changes):
    source, _ = manifest(tmp_path, **changes)
    with pytest.raises(ValueError, match='public cache'):
        baker.bake(source, tmp_path / 'assets')


@pytest.mark.parametrize('name', ['datasets/../../outside.json', 'models/../outside.json', '/etc/model.json'])
def test_traversal_cannot_create_files_outside_asset_root(tmp_path, baker, name):
    source, _ = manifest(tmp_path, path=name)
    with pytest.raises(ValueError, match='invalid public cache member'):
        baker.bake(source, tmp_path / 'assets')
    assert not (tmp_path / 'outside.json').exists()
    assert list((tmp_path / 'assets').iterdir()) == []


def test_nonapproved_download_origin_is_refused_before_network_access(tmp_path, baker, monkeypatch):
    source, _ = manifest(tmp_path, url='https://example.invalid/model.safetensors')
    class NoNetwork:
        def open(self, *args, **kwargs):
            pytest.fail('an unapproved URL reached the network')
    monkeypatch.setattr(baker, 'build_opener', lambda *args: NoNetwork())
    with pytest.raises(ValueError, match='public Hugging Face'):
        baker.bake(source, tmp_path / 'assets')
