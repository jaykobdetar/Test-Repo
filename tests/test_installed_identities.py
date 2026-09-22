from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from probe_core.ledger import Ledger
from probe_core.audit import canonical_json
from probe_core.controller import Controller
from probe_core.provider import DeploymentSpec
from probe_core.research_api import ResearchPolicy, ResearchService
from probe_core.rpc import UnixRPCServer
from probe_core.runpod_provider import RunPodConfig, RunPodLaunchConfig, RunPodProvider, StorageRates
from probe_core.schemas import ApprovalNonce, JobSpec


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "deploy/verify-installed-identities.py"
spec = importlib.util.spec_from_file_location("installed_identities", SCRIPT)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def local_plan(checks):
    return {
        "role": "research",
        "account": "local-test",
        "uid": os.getuid(),
        "gid": os.getgid(),
        "groups": os.getgroups(),
        "checks": checks,
    }


def child(checks, **updates):
    plan = local_plan(checks) | updates
    result = subprocess.run(
        [sys.executable, "-I", "-c", verify.CHILD],
        input=json.dumps(plan).encode(),
        capture_output=True,
        env=verify.ENV,
        cwd="/",
        timeout=10,
    )
    assert result.returncode == 0, "child must return only bounded boolean results"
    return json.loads(result.stdout)


def file_check(path, kind="read_file"):
    return {"code": "file_access", "kind": kind, "path": str(path)}


def rpc_check(path, kind="lab_status", **values):
    return {"code": "rpc_access", "kind": kind, "path": str(path), "server_uid": os.getuid(), **values}


def test_child_reads_without_returning_any_credential_bytes(tmp_path):
    path = tmp_path / "credential"
    path.write_bytes(b"SECRET THAT MUST NEVER APPEAR")
    path.chmod(0o600)
    result = child([file_check(path)])
    assert result == {"identity": True, "checks": {"file_access": True}}
    assert "SECRET" not in json.dumps(result)
    assert child([file_check(path, "deny_read")])["checks"]["file_access"] is False


def test_actual_permission_denial_counts_but_missing_empty_and_symlink_do_not(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("real unprivileged filesystem denial required")
    path = tmp_path / "private"
    path.write_bytes(b"private")
    path.chmod(0o000)
    assert child([file_check(path, "deny_read")])["checks"]["file_access"] is True
    assert child([file_check(tmp_path / "missing", "deny_read")])["checks"]["file_access"] is False
    empty = tmp_path / "empty"
    empty.touch()
    assert child([file_check(empty)])["checks"]["file_access"] is False
    link = tmp_path / "link"
    link.symlink_to(path)
    assert child([file_check(link, "deny_read")])["checks"]["file_access"] is False


@pytest.mark.parametrize("changed", ["uid", "gid", "groups"])
def test_wrong_effective_identity_prevents_checks(tmp_path, changed):
    path = tmp_path / "readable"
    path.write_text("public")
    value = [] if changed == "groups" and os.getgroups() else [1234567] if changed == "groups" else 1234567
    assert child([file_check(path)], **{changed: value}) == {"identity": False, "checks": {"file_access": False}}


@pytest.fixture
def rpc_server(tmp_path):
    servers = []

    def start(dispatch):
        directory = tmp_path / str(len(servers))
        directory.mkdir(mode=0o700)
        server = UnixRPCServer(directory / "rpc.sock", dispatch, allowed_uids={os.getuid()}, allow_service_uid=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server.path

    yield start
    for server, thread in servers:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_real_research_rpc_reads_only_append_normal_audit(tmp_path, rpc_server):
    with Ledger(tmp_path / "research.sqlite") as ledger:
        service = ResearchService(ledger, ResearchPolicy())
        path = rpc_server(service.dispatch)
        for kind in ("lab_status", "discovery"):
            assert child([rpc_check(path, kind)])["checks"]["rpc_access"] is True
        assert ledger.list_jobs() == []
        with ledger.read_connection() as connection:
            assert connection.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        records = ledger.audit_records()
        assert [record["payload"]["tool"] for record in records] == ["lab_status", "query_runs"]


def test_stop_probe_uses_only_status_and_checks_absent_worker(rpc_server):
    requests = []

    def dispatch(method, params):
        requests.append((method, params))
        return {"worker_id": params["worker_id"], "state": "ABSENT", "provider_id": None}

    path = rpc_server(dispatch)
    check = rpc_check(path, "stop_status", worker_id="identity-check-test")
    assert child([check])["checks"]["rpc_access"] is True
    assert requests == [("status", {"worker_id": "identity-check-test"})]


def test_peer_uid_is_verified_before_any_request(rpc_server):
    requests = []
    path = rpc_server(lambda method, params: requests.append(method) or [])
    check = rpc_check(path, "controller_status", server_uid=os.getuid() + 1)
    assert child([check])["checks"]["rpc_access"] is False
    assert requests == []


def test_socket_connection_denial_is_distinguished_from_missing_or_available(rpc_server, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("real unprivileged socket permission denial required")
    path = rpc_server(lambda method, params: [])
    assert child([rpc_check(path, "deny_connect")])["checks"]["rpc_access"] is False
    path.chmod(0o000)
    assert child([rpc_check(path, "deny_connect")])["checks"]["rpc_access"] is True
    assert child([rpc_check(tmp_path / "missing.sock", "deny_connect")])["checks"]["rpc_access"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"jobs": [], "gpu_start_authority": True, "evaluation_authority": False},
        {"jobs": [], "gpu_start_authority": False},
        "PRIVATE_SERVER_ERROR",
    ],
)
def test_unexpected_status_contract_fails_without_disclosing_payload(rpc_server, body):
    path = rpc_server(lambda method, params: body)
    result = child([rpc_check(path)])
    assert result["checks"]["rpc_access"] is False
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "kind,states,allowed",
    [
        ("discovery", ["COMPLETED", "FAILED"], True),
        ("discovery", ["COMPLETED", "RUNNING"], False),
        ("controller_status", ["STOPPED", "REJECTED", "PENDING"], True),
        ("controller_status", ["STOPPED", "UNCERTAIN"], False),
    ],
)
def test_rpc_idle_views_retain_history_but_reject_active_states(rpc_server, kind, states, allowed):
    path = rpc_server(lambda method, params: [{"state": state} for state in states])
    assert child([rpc_check(path, kind)])["checks"]["rpc_access"] is allowed


