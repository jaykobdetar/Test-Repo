"""Stage explicitly hashed public assets; standard library only, no inference.

Manifest: {schema_version:1, assets:[{path:"models/..." or "datasets/...",
sha256:"sha256:<hex>", bytes:<integer>, url:"https://..." OR inline_base64:"..."}]}.
Only /workspace/probe is managed. /workspace must be a separate real mount.
Partials and their identity sidecars remain private for a reviewed same-asset
resume. Every transfer and verification stops 20 seconds before the supplied
absolute deadline. URLs, including signed redirect locations, are never logged.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

VOLUME = Path("/workspace")
ROOT_UID = 0
GROUP = 10001
MAX_TOTAL = 12_000_000_000
MAX_MANIFEST = 8*1024*1024
MAX_INLINE = 1024*1024
CHUNK = 1024*1024
STOP_MARGIN = 20


class StageError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise StageError(code)


class Deadline:
    def __init__(self, text):
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
            require(value.tzinfo is not None and value.utcoffset() is not None, "DeadlineMustBeAware")
            remaining = (value-datetime.now(timezone.utc)).total_seconds()-STOP_MARGIN
        except (ValueError, OverflowError):
            raise StageError("InvalidDeadline") from None
        self.cutoff = time.monotonic()+remaining
        self.check()

    def check(self):
        require(time.monotonic() < self.cutoff, "TransferDeadlineReached")

    def timeout(self):
        self.check()
        return min(5.0, max(0.01, self.cutoff-time.monotonic()))


def directory(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "InvalidDirectory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(fd, name, maximum):
    stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    try:
        require(stat.S_ISREG(os.fstat(stream).st_mode), "NonregularFile")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(stream, min(CHUNK, maximum+1-len(data)))
            if not chunk:
                break
            data.extend(chunk)
        require(len(data) <= maximum, "FileReadLimit")
        return bytes(data)
    finally:
        os.close(stream)


def public_url(value):
    try:
        parts = urllib.parse.urlsplit(value)
        require(parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                and parts.port in {None, 443} and not parts.fragment and len(value) <= 8192
                and not any(ord(character) < 32 for character in value), "InvalidPublicHttpsUrl")
    except (TypeError, ValueError):
        raise StageError("InvalidPublicHttpsUrl") from None
    return value


def validate_manifest(value):
    require(type(value) is dict and set(value) == {"schema_version", "assets"}
            and type(value["schema_version"]) is int and value["schema_version"] == 1, "InvalidManifest")
    require(type(value["assets"]) is list and 1 <= len(value["assets"]) <= 256, "InvalidAssetCount")
    total = 0
    paths = set()
    for asset in value["assets"]:
        require(type(asset) is dict and set(asset) in ({"path", "sha256", "bytes", "url"},
                                                     {"path", "sha256", "bytes", "inline_base64"}), "InvalidAssetFields")
        path = asset["path"]
        require(isinstance(path, str) and len(path) <= 512 and 2 <= len(path.split("/")) <= 10
                and path.split("/")[0] in {"models", "datasets"}
                and all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."}
                        and not part.startswith(".") for part in path.split("/")), "InvalidAssetPath")
        require(path not in paths, "DuplicateAssetPath")
        paths.add(path)
        require(isinstance(asset["sha256"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", asset["sha256"]), "InvalidAssetHash")
        require(type(asset["bytes"]) is int and 0 <= asset["bytes"] <= MAX_TOTAL, "InvalidAssetSize")
        total += asset["bytes"]
        require(total <= MAX_TOTAL, "DeclaredTotalExceeds12GB")
        if "url" in asset:
            public_url(asset["url"])
        else:
            require(isinstance(asset["inline_base64"], str) and len(asset["inline_base64"]) <= 4*((MAX_INLINE+2)//3),
                    "InlineAssetTooLarge")
            try:
                data = base64.b64decode(asset["inline_base64"], validate=True)
            except ValueError:
                raise StageError("InvalidInlineBase64") from None
            require(len(data) <= MAX_INLINE and len(data) == asset["bytes"], "InlineSizeMismatch")
            require("sha256:"+hashlib.sha256(data).hexdigest() == asset["sha256"], "InlineHashMismatch")
    require(not any(path.startswith(other+"/") for path in paths for other in paths if path != other), "AssetPathPrefixConflict")
    return value


def load_manifest(path):
    parent = directory(Path(path).absolute().parent)
    try:
        raw = read_file(parent, Path(path).name, MAX_MANIFEST)
    finally:
        os.close(parent)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise StageError("InvalidManifestJson") from None
    return validate_manifest(value), "sha256:"+hashlib.sha256(raw).hexdigest()


def validate_volume(path):
    require(Path(path) == VOLUME and os.geteuid() == ROOT_UID, "RootAndFixedVolumeRequired")
    fd = directory(path)
    proc = None
    try:
        proc = directory(f"/proc/{os.getpid()}")
        text = read_file(proc, "mountinfo", 1024*1024).decode("utf-8")
        rows = []
        for line in text.splitlines():
            left, separator, right = line.partition(" - ")
            fields, after = left.split(), right.split()
            if not separator or len(fields) < 6 or len(after) < 3:
                continue
            mountpoint = re.sub(r"\\([0-7]{3})", lambda match:chr(int(match[1], 8)), fields[4])
            if mountpoint == str(path):
                rows.append((fields, after))
        info = os.fstat(fd)
        require(len(rows) == 1 and rows[0][0][2] == f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}", "SeparateWorkspaceMountRequired")
        require(info.st_dev != os.stat("/").st_dev and rows[0][1][0] not in {"overlay", "tmpfs", "ramfs"}, "PersistentWorkspaceMountRequired")
        require(not os.fstatvfs(fd).f_flag & os.ST_RDONLY, "WorkspaceReadOnly")
        return fd
    except BaseException:
        os.close(fd)
        raise
    finally:
        if proc is not None:
            os.close(proc)


def child_directory(parent, name):
    require(re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name not in {".", ".."}, "InvalidDirectoryName")
    created = False
    try:
        os.mkdir(name, 0o750, dir_fd=parent)
        created = True
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        if created:
            os.fchown(fd, ROOT_UID, GROUP)
            os.fchmod(fd, 0o750)
            os.fsync(parent)
        info = os.fstat(fd)
        require(info.st_uid == ROOT_UID and info.st_gid == GROUP and stat.S_IMODE(info.st_mode) == 0o750, "UnexpectedDirectoryOwnershipOrMode")
        require(info.st_dev == os.fstat(parent).st_dev, "UnexpectedNestedFilesystem")
        return fd
    except BaseException:
        os.close(fd)
        raise


def asset_parent(root, path):
    fd = os.dup(root)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            next_fd = child_directory(fd, part)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def file_identity(info, *, partial=False):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1, "UnexpectedFileIdentity")
    require(stat.S_IMODE(info.st_mode) in ({0o600, 0o440} if partial else {0o440}), "UnexpectedFileMode")
    if not partial:
        require(info.st_gid == GROUP, "UnexpectedFileGroup")


def hash_contents(fd, expected, deadline):
    require(os.fstat(fd).st_size <= expected, "ExistingFileTooLarge")
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        deadline.check()
        data = os.read(fd, min(CHUNK, expected-size+1))
        if not data:
            break
        size += len(data)
        require(size <= expected, "ExistingFileTooLarge")
        digest.update(data)
    return digest, size


def asset_identity(asset):
    return hashlib.sha256(json.dumps(asset, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 1

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), HttpsRedirects())


def validate_response(response, offset, expected):
    require(response.status == (206 if offset else 200), "RangeResponseRejected" if offset else "HttpStatusRejected")
    require(response.headers.get("Content-Encoding", "identity").lower() == "identity", "EncodedResponseRejected")
    length = response.headers.get("Content-Length", "")
    require(re.fullmatch(r"[0-9]+", length) is not None and int(length) == expected-offset, "ResponseLengthMismatch")
    if offset:
        value = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", value)
        require(match is not None and tuple(map(int, match.groups())) == (offset, expected-1, expected), "ContentRangeMismatch")


def append_bytes(fd, data, deadline):
    view = memoryview(data)
    while view:
        deadline.check()
        count = os.write(fd, view)
        require(count > 0, "PartialWriteFailed")
        view = view[count:]


def stage_asset(root, asset, deadline, client):
    deadline.check()
    parent, name = asset_parent(root, asset["path"])
    partial, metadata = "."+name+".partial", "."+name+".partial.json"
    stream = None
    try:
        try:
            final = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            final = None
        if final is not None:
            try:
                file_identity(os.fstat(final))
                digest, size = hash_contents(final, asset["bytes"], deadline)
                require(size == asset["bytes"] and "sha256:"+digest.hexdigest() == asset["sha256"], "ExistingAssetMismatch")
                return {"path":asset["path"], "bytes":size, "sha256":"sha256:"+digest.hexdigest(), "status":"already_verified"}
            finally:
                os.close(final)
        identity = {"schema_version":1, "asset_id":asset_identity(asset)}
        try:
            existing = read_file(parent, metadata, 4096)
        except FileNotFoundError:
            try:
                os.stat(partial, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise StageError("UnregisteredPartial")
            meta = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            try:
                os.fchmod(meta, 0o600)
                append_bytes(meta, json.dumps(identity).encode(), deadline)
                os.fsync(meta)
            finally:
                os.close(meta)
            os.fsync(parent)
        else:
            info = os.stat(metadata, dir_fd=parent, follow_symlinks=False)
            require(info.st_uid == ROOT_UID and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, "UnexpectedPartialMetadata")
            require(json.loads(existing) == identity, "PartialIdentityMismatch")
        stream = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=parent)
        file_identity(os.fstat(stream), partial=True)
        digest, size = hash_contents(stream, asset["bytes"], deadline)
        offset = size
        if size < asset["bytes"]:
            os.fchmod(stream, 0o600)
            if "inline_base64" in asset:
                data = base64.b64decode(asset["inline_base64"], validate=True)
                require(size == 0, "InlinePartialRequiresReview")
                append_bytes(stream, data, deadline)
                digest.update(data)
                size = len(data)
            else:
                headers = {"Accept-Encoding":"identity", "User-Agent":"probe-public-assets/1"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                request = urllib.request.Request(asset["url"], headers=headers, method="GET")
                with client.open(request, timeout=deadline.timeout()) as response:
                    validate_response(response, offset, asset["bytes"])
                    while True:
                        deadline.check()
                        data = response.read(min(CHUNK, asset["bytes"]-size+1))
                        if not data:
                            break
                        require(size+len(data) <= asset["bytes"], "DownloadTooLarge")
                        append_bytes(stream, data, deadline)
                        digest.update(data)
                        size += len(data)
        deadline.check()
        require(size == asset["bytes"], "DownloadIncomplete")
        actual = "sha256:"+digest.hexdigest()
        require(actual == asset["sha256"], "AssetHashMismatch")
        os.fsync(stream)
        os.fchown(stream, ROOT_UID, GROUP)
        os.fchmod(stream, 0o440)
        os.fsync(stream)
        # The private directory and process lock make this a single-writer
        # operation. Never replace an unrelated existing destination.
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StageError("DestinationAppearedDuringStaging")
        os.rename(partial, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        os.unlink(metadata, dir_fd=parent)
        os.fsync(parent)
        return {"path":asset["path"], "bytes":size, "sha256":actual, "status":"downloaded_verified", "resume_offset":offset}
    finally:
        if stream is not None:
            try:
                os.fsync(stream)
            finally:
                os.close(stream)
        os.close(parent)


def stage(manifest, workspace, deadline, manifest_hash):
    report = {"schema_version":1, "kind":"public_asset_staging", "all_assets_verified":False,
              "model_acceptance_performed":False, "scientific_evidence":False, "assets":[],
              "manifest_sha256":manifest_hash, "declared_bytes":sum(asset["bytes"] for asset in manifest["assets"]),
              "stop_margin_seconds":STOP_MARGIN, "observed_at":datetime.now(timezone.utc).isoformat(),
              "script_sha256":"sha256:"+hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    volume = root = lock = None
    try:
        deadline.check()
        require(Path(workspace) == VOLUME/"probe", "FixedWorkspaceRequired")
        volume = validate_volume(VOLUME)
        root = child_directory(volume, "probe")
        lock = os.open(".staging.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=root)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, "UnexpectedStagingLock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = opener()
        for asset in manifest["assets"]:
            report["assets"].append(stage_asset(root, asset, deadline, client))
        report["all_assets_verified"] = True
    except StageError as exc:
        report["error_code"] = str(exc)
    except urllib.error.HTTPError as exc:
        report["error_code"] = "HttpStatus"+str(exc.code)
    except Exception as exc:
        report["error_code"] = type(exc).__name__  # Never stringify URLs, redirects, or credential-bearing errors.
    finally:
        for fd in (lock, root, volume):
            if fd is not None:
                os.close(fd)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--deadline", required=True)
    parser.add_argument("--workspace", type=Path, default=VOLUME/"probe")
    args = parser.parse_args()
    def expired(*_):
        raise StageError("TransferDeadlineReached")
    signal.signal(signal.SIGALRM, expired)
    try:
        deadline = Deadline(args.deadline)
        signal.setitimer(signal.ITIMER_REAL, deadline.cutoff-time.monotonic())
        manifest, manifest_hash = load_manifest(args.manifest)
        report = stage(manifest, args.workspace, deadline, manifest_hash)
    except StageError as exc:
        report = {"schema_version":1, "kind":"public_asset_staging", "all_assets_verified":False,
                  "model_acceptance_performed":False, "error_code":str(exc)}
    except Exception as exc:
        report = {"schema_version":1, "kind":"public_asset_staging", "all_assets_verified":False,
                  "model_acceptance_performed":False, "error_code":type(exc).__name__}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["all_assets_verified"] else 2)


if __name__ == "__main__":
    main()
