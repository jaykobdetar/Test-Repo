import hashlib
from pathlib import Path

import numpy as np
import pytest
from safetensors.numpy import save_file

from probe_core.artifact_store import ArtifactStore


def test_generated_tensor_becomes_stable_job_input(tmp_path):
    source = tmp_path / "direction.safetensors"
    save_file({"direction": np.arange(8, dtype=np.float32)}, str(source))
    store = ArtifactStore(tmp_path / "store")
    record = store.register(source)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    assert record["artifact_id"] == expected
    assert record["tensor_refs"] == [
        {"path": f"{expected}/tensor.safetensors", "sha256": "sha256:" + expected, "tensor_name": "direction"}
    ]
    source.unlink()
    assert store.read(expected, max_bytes=1024)[1]
    assert store.describe(expected) == record


def test_registration_idempotency_and_checksum(tmp_path):
    source = tmp_path / "summary.txt"
    source.write_text("an observation")
    store = ArtifactStore(tmp_path / "store")
    first = store.register(source)
    assert store.register(source) == first
    with pytest.raises(ValueError, match="checksum"):
        store.register(source, expected_sha256="0" * 64)
    with pytest.raises(ValueError, match="input limit"):
        store.read(first["artifact_id"], max_bytes=1)


def test_unsafe_or_invalid_tensor_inputs_are_rejected(tmp_path):
    source = tmp_path / "invalid.safetensors"
    source.write_text("not a tensor")
    store = ArtifactStore(tmp_path / "store")
    with pytest.raises(Exception):
        store.register(source)
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        store.register(link)
    with pytest.raises(ValueError):
        store.read("../private", max_bytes=1024)
    assert not list(store.root.glob(".stage-*"))
