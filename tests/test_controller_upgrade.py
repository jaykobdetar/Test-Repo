"""Offline upgrade refusals and fail-closed ordering; no host services are used."""

import base64
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/upgrade-controller.py"
spec = importlib.util.spec_from_file_location("controller_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)
OWNER = os.geteuid()
DIST = "probe_core-0.2.0.dist-info"
NAME = "probe_core-0.2.0-py3-none-any.whl"
IMAGE = "sha256:" + "a" * 64


def write(path, raw, mode=0o600):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    for parent in missing:
        parent.chmod(0o755)
    path.write_bytes(raw)
    path.chmod(mode)


def wheel_bytes(code=b"VERSION = 'old'\n", dependency="pydantic==2.13.5", extras=None):
    members = {
        "probe_core/__init__.py": code,
        "probe_core/resources/seccomp.json": b"{}",
        DIST
        + "/METADATA": f"Metadata-Version: 2.4\nName: probe-core\nVersion: 0.2.0\nRequires-Python: >=3.13,<3.14\nRequires-Dist: {dependency}\n\n".encode(),
        DIST + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    members.update(extras or {})
    rows = [
        [name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="), str(len(raw))]
        for name, raw in members.items()
    ]
    rows.append([DIST + "/RECORD", "", ""])
    csvfile = io.StringIO()
    csv.writer(csvfile, lineterminator="\n").writerows(rows)
    members[DIST + "/RECORD"] = csvfile.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return output.getvalue()


def install_members(raw, site):
    for name, content in upgrade.inspect_wheel(raw)["members"].items():
        write(site / name, content, 0o644)


def make_original(root):
    old = wheel_bytes()
    write(root / NAME, old)
    write(root / "python/bin/python3.13", b"fixture interpreter\n", 0o755)
    write(root / "python/lib/module.py", b"SOURCE = 1\n")
    write(root / "python/lib/__pycache__/module.cpython-313.pyc", b"original cache")
    root.chmod(0o755)
    files = [
        {"path": str(p.relative_to(root)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in root.rglob("*")
        if p.is_file()
    ]
    manifest = json.dumps({"schema_version": 1, "files": files}).encode()
    write(root / "release-manifest.json", manifest)
    site = root / "venv/lib/python3.13/site-packages"
    install_members(old, site)
    (root / "venv/bin").mkdir(mode=0o755)
    (root / "venv/bin/python").symlink_to(root / "python/bin/python3.13")
    (root / "venv/lib64").symlink_to("lib")
    return old, hashlib.sha256(manifest).hexdigest(), site


def make_release(directory, raw=None):
    raw = raw or wheel_bytes(b"VERSION = 'new'\n")
    wheel, checker, manifest = (directory / name for name in (NAME, "identity.py", "upgrade-release.json"))
    write(wheel, raw)
    write(checker, (SCRIPT.parent / "verify-installed-identities.py").read_bytes())
    body = {
        "schema_version": 1,
        "source_commit": "b" * 40,
        "wheel_filename": NAME,
        "wheel_sha256": hashlib.sha256(raw).hexdigest(),
        "identity_checker_sha256": hashlib.sha256(checker.read_bytes()).hexdigest(),
    }
    write(manifest, json.dumps(body).encode())
    return SimpleNamespace(
        wheel=wheel,
        identity_checker=checker,
        release_manifest=manifest,
        release_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        human="human",
    ), body


def test_original_accepts_internal_links_and_root_generated_cache(tmp_path):
    root = tmp_path / "root"
    _, digest, _ = make_original(root)
    write(root / "python/lib/__pycache__/module.cpython-313.pyc", b"regenerated cache")
    assert upgrade.verify_original(root, digest, owner=OWNER)[0] == NAME
    (root / "venv/bin/python").unlink()
    (root / "venv/bin/python").symlink_to("/usr/bin/python3")
    with pytest.raises(upgrade.UpgradeError, match="RUNTIME_LINK_ESCAPES"):
        upgrade.verify_original(root, digest, owner=OWNER)


@pytest.mark.parametrize("change", ["manifest", "source", "installed", "extra", "writable"])
def test_original_refuses_changed_bytes_inventory_and_permissions(tmp_path, change):
    root = tmp_path / "root"
    _, digest, site = make_original(root)
    path = {
        "manifest": root / "release-manifest.json",
        "source": root / "python/lib/module.py",
        "installed": site / "probe_core/__init__.py",
        "extra": site / "probe_core/unreviewed.py",
    }.get(change)
    if path:
        write(path, b"changed")
    else:
        (site / "probe_core/__init__.py").chmod(0o666)
    with pytest.raises(upgrade.UpgradeError):
        upgrade.verify_original(root, digest, owner=OWNER)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "other_package/code.py", "probe_core/../escape.py"])
def test_wheel_cannot_write_outside_project(path):
    with pytest.raises(upgrade.UpgradeError):
        upgrade.inspect_wheel(wheel_bytes(extras={path: b"unexpected"}))


def test_wheel_record_tampering_and_replaced_offered_input(tmp_path):
    raw = wheel_bytes()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            target.writestr(name, b"tampered" if name == "probe_core/__init__.py" else source.read(name))
    with pytest.raises(upgrade.UpgradeError, match="WHEEL_RECORD_HASH"):
        upgrade.inspect_wheel(output.getvalue())
    path = tmp_path / NAME
    write(path, raw)
    captured = upgrade.pin_bytes(path, hashlib.sha256(raw).hexdigest())
    write(path, b"replaced after verification")
    assert captured == raw
    with pytest.raises(upgrade.UpgradeError, match="TARGET_INPUT_HASH"):
        upgrade.pin_bytes(path, hashlib.sha256(raw).hexdigest())


def counts():
    return dict(
        jobs=0,
        attempts=0,
        approvals=0,
        compute_requests=0,
        runpod_intents=0,
        audit_events=17,
        pods=0,
        network_volumes=0,
        local_containers=0,
        history_sha256="a" * 64,
        audit_tip="b" * 64,
        audit_prefix_sha256="b" * 64,
        idle=True,
        audit_valid=True,
    )


@pytest.mark.parametrize("key", ["pods", "local_containers", "idle", "audit_valid"])
def test_idle_scope_refuses_execution_resources_or_invalid_history(key):
    value = counts()
    upgrade.check_idle_counts(value)
    value[key] = False if key in {"idle", "audit_valid"} else 1
    with pytest.raises(upgrade.UpgradeError, match="IDLE_EXECUTION_REQUIRED"):
        upgrade.check_idle_counts(value)


def test_idle_scope_allows_verified_completed_history_and_retained_storage():
    value = counts()
    value.update(jobs=3, attempts=4, approvals=5, compute_requests=6, runpod_intents=5, network_volumes=1)
    upgrade.check_idle_counts(value)


def test_history_reader_requires_one_pinned_literal_and_does_not_execute_offered_code():
    with pytest.raises(upgrade.UpgradeError, match="MISSING"):
        upgrade.history_reader(b"STATE_READER = dangerous_call()")
    with pytest.raises(upgrade.UpgradeError, match="MISSING"):
        upgrade.history_reader(b"STATE_READER = 'a'\nSTATE_READER = 'b'")
    assert (
        upgrade.history_reader(b"raise RuntimeError('must not execute')\nSTATE_READER = 'reviewed reader'")
        == "reviewed reader"
    )


def acceptance():
    now = datetime.now(timezone.utc).isoformat()
    return {
        "status": "passed",
        "stage": "complete",
        "image": IMAGE,
        "service_uid": OWNER,
        "started_at": now,
        "finished_at": now,
        "checks": {name: True for name in upgrade.SANDBOX_CHECKS},
        "lifecycle": {
            key: True
            for key in (
                "program_started",
                "host_timer_excluded",
                "launchers_killed",
                "container_processes_stopped",
                "container_removed",
            )
        },
    }


def identity_acceptance():
    return {
        "schema_version": 1,
        "passed": True,
        "check_count": 26,
        "passed_count": 26,
        "checks": {"check_" + str(index): True for index in range(26)},
        "failure_codes": [],
        "normal_research_audit_events": 2,
        "paid_actions_performed": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def save_prior_upgrade(root, previous_raw, raw, *, previous=None, recovered=False):
    digest = hashlib.sha256(raw).hexdigest()
    directory = root / "upgrades" / digest
    checker = (SCRIPT.parent / "verify-installed-identities.py").read_bytes()
    manifest = {
        "schema_version": 2 if previous else 1,
        "source_commit": "c" * 40,
        "wheel_filename": NAME,
        "wheel_sha256": digest,
        "identity_checker_sha256": hashlib.sha256(checker).hexdigest(),
    }
    if previous:
        manifest["previous_upgrade"] = previous
    manifest_raw = json.dumps(manifest).encode()
    write(directory / "upgrade-release.json", manifest_raw)
    write(directory / NAME, raw)
    write(directory / "verify-installed-identities.py", checker)
    write(directory / "rollback" / NAME, previous_raw)
    write(directory / "sandbox-acceptance.json", json.dumps(acceptance()).encode())
    report_directory = directory / ("identity-recovery-" + "a" * 32) if recovered else directory
    write(report_directory / "identity-acceptance.json", json.dumps(identity_acceptance()).encode())
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "source_commit": manifest["source_commit"],
        "wheel_sha256": digest,
        "sandbox_checks": 16,
        "identity_checks": 26,
        "cloud_mutations_performed": False,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if recovered:
        receipt.update(
            cause_confirmed=True,
            research_parent_group_correct=True,
            normal_research_reads_audited=True,
            unit_before_sha256="d" * 64,
            unit_after_sha256="e" * 64,
            application_reinstalled=False,
        )
    else:
        receipt.update(previous_wheel_sha256=hashlib.sha256(previous_raw).hexdigest(), dependencies_unchanged=True)
    report_name = "recovery-receipt.json" if recovered else "upgrade-report.json"
    write(report_directory / report_name, json.dumps(receipt).encode())
    return (
        {"wheel_sha256": digest, "release_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest()},
        directory,
        report_directory / report_name,
    )


@pytest.mark.parametrize("recovered", [False, True])
def test_verified_prior_upgrade_is_the_installed_and_rollback_baseline(tmp_path, recovered):
    root = tmp_path / "root"
    old, digest, site = make_original(root)
    current = wheel_bytes(b"VERSION = 'accepted-first-upgrade'\n")
    previous, directory, receipt = save_prior_upgrade(root, old, current, recovered=recovered)
    install_members(current, site)
    name, raw, wheel, actual_site, evidence = upgrade.verify_baseline(root, digest, previous, owner=OWNER)
    assert name == NAME and raw == current and actual_site == site and wheel == upgrade.inspect_wheel(current)
    assert evidence == [
        {
            **previous,
            "completion_receipt": str(receipt.relative_to(directory)),
            "completion_receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "identity_report_sha256": hashlib.sha256(
                (receipt.parent / "identity-acceptance.json").read_bytes()
            ).hexdigest(),
            "sandbox_report_sha256": hashlib.sha256((directory / "sandbox-acceptance.json").read_bytes()).hexdigest(),
        }
    ]
    with pytest.raises(upgrade.UpgradeError, match="INSTALLED_PROJECT_BYTES_DIFFER"):
        upgrade.verify_original(root, digest, owner=OWNER)


def test_prior_upgrade_chain_is_rooted_in_original_and_checks_every_transition(tmp_path):
    root = tmp_path / "root"
    old, digest, site = make_original(root)
    first, second = wheel_bytes(b"first\n"), wheel_bytes(b"second\n")
    first_ref, first_dir, _ = save_prior_upgrade(root, old, first, recovered=True)
    second_ref, _, _ = save_prior_upgrade(root, first, second, previous=first_ref)
    install_members(second, site)
    result = upgrade.verify_baseline(root, digest, second_ref, owner=OWNER)
    assert result[1] == second and [item["wheel_sha256"] for item in result[4]] == [
        first_ref["wheel_sha256"],
        second_ref["wheel_sha256"],
    ]
    write(first_dir / "rollback" / NAME, first)
    with pytest.raises(upgrade.UpgradeError, match="ROLLBACK_HASH"):
        upgrade.verify_baseline(root, digest, second_ref, owner=OWNER)


@pytest.mark.parametrize(
    "fault", ["unconfirmed_cause", "wrong_identity_count", "legacy_only", "original_runtime_changed"]
)
def test_recovery_receipt_does_not_bypass_original_runtime_or_completed_repair(tmp_path, fault):
    root = tmp_path / "root"
    old, digest, site = make_original(root)
    current = wheel_bytes(b"recovered-upgrade\n")
    reference, directory, receipt_path = save_prior_upgrade(root, old, current, recovered=True)
    install_members(current, site)
    if fault == "original_runtime_changed":
        write(root / "python/lib/module.py", b"changed runtime")
    elif fault == "legacy_only":
        manifest = json.loads((directory / "upgrade-release.json").read_bytes())
        manifest["schema_version"] = 2
        with pytest.raises(upgrade.UpgradeError, match="PRIOR_UPGRADE_RECOVERY_INVALID"):
            upgrade.completed_upgrade(directory, manifest, old, owner=OWNER)
        return
    else:
        body = json.loads(receipt_path.read_bytes())
        body.update(
            {"unconfirmed_cause": {"cause_confirmed": False}, "wrong_identity_count": {"identity_checks": 25}}[fault]
        )
        write(receipt_path, json.dumps(body).encode())
    with pytest.raises(upgrade.UpgradeError):
        upgrade.verify_baseline(root, digest, reference, owner=OWNER)


@pytest.mark.parametrize(
    "fault",
    [
        "manifest",
        "wheel",
        "checker",
        "rollback",
        "installed",
        "receipt_missing",
        "receipt_failed",
        "wrong_receipt_wheel",
        "wrong_previous_wheel",
        "identity_failed",
        "sandbox_failed",
        "report_order",
        "receipt_writable",
        "receipt_symlink",
        "ambiguous_success",
        "dependency_change",
    ],
)
def test_prior_upgrade_evidence_failure_never_falls_back_to_original(tmp_path, fault):
    root = tmp_path / "root"
    old, digest, site = make_original(root)
    current = wheel_bytes(
        b"accepted-upgrade\n", dependency="pydantic==999" if fault == "dependency_change" else "pydantic==2.13.5"
    )
    reference, directory, receipt_path = save_prior_upgrade(root, old, current)
    install_members(current, site)
    if fault in {"manifest", "wheel", "checker", "rollback", "installed"}:
        path = {
            "manifest": directory / "upgrade-release.json",
            "wheel": directory / NAME,
            "checker": directory / "verify-installed-identities.py",
            "rollback": directory / "rollback" / NAME,
            "installed": site / "probe_core/__init__.py",
        }[fault]
        write(path, b"changed")
    elif fault == "receipt_missing":
        receipt_path.unlink()
    elif fault in {"receipt_failed", "wrong_receipt_wheel", "wrong_previous_wheel", "report_order"}:
        body = json.loads(receipt_path.read_bytes())
        body.update(
            {
                "receipt_failed": {"status": "failed"},
                "wrong_receipt_wheel": {"wheel_sha256": "f" * 64},
                "wrong_previous_wheel": {"previous_wheel_sha256": "f" * 64},
                "report_order": {"finished_at": "2000-01-01T00:00:00+00:00"},
            }[fault]
        )
        write(receipt_path, json.dumps(body).encode())
    elif fault in {"identity_failed", "sandbox_failed"}:
        path = directory / ("identity-acceptance.json" if fault == "identity_failed" else "sandbox-acceptance.json")
        body = json.loads(path.read_bytes())
        body["checks"][next(iter(body["checks"]))] = False
        write(path, json.dumps(body).encode())
    elif fault == "receipt_writable":
        receipt_path.chmod(0o666)
    elif fault == "receipt_symlink":
        receipt_path.rename(directory / "other-receipt.json")
        receipt_path.symlink_to("other-receipt.json")
    elif fault == "ambiguous_success":
        write(directory / ("identity-recovery-" + "a" * 32) / "recovery-receipt.json", receipt_path.read_bytes())
    with pytest.raises((upgrade.UpgradeError, OSError)):
        upgrade.verify_baseline(root, digest, reference, owner=OWNER)


@pytest.mark.parametrize(
    "previous",
    [None, {}, {"wheel_sha256": "a" * 64}, {"wheel_sha256": "../escape", "release_manifest_sha256": "b" * 64}],
)
def test_second_upgrade_manifest_requires_exact_pinned_previous_release(tmp_path, previous):
    args, manifest = make_release(tmp_path / "release")
    manifest.update(schema_version=2, previous_upgrade=previous)
    raw = json.dumps(manifest).encode()
    write(args.release_manifest, raw)
    with pytest.raises(upgrade.UpgradeError, match="PREVIOUS_UPGRADE_SCHEMA"):
        upgrade.read_release(args.release_manifest, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("change", ["old14", "stale", "launchers_alive", "host_timer", "wrong_image"])
def test_new_gate_requires_fresh_independent_crash_cleanup(change):
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    report = acceptance()
    if change == "old14":
        report["checks"].pop("crash_deadline_enforced")
        report["checks"].pop("crash_removal_confirmed")
    elif change == "stale":
        report["started_at"] = (before - timedelta(seconds=1)).isoformat()
    elif change == "launchers_alive":
        report["lifecycle"]["launchers_killed"] = False
    elif change == "host_timer":
        report["lifecycle"]["host_timer_excluded"] = False
    else:
        report["image"] = "sha256:" + "c" * 64
    with pytest.raises(upgrade.UpgradeError):
        upgrade.validate_acceptance(report, IMAGE, OWNER, before)


def test_readiness_waits_for_listener_and_times_out_without_retrying_identity_gate():
    clock, attempts = [0.0], []

    def sleep(seconds):
        clock[0] += seconds

    def check():
        attempts.append(True)
        return len(attempts) >= 3

    upgrade.wait_ready(check, monotonic=lambda: clock[0], sleep=sleep, timeout=1)
    assert len(attempts) == 3
    with pytest.raises(upgrade.UpgradeError, match="LISTENER_NOT_READY"):
        upgrade.wait_ready(lambda: False, monotonic=lambda: clock[0], sleep=sleep, timeout=1)


class FakeHost:
    def __init__(self, root, units, site, report):
        self.root, self.units, self.site, self.report = root, units, site, report
        self.commands, self.counts, self.fail, self.race = [], counts(), None, False
        self.state = {
            n: ("active" if n in upgrade.SERVICES or n.endswith(".timer") else "inactive")
            for n in (*upgrade.SERVICES, *upgrade.OTHER_UNITS)
        }

    def __call__(self, command, **options):
        self.commands.append(command)
        assert options["cwd"] == self.root and options["env"] == upgrade.ENV and options["umask"] == 0o022
        output = b""
        if self.fail == "pip" and "pip" in command:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"private install diagnostic")
        if command[0] == "/usr/bin/systemctl":
            action = command[1]
            if action == "show":
                name = command[2]
                output = (
                    (self.state[name] + "\n")
                    if "--value" in command
                    else f"LoadState=loaded\nFragmentPath={self.units / name}\nDropInPaths=\nActiveState={self.state[name]}\nUnitFileState=enabled\n"
                ).encode()
            elif action in {"stop", "start", "restart"}:
                if action == "restart" and self.fail == "final_restart":
                    assert json.loads((self.root.parent / "etc/research.json").read_bytes())["sandbox_image"] == IMAGE
                    return SimpleNamespace(returncode=1, stdout=b"", stderr=b"late restart failed")
                for name in command[2:]:
                    if name in upgrade.SERVICES or name.endswith(".timer"):
                        self.state[name] = "inactive" if action == "stop" else "active"
                if action == "stop" and "probe-controller.service" in command and self.race:
                    self.counts["idle"] = False
                if action == "start" and "probe-sandbox-acceptance.service" in command:
                    assert self.state["probe-research.service"] == "inactive"
                    if self.fail == "sandbox":
                        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"sandbox failed")
                    write(self.report, json.dumps(acceptance()).encode())
        elif command[0] == "/usr/sbin/runuser":
            assert command[1:6] == ["-u", "probe-trusted", "-g", "probe-trusted", "--"]
            if "/usr/bin/podman" in command:
                assert "HOME=/var/lib/probe-sandbox" in command and command[-4:] == [
                    "ps",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                ]
                output = (("0" * 64 + "\n") * self.counts["local_containers"]).encode()
            else:
                assert command[-1].endswith(upgrade.READ_IDLE) and "def idle_history_snapshot(" in command[-1]
                output = json.dumps(self.counts).encode()
        elif command[-1] == upgrade.DEPENDENCIES:
            output = b'[["pydantic", "2.13.5"]]\n'
        elif "pip" in command:
            assert command[-4:-1] == ["--no-index", "--no-deps", "--force-reinstall"]
            install_members(Path(command[-1]).read_bytes(), self.site)
        elif command[0] == "/usr/bin/python3":
            assert not (self.units / "probe-research.service.d").exists()
            assert json.loads((self.root.parent / "etc/research.json").read_bytes())["sandbox_image"] is None
            if self.fail == "identity":
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"identity failed")
            self.counts["audit_events"] += 2
            write(Path(command[-1]), json.dumps(identity_acceptance()).encode())
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"")


@pytest.fixture
def host(tmp_path, monkeypatch):
    root, config, units = (tmp_path / name for name in ("root", "etc", "units"))
    old, digest, site = make_original(root)
    args, release = make_release(tmp_path / "offered")
    args.original_manifest_sha256 = digest
    for name in (*upgrade.SERVICES, *upgrade.OTHER_UNITS):
        write(units / name, b"[Unit]\nDescription=fixture\n", 0o644)
    users = {
        name: OWNER + offset
        for offset, name in enumerate(("probe-trusted", "probe-research", "probe-watchdog", "probe-backup", "human"))
    }
    monkeypatch.setattr(upgrade, "discover_users", lambda _: users)
    original_config = json.dumps(
        {
            "sandbox_image": IMAGE,
            "service_uid": OWNER,
            "research_uid": OWNER + 1,
            "admin_uid": OWNER + 4,
            "private_setting": "preserve exactly",
        },
        indent=2,
    ).encode()
    write(config / "research.json", original_config, 0o640)
    provenance = b"SOURCE_COMMIT=" + b"a" * 40 + b"\nDRIVE_FOLDER_ID=unchanged-public-folder\n"
    write(config / "backup.env", provenance, 0o640)
    backup_copy = tmp_path / "backup/backup.env"
    write(backup_copy, provenance)
    original_read = upgrade.read_file

    def read(path, **kwargs):
        if Path(path) == backup_copy:
            kwargs["owner"] = OWNER
        return original_read(path, **kwargs)

    monkeypatch.setattr(upgrade, "read_file", read)
    report = tmp_path / "sandbox/acceptance-report.json"
    fake = FakeHost(root, units, site, report)
    operation = upgrade.Upgrade(
        root=root, config=config, units=units, owner=OWNER, run=fake, backup_copy=backup_copy, sandbox_report=report
    )
    readiness = []
    operation.ready = lambda _: readiness.append(operation.stage)
    return SimpleNamespace(
        root=root,
        config=config,
        units=units,
        old=old,
        args=args,
        release=release,
        original_config=original_config,
        provenance=provenance,
        fake=fake,
        operation=operation,
        backup_copy=backup_copy,
        site=site,
        readiness=readiness,
    )


def test_success_preserves_dependencies_and_enables_only_after_both_gates(host):
    result = host.operation.execute(host.args)
    assert result["status"] == "passed" and result["sandbox_checks"] == 16
    assert (host.config / "research.json").read_bytes() == host.original_config
    assert not (host.config / "upgrade-blocked").exists() and not (host.units / "probe-controller.service.d").exists()
    assert (host.operation.work / "rollback" / NAME).read_bytes() == host.old == (host.root / NAME).read_bytes()
    for path in (host.config / "backup.env", host.backup_copy):
        assert path.read_bytes() == host.provenance.replace(b"a" * 40, b"b" * 40)
    commands = host.fake.commands
    first_stop = next(i for i, c in enumerate(commands) if c[:2] == ["/usr/bin/systemctl", "stop"])
    assert sum(c[0] == "/usr/sbin/runuser" for c in commands[:first_stop]) == 4
    assert ["/usr/bin/systemctl", "start", "probe-backup.service"] in commands[:first_stop]
    gate = commands.index(["/usr/bin/systemctl", "start", "probe-sandbox-acceptance.service"])
    identity = next(i for i, c in enumerate(commands) if c[0] == "/usr/bin/python3")
    assert first_stop < gate < identity < commands.index(["/usr/bin/systemctl", "restart", "probe-research.service"])
    assert host.readiness == ["identity_acceptance", "restore_research"]
    assert stat.S_IMODE(host.operation.work.stat().st_mode) == 0o700
    assert stat.S_IMODE((host.operation.work / NAME).stat().st_mode) == 0o600


def test_second_upgrade_uses_verified_recovered_wheel_for_rollback(host):
    current = wheel_bytes(b"VERSION = 'recovered-current'\n")
    reference, _, _ = save_prior_upgrade(host.root, host.old, current, recovered=True)
    install_members(current, host.site)
    release = {**host.release, "schema_version": 2, "previous_upgrade": reference}
    raw = json.dumps(release).encode()
    write(host.args.release_manifest, raw)
    host.args.release_manifest_sha256 = hashlib.sha256(raw).hexdigest()
    result = host.operation.execute(host.args)
    assert result["previous_wheel_sha256"] == hashlib.sha256(current).hexdigest()
    assert (host.operation.work / "rollback" / NAME).read_bytes() == current
    assert (host.root / NAME).read_bytes() == host.old
    assert result["previous_upgrade_evidence"][0]["wheel_sha256"] == reference["wheel_sha256"]
    selected = {"wheel_sha256": release["wheel_sha256"], "release_manifest_sha256": host.args.release_manifest_sha256}
    assert (
        upgrade.verify_baseline(host.root, host.args.original_manifest_sha256, selected, owner=OWNER)[1]
        == host.args.wheel.read_bytes()
    )


def test_second_upgrade_gate_failure_retains_current_rollback_and_guards(host):
    current = wheel_bytes(b"VERSION = 'recovered-current'\n")
    reference, _, _ = save_prior_upgrade(host.root, host.old, current, recovered=True)
    install_members(current, host.site)
    raw = json.dumps({**host.release, "schema_version": 2, "previous_upgrade": reference}).encode()
    write(host.args.release_manifest, raw)
    host.args.release_manifest_sha256 = hashlib.sha256(raw).hexdigest()
    host.fake.fail = "identity"
    with pytest.raises(upgrade.UpgradeError):
        host.operation.execute(host.args)
    assert host.operation.closed_after_failure and (host.config / "upgrade-blocked").exists()
    assert (host.operation.work / "rollback" / NAME).read_bytes() == current
    assert all(host.fake.state[name] == "inactive" for name in upgrade.GUARDED)


@pytest.fixture
def bridge_host(host, tmp_path, monkeypatch):
    operation = host.operation
    operation.runtime = tmp_path / "run"
    operation.runtime_units = operation.runtime / "systemd/system"
    operation.runtime_units.mkdir(mode=0o755, parents=True)
    operation.runtime.chmod(0o755)
    operation.backup_state = tmp_path / "backup-state"
    operation.backup_state.mkdir(mode=0o700)
    operation.backup_outbox = tmp_path / "outbox"
    operation.backup_receipts = tmp_path / "receipts"
    operation.backup_outbox.mkdir()
    operation.backup_receipts.mkdir()
    users = {
        name: OWNER + offset
        for offset, name in enumerate(("probe-trusted", "probe-research", "probe-watchdog", "probe-backup", "human"))
    }
    users["probe-backup"] = OWNER  # Fake service writes under the test identity.
    monkeypatch.setattr(upgrade, "discover_users", lambda _: users)
    write(host.units / "probe-backup.service", (SCRIPT.parent / "live/probe-backup.service").read_bytes(), 0o644)
    transport = b"# exact reviewed test transport\n"
    args, release = make_release(
        host.args.release_manifest.parent,
        wheel_bytes(b"VERSION = 'bridge'\n", extras={"probe_core/backup.py": transport}),
    )
    args.original_manifest_sha256 = host.args.original_manifest_sha256
    release.update(
        schema_version=3, previous_upgrade=None, backup_transport_sha256=hashlib.sha256(transport).hexdigest()
    )
    write(args.release_manifest, json.dumps(release).encode())
    args.release_manifest_sha256 = hashlib.sha256(args.release_manifest.read_bytes()).hexdigest()
    host.args, host.release, host.transport = args, release, transport
    host.bridge_phases, host.bridge_fault = [], None
    original_run = operation.run

    def run(command, **options):
        override = operation.runtime_units / "probe-backup.service.d/90-probe-upgrade-backup.conf"
        if command[:2] == ["/usr/bin/systemctl", "show"] and "MainPID" in command[-1]:
            host.fake.commands.append(command)
            name = command[2]
            paths = str(override) if name == "probe-backup.service" and override.exists() else ""
            state = host.fake.state[name]
            pid = "123" if host.bridge_fault == "unconfirmed_stop" and host.bridge_phases else "0"
            return SimpleNamespace(
                returncode=0,
                stderr=b"",
                stdout=(
                    f"LoadState=loaded\nFragmentPath={host.units / name}\n"
                    f"DropInPaths={paths}\nActiveState={state}\nMainPID={pid}\nControlPID=0\nKillMode=control-group\n"
                ).encode(),
            )
        if command == ["/usr/bin/systemctl", "start", "probe-backup.service"]:
            host.fake.commands.append(command)
            directory = next(
                path for path in operation.runtime.iterdir() if path.name.startswith("probe-upgrade-backup-")
            )
            assert directory.stat().st_mode & 0o777 == 0o755
            assert (directory / "backup.py").read_bytes() == transport
            assert all(
                (directory / name).stat().st_mode & 0o777 == 0o644
                for name in ("backup.py", "bridge.py", "history.py", "request.json")
            )
            assert "User=" not in override.read_text() and "Requires=" not in override.read_text()
            assert (host.site / "probe_core/__init__.py").read_bytes() == b"VERSION = 'old'\n"
            request = json.loads((directory / "request.json").read_bytes())
            phase = request["phase"]
            host.bridge_phases.append(phase)
            report = {
                "schema_version": 1,
                "phase": phase,
                "ok": True,
                "snapshot_ids": ["a" * 64] if phase == "drain" else ["a" * 64, "c" * 64],
            }
            if phase == "fresh":
                report["fresh"] = {
                    "snapshot_id": "c" * 64,
                    "archive_sha256": "d" * 64,
                    "created_at": request["not_before"],
                    "history_sha256": request["baseline"]["history_sha256"],
                    "audit_prefix_sha256": request["baseline"]["audit_tip"],
                    "audit_tip": {
                        "sequence": request["baseline"]["audit_events"],
                        "hash": request["baseline"]["audit_tip"],
                    },
                }
            if host.bridge_fault == "stale" and phase == "fresh":
                report["fresh"]["snapshot_id"] = "a" * 64
            if host.bridge_fault == "history" and phase == "fresh":
                report["fresh"]["history_sha256"] = "e" * 64
            if host.bridge_fault in {"failed", "timeout"}:
                report.update(
                    ok=False, error_type="BackupError", error_code="BACKUP_TRANSPORT_CAT_RATE_LIMITED_AFTER_3_ATTEMPTS"
                )
            write(Path(request["report"]), json.dumps(report).encode())
            if host.bridge_fault == "timeout":
                raise subprocess.TimeoutExpired(command, options["timeout"])
            return SimpleNamespace(returncode=int(not report["ok"]), stderr=b"", stdout=b"")
        if "pip" in command:
            assert not override.parent.exists()
            assert not list(operation.runtime.glob("probe-upgrade-backup-*"))
            assert host.bridge_phases == ["drain", "fresh"]
        return original_run(command, **options)

    operation.run = run
    return host


def test_schema3_bridge_preserves_gate_and_removes_override_before_install(bridge_host):
    host = bridge_host
    result = host.operation.execute(host.args)
    assert result["backup_bridge"]["transport_sha256"] == host.release["backup_transport_sha256"]
    assert result["backup_bridge"]["snapshot_id"] == "c" * 64
    assert host.bridge_phases == ["drain", "fresh"]
    assert not list(host.operation.backup_state.iterdir())
    assert host.fake.state["probe-backup.timer"] == "active"
    reference = {
        "wheel_sha256": host.release["wheel_sha256"],
        "release_manifest_sha256": host.args.release_manifest_sha256,
    }
    assert (
        upgrade.verify_baseline(host.root, host.args.original_manifest_sha256, reference, owner=OWNER)[1]
        == host.args.wheel.read_bytes()
    )


@pytest.mark.parametrize("fault", ["failed", "timeout", "stale", "history"])
def test_schema3_failed_backup_never_installs_and_restores_original_schedule(bridge_host, fault):
    host = bridge_host
    host.bridge_fault = fault
    with pytest.raises(upgrade.UpgradeError):
        host.operation.execute(host.args)
    assert not host.operation.changed and host.operation.work is None
    assert not any("pip" in command for command in host.fake.commands)
    assert not (host.operation.runtime_units / "probe-backup.service.d").exists()
    assert not list(host.operation.runtime.glob("probe-upgrade-backup-*"))
    assert host.fake.state["probe-backup.timer"] == "active"
    assert (host.config / "research.json").read_bytes() == host.original_config


def test_schema3_unconfirmed_shutdown_keeps_code_and_timer_paused(bridge_host):
    host = bridge_host
    host.bridge_fault = "unconfirmed_stop"
    with pytest.raises(upgrade.UpgradeError, match="SHUTDOWN_UNCONFIRMED"):
        host.operation.execute(host.args)
    assert (host.operation.runtime_units / "probe-backup.service.d/90-probe-upgrade-backup.conf").exists()
    assert list(host.operation.runtime.glob("probe-upgrade-backup-*"))
    assert host.fake.state["probe-backup.timer"] == "inactive"
    assert not any("pip" in command for command in host.fake.commands)


def test_schema3_wrong_transport_pin_refuses_before_service_or_installed_code(bridge_host):
    host = bridge_host
    host.release["backup_transport_sha256"] = "f" * 64
    raw = json.dumps(host.release).encode()
    write(host.args.release_manifest, raw)
    host.args.release_manifest_sha256 = hashlib.sha256(raw).hexdigest()
    with pytest.raises(upgrade.UpgradeError, match="BACKUP_TRANSPORT_HASH"):
        host.operation.execute(host.args)
    assert host.fake.commands == []


def test_schema3_override_publish_failure_removes_own_partial_state(bridge_host, monkeypatch):
    host = bridge_host
    original = upgrade.os.rename

    def fail(source, destination):
        if Path(destination).name == "90-probe-upgrade-backup.conf":
            raise OSError("injected rename failure")
        return original(source, destination)

    monkeypatch.setattr(upgrade.os, "rename", fail)
    with pytest.raises(OSError):
        host.operation.execute(host.args)
    assert not (host.operation.runtime_units / "probe-backup.service.d").exists()
    assert not list(host.operation.runtime.glob("probe-upgrade-backup-*"))
    assert host.fake.state["probe-backup.timer"] == "active"
    assert not host.bridge_phases


def test_schema3_timer_stop_timeout_restores_schedule(bridge_host):
    host = bridge_host
    original = host.operation.run

    def timeout(command, **options):
        result = original(command, **options)
        if command == ["/usr/bin/systemctl", "stop", "probe-backup.timer"]:
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return result

    host.operation.run = timeout
    with pytest.raises(subprocess.TimeoutExpired):
        host.operation.execute(host.args)
    assert host.fake.state["probe-backup.timer"] == "active"
    assert not list(host.operation.runtime.glob("probe-upgrade-backup-*"))


@pytest.mark.parametrize(
    "fault", [None, "old_snapshot", "different_history", "different_audit", "sensitive_error", "quota"]
)
def test_bridge_wrapper_checks_real_snapshot_history_without_network(tmp_path, monkeypatch, fault):
    from contextlib import closing
    import sqlite3
    import sys
    from probe_core.artifact_store import ArtifactStore
    from probe_core.backup import create_snapshot, verify_snapshot, restore_snapshot
    from probe_core.ledger import Ledger
    from probe_core.controller import Controller

    code = tmp_path / "code"
    code.mkdir(mode=0o755)
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    outbox.mkdir()
    receipts.mkdir()
    provider = tmp_path / "provider.sqlite"
    with closing(sqlite3.connect(provider)) as connection:
        connection.execute("CREATE TABLE runpod_intents (worker_id TEXT PRIMARY KEY)")
        connection.commit()
    history_raw = upgrade.history_reader((SCRIPT.parent / "verify-installed-identities.py").read_bytes())
    history_namespace = {}
    exec(history_raw, history_namespace)
    store = ArtifactStore(tmp_path / "inputs")
    before = datetime.now(timezone.utc)
    with Ledger(tmp_path / "live.sqlite") as ledger:
        Controller(ledger, object(), watchdog_health_path=tmp_path / "unused-health", controller_idle_usd_per_day=0)
        ledger.record_event("tool_call", {"tool": "local_bridge_acceptance"})
        baseline = history_namespace["idle_history_snapshot"](tmp_path / "live.sqlite", provider)
        snapshot = create_snapshot(
            ledger,
            outbox / "probe-snapshot.tar",
            input_store=store.root,
            source_commit="a" * 40,
            provider_database=provider,
        )
    verified = verify_snapshot(snapshot["archive"], expected_sha256=snapshot["archive_sha256"])
    restored = restore_snapshot(snapshot["archive"], tmp_path / "restored", expected_sha256=snapshot["archive_sha256"])
    results = [{**verified, "readback_verified": True, "restore_verified": restored["restored"]}]
    transport = (SCRIPT.parent.parent / "probe_core/backup.py").read_bytes()
    write(code / "backup.py", transport, 0o644)
    write(code / "history.py", history_raw.encode(), 0o644)
    request = {
        "phase": "fresh",
        "transport_sha256": hashlib.sha256(transport).hexdigest(),
        "history_sha256": hashlib.sha256(history_raw.encode()).hexdigest(),
        "baseline": baseline,
        "not_before": before.isoformat(),
        "drained_ids": [],
        "outbox": str(outbox),
        "receipts": str(receipts),
        "credential": str(tmp_path / "never-read.conf"),
        "report": str(tmp_path / "report.json"),
    }
    if fault == "old_snapshot":
        request["not_before"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    if fault == "different_history":
        request["baseline"]["history_sha256"] = "f" * 64
    if fault == "different_audit":
        request["baseline"]["audit_tip"] = "f" * 64
    write(code / "request.json", json.dumps(request).encode(), 0o644)
    namespace = {"__name__": "bridge_unit_test", "__file__": str(code / "bridge.py")}
    exec(upgrade.BACKUP_BRIDGE, namespace)
    # Service ownership is covered by the host/profile tests. This unit test
    # executes as the developer UID and substitutes only that root read check.
    namespace["checked"] = lambda path: path.read_bytes()
    original_spec = importlib.util.spec_from_file_location

    def module_spec(name, path):
        value = original_spec(name, path)
        original_exec = value.loader.exec_module

        def load(module):
            original_exec(module)

            def uploaded():
                if fault == "sensitive_error":
                    raise module.BackupError("secret-must-not-appear")
                if fault == "quota":
                    raise module.BackupError("BACKUP_TRANSPORT_CAT_RATE_LIMITED_AFTER_3_ATTEMPTS")
                print(json.dumps(results))

            module.main = uploaded

        value.loader.exec_module = load
        return value

    monkeypatch.setattr(importlib.util, "spec_from_file_location", module_spec)
    monkeypatch.setattr(sys, "argv", ["bridge", "--drive-folder-id", "public-folder-id"])
    status = namespace["main"]()
    raw = Path(request["report"]).read_text()
    report = json.loads(raw)
    assert Path(request["report"]).stat().st_mode & 0o777 == 0o600
    assert status == (0 if fault is None else 1)
    assert report["ok"] is (fault is None)
    assert "secret-must-not-appear" not in raw
    if fault is None:
        assert report["fresh"]["history_sha256"] == baseline["history_sha256"]
        assert report["fresh"]["audit_prefix_sha256"] == baseline["audit_tip"]
        assert report["fresh"]["archive_sha256"] == snapshot["archive_sha256"]
    if fault == "quota":
        assert report["error_code"] == "BACKUP_TRANSPORT_CAT_RATE_LIMITED_AFTER_3_ATTEMPTS"


def test_failed_previous_completion_refuses_before_any_service_or_installed_code(host):
    current = wheel_bytes(b"VERSION = 'incomplete'\n")
    reference, _, receipt = save_prior_upgrade(host.root, host.old, current)
    receipt.unlink()
    install_members(current, host.site)
    raw = json.dumps({**host.release, "schema_version": 2, "previous_upgrade": reference}).encode()
    write(host.args.release_manifest, raw)
    host.args.release_manifest_sha256 = hashlib.sha256(raw).hexdigest()
    with pytest.raises(upgrade.UpgradeError, match="COMPLETION_MISSING"):
        host.operation.execute(host.args)
    assert host.fake.commands == [] and not host.operation.changed
    assert (host.config / "research.json").read_bytes() == host.original_config


@pytest.mark.parametrize("failure", ["pip", "sandbox", "identity", "final_restart"])
def test_failures_leave_persistent_guard_and_disabled_config(host, failure):
    host.fake.fail = failure
    with pytest.raises(upgrade.UpgradeError):
        host.operation.execute(host.args)
    assert (
        host.operation.closed_after_failure
        and json.loads((host.config / "research.json").read_bytes())["sandbox_image"] is None
    )
    assert (host.config / "upgrade-blocked").is_file()
    for name in upgrade.GUARDED:
        assert (host.units / (name + ".d") / upgrade.GUARD_NAME).read_bytes() == upgrade.GUARD
        assert host.fake.state[name] == "inactive"
    expected = host.provenance.replace(b"a" * 40, b"b" * 40) if failure == "final_restart" else host.provenance
    assert (host.config / "backup.env").read_bytes() == expected
    assert (host.operation.work / "rollback" / NAME).read_bytes() == host.old
    assert not (host.operation.work / "upgrade-report.json").exists()


@pytest.mark.parametrize("failure", ["idle", "pods", "local_containers", "manifest", "dependencies"])
def test_preflight_refusal_does_not_stop_services_or_change_config(host, failure):
    if failure in host.fake.counts:
        host.fake.counts[failure] = False if failure == "idle" else 1
    elif failure == "manifest":
        write(host.args.release_manifest, b"{}")
    else:
        args, _ = make_release(host.args.release_manifest.parent, wheel_bytes(b"new", dependency="pydantic==999"))
        args.original_manifest_sha256 = host.args.original_manifest_sha256
        host.args = args
    with pytest.raises(upgrade.UpgradeError):
        host.operation.execute(host.args)
    assert (host.config / "research.json").read_bytes() == host.original_config and not host.operation.changed
    assert not any(c[:2] == ["/usr/bin/systemctl", "stop"] for c in host.fake.commands)


def test_racing_work_keeps_old_watchdog_and_broker_alive(host):
    host.fake.race = True
    with pytest.raises(upgrade.UpgradeError, match="IDLE_EXECUTION_REQUIRED"):
        host.operation.execute(host.args)
    assert all(host.fake.state[n] == "active" for n in ("probe-watchdog.service", "probe-provider-stop.service"))
    assert not any("pip" in c for c in host.fake.commands)


@pytest.mark.parametrize("changed", ["history_sha256", "audit_prefix_sha256"])
def test_same_count_history_or_audit_changes_abort_before_replacement(host, changed):
    original_run = host.operation.run

    def mutate_after_stop(command, **kwargs):
        result = original_run(command, **kwargs)
        if command[:2] == ["/usr/bin/systemctl", "stop"] and "probe-controller.service" in command:
            host.fake.counts[changed] = "f" * 64
        return result

    host.operation.run = mutate_after_stop
    with pytest.raises(upgrade.UpgradeError, match="HISTORICAL_STATE_CHANGED"):
        host.operation.execute(host.args)
    assert host.operation.closed_after_failure
    assert all(host.fake.state[name] == "active" for name in ("probe-watchdog.service", "probe-provider-stop.service"))
    assert not any("pip" in command for command in host.fake.commands)


def test_failed_persistence_cannot_prevent_both_guard_and_stop_attempts(host):
    calls = []
    host.operation.write_config = lambda _: (_ for _ in ()).throw(OSError("disk full"))
    host.operation.disabled_config = b"{}"
    host.operation.guard = lambda enabled: calls.append(("guard", enabled))
    host.operation.systemctl = lambda *args, **kwargs: calls.append(args)
    with pytest.raises(upgrade.UpgradeError, match="UNCONFIRMED"):
        host.operation.fail_closed()
    assert calls == [("guard", True), ("stop", "probe-research.service", "probe-controller.service")]
    assert host.operation.closed_after_failure is False


def test_main_uses_actual_administrator_check():
    if os.geteuid() == 0:
        pytest.skip("actual non-administrator identity required")
    assert upgrade.main([]) == 1