def test_runuser_receives_exact_groups_and_accessible_cwd(tmp_path, monkeypatch):
    path = tmp_path / "readable"
    path.write_text("content")
    plan = local_plan([file_check(path)])
    monkeypatch.setattr(verify.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name="group" + str(gid)))

    def run(command, **kwargs):
        expected = ["/usr/sbin/runuser", "-u", "local-test", "-g", "group" + str(plan["gid"])]
        for gid in plan["groups"]:
            expected += ["-G", "group" + str(gid)]
        assert command == expected + ["--", "/usr/bin/python3", "-I", "-c", verify.CHILD]
        assert kwargs["cwd"] == "/" and kwargs["env"] == verify.ENV and kwargs["timeout"] == 30
        return subprocess.run([sys.executable, "-I", "-c", verify.CHILD], **kwargs)

    assert verify.run_probe(plan, run=run) == {"research_identity": True, "file_access": True}


@pytest.mark.parametrize(
    "payload", [b'{"identity":true,"checks":{}}', b'{"identity":true,"checks":{"file_access":1}}', b"x" * 8193]
)
def test_invalid_child_results_never_count_as_success(payload):
    plan = local_plan([file_check("/not-read")])
    plan["groups"] = []
    with pytest.raises(verify.CheckFailure):
        verify.run_probe(
            plan, run=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, payload, b"SECRET")
        )


