"""Offline tests: no canonical weight downloads and no cloud activity."""

import hashlib
import io
import json
from pathlib import Path

import pytest

from probe_core.model_assets import LockedFile, ModelLock, canonical_locks, inventory, plan, prepare, verify_file


@pytest.fixture
def source():
    config = {
        "model_type": "qwen3",
        "num_hidden_layers": 28,
        "num_attention_heads": 16,
        "num_key_value_heads": 8,
        "hidden_size": 2048,
        "head_dim": 128,
        "vocab_size": 151936,
    }
    contents = {
        "config.json": json.dumps(config).encode(),
        "tokenizer.json": b"{}",
        "tokenizer_config.json": json.dumps({"chat_template": "{{ enable_thinking }}"}).encode(),
        "model.safetensors": b"synthetic bytes used only to test content verification",
    }
    entries = []
    for name, body in contents.items():
        entries.append(
            LockedFile(
                path=name,
                size_bytes=len(body),
                sha256="sha256:" + hashlib.sha256(body).hexdigest() if name.endswith(".safetensors") else None,
                git_blob_sha1=hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest(),
            )
        )
    lock = ModelLock(
        repo="Qwen/Qwen3-1.7B-Base", revision="a" * 40, files=entries, source_url="https://huggingface.co/test-fixture"
    )

    def opener(request, timeout):
        assert "/resolve/" + "a" * 40 + "/" in request.full_url
        assert not request.has_header("Authorization")
        return io.BytesIO(contents[request.full_url.rsplit("/", 1)[-1]])

    return lock, contents, opener


def test_canonical_lock_has_exact_two_public_revisions_and_sizes():
    locks = canonical_locks()
    assert [lock.revision for lock in locks] == [
        "ea980cb0a6c2ae4b936e82123acc929f1cec04c1",
        "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e",
    ]
    assert sum(lock.size_bytes for lock in locks) == 7532122402
    assert all(
        entry.sha256 is not None for lock in locks for entry in lock.files if entry.path.endswith(".safetensors")
    )


def test_plan_is_offline_and_download_requires_exact_allowance(source, tmp_path):
    lock, _, opener = source
    assert plan(tmp_path, (lock,))["download_bytes"] == lock.size_bytes
    with pytest.raises(ValueError, match="allowance"):
        prepare(tmp_path, (lock,), max_download_bytes=lock.size_bytes - 1, opener=opener)
    assert not list(tmp_path.iterdir())
    prepared = prepare(tmp_path, (lock,), max_download_bytes=lock.size_bytes, opener=opener)
    assert plan(tmp_path, (lock,))["download_bytes"] == 0
    prepare(
        tmp_path, (lock,), max_download_bytes=0, opener=lambda *_: pytest.fail("already downloaded asset fetched again")
    )
    result = inventory(Path(prepared[0]["directory"]), lock)
    assert result.model.dtype == "bfloat16"
    assert result.model.thinking_mode is None
    assert result.model.revision_sha == lock.revision


def test_bad_content_never_publishes(source, tmp_path):
    lock, contents, _ = source
    name = lock.files[0].path

    def corrupted(request, timeout):
        body = contents[request.full_url.rsplit("/", 1)[-1]]
        return io.BytesIO(b"x" * len(body))

    with pytest.raises(ValueError, match="hash mismatch"):
        prepare(tmp_path, (lock,), max_download_bytes=lock.size_bytes, opener=corrupted)
    directory = tmp_path / lock.repo.split("/")[-1] / lock.revision
    assert not (directory / name).exists()
    assert not list(directory.glob("*.partial"))


def test_existing_partial_is_not_deleted_or_overwritten(source, tmp_path):
    lock, _, opener = source
    directory = tmp_path / lock.repo.split("/")[-1] / lock.revision
    directory.mkdir(parents=True)
    partial = directory / (lock.files[0].path + ".partial")
    partial.write_bytes(b"preserve interrupted transfer for operator inspection")
    with pytest.raises(FileExistsError):
        prepare(tmp_path, (lock,), max_download_bytes=lock.size_bytes, opener=opener)
    assert partial.read_bytes().startswith(b"preserve")


def test_symlink_and_wrong_size_rejected(source, tmp_path):
    lock, contents, _ = source
    entry = lock.files[0]
    actual = tmp_path / "actual"
    actual.write_bytes(contents[entry.path])
    alias = tmp_path / "alias"
    alias.symlink_to(actual)
    with pytest.raises(ValueError, match="symlink"):
        verify_file(alias, entry)
    actual.write_bytes(b"truncated")
    with pytest.raises(ValueError, match="size"):
        verify_file(actual, entry)


def test_posttrained_inventory_requires_explicit_thinking_choice(source, tmp_path):
    lock, _, opener = source
    directory = Path(prepare(tmp_path, (lock,), max_download_bytes=lock.size_bytes, opener=opener)[0]["directory"])
    post = lock.model_copy(update={"repo": "Qwen/Qwen3-1.7B"})
    with pytest.raises(ValueError, match="explicit thinking"):
        inventory(directory, post)
    yes = inventory(directory, post, thinking_mode=True)
    no = inventory(directory, post, thinking_mode=False)
    assert yes.model.chat_template_hash == no.model.chat_template_hash
    assert yes.model.thinking_mode and not no.model.thinking_mode


@pytest.mark.parametrize("path", ["../config.json", "pytorch_model.bin", "model.py", "/config.json"])
def test_executable_or_traversing_asset_paths_are_forbidden(path):
    with pytest.raises(ValueError):
        LockedFile(path=path, size_bytes=1, sha256="sha256:" + "0" * 64)
