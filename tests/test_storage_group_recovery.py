"""Narrow recovery guards; real rootless failure/pass is verified separately.

These tests use real directories, descriptors, modes, links and file contents.
Only ownership changes unavailable to an unprivileged test account are modeled.
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

import pytest


PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("storage_group_recovery", PROJECT / "deploy/resume-controller-runtime.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def identity(info):
    return info.st_dev, info.st_ino


@pytest.fixture
def storage(tmp_path, monkeypatch):
    home = tmp_path / "sandbox"
    home.mkdir(mode=0o700)
    paths = [home / relative for relative in recovery.STORAGE_ANCESTORS]
    for path in paths:
        path.mkdir(mode=0o700)
    overlay = paths[-1] / "overlay"
    overlay.mkdir(mode=0o700)
    layer = overlay / "retained-layer"
    layer.mkdir(mode=0o700)
    (layer / "image-bytes").write_bytes(b"immutable image content\x00\xff")
    (home / "unrelated-private-file").write_bytes(b"private content")
    uid, gid, stale_gid = os.getuid(), os.getgid(), os.getgid() + 1000000
    real_fstat = os.fstat
    metadata = {identity(path.stat()): {"st_gid": stale_gid} for path in paths}
    calls = []

    def fstat(fd):
        actual = real_fstat(fd)
        updates = metadata.get(identity(actual))
        if updates is None:
            return actual
        values = {name: getattr(actual, name) for name in dir(actual) if name.startswith("st_")}
        return SimpleNamespace(**(values | updates))

    def fchown(fd, owner, group):
        key = identity(real_fstat(fd))
        assert key in metadata, "repair must not chown any descendant or unrelated inode"
        assert owner == -1 and group == gid, "repair must preserve owners"
        calls.append(key)
        metadata[key]["st_gid"] = group

    monkeypatch.setattr(recovery.os, "fstat", fstat)
    monkeypatch.setattr(recovery.os, "fchown", fchown)
    return SimpleNamespace(
        home=home,
        paths=paths,
        overlay=overlay,
        uid=uid,
        gid=gid,
        stale_gid=stale_gid,
        metadata=metadata,
        calls=calls,
        fchown=fchown,
        real_fstat=real_fstat,
    )


def repair(storage):
    return recovery.repair_storage_groups(storage.home, storage.uid, storage.gid, storage.stale_gid)


def snapshot(home):
    return {
        str(path.relative_to(home)): (
            path.lstat().st_mode,
            path.lstat().st_uid,
            path.lstat().st_gid,
            path.read_bytes() if path.is_file() else None,
        )
        for path in home.rglob("*")
    }


def test_only_four_groups_change_while_image_contents_and_private_modes_remain(storage):
    before = snapshot(storage.home)
    assert repair(storage) == 4
    assert storage.calls == [identity(path.stat()) for path in storage.paths]
    assert all(value["st_gid"] == storage.gid for value in storage.metadata.values())
    assert snapshot(storage.home) == before
    storage.calls.clear()
    assert repair(storage) == 0
    assert storage.calls == []


@pytest.mark.parametrize("position", range(4))
@pytest.mark.parametrize("field", ["st_uid", "st_gid"])
def test_unexpected_owner_on_any_ancestor_prevents_every_change(storage, position, field):
    key = identity(storage.paths[position].stat())
    storage.metadata[key][field] = 4000000
    before = snapshot(storage.home)
    with pytest.raises(RuntimeError, match="Unexpected storage ancestor"):
        repair(storage)
    assert storage.calls == [] and snapshot(storage.home) == before


@pytest.mark.parametrize("target", ["home", "last_ancestor", "overlay"])
@pytest.mark.parametrize("mode", [0o750, 0o701, 0o2700])
def test_permission_changes_are_refused_before_any_group_change(storage, target, mode):
    path = {"home": storage.home, "last_ancestor": storage.paths[-1], "overlay": storage.overlay}[target]
    path.chmod(mode)
    before = snapshot(storage.home)
    with pytest.raises(RuntimeError):
        repair(storage)
    assert storage.calls == [] and snapshot(storage.home) == before


@pytest.mark.parametrize("position", range(5))
def test_symlink_at_any_storage_component_is_not_followed(storage, position):
    path = [*storage.paths, storage.overlay][position]
    retained = path.with_name(path.name + "-retained")
    path.rename(retained)
    path.symlink_to(retained, target_is_directory=True)
    with pytest.raises(OSError):
        repair(storage)
    assert storage.calls == [] and path.is_symlink()


def test_symlink_home_and_missing_overlay_do_not_change_ancestors(storage, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(storage.home, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinks"):
        recovery.repair_storage_groups(alias, storage.uid, storage.gid, storage.stale_gid)
    storage.overlay.rename(storage.overlay.with_name("retained-overlay"))
    with pytest.raises(FileNotFoundError):
        repair(storage)
    assert storage.calls == []


def test_interrupted_prefix_is_safely_retried_without_rewriting_completed_groups(storage, monkeypatch):
    before = snapshot(storage.home)
    attempts = 0

    def interrupt(fd, owner, group):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            raise OSError("injected interruption before third group change")
        storage.fchown(fd, owner, group)

    monkeypatch.setattr(recovery.os, "fchown", interrupt)
    with pytest.raises(OSError, match="injected interruption"):
        repair(storage)
    assert len(storage.calls) == 2
    monkeypatch.setattr(recovery.os, "fchown", storage.fchown)
    assert repair(storage) == 2
    assert storage.calls == [identity(path.stat()) for path in storage.paths]
    assert snapshot(storage.home) == before


def test_already_corrected_nonprefix_groups_are_preserved(storage):
    for path in storage.paths[::2]:
        storage.metadata[identity(path.stat())]["st_gid"] = storage.gid
    assert repair(storage) == 2
    assert storage.calls == [identity(path.stat()) for path in storage.paths[1::2]]


def test_equal_old_and_new_groups_fail_without_changes(storage):
    with pytest.raises(RuntimeError, match="must differ"):
        recovery.repair_storage_groups(storage.home, storage.uid, storage.gid, storage.gid)
    assert storage.calls == []


@pytest.fixture
def runtime_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    for relative in (".config", ".config/containers"):
        (home / relative).mkdir(mode=0o750)
    config = home / ".config/containers/containers.conf"
    config.write_bytes(recovery.RUNTIME_CONFIG)
    config.chmod(0o640)
    return home, config


def test_existing_runtime_config_requires_exact_bytes_and_metadata(runtime_config):
    home, _ = runtime_config
    before = snapshot(home)
    recovery.verify_installed_runtime_config(home, os.getgid(), owner=os.getuid())
    assert snapshot(home) == before


@pytest.mark.parametrize(
    "change", ["bytes", "file_mode", "parent_mode", "symlink", "hardlink", "parent_symlink", "owner", "group"]
)
def test_unexpected_runtime_config_is_refused_unchanged(runtime_config, change):
    home, config = runtime_config
    owner, group = os.getuid(), os.getgid()
    if change == "bytes":
        config.write_bytes(recovery.RUNTIME_CONFIG + b"\n")
    elif change == "file_mode":
        config.chmod(0o600)
    elif change == "parent_mode":
        config.parent.chmod(0o700)
    elif change == "symlink":
        target = config.with_name("retained.conf")
        config.rename(target)
        config.symlink_to(target)
    elif change == "hardlink":
        os.link(config, config.with_name("linked.conf"))
    elif change == "parent_symlink":
        original = config.parent
        retained = original.with_name("retained")
        original.rename(retained)
        original.symlink_to(retained, target_is_directory=True)
    elif change == "owner":
        owner += 1
    else:
        group += 1
    before = snapshot(home)
    with pytest.raises(RuntimeError):
        recovery.verify_installed_runtime_config(home, group, owner=owner)
    assert snapshot(home) == before


@pytest.mark.parametrize(
    "mapping,containers,accepted",
    [
        ("0 982 1\n1 165536 65536\n", "", True),
        ("0 981 2\n", "", False),
        ("0 999 1\n", "", False),
        ("0 982 0\n", "", False),
        ("0 982 -1\n", "", False),
        ("unexpected\n", "", False),
        ("", "", False),
        ("0 982 1\n", "retained-container-id\n", False),
    ],
)
def test_mapping_and_empty_container_inventory_are_required(mapping, containers, accepted):
    calls = []

    def run(command, **kwargs):
        assert command[:7] == ["/usr/sbin/runuser", "-u", "probe-trusted", "-g", "probe-trusted", "--", "env"]
        assert kwargs["cwd"] == recovery.ROOT and kwargs["timeout"] == 30
        assert kwargs["env"] == recovery.ENV and kwargs["stdin"] == subprocess.DEVNULL
        arguments = command[command.index("--remote=false") + 1 :]
        calls.append(arguments)
        assert arguments in (["unshare", "/usr/bin/cat", "/proc/self/gid_map"], ["ps", "--all", "--quiet"])
        output = mapping if arguments[0] == "unshare" else containers
        return subprocess.CompletedProcess(command, 0, output.encode(), b"")

    if accepted:
        recovery.verify_storage_runtime(994, 982, 981, run=run)
        assert len(calls) == 2
    else:
        with pytest.raises(RuntimeError):
            recovery.verify_storage_runtime(994, 982, 981, run=run)
        assert len(calls) == (2 if containers else 1)


def test_failed_inventory_command_cannot_be_mistaken_for_empty_inventory():
    def run(command, **kwargs):
        if "unshare" in command:
            return subprocess.CompletedProcess(command, 0, b"0 982 1\n", b"")
        return subprocess.CompletedProcess(command, 125, b"", b"inventory unavailable")

    with pytest.raises(RuntimeError, match="inventory unavailable"):
        recovery.verify_storage_runtime(994, 982, 981, run=run)


@pytest.mark.parametrize("failure", [None, "state", "config", "mapping", "image", "syntax", "repair", "no_opt_in"])
def test_opt_in_orchestration_checks_original_state_before_mutation(tmp_path, monkeypatch, failure):
    root = tmp_path / "installed"
    root.mkdir()
    image = "sha256:" + "a" * 64
    (root / "deployment.json").write_text(json.dumps({"sandbox_image": image}))
    original = b"reviewed original installer bytes"
    events = []

    def event(name, result=None):
        def call(*args, **kwargs):
            events.append(name)
            if failure == name:
                raise RuntimeError("injected " + name)
            return result

        return call

    verifier = SimpleNamespace(
        verify_runtime=event("release", original),
        identities=event("identities", ({"probe-trusted": 994}, {"probe-trusted": 982, "probe-research": 981})),
        expected_units=event("expected_units", {}),
        verify_units=event("units"),
        verify_state=event("state"),
    )
    monkeypatch.setattr(recovery.os, "geteuid", lambda: 0)
    monkeypatch.setattr(recovery, "ROOT", root)
    monkeypatch.setattr(recovery, "load_verifier", event("verifier", verifier))
    monkeypatch.setattr(recovery, "verify_installed_runtime_config", event("config"))
    monkeypatch.setattr(recovery, "verify_selected_runtime", event("runtime"))
    monkeypatch.setattr(recovery, "verify_storage_runtime", event("mapping"))
    monkeypatch.setattr(recovery, "verify_image", event("image"))
    monkeypatch.setattr(recovery, "repair_storage_groups", event("repair", 4))

    def refuse_existing_config(*args):
        events.append("config_location")
        raise RuntimeError("Existing container runtime configuration will not be overwritten")

    monkeypatch.setattr(recovery, "verify_config_location", refuse_existing_config)

    def continuation(installer, uid):
        assert installer == original and uid == 994
        events.append("continuation")
        return "#!/bin/bash\nexit 23\n"

    monkeypatch.setattr(recovery, "continuation", continuation)
    real_temporary_directory = tempfile.TemporaryDirectory
    monkeypatch.setattr(
        recovery.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: real_temporary_directory(prefix=kwargs["prefix"], dir=tmp_path),
    )

    def run(command, **kwargs):
        assert command[0] == "/bin/bash", "storage repair must neither install packages nor import images"
        assert kwargs["cwd"] == root and kwargs["env"] == recovery.ENV
        name = "syntax" if "-n" in command else "gate_continuation"
        events.append(name)
        if failure == name:
            raise subprocess.CalledProcessError(2, command)
        return subprocess.CompletedProcess(command, 0 if name == "syntax" else 23)

    monkeypatch.setattr(recovery.subprocess, "run", run)
    before = snapshot(root)
    arguments = [
        "--verification-helper",
        "pinned-verifier.py",
        "--verification-sha256",
        "a" * 64,
        "--manifest-sha256",
        "b" * 64,
        "--human",
        "human",
    ]
    if failure != "no_opt_in":
        arguments.append("--repair-stale-storage-groups")
    result = recovery.main(arguments)
    expected = [
        "verifier",
        "release",
        "identities",
        "expected_units",
        "units",
        "state",
        "config",
        "runtime",
        "mapping",
        "image",
        "continuation",
        "syntax",
        "repair",
        "gate_continuation",
    ]
    if failure == "no_opt_in":
        assert events == expected[:6] + ["config_location"]
    else:
        assert events == (expected if failure is None else expected[: expected.index(failure) + 1])
    assert result == (23 if failure is None else 1)
    assert snapshot(root) == before