@pytest.fixture
def installed_units(tmp_path):
    unit_root, proc_root = tmp_path / "units", tmp_path / "proc"
    unit_root.mkdir()
    proc_root.mkdir()
    groups = {name: 3000 + index for index, name in enumerate(verify.GROUPS)}
    users = {
        name: SimpleNamespace(pw_uid=2000 + index, pw_gid=groups[name]) for index, name in enumerate(verify.ACCOUNTS)
    }
    memberships = {name: set() for name in users}
    memberships["probe-watchdog"] = {groups["probe-ledger-read"], groups["probe-stop"], groups["probe-watch-read"]}
    values = {}
    for index, (name, (account, primary, supplements, module, command)) in enumerate(verify.UNITS.items()):
        path = unit_root / name
        path.write_text("[Service]\n")
        path.chmod(0o644)
        research = name == "probe-research.service"
        pid = 500 + index if module else 0
        values[name] = {
            "LoadState": "loaded",
            "ActiveState": "active" if module else "inactive",
            "SubState": "running" if module else "dead",
            "MainPID": str(pid),
            "User": account,
            "Group": primary,
            "SupplementaryGroups": " ".join(sorted(supplements)),
            "FragmentPath": str(path),
            "DropInPaths": "",
            "UnitFileState": "enabled" if module else "static",
            "ExecMainStatus": "0",
            "NoNewPrivileges": "no" if research else "yes",
            "ProtectSystem": "strict",
            "ProtectHome": "read-only" if research else "yes",
            "PrivateTmp": "yes",
        }
        if module:
            process = proc_root / str(pid)
            process.mkdir()
            uid, gid = users[account].pw_uid, groups[primary]
            effective_groups = memberships[account] | {groups[item] for item in supplements} | {gid}
            (process / "status").write_text(
                "Uid:\t"
                + "\t".join([str(uid)] * 4)
                + "\nGid:\t"
                + "\t".join([str(gid)] * 4)
                + "\nGroups:\t"
                + " ".join(map(str, sorted(effective_groups)))
                + "\n"
            )
            argv = ["/opt/probe-core/venv/bin/python", "-I", "-m", module] + ([command] if command else [])
            (process / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")

    def run(command, **kwargs):
        assert command[:2] == ["/usr/bin/systemctl", "show"]
        assert kwargs["timeout"] == 5 and kwargs["cwd"] == "/"
        return subprocess.CompletedProcess(
            command, 0, "\n".join(key + "=" + value for key, value in values[command[2]].items()).encode(), b""
        )

    return SimpleNamespace(
        users=users,
        groups=groups,
        memberships=memberships,
        values=values,
        run=run,
        proc_root=proc_root,
        unit_root=unit_root,
    )


def inspect(fixture):
    return verify.inspect_units(
        fixture.users,
        fixture.groups,
        fixture.memberships,
        run=fixture.run,
        proc_root=fixture.proc_root,
        unit_root=fixture.unit_root,
        owner=os.getuid(),
    )


def test_effective_service_groups_follow_unit_and_nss_without_passwd_primary(installed_units):
    fixture = installed_units
    roles, pids = inspect(fixture)
    watchdog = roles["probe-watchdog.service"]
    assert watchdog["groups"] == sorted(fixture.memberships["probe-watchdog"])
    assert fixture.users["probe-watchdog"].pw_gid not in watchdog["groups"]
    assert len(pids) == 4 and "probe-backup.service" not in pids


@pytest.mark.parametrize(
    "field,value",
    [
        ("User", "root"),
        ("Group", "probe-trusted"),
        ("SupplementaryGroups", "probe-trusted"),
        ("DropInPaths", "/unexpected.conf"),
        ("MainPID", "0"),
        ("ProtectSystem", "no"),
        ("NoNewPrivileges", "no"),
        ("ActiveState", "failed"),
    ],
)
def test_changed_installed_unit_is_refused(installed_units, field, value):
    installed_units.values["probe-watchdog.service"][field] = value
    with pytest.raises(verify.CheckFailure):
        inspect(installed_units)


@pytest.mark.parametrize(
    "file,content",
    [("status", b"Uid: 0 0 0 0\nGid: 0 0 0 0\nGroups: 0\n"), ("cmdline", b"/usr/bin/python3\0-I\0-m\0other_module\0")],
)
def test_live_process_must_match_unit_identity_and_entrypoint(installed_units, file, content):
    pid = installed_units.values["probe-watchdog.service"]["MainPID"]
    (installed_units.proc_root / pid / file).write_bytes(content)
    with pytest.raises(verify.CheckFailure, match="PROCESS_"):
        inspect(installed_units)


def test_failed_backup_cannot_pass_as_completed(installed_units):
    installed_units.values["probe-backup.service"]["ExecMainStatus"] = "1"
    with pytest.raises(verify.CheckFailure, match="BACKUP_NOT_COMPLETED"):
        inspect(installed_units)


def history_fixture(tmp_path, *, closed=True, pending=False):
    ledger, provider = tmp_path / "ledger.sqlite", tmp_path / "provider.sqlite"
    now = datetime.now(timezone.utc)
    RunPodProvider(
        RunPodConfig(
            state_path=str(provider),
            api_key_file=str(tmp_path / "unused-key"),
            launch=RunPodLaunchConfig(image_repository="example/worker"),
            storage_rates=StorageRates(checked_at=now),
        )
    )
    deployment = DeploymentSpec(
        gpu_model="NVIDIA GeForce RTX 4090",
        image_digest="sha256:" + "a" * 64,
        image_repository="example/worker",
        launch_config_hash="sha256:" + "b" * 64,
        storage_mode="ephemeral_preflight",
        volume_gb=0,
        region="EU-RO-1",
    )
    with Ledger(ledger, clock=lambda: now) as core:
        controller = Controller(
            core, object(), watchdog_health_path=tmp_path / "unused-health", controller_idle_usd_per_day=0
        )
        request = controller.request_infrastructure_preflight(deployment, "sha256:" + "c" * 64, 60)
        if pending:
            return ledger, provider, request
        nonce = ApprovalNonce(
            approval_id=request["approval_id"],
            token="synthetic-test-value-" + "x" * 48,
            pod_id=request["worker_id"],
            batch_hash=request["batch_hash"],
            purpose="infrastructure_preflight",
            max_runtime_seconds=60,
            price_ceiling_usd_per_hour=0.8,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        core.register_approval(nonce)
        grant = core.consume_infrastructure_approval(
            nonce.approval_id,
            nonce.token.get_secret_value(),
            infrastructure_hash=request["batch_hash"],
            pod_id=request["worker_id"],
            live_price_usd_per_hour=0.74,
            requested_runtime_seconds=60,
        )
        controller._state(
            request["request_id"],
            "STOPPED" if closed else "RUNNING",
            deadline=grant.deadline.timestamp(),
            provider_id="owned-test-pod",
        )
        if closed:
            core.end_approval(nonce.approval_id)
    with closing(sqlite3.connect(provider)) as connection, connection:
        connection.execute(
            "INSERT INTO runpod_intents VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                request["worker_id"],
                request["request_id"],
                canonical_json(deployment.model_dump(exclude_none=True)),
                deployment.digest,
                grant.deadline.timestamp(),
                "owned-test-pod",
                now.timestamp(),
                "{}",
                0.8,
                1,
            ),
        )
    return ledger, provider, request


def test_state_gate_preserves_completed_history_and_checks_probe_identity(tmp_path):
    ledger, provider, request = history_fixture(tmp_path)
    before = ledger.read_bytes(), provider.read_bytes()
    result = verify.state_counts(ledger=ledger, provider=provider, worker_id="identity-check-test")
    assert result["audit_events"] > 1 and result["approvals"] == result["runpod_intents"] == 1
    assert result["idle"] and result["audit_valid"] and (ledger.read_bytes(), provider.read_bytes()) == before
    with pytest.raises(verify.CheckFailure, match="PROBE_WORKER_ALREADY_REGISTERED"):
        verify.state_counts(ledger=ledger, provider=provider, worker_id=request["worker_id"])


def test_unapproved_pending_proposal_without_authority_is_retained(tmp_path):
    ledger, provider, _ = history_fixture(tmp_path, pending=True)
    result = verify.state_counts(ledger=ledger, provider=provider)
    assert result["compute_requests"] == 1 and result["approvals"] == result["runpod_intents"] == 0


def test_consumed_unclosed_interval_refuses_upgrade_even_after_wall_deadline(tmp_path):
    ledger, provider, _ = history_fixture(tmp_path, closed=False)
    with closing(sqlite3.connect(ledger)) as connection, connection:
        connection.execute("UPDATE approvals SET deadline=1")
    with pytest.raises(verify.CheckFailure, match="UNCLOSED_APPROVAL_EXISTS"):
        verify.state_counts(ledger=ledger, provider=provider)


@pytest.mark.parametrize("state", ["PENDING", "DISPATCHED", "RUNNING", "FINALIZING", "COMPLETED", "FAILED"])
def test_job_gate_rejects_all_nonterminal_states(tmp_path, state):
    ledger, provider, _ = history_fixture(tmp_path)
    manifest = json.loads((PROJECT / "tests/fixtures/manifest.json").read_text())
    with Ledger(ledger) as core:
        job = core.submit_job(
            JobSpec(
                idempotency_key="preserved-job",
                model=manifest["model"],
                inputs=manifest["inputs"],
                operation={
                    "kind": "capture",
                    "modules": [{"layer": 0, "component": "residual"}],
                    "positions": ["last"],
                },
                limits={"max_runtime_seconds": 60, "max_output_bytes": 1000000},
            )
        )
    with closing(sqlite3.connect(ledger)) as connection, connection:
        connection.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, job.job_id))
    if state in {"COMPLETED", "FAILED"}:
        assert verify.state_counts(ledger=ledger, provider=provider)["jobs"] == 1
    else:
        with pytest.raises(verify.CheckFailure, match="NONTERMINAL_JOB_EXISTS"):
            verify.state_counts(ledger=ledger, provider=provider)


