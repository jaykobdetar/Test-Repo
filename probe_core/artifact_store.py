"""Controller-owned immutable inputs for later jobs, independent of GPU lifetime."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .schemas import TensorArtifact


class ArtifactStore:
    def __init__(self, root: str | Path, *, max_bytes: int = 1024**3):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("input store must be private and service-owned")
        self.max_bytes = max_bytes

    def _directory(self, artifact_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", artifact_id) is None:
            raise ValueError("invalid artifact identifier")
        directory = self.root / artifact_id
        if directory.is_symlink():
            raise PermissionError("artifact directory must not be a symlink")
        return directory

    @staticmethod
    def _sync(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def register(self, source: str | Path, *, expected_sha256: str | None = None) -> dict:
        source = Path(source)
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.root))
        name = "tensor.safetensors" if source.suffix == ".safetensors" else "data.bin"
        destination = staging / name
        digest = hashlib.sha256()
        total = 0
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as incoming, destination.open("xb") as outgoing:
                info = os.fstat(incoming.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > self.max_bytes:
                    raise ValueError("only bounded regular artifacts may be registered")
                while block := incoming.read(1024 * 1024):
                    total += len(block)
                    if total > self.max_bytes:
                        raise ValueError("artifact exceeds limit")
                    digest.update(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            artifact_id = digest.hexdigest()
            if expected_sha256 is not None and expected_sha256.removeprefix("sha256:") != artifact_id:
                raise ValueError("artifact checksum mismatch")
            tensor_refs = []
            if name.endswith(".safetensors"):
                from safetensors import safe_open
                with safe_open(str(destination), framework="numpy") as tensors:
                    for tensor_name in tensors.keys():
                        tensor_refs.append(TensorArtifact(path=f"{artifact_id}/{name}", sha256="sha256:" + artifact_id,
                                                          tensor_name=tensor_name).model_dump(mode="json"))
            record = {"artifact_id": artifact_id, "path": f"{artifact_id}/{name}",
                      "bytes": total, "sha256": "sha256:" + artifact_id, "tensor_refs": tensor_refs}
            with (staging / "record.json").open("x") as stream:
                json.dump(record, stream, allow_nan=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            destination.chmod(0o400)
            (staging / "record.json").chmod(0o400)
            self._sync(staging)
            final = self._directory(artifact_id)
            try:
                os.rename(staging, final)
            except FileExistsError:
                return self.describe(artifact_id)
            except OSError:
                if final.is_dir():
                    return self.describe(artifact_id)
                raise
            self._sync(self.root)
            return record
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def describe(self, artifact_id: str) -> dict:
        directory = self._directory(artifact_id)
        fd = os.open(directory / "record.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("artifact record exceeds limit")
        record = json.loads(data)
        if record["artifact_id"] != artifact_id or record["path"] not in {
            f"{artifact_id}/tensor.safetensors", f"{artifact_id}/data.bin"
        }:
            raise ValueError("artifact record is inconsistent")
        return record

    def read(self, artifact_id: str, *, max_bytes: int) -> tuple[dict, bytes]:
        record = self.describe(artifact_id)
        if record["bytes"] > max_bytes:
            raise ValueError("artifact exceeds input limit")
        fd = os.open(self.root / record["path"], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != artifact_id:
            raise ValueError("registered artifact changed")
        return record, data
