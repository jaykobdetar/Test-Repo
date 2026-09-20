"""Output accounting must tolerate publication without hiding other I/O errors."""
import errno
import os
from pathlib import Path

import pytest

from probe_core.worker import _output_size


def test_atomic_publish_uses_one_metadata_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir()
    partial, final = root / "summary.json.partial", root / "summary.json"
    partial.write_bytes(b'{"complete":true}')
    original = Path.lstat
    published = []

    def publish_after_inspection(path):
        info = original(path)
        if path == partial:
            partial.replace(final)
            published.append(True)
        return info

    monkeypatch.setattr(Path, "lstat", publish_after_inspection)
    assert _output_size(root) == len(b'{"complete":true}')
    assert published == [True] and final.is_file() and not partial.exists()
    assert _output_size(root) == final.stat().st_size


def test_enumerated_file_disappears_before_inspection(tmp_path, monkeypatch):
    gone = tmp_path / "gone.partial"
    gone.write_bytes(b"temporary")
    (tmp_path / "stable.json").write_bytes(b"stable")
    original = Path.lstat

    def disappear(path):
        if path == gone:
            gone.unlink()
        return original(path)

    monkeypatch.setattr(Path, "lstat", disappear)
    assert _output_size(tmp_path) == len(b"stable")


def test_counts_nested_regular_files_without_following_symlinks(tmp_path):
    root, outside = tmp_path / "artifacts", tmp_path / "outside"
    (root / "nested").mkdir(parents=True)
    outside.mkdir()
    (root / "nested" / "actual.json").write_bytes(b"actual")
    (outside / "large.bin").write_bytes(b"outside" * 100)
    (root / "file-link").symlink_to(outside / "large.bin")
    (root / "directory-link").symlink_to(outside, target_is_directory=True)
    (root / "broken-link").symlink_to(tmp_path / "missing")
    os.mkfifo(root / "pipe")
    assert _output_size(root) == len(b"actual")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(root, target_is_directory=True)
    assert _output_size(root_link) == 0
    assert _output_size(tmp_path / "absent") == 0


@pytest.mark.parametrize("error_type,number", [(PermissionError, errno.EACCES), (OSError, errno.EIO)])
def test_file_metadata_errors_other_than_disappearance_propagate(tmp_path, monkeypatch, error_type, number):
    output = tmp_path / "output.json"
    output.write_bytes(b"output")
    original = Path.lstat
    failure = error_type(number, "fixture metadata failure")

    def denied(path):
        if path == output:
            raise failure
        return original(path)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(error_type) as caught:
        _output_size(tmp_path)
    assert caught.value is failure


@pytest.mark.parametrize("error_type,number,ignored", [
    (FileNotFoundError, errno.ENOENT, True),
    (PermissionError, errno.EACCES, False),
    (OSError, errno.EIO, False),
])
def test_directory_scan_ignores_only_concurrent_disappearance(tmp_path, monkeypatch, error_type, number, ignored):
    directory = tmp_path / "nested"
    directory.mkdir()
    (tmp_path / "stable").write_bytes(b"stable")
    original = os.scandir
    failure = error_type(number, "fixture enumeration failure")

    def scan(path):
        if Path(path) == directory:
            raise failure
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    if ignored:
        assert _output_size(tmp_path) == len(b"stable")
    else:
        with pytest.raises(error_type) as caught:
            _output_size(tmp_path)
        assert caught.value is failure