@pytest.mark.parametrize("expired", [False, True])
def test_unconsumed_nonce_is_only_idle_after_expiry(tmp_path, expired):
    ledger, provider, _ = history_fixture(tmp_path)
    now = datetime.now(timezone.utc)
    issued = now - timedelta(minutes=10)
    expiry = now + timedelta(minutes=-1 if expired else 1)
    with Ledger(ledger, clock=lambda: issued) as core:
        core.register_approval(
            ApprovalNonce(
                approval_id="unused-test-approval",
                token="synthetic-unconsumed-" + "z" * 48,
                pod_id="unused-test-worker",
                batch_hash="sha256:" + "d" * 64,
                purpose="infrastructure_preflight",
                max_runtime_seconds=60,
                price_ceiling_usd_per_hour=0.8,
                issued_at=issued,
                expires_at=expiry,
            )
        )
    if expired:
        assert verify.state_counts(ledger=ledger, provider=provider)["approvals"] == 2
    else:
        with pytest.raises(verify.CheckFailure, match="UNCONSUMED_AUTHORITY_EXISTS"):
            verify.state_counts(ledger=ledger, provider=provider)


def test_pending_proposal_with_even_closed_authority_requires_reconciliation(tmp_path):
    ledger, provider, _ = history_fixture(tmp_path)
    with closing(sqlite3.connect(ledger)) as connection, connection:
        connection.execute("UPDATE compute_requests SET state='PENDING'")
    with pytest.raises(verify.CheckFailure, match="PENDING_REQUEST_HAS_AUTHORITY"):
        verify.state_counts(ledger=ledger, provider=provider)


