"""Prepare immutable, revision-pinned canonical Qwen assets without cloud authority.

Planning is offline. Downloading requires an explicit byte allowance; no Hugging
Face token, user cache, remote Python, or mutable branch is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Annotated, Literal
from urllib.request import Request, urlopen

from pydantic import Field, model_validator

from .audit import canonical_json
from .schemas import FrozenModel, GitSHA, ModelIdentity, SHA256
from .worker_contracts import FileDigest

CANONICAL_REPOS = ("Qwen/Qwen3-1.7B-Base", "Qwen/Qwen3-1.7B")
ALLOWED_SMALL_FILES = {"config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json", "merges.txt", "vocab.json", "model.safetensors.index.json", "LICENSE"}


class LockedFile(FrozenModel):
    path: str
    size_bytes: Annotated[int, Field(strict=True, gt=0, le=8 * 1024**3)]
    sha256: SHA256 | None = None
    git_blob_sha1: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None = None

    @model_validator(mode="after")
    def safe(self):
        if self.path not in ALLOWED_SMALL_FILES and not re.fullmatch(r"model(?:-\d{5}-of-\d{5})?\.safetensors", self.path):
            raise ValueError("only known tokenizer/config files and safetensors weights are allowed")
        if self.sha256 is None and self.git_blob_sha1 is None:
            raise ValueError("every source file needs a cryptographic content identity")
        if self.path.endswith(".safetensors") and self.sha256 is None:
            raise ValueError("model weights require the Hub's LFS SHA256")
        return self


class ModelLock(FrozenModel):
    repo: Literal["Qwen/Qwen3-1.7B-Base", "Qwen/Qwen3-1.7B"]
    revision: GitSHA
    files: Annotated[tuple[LockedFile, ...], Field(min_length=4, max_length=128)]
    source_url: str

    @model_validator(mode="after")
    def complete(self):
        names = {item.path for item in self.files}
        if len(names) != len(self.files) or not {"config.json", "tokenizer.json", "tokenizer_config.json"} <= names or not any(name.endswith(".safetensors") for name in names):
            raise ValueError("asset inventory is incomplete or repeats a filename")
        return self

    @property
    def size_bytes(self):
        return sum(item.size_bytes for item in self.files)


class PreparedBundle(FrozenModel):
    schema_version: Literal[1] = 1
    model: ModelIdentity
    assets: tuple[FileDigest, ...]
    source_lock_hash: SHA256


def canonical_locks() -> tuple[ModelLock, ...]:
    data = json.loads(files("probe_core.resources").joinpath("canonical-models.json").read_text())
    return tuple(ModelLock.model_validate(item) for item in data["models"])


def lock_hash(lock: ModelLock) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(lock.model_dump(mode="json")).encode()).hexdigest()


def _regular(path: Path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("asset paths cannot contain symlinks")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("assets must be unshared regular files")
    return info


def verify_file(path: Path, entry: LockedFile) -> str:
    if _regular(path).st_size != entry.size_bytes:
        raise ValueError("asset size differs from the frozen source inventory")
    digest = hashlib.sha256()
    git = hashlib.sha1(b"blob " + str(entry.size_bytes).encode() + b"\0")
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            git.update(chunk)
    actual = "sha256:" + digest.hexdigest()
    if entry.sha256 is not None and actual != entry.sha256:
        raise ValueError("asset SHA256 mismatch")
    # LFS blob IDs identify pointer files, not downloaded tensor bytes.
    if entry.sha256 is None and entry.git_blob_sha1 != git.hexdigest():
        raise ValueError("asset Git blob hash mismatch")
    return actual


def plan(root: Path, locks: tuple[ModelLock, ...]):
    anchor = root.absolute()
    while not anchor.exists():
        anchor = anchor.parent
    missing = 0
    for lock in locks:
        directory = root / lock.repo.split("/")[-1] / lock.revision
        for entry in lock.files:
            path = directory / entry.path
            if path.exists() or path.is_symlink():
                verify_file(path, entry)
            else:
                missing += entry.size_bytes
    return {"model_bytes": sum(lock.size_bytes for lock in locks), "download_bytes": missing,
            "free_bytes": shutil.disk_usage(anchor).free, "reserve_bytes": 512 * 1024**2,
            "models": [{"repo": lock.repo, "revision": lock.revision, "size_bytes": lock.size_bytes,
                        "lock_hash": lock_hash(lock)} for lock in locks]}


def prepare(root: Path, locks: tuple[ModelLock, ...], *, max_download_bytes: int, opener=urlopen):
    if type(max_download_bytes) is not int or max_download_bytes < 0:
        raise ValueError("explicit nonnegative download byte allowance required")
    summary = plan(root, locks)
    if summary["download_bytes"] > max_download_bytes:
        raise ValueError("frozen downloads exceed the approved byte allowance")
    if summary["download_bytes"] + summary["reserve_bytes"] > summary["free_bytes"]:
        raise ValueError("insufficient free space for assets plus the fixed safety reserve")
    results = []
    for lock in locks:
        directory = root / lock.repo.split("/")[-1] / lock.revision
        if any(item.is_symlink() for item in (directory, *directory.parents)):
            raise ValueError("model destination cannot contain symlinks")
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        for entry in lock.files:
            destination = directory / entry.path
            if destination.exists():
                verify_file(destination, entry)
                continue
            temporary = directory / (entry.path + ".partial")
            # A previous interrupted download is explicit unresolved state; do
            # not silently overwrite it or follow attacker-created links.
            created = False
            try:
                with temporary.open("xb") as output:
                    created = True
                    request = Request(f"https://huggingface.co/{lock.repo}/resolve/{lock.revision}/{entry.path}", headers={"User-Agent": "probe-core-model-preparation/1"})
                    with opener(request, timeout=60) as response:
                        remaining = entry.size_bytes
                        while remaining:
                            chunk = response.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("truncated model asset")
                            output.write(chunk)
                            remaining -= len(chunk)
                        if response.read(1):
                            raise ValueError("model asset exceeds its frozen length")
                    output.flush()
                    os.fsync(output.fileno())
                verify_file(temporary, entry)
                temporary.chmod(0o440)
                os.link(temporary, destination)
                temporary.unlink()
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except BaseException:
                # Only remove our own newly created temporary file.
                if created and temporary.exists() and not temporary.is_symlink() and temporary.stat().st_nlink == 1:
                    temporary.unlink()
                raise
        results.append({"directory": str(directory.absolute()), "repo": lock.repo, "revision": lock.revision})
    return results


def inventory(directory: Path, lock: ModelLock, *, thinking_mode: bool | None = None) -> PreparedBundle:
    if lock.repo.endswith("-Base") and thinking_mode is not None:
        raise ValueError("Base uses raw text/token input, never a chat mode")
    if not lock.repo.endswith("-Base") and type(thinking_mode) is not bool:
        raise ValueError("posttrained preparation requires explicit thinking true or false")
    assets = tuple(FileDigest(path=entry.path, sha256=verify_file(directory / entry.path, entry)) for entry in lock.files)
    config = json.loads((directory / "config.json").read_text())
    expected = {"model_type": "qwen3", "num_hidden_layers": 28, "num_attention_heads": 16,
                "num_key_value_heads": 8, "hidden_size": 2048, "head_dim": 128, "vocab_size": 151936}
    if any(config.get(key) != value for key, value in expected.items()) or config.get("quantization_config") or config.get("auto_map"):
        raise ValueError("downloaded checkpoint is not the canonical unquantized architecture")
    template_hash = None
    if thinking_mode is not None:
        template = json.loads((directory / "tokenizer_config.json").read_text()).get("chat_template")
        if not isinstance(template, str) or "enable_thinking" not in template:
            raise ValueError("posttrained tokenizer lacks a verifiable thinking template")
        template_hash = "sha256:" + hashlib.sha256(template.encode()).hexdigest()
    model = ModelIdentity(repo=lock.repo, revision_sha=lock.revision,
                          local_weight_hashes=[asset.sha256 for asset in assets if asset.path.endswith(".safetensors")],
                          tokenizer_revision=lock.revision, dtype="bfloat16", quantized=False,
                          chat_template_hash=template_hash, thinking_mode=thinking_mode)
    return PreparedBundle(model=model, assets=assets, source_lock_hash=lock_hash(lock))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "download", "inventory"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--repo", choices=CANONICAL_REPOS)
    parser.add_argument("--max-download-bytes", type=int)
    parser.add_argument("--thinking", choices=["true", "false"])
    args = parser.parse_args()
    locks = tuple(lock for lock in canonical_locks() if args.repo is None or lock.repo == args.repo)
    if args.command == "plan":
        result = plan(args.root, locks)
    elif args.command == "download":
        if args.max_download_bytes is None:
            parser.error("download requires --max-download-bytes after reviewing the offline plan")
        result = prepare(args.root, locks, max_download_bytes=args.max_download_bytes)
    else:
        if len(locks) != 1:
            parser.error("inventory requires one --repo")
        lock = locks[0]
        directory = args.root / lock.repo.split("/")[-1] / lock.revision
        result = inventory(directory, lock, thinking_mode=None if args.thinking is None else args.thinking == "true").model_dump(mode="json")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
