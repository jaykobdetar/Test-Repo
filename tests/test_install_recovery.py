"""Conservative interrupted-install recovery, without administrator changes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/resume-controller-install.py"
spec = importlib.util.spec_from_file_location("install_recovery", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "installed"
    root.mkdir(mode=0o700)
    executable = root / "python/bin/python3.13"
    executable.parent.mkdir(mode=0o700, parents=True)
    shutil.copyfile("/usr/bin/true", executable)
    executable.chmod(0o600)
    (executable.parent / "python").symlink_to("python3.13")
    installer = root / "deploy/install-controller.sh"
    installer.parent.mkdir(mode=0o700)
    installer.write_text('echo "earlier install must not run"\nexit 99\n'
        + f'{root}/python/bin/python3.13 -I -m venv {root}/venv\n'
        + '/usr/bin/printf "%s\\n" "$PROBE_HUMAN" "$PROBE_RCLONE_SOURCE" "$PROBE_PROVIDER_SOURCE" > '
        + shlex.quote(str(root / "continued")) + '\n')
    (root / "controller-requirements.lock").write_text("# fixture\n")
    (root / "deployment.json").write_text("{}\n")
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            path.chmod(0o700)
        if path.is_file():
            entries.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            if not path.is_symlink():
                path.chmod(0o600)
    manifest = root / "release-manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "source_commit": "a" * 40, "files": entries}))
    manifest.chmod(0o600)
    return root, hashlib.sha256(manifest.read_bytes()).hexdigest()


def verify(bundle):
    return recovery.verify_bundle(*bundle, expected_uid=os.getuid())


def test_verifies_the_entire_existing_bundle_and_internal_alias(bundle):
    root, _ = bundle
    assert verify(bundle) == (root / "deploy/install-controller.sh").read_bytes()
    assert (root / "python/bin/python").is_symlink()
    assert stat.S_IMODE((root / "python/bin/python3.13").stat().st_mode) == 0o600


@pytest.mark.parametrize("damage", ["manifest", "member", "extra_file", "later_directory", "escaping_link", "writable", "hardlink"])
def test_bundle_tampering_or_unexpected_state_is_preserved_and_rejected(bundle, tmp_path, damage):
    root, _ = bundle
    if damage == "manifest":
        (root / "release-manifest.json").write_text("{}")
    elif damage == "member":
        (root / "controller-requirements.lock").write_text("modified")
    elif damage == "extra_file":
        (root / ".unexpected").write_text("preserve me")
    elif damage == "later_directory":
        (root / "venv").mkdir()
    elif damage == "escaping_link":
        alias = root / "python/bin/python"
        alias.unlink()
        alias.symlink_to("/usr/bin/true")
    elif damage == "writable":
        (root / "deployment.json").chmod(0o620)
    else:
        os.link(root / "deployment.json", tmp_path / "outside-hardlink")
    before = set(root.rglob("*"))
    with pytest.raises(recovery.RecoveryError):
        verify(bundle)
    assert set(root.rglob("*")) == before
    assert stat.S_IMODE((root / "python/bin/python3.13").stat().st_mode) == 0o600


def test_requires_the_expected_administrator_ownership(bundle):
    with pytest.raises(recovery.RecoveryError, match="administrator-owned"):
        recovery.verify_bundle(*bundle, expected_uid=os.getuid() + 1)


@pytest.fixture
def partial_state(tmp_path):
    root = tmp_path / "filesystem"
    root.mkdir()
    # Use the real directory-creation commands from the installer; this catches
    # inherited setgid behavior that a hand-built permissions fixture misses.
    # Only replay directory creation before this recovery's failed first exec.
    # Later installation phases may create additional runtime configuration.
    installer = SCRIPT.with_name("install-controller.sh").read_text().split(
        "/opt/probe-core/python/bin/python3.13 -I -m venv /opt/probe-core/venv\n", 1)[0]
    for line in installer.splitlines():
        if not line.startswith("install -d "):
            continue
        tokens = shlex.split(line)
        tokens[0] = "/usr/bin/install"
        for index, token in enumerate(tokens):
            if index and tokens[index - 1] == "-o":
                tokens[index] = str(os.getuid())
            elif index and tokens[index - 1] == "-g":
                tokens[index] = str(os.getgid())
            elif token.startswith(("/etc/", "/var/")):
                tokens[index] = str(root / token.lstrip("/"))
        subprocess.run(tokens, check=True)
    users = {name: os.getuid() for name in recovery.ACCOUNTS}
    groups = {name: os.getgid() for name in recovery.GROUPS}
    return root, users, groups


def test_accepts_exact_empty_state_created_by_original_directory_commands(partial_state):
    root, users, groups = partial_state
    recovery.verify_partial_state(users, groups, filesystem_root=root, root_uid=os.getuid())
    assert stat.S_IMODE((root / "var/lib/probe-core/input-artifacts").stat().st_mode) == 0o2700


@pytest.mark.parametrize("relative", ["etc/probe-core/runpod.json", "var/lib/probe-core/research.sqlite",
    "var/lib/probe-provider/runpod.sqlite", "var/lib/probe-sandbox/.runtime", "var/lib/probe-backup/rclone.conf",
    "var/lib/probe-backups/outbox/existing.tar", "var/lib/probe-backups/receipts/existing.json"])
def test_later_state_always_aborts_without_overwrite_or_deletion(partial_state, relative):
    root, users, groups = partial_state
    existing = root / relative
    existing.write_text("existing data must survive")
    with pytest.raises(recovery.RecoveryError, match="Unexpected later"):
        recovery.verify_partial_state(users, groups, filesystem_root=root, root_uid=os.getuid())
    assert existing.read_text() == "existing data must survive"


@pytest.mark.parametrize("text", ["", "probe-trusted:165536:65535", "probe-trusted:0:65536",
    "probe-trusted:165536:65536\nother:200000:65536", "probe-trusted:165536:65536\nprobe-trusted:165537:65536",
    "probe-trusted:165536:65536\nmalformed", "probe-trusted:4294967290:65536"])
def test_invalid_or_overlapping_subordinate_ids_are_rejected(text):
    with pytest.raises(recovery.RecoveryError):
        recovery.verify_subordinate_ranges(text)


def test_valid_subordinate_ranges_preserve_existing_other_allocations():
    recovery.verify_subordinate_ranges("other:100000:65536\nprobe-trusted:165536:65536\n")


def test_loaded_unit_is_rejected_even_if_unit_file_is_absent(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"probe-controller.service loaded active running\n" if command[1] == "list-units" else b"", b"")

    with pytest.raises(recovery.RecoveryError, match="loaded Probe"):
        recovery.verify_no_units(run=run, filesystem_root=tmp_path)
    assert [call[1] for call in calls] == ["list-unit-files", "list-units"]


def test_unit_symlink_is_rejected_without_querying_or_removing_it(tmp_path):
    link = tmp_path / "etc/systemd/system/multi-user.target.wants/probe-controller.service"
    link.parent.mkdir(parents=True)
    link.symlink_to("/nonexistent")
    with pytest.raises(recovery.RecoveryError, match="unit files"):
        recovery.verify_no_units(run=lambda *args, **kwargs: pytest.fail("must fail before query"), filesystem_root=tmp_path)
    assert link.is_symlink()


def test_ubuntu_unit_file_empty_no_match_status_is_accepted(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if command[1] == "list-unit-files" else 0, b"", b"")

    recovery.verify_no_units(run=run, filesystem_root=tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize("operation,stderr", [("list-unit-files", b"Failed to connect to bus"), ("list-units", b"")])
def test_failed_systemd_inspection_is_not_mistaken_for_no_matches(tmp_path, operation, stderr):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1 if command[1] == operation else 0, b"", stderr if command[1] == operation else b"")

    with pytest.raises(recovery.RecoveryError, match="inspection failed"):
        recovery.verify_no_units(run=run, filesystem_root=tmp_path)


def test_continuation_executes_only_verified_tail_and_restores_only_interpreter(bundle, tmp_path):
    root, _ = bundle
    old = verify(bundle)
    hostile_looking_path = tmp_path / "rclone'; touch unexpected; #.conf"
    script = recovery.continuation_script(old, human="human", rclone_config=hostile_looking_path,
        provider_directory=tmp_path / "private provider", installed_root=root)
    assert "earlier install must not run" not in script
    assert recovery.execute_continuation(root, script, temporary_parent=tmp_path) == 0
    assert (root / "continued").read_text().splitlines() == ["human", str(hostile_looking_path), str(tmp_path / "private provider")]
    assert (root / "deploy/install-controller.sh").read_bytes() == old
    assert stat.S_IMODE((root / "python/bin/python3.13").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "deployment.json").stat().st_mode) == 0o600
    subprocess.run([str(root / "python/bin/python")], check=True)
    assert not (root / "unexpected").exists()
    assert not list(tmp_path.glob(".probe-resume-*"))


@pytest.mark.parametrize("count", [0, 2])
def test_continuation_requires_unique_exact_boundary(count):
    marker = "/opt/probe-core/python/bin/python3.13 -I -m venv /opt/probe-core/venv\n"
    with pytest.raises(recovery.RecoveryError, match="unique exact"):
        recovery.continuation_script((marker * count).encode(), human="human", rclone_config=Path("/private/drive"),
                                     provider_directory=Path("/private/provider"))


def test_administrator_requirement_is_checked_by_real_process_without_mocking():
    if os.geteuid() == 0:
        pytest.skip("this check requires an actually unprivileged test process")
    result = subprocess.run(["/usr/bin/python3", "-I", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "must be run explicitly by the administrator" in result.stderr