def test_terminal_job_with_unstopped_attempt_still_refuses_upgrade(tmp_path):
    ledger, provider, request = history_fixture(tmp_path)
    manifest = json.loads((PROJECT / "tests/fixtures/manifest.json").read_text())
    with Ledger(ledger) as core:
        job = core.submit_job(
            JobSpec(
                idempotency_key="orphaned-attempt-job",
                model=manifest["model"],
                inputs=manifest["inputs"],
                operation={
                    "kind": "capture",
                    "modules": [{"layer": 0, "component": "residual"}],
                    "positions": ["last"],
                },
                limits={"max_runtime_seconds": 60, "max_output_bytes": 1000000},
            )
        )
    with closing(sqlite3.connect(ledger)) as connection, connection:
        connection.execute("UPDATE jobs SET state='FAILED' WHERE job_id=?", (job.job_id,))
        connection.execute(
            "INSERT INTO attempts(attempt_id,job_id,attempt_number,worker_id,approval_id,dispatched_at,heartbeat_at,lease_expires_at,execution_deadline) VALUES(?,?,?,?,?,?,?,?,?)",
            ("orphaned-attempt", job.job_id, 1, request["worker_id"], request["approval_id"], 1, 1, 2, 2),
        )
    with pytest.raises(verify.CheckFailure, match="UNSTOPPED_ATTEMPT_EXISTS"):
        verify.state_counts(ledger=ledger, provider=provider)


@pytest.mark.parametrize("fault", ["unbound", "wrong_worker", "wrong_configuration", "unknown_request"])
def test_provider_mapping_must_be_complete_and_consistent(tmp_path, fault):
    ledger, provider, _ = history_fixture(tmp_path)
    statement = {
        "unbound": "UPDATE runpod_intents SET provider_id=NULL",
        "wrong_worker": "UPDATE runpod_intents SET worker_id='other'",
        "wrong_configuration": "UPDATE runpod_intents SET configuration='{}'",
        "unknown_request": "UPDATE runpod_intents SET request_key='other'",
    }[fault]
    with closing(sqlite3.connect(provider)) as connection, connection:
        connection.execute(statement)
    with pytest.raises(verify.CheckFailure, match="UNRESOLVED_PROVIDER_INTENT|PROVIDER_CONFIGURATION_MISMATCH"):
        verify.state_counts(ledger=ledger, provider=provider)


