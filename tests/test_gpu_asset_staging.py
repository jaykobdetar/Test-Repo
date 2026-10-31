"""Small synthetic payloads only; no public model downloads or live mounts."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / "deploy/gpu/stage-assets.py"
    spec = importlib.util.spec_from_file_location("gpu_asset_staging", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def asset(data=b"verified public test data", *, inline=False, path="models/base/model.safetensors"):
    result = {"path": path, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    result.update(
        {"inline_base64": base64.b64encode(data).decode()}
        if inline
        else {"url": "https://huggingface.co/public/pinned/model.safetensors"}
    )
    return result


class Clock:
    def check(self):
        pass

    def timeout(self):
        return 1


class Response(io.BytesIO):
    def __init__(self, data, *, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}


class Client:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


@pytest.fixture
def local(tmp_path, monkeypatch):
    implementation = module()
    # Exercise actual filesystem ownership/modes under the unprivileged test
    # account, without pretending that this proves root/worker live ownership.
    monkeypatch.setattr(implementation, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(implementation, "GROUP", os.getegid())
    root = implementation.directory(tmp_path)
    yield implementation, root, tmp_path
    os.close(root)


def test_streamed_hash_size_permissions_and_second_readback(local):
    implementation, root, path = local
    payload = b"small streamed fixture"
    entry = asset(payload)
    client = Client(Response(payload))
    report = implementation.stage_asset(root, entry, Clock(), client)
    target = path / entry["path"]
    assert report["sha256"] == entry["sha256"] and report["bytes"] == len(payload)
    assert target.read_bytes() == payload and stat.S_IMODE(target.stat().st_mode) == 0o440
    assert target.stat().st_gid == os.getegid()
    assert all(stat.S_IMODE(parent.stat().st_mode) == 0o750 for parent in (target.parent, target.parent.parent))
    assert not list(target.parent.glob(".*.partial*"))
    report = implementation.stage_asset(
        root,
        entry,
        Clock(),
        SimpleNamespace(open=lambda *_a, **_k: pytest.fail("existing verified file fetched again")),
    )
    assert report["status"] == "already_verified"


def prepare_partial(implementation, root, path, entry, prefix):
    parent, name = implementation.asset_parent(root, entry["path"])
    os.close(parent)
    target = path / entry["path"]
    partial = target.parent / ("." + name + ".partial")
    partial.write_bytes(prefix)
    partial.chmod(0o600)
    sidecar = target.parent / ("." + name + ".partial.json")
    sidecar.write_text(json.dumps({"schema_version": 1, "asset_id": implementation.asset_identity(entry)}))
    sidecar.chmod(0o600)
    return target, partial, sidecar


def test_resume_requires_exact_range_and_rehashes_entire_file(local):
    implementation, root, path = local
    payload = b"abcdefghij"
    entry = asset(payload)
    target, partial, _ = prepare_partial(implementation, root, path, entry, payload[:4])
    client = Client(Response(payload[4:], status=206, headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"}))
    report = implementation.stage_asset(root, entry, Clock(), client)
    assert client.requests[0][0].get_header("Range") == "bytes=4-"
    assert client.requests[0][0].get_header("Accept-encoding") == "identity"
    assert report["resume_offset"] == 4 and target.read_bytes() == payload and not partial.exists()


@pytest.mark.parametrize(
    "status,length,content_range",
    [
        (200, "10", None),
        (206, "6", "bytes 0-5/10"),
        (206, "6", "bytes 4-9/11"),
        (206, "5", "bytes 4-9/10"),
        (206, "6", "bytes 4-8/10"),
        (206, "6", None),
    ],
)
def test_bad_resume_headers_leave_existing_partial_unchanged(local, status, length, content_range):
    implementation, root, path = local
    payload = b"abcdefghij"
    entry = asset(payload)
    target, partial, _ = prepare_partial(implementation, root, path, entry, payload[:4])
    headers = {"Content-Length": length}
    if content_range is not None:
        headers["Content-Range"] = content_range
    with pytest.raises(implementation.StageError):
        implementation.stage_asset(root, entry, Clock(), Client(Response(payload[4:], status=status, headers=headers)))
    assert not target.exists() and partial.read_bytes() == payload[:4]


def test_corrupt_partial_cannot_pass_using_only_downloaded_suffix(local):
    implementation, root, path = local
    payload = b"abcdefghij"
    entry = asset(payload)
    target, partial, _ = prepare_partial(implementation, root, path, entry, b"XXXX")
    with pytest.raises(implementation.StageError, match="AssetHashMismatch"):
        implementation.stage_asset(
            root,
            entry,
            Clock(),
            Client(Response(payload[4:], status=206, headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"})),
        )
    assert not target.exists() and partial.read_bytes() == b"XXXXefghij"
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "data,code",
    [(b"abc", "DownloadIncomplete"), (b"abcdefghijk", "DownloadTooLarge"), (b"XXXXXXXXXX", "AssetHashMismatch")],
)
def test_download_length_and_hash_fail_before_publication(local, data, code):
    implementation, root, path = local
    entry = asset(b"abcdefghij")
    with pytest.raises(implementation.StageError, match=code):
        implementation.stage_asset(root, entry, Clock(), Client(Response(data, headers={"Content-Length": "10"})))
    assert not (path / entry["path"]).exists()


def test_deadline_interruption_keeps_known_resumable_private_prefix(local, monkeypatch):
    implementation, root, path = local
    entry = asset(b"abcdefghij")
    monkeypatch.setattr(implementation, "CHUNK", 4)

    class Interrupted(Response):
        def read(self, count=-1):
            if self.tell() >= 4:
                raise implementation.StageError("TransferDeadlineReached")
            return super().read(count)

    with pytest.raises(implementation.StageError, match="TransferDeadlineReached"):
        implementation.stage_asset(root, entry, Clock(), Client(Interrupted(b"abcdefghij")))
    target = path / entry["path"]
    assert not target.exists()
    assert (target.parent / ("." + target.name + ".partial")).read_bytes() == b"abcd"
    assert (target.parent / ("." + target.name + ".partial.json")).exists()


def test_inline_asset_is_validated_offline_and_published_without_http(local):
    implementation, root, path = local
    entry = asset(b"public dataset", inline=True, path="datasets/calibration.json")
    implementation.validate_manifest({"schema_version": 1, "assets": [entry]})
    report = implementation.stage_asset(
        root, entry, Clock(), SimpleNamespace(open=lambda *_a, **_k: pytest.fail("inline asset used network"))
    )
    assert report["bytes"] == 14 and (path / entry["path"]).read_bytes() == b"public dataset"


@pytest.mark.parametrize(
    "bad_path",
    [
        "/models/a",
        "models/../config/token",
        "models//a",
        "models/.private/a",
        "models/a/./b",
        "config/worker.json",
        "datasets",
        "models/a\\b",
    ],
)
def test_manifest_rejects_escape_and_nonasset_paths(bad_path):
    implementation = module()
    entry = asset(path=bad_path)
    with pytest.raises(implementation.StageError, match="InvalidAssetPath"):
        implementation.validate_manifest({"schema_version": 1, "assets": [entry]})


def test_declared_total_and_inline_integrity_are_checked_before_staging():
    implementation = module()
    first = asset(path="models/a")
    second = asset(path="models/b")
    first["bytes"] = second["bytes"] = 7_000_000_000
    with pytest.raises(implementation.StageError, match="DeclaredTotalExceeds12GB"):
        implementation.validate_manifest({"schema_version": 1, "assets": [first, second]})
    entry = asset(b"abc", inline=True)
    entry["inline_base64"] = base64.b64encode(b"XYZ").decode()
    with pytest.raises(implementation.StageError, match="InlineHashMismatch"):
        implementation.validate_manifest({"schema_version": 1, "assets": [entry]})


def test_partial_identity_conflicts_and_unregistered_partial_are_preserved(local):
    implementation, root, path = local
    entry = asset(b"abcdefghij")
    target, partial, sidecar = prepare_partial(implementation, root, path, entry, b"abcd")
    sidecar.write_text('{"schema_version":1,"asset_id":"wrong"}')
    with pytest.raises(implementation.StageError, match="PartialIdentityMismatch"):
        implementation.stage_asset(root, entry, Clock(), Client(Response(b"efghij")))
    sidecar.unlink()
    with pytest.raises(implementation.StageError, match="UnregisteredPartial"):
        implementation.stage_asset(root, entry, Clock(), Client(Response(b"efghij")))
    assert partial.read_bytes() == b"abcd" and not target.exists()


def test_symlinked_parent_or_partial_never_writes_outside_workspace(local):
    implementation, root, path = local
    outside = path / "outside"
    outside.mkdir()
    (path / "models").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        implementation.stage_asset(root, asset(), Clock(), Client(Response(b"anything")))
    assert list(outside.iterdir()) == []
    (path / "models").unlink()
    entry = asset(b"abcdefghij")
    _, partial, _ = prepare_partial(implementation, root, path, entry, b"abcd")
    untouched = outside / "untouched"
    untouched.write_text("untouched")
    partial.unlink()
    partial.symlink_to(untouched)
    with pytest.raises(OSError):
        implementation.stage_asset(root, entry, Clock(), Client(Response(b"anything")))
    assert untouched.read_text() == "untouched"


def test_deadline_reserves_twenty_seconds_and_requires_timezone():
    implementation = module()
    with pytest.raises(implementation.StageError, match="DeadlineMustBeAware"):
        implementation.Deadline("2026-09-19T12:00:00")
    with pytest.raises(implementation.StageError, match="TransferDeadlineReached"):
        implementation.Deadline((datetime.now(timezone.utc) + timedelta(seconds=19)).isoformat())
    deadline = implementation.Deadline((datetime.now(timezone.utc) + timedelta(seconds=23)).isoformat())
    assert 0 < deadline.timeout() <= 3


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/file",
        "https://user:secret@example.com/file",
        "file:///etc/passwd",
        "https://example.com:8443/file",
    ],
)
def test_source_and_redirect_urls_must_remain_credential_free_https(url):
    implementation = module()
    with pytest.raises(implementation.StageError, match="InvalidPublicHttpsUrl"):
        implementation.public_url(url)
    handler = implementation.HttpsRedirects()
    with pytest.raises(implementation.StageError, match="InvalidPublicHttpsUrl"):
        handler.redirect_request(urllib.request.Request("https://example.com/start"), None, 302, "", {}, url)


def test_error_report_never_includes_secret_url(local, monkeypatch):
    implementation, root, path = local
    monkeypatch.setattr(implementation, "VOLUME", path)
    monkeypatch.setattr(implementation, "validate_volume", lambda _: os.dup(root))

    def fail(*_a, **_k):
        raise urllib.error.URLError("https://example.com/?secret=do-not-print")

    monkeypatch.setattr(implementation, "opener", lambda: SimpleNamespace(open=fail))
    manifest = {"schema_version": 1, "assets": [asset()]}
    result = implementation.stage(manifest, path / "probe", Clock(), "sha256:" + "a" * 64)
    assert not result["all_assets_verified"] and result["error_code"] == "URLError"
    assert "do-not-print" not in json.dumps(result)
    assert not result["model_acceptance_performed"] and not result["scientific_evidence"]


@pytest.mark.parametrize(
    "has_mount,different_device,filesystem,passed",
    [
        (True, True, "nfs4", True),
        (False, True, "nfs4", False),
        (True, False, "nfs4", False),
        (True, True, "tmpfs", False),
        (True, True, "overlay", False),
    ],
)
def test_workspace_requires_exact_separate_persistent_mount(
    local, monkeypatch, has_mount, different_device, filesystem, passed
):
    implementation, root, path = local
    monkeypatch.setattr(implementation, "VOLUME", path)
    info = os.fstat(root)
    text = (
        f"40 20 {os.major(info.st_dev)}:{os.minor(info.st_dev)} / {path} rw - {filesystem} volume rw\n"
        if has_mount
        else ""
    )
    original_read = implementation.read_file
    original_stat = os.stat
    monkeypatch.setattr(
        implementation,
        "read_file",
        lambda fd, name, limit: text.encode() if name == "mountinfo" else original_read(fd, name, limit),
    )

    def fake_stat(target, *args, **kwargs):
        if target == "/":
            return SimpleNamespace(st_dev=info.st_dev + 1 if different_device else info.st_dev)
        return original_stat(target, *args, **kwargs)

    monkeypatch.setattr(implementation.os, "stat", fake_stat)
    if passed:
        verified = implementation.validate_volume(path)
        os.close(verified)
    else:
        with pytest.raises(implementation.StageError):
            implementation.validate_volume(path)


def test_complete_partial_is_rehashed_then_published_without_network(local):
    implementation, root, path = local
    data = b"already complete"
    entry = asset(data)
    target, partial, _ = prepare_partial(implementation, root, path, entry, data)
    report = implementation.stage_asset(
        root, entry, Clock(), SimpleNamespace(open=lambda *_a, **_k: pytest.fail("complete partial fetched again"))
    )
    assert report["resume_offset"] == len(data) and target.read_bytes() == data and not partial.exists()


def test_existing_bad_final_is_never_replaced(local):
    implementation, root, path = local
    entry = asset(b"expected")
    parent, name = implementation.asset_parent(root, entry["path"])
    os.close(parent)
    final = path / entry["path"]
    final.write_bytes(b"WRONG")
    final.chmod(0o440)
    with pytest.raises(implementation.StageError, match="ExistingAssetMismatch"):
        implementation.stage_asset(root, entry, Clock(), Client(Response(b"expected")))
    assert final.read_bytes() == b"WRONG"


def test_normalized_modes_survive_private_launcher_umask(local):
    implementation, root, path = local
    entry = asset(b"public", inline=True)
    previous = os.umask(0o077)
    try:
        implementation.stage_asset(root, entry, Clock(), SimpleNamespace())
    finally:
        os.umask(previous)
    target = path / entry["path"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o440
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o750


def test_http_client_disables_environment_proxies(monkeypatch):
    implementation = module()
    seen = []
    monkeypatch.setattr(
        implementation.urllib.request, "ProxyHandler", lambda options: seen.append(options) or "proxy-disabled"
    )
    monkeypatch.setattr(implementation.urllib.request, "build_opener", lambda *handlers: handlers)
    assert implementation.opener()[0] == "proxy-disabled" and seen == [{}]