@pytest.mark.parametrize("fault", ["same_count_row_edit", "audit_edit", "audit_suffix_loss", "extra_table_edit"])
def test_all_historical_rows_and_audit_prefix_are_preserved(tmp_path, fault):
    ledger, provider, _ = history_fixture(tmp_path)
    with closing(sqlite3.connect(ledger)) as connection, connection:
        connection.execute("CREATE TABLE retained_extension(value TEXT)")
        connection.execute("INSERT INTO retained_extension VALUES('original')")
    before = verify.state_counts(ledger=ledger, provider=provider)
    with closing(sqlite3.connect(ledger)) as connection, connection:
        if fault == "same_count_row_edit":
            connection.execute("UPDATE approvals SET token_hash='changed-synthetic-hash'")
        elif fault == "extra_table_edit":
            connection.execute("UPDATE retained_extension SET value='changed'")
        elif fault == "audit_edit":
            connection.execute("DROP TRIGGER audit_no_update")
            connection.execute("UPDATE audit_events SET hash=? WHERE sequence=1", ("f" * 64,))
        else:
            connection.execute("DROP TRIGGER audit_no_delete")
            connection.execute("DELETE FROM audit_events WHERE sequence=(SELECT max(sequence) FROM audit_events)")
    with pytest.raises(verify.CheckFailure, match="HISTORICAL_STATE_CHANGED|AUDIT_HASH_INVALID"):
        verify.state_counts(ledger=ledger, provider=provider, expected=before)


def test_valid_audit_appends_preserve_old_prefix(tmp_path):
    ledger, provider, _ = history_fixture(tmp_path)
    before = verify.state_counts(ledger=ledger, provider=provider)
    with Ledger(ledger) as core:
        core.record_event("tool_call", {"tool": "query_runs"})
    after = verify.state_counts(ledger=ledger, provider=provider, expected=before)
    assert after["audit_events"] > before["audit_events"]
    assert after["audit_prefix_sha256"] == before["audit_tip"]
    assert after["history_sha256"] == before["history_sha256"]


def test_completed_archive_requires_exact_hash_and_full_verification_receipt(tmp_path):
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    outbox.mkdir()
    receipts.mkdir()
    archive = outbox / "probe-retained.tar"
    archive.write_bytes(b"bounded retained archive fixture")
    archive.chmod(0o640)
    receipt = receipts / "receipt.json"
    data = {
        "verified": True,
        "readback_verified": True,
        "restore_verified": True,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    receipt.write_text(json.dumps(data))
    receipt.chmod(0o640)
    kwargs = dict(outbox=outbox, receipts=receipts, trusted_uid=os.getuid(), backup_uid=os.getuid())
    assert verify.completed_archive(**kwargs) == archive
    archive.write_bytes(b"changed")
    with pytest.raises(verify.CheckFailure, match="NO_VERIFIED_LOCAL_ARCHIVE"):
        verify.completed_archive(**kwargs)
    data["restore_verified"] = False
    receipt.write_text(json.dumps(data))
    with pytest.raises(verify.CheckFailure, match="BACKUP_NOT_VERIFIED"):
        verify.completed_archive(**kwargs)


def test_main_emits_only_fixed_codes_when_dependency_has_secret_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(verify.os, "geteuid", lambda: 0)

    def fail(_):
        raise ValueError("PRIVATE CREDENTIAL THAT MUST NEVER BE EMITTED")

    monkeypatch.setattr(verify, "acceptance", fail)
    reports = []
    monkeypatch.setattr(verify, "write_report", lambda path, report: reports.append(report))
    assert verify.main(["--human", "human", "--output", str(tmp_path / "report.json")]) == 1
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err + json.dumps(reports)
    assert reports[0]["failure_codes"] == ["IDENTITY_ACCEPTANCE_UNAVAILABLE"]
    assert reports[0]["paid_actions_performed"] is False and reports[0]["passed"] is False


def test_standalone_script_rejects_nonadministrator():
    if os.geteuid() == 0:
        pytest.skip("requires a real unprivileged process")
    result = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, timeout=10)
    assert result.returncode == 1
    assert result.stderr.strip() == b"IDENTITY_ACCEPTANCE_REQUIRES_ADMINISTRATOR"


def test_actual_provider_unknown_worker_status_never_uses_http(tmp_path):
    class ForbiddenTransport:
        def request(self, *args, **kwargs):
            raise AssertionError("identity acceptance must never reach provider HTTP")

    config = RunPodConfig(
        state_path=str(tmp_path / "provider" / "runpod.sqlite"),
        api_key_file=str(tmp_path / "absent-key-must-not-be-read"),
        launch=RunPodLaunchConfig(image_repository="ghcr.io/test/worker", ports=("22/tcp",)),
        storage_rates=StorageRates(checked_at=datetime.now(timezone.utc)),
    )
    provider = RunPodProvider(config, transport=ForbiddenTransport())
    before = provider.path.read_bytes()
    result = provider.status("identity-check-never-registered")
    assert result.state.value == "ABSENT" and result.provider_id is None
    assert provider.path.read_bytes() == before


@pytest.fixture
def nss(monkeypatch):
    groups = {name: 3000 + index for index, name in enumerate(verify.GROUPS)}
    users = {
        name: SimpleNamespace(pw_uid=2000 + index, pw_gid=groups[name]) for index, name in enumerate(verify.ACCOUNTS)
    }
    users["human"] = SimpleNamespace(pw_uid=1000, pw_gid=1000)
    entries = [SimpleNamespace(gr_gid=gid, gr_mem=[]) for gid in groups.values()]
    monkeypatch.setattr(verify.pwd, "getpwnam", users.__getitem__)
    monkeypatch.setattr(verify.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=groups[name]))
    monkeypatch.setattr(verify.grp, "getgrall", lambda: entries)
    return SimpleNamespace(groups=groups, users=users, entries=entries)


def test_identities_are_discovered_without_hardcoded_uid(nss):
    users, groups, memberships = verify.discover("human")
    assert users["probe-research"].pw_uid == 2001
    assert groups == nss.groups and memberships["probe-research"] == set()


@pytest.mark.parametrize("problem", ["root", "duplicate", "wrong_primary", "extra_research_group"])
def test_invalid_nss_identity_layout_fails(nss, problem):
    if problem == "root":
        nss.users["human"].pw_uid = 0
    elif problem == "duplicate":
        nss.users["human"].pw_uid = nss.users["probe-trusted"].pw_uid
    elif problem == "wrong_primary":
        nss.users["probe-watchdog"].pw_gid = 1000
    else:
        nss.entries.append(SimpleNamespace(gr_gid=1000, gr_mem=["probe-research"]))
    with pytest.raises(verify.CheckFailure):
        verify.discover("human")


def test_report_is_private_exclusive_and_rejects_symlink_parents(tmp_path, monkeypatch):
    directory = tmp_path / "private-output"
    directory.mkdir(mode=0o700)
    real_lstat = Path.lstat

    def report_lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0) if path == directory else info

    monkeypatch.setattr(Path, "lstat", report_lstat)
    output = directory / "result.json"
    verify.write_report(output, {"passed": True})
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == {"passed": True}
    with pytest.raises(FileExistsError):
        verify.write_report(output, {"passed": False})
    assert json.loads(output.read_text()) == {"passed": True}
    link = tmp_path / "symlink"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(verify.CheckFailure, match="REPORT_PATH_UNSAFE"):
        verify.write_report(link / "other.json", {})
    assert not (directory / "other.json").exists()
    directory.chmod(0o750)
    with pytest.raises(verify.CheckFailure, match="REPORT_DIRECTORY_NOT_PRIVATE"):
        verify.write_report(directory / "insecure.json", {})


@pytest.mark.parametrize("audit_after,restarted,passed", [(11, False, True), (9, False, False), (11, True, False)])
def test_orchestration_preserves_prior_audit_and_checks_service_continuity(
    installed_units, monkeypatch, audit_after, restarted, passed
):
    fixture = installed_units
    fixture.users["human"] = SimpleNamespace(pw_uid=1000, pw_gid=1000)
    fixture.memberships["human"] = {1000}
    roles, pids = inspect(fixture)
    monkeypatch.setattr(verify, "discover", lambda human: (fixture.users, fixture.groups, fixture.memberships))
    monkeypatch.setattr(verify, "inspect_units", lambda *args: (roles, pids))
    monkeypatch.setattr(verify, "validate_config", lambda *args: None)
    monkeypatch.setattr(verify, "completed_archive", lambda **kwargs: Path("/verified/archive.tar"))
    states = [
        {
            "jobs": 0,
            "approvals": 0,
            "compute_requests": 0,
            "runpod_intents": 0,
            "audit_events": count,
            "history_sha256": "a" * 64,
            "audit_tip": "b" * 64,
            "audit_prefix_sha256": "b" * 64,
        }
        for count in (9, audit_after)
    ]
    workers = []

    def state_counts(**kwargs):
        workers.append(kwargs["worker_id"])
        return states.pop(0)

    monkeypatch.setattr(verify, "state_counts", state_counts)
    probes = []

    def probe(plan):
        probes.append(plan)
        return {plan["role"] + "_identity": True, **{item["code"]: True for item in plan["checks"]}}

    monkeypatch.setattr(verify, "run_probe", probe)
    monkeypatch.setattr(verify, "unit_values", lambda name: {"MainPID": str(pids[name] + int(restarted))})
    checks, delta = verify.acceptance("human")
    assert all(checks.values()) is passed
    assert delta == audit_after - 9
    assert len(workers) == 2 and workers[0] == workers[1] and workers[0].startswith("identity-check-")
    assert [plan["role"] for plan in probes] == ["research", "watchdog", "backup", "human"]
    assert len(checks) == 26
    assert {check["kind"] for plan in probes for check in plan["checks"]} == {
        "deny_read",
        "read_file",
        "deny_connect",
        "lab_status",
        "discovery",
        "stop_status",
        "controller_status",
    }


@pytest.fixture
def installed_config(tmp_path, rpc_server, monkeypatch):
    for name in ("LEDGER", "PROVIDER_KEY", "BACKUP_KEY"):
        path = tmp_path / name.lower()
        path.write_bytes(b"private fixture data")
        path.chmod(0o640 if name == "LEDGER" else 0o600)
        monkeypatch.setattr(verify, name, path)
    monkeypatch.setattr(verify, "PROVIDER", tmp_path / "provider.sqlite")
    for name in ("RESEARCH_SOCKET", "ADMIN_SOCKET", "STOP_SOCKET"):
        path = rpc_server(lambda method, params: [])
        path.chmod(0o660)
        monkeypatch.setattr(verify, name, path)
    users = {name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()) for name in (*verify.ACCOUNTS, "human")}
    groups = {name: os.getgid() for name in verify.GROUPS}
    research = {
        "ledger_path": str(verify.LEDGER),
        "socket_path": str(verify.RESEARCH_SOCKET),
        "service_uid": os.getuid(),
        "research_uid": os.getuid(),
        "admin_uid": os.getuid(),
        "socket_gid": os.getgid(),
        "controller_uid": os.getuid(),
        "controller_socket": "/run/probe-controller/research.sock",
        "sandbox_image": None,
    }
    path = tmp_path / "research.json"
    path.write_text(json.dumps(research))
    path.chmod(0o640)
    provider = tmp_path / "runpod.json"
    provider.write_text(json.dumps({"state_path": str(verify.PROVIDER), "api_key_file": str(verify.PROVIDER_KEY)}))
    provider.chmod(0o640)
    return SimpleNamespace(users=users, groups=groups, research=research, path=path, root=tmp_path)


def validate(fixture):
    verify.validate_config(fixture.users, fixture.groups, "human", config_root=fixture.root, owner=os.getuid())


def test_guarded_null_sandbox_config_keeps_identity_gate_available(installed_config):
    validate(installed_config)


@pytest.mark.parametrize(
    "field,value", [("service_uid", False), ("research_uid", 1234567), ("controller_socket", "/unexpected.sock")]
)
def test_identity_config_mismatch_fails(installed_config, field, value):
    installed_config.research[field] = value
    installed_config.path.write_text(json.dumps(installed_config.research))
    with pytest.raises(verify.CheckFailure, match="SERVICE_CONFIG_IDENTITY"):
        validate(installed_config)


@pytest.mark.parametrize("problem", ["shared_credential", "writable_ledger", "socket_mode", "symlink", "hardlink"])
def test_installed_protected_metadata_must_be_safe(installed_config, problem):
    if problem == "shared_credential":
        verify.PROVIDER_KEY.chmod(0o640)
    elif problem == "writable_ledger":
        verify.LEDGER.chmod(0o660)
    elif problem == "socket_mode":
        verify.STOP_SOCKET.chmod(0o666)
    elif problem == "symlink":
        destination = verify.BACKUP_KEY.with_suffix(".real")
        verify.BACKUP_KEY.rename(destination)
        verify.BACKUP_KEY.symlink_to(destination)
    else:
        os.link(verify.BACKUP_KEY, verify.BACKUP_KEY.with_suffix(".link"))
    with pytest.raises(verify.CheckFailure):
        validate(installed_config)
