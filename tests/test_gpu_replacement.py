"""Real ledger/controller replacement, with synthetic receipts and fake HTTP only."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from probe_core import gpu_acceptance_runner as runner
from probe_core import gpu_replacement as replacement
from probe_core.audit import canonical_json
from probe_core.controller import StopWatchdog
from probe_core.gpu_acceptance import AcceptancePlan
from probe_core.ledger import ArtifactError
from probe_core.model_assets import ModelLock, inventory
from probe_core.provider import StopOnlyBackend, WorkerState
from test_gpu_acceptance_runner import setup, queue, approve_fixture, Endpoint, Tunnel
from test_runpod_provider import runpod
from test_schemas import manifest_data


def encoded(value):
    return canonical_json(value).encode()


def run_case(s, **overrides):
    endpoint = Endpoint(s)
    args = dict(
        clock=lambda: s.clock().timestamp(),
        sleep=lambda _: None,
        endpoint=lambda *_: {},
        tunnel_factory=Tunnel,
        client_factory=lambda *_a, **_k: endpoint,
        readiness=lambda *_a, **_k: None,
        configure=lambda *_a, **_k: {"configured": True},
        replacement_reader=lambda *args: replacement.verify_predecessor(*args, owner=os.geteuid()),
    )
    args.update(overrides)
    with runner.State(s.config.trusted_state_directory) as state:
        result = runner.run(
            s.config, s.plan, s.ledger, s.backend, SimpleNamespace(stop_gpu=s.controller.stop_gpu), state, **args
        )
    return result, endpoint


@pytest.fixture
def predecessor(setup):
    s = setup
    config_path = s.root / "old-config.json"
    config_path.write_bytes(encoded(s.config.model_dump(mode="json")))
    config_path.chmod(0o644)
    request = approve_fixture(s)
    result, endpoint = run_case(s)
    assert result["status"] == "passed", canonical_json(result)
    assert result["teardown"]["confirmed"] is True and endpoint.posts == 1
    old = SimpleNamespace(config=s.config, plan=s.plan, request=request, result=result)
    binding = replacement.ReplacementBinding(
        **{
            name: result[name]
            for name in ("request_id", "worker_id", "provider_id", "job_id", "attempt_id", "manifest_sha256")
        },
        approval_id=request["approval_id"],
        runner_config_path=str(config_path),
        runner_config_sha256="sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest(),
        result_sha256=runner.digest(result),
    )
    plan_body = s.plan.model_dump(mode="json")
    plan_body["label"] += "-replacement"
    plan_body["cases"][0]["spec"]["idempotency_key"] += "-replacement"
    s.plan = AcceptancePlan.model_validate(plan_body)
    plan_path = s.root / "replacement-plan.json"
    plan_path.write_text(s.plan.model_dump_json())
    plan_path.chmod(0o644)
    for name in ("replacement-submit", "replacement-run"):
        (s.root / name).mkdir(mode=0o700)
    s.config = s.config.model_copy(
        update={
            "plan_path": str(plan_path),
            "plan_sha256": "sha256:" + hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "submission_state_directory": str(s.root / "replacement-submit"),
            "trusted_state_directory": str(s.root / "replacement-run"),
            "replacement": binding,
        }
    )
    s.old = old
    with runner.State(s.config.trusted_state_directory) as state:
        verified = replacement.verify_before_approval(s.config, s.plan, s.ledger, s.backend, state, owner=os.geteuid())
    assert verified["status"] == "passed" and verified["approval_consumed_by_runner"] is False
    assert len(s.controller.status()) == 1 and len(s.http.purchases) == 1
    # The generic fake HTTP uses len(live Pods) as ID. Give subsequent physical
    # resources unique IDs, as required by the actual replacement contract.
    original = s.http.request

    def request_http(method, path, body=None):
        response = original(method, path, body)
        if method == "POST" and path == "/graphql":
            physical = "replacement-pod-" + str(len(s.http.purchases))
            s.http.pods[-1]["id"] = physical
            response["data"]["podFindAndDeployOnDemand"]["id"] = physical
        return response

    s.http.request = request_http
    yield s


def approve_replacement(s):
    queued = queue(s)
    s.controller.clock = s.clock
    watcher = StopWatchdog(
        s.ledger.path,
        StopOnlyBackend(s.backend),
        state_path=s.root / "watchdog" / "state.sqlite",
        health_path=s.root / "watchdog" / "health.json",
        clock=s.clock,
    )
    s.controller.health_path = s.root / "watchdog" / "health.json"
    watcher.tick()
    original_ack = s.controller._await_watchdog_ack

    def acknowledge(request, deadline):
        watcher.tick()
        return original_ack(request, deadline)

    s.controller._await_watchdog_ack = acknowledge
    return s.controller.admin_dispatch(
        "approve", {"request_id": queued["request_id"], "price_ceiling_usd_per_hour": 0.8}
    )


def test_replacement_research_path_never_approves_and_uncertain_reply_never_repeats(predecessor):
    s = predecessor
    calls = []

    def call(method, params=None):
        calls.append((method, params))
        result = s.rpc.call(method, params)
        if method == "request_gpu_provision":
            raise TimeoutError("lost reply")
        return result

    with runner.State(s.config.submission_state_directory) as state:
        first = runner.submit(s.config, s.plan, SimpleNamespace(call=call), state)
        assert runner.submit(s.config, s.plan, SimpleNamespace(call=call), state) == first
    mutations = [params for method, params in calls if method == "request_gpu_provision"]
    assert len(mutations) == 1 and mutations[0]["replaces_worker_id"] == s.old.request["worker_id"]
    assert len(s.http.purchases) == 1
    request = runner.find_request(s.ledger, s.config, s.plan)
    assert request["action"] == "REPLACE" and request["state"] == "PENDING"
    assert request["approval_id"] != s.old.request["approval_id"]
    with s.ledger.read_connection() as connection:
        assert connection.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1


def test_real_approved_replacement_rehashes_old_bundle_then_fresh_parity_and_deletes(predecessor):
    s = predecessor
    request = approve_replacement(s)
    old_bytes = (s.ledger.get_artifact_root(s.old.result["job_id"]) / "summary.json").read_bytes()
    seen = []

    def observe(config, plan, settings, *, deadline, clock):
        seen.append(deadline)
        return {"schema_version": 1, "receipt_sha256": "sha256:" + "f" * 64, "fixture_readback": True}

    result, endpoint = run_case(s, model_inventory=observe)
    assert result["status"] == "passed", canonical_json(result)
    assert result["replacement"]["complete"] is True
    assert result["scientific_evidence"] is False
    for key in ("request_id", "worker_id", "provider_id", "job_id", "attempt_id"):
        assert result[key] != s.old.result[key]
    assert request["approval_id"] != s.old.request["approval_id"]
    assert seen == [request["deadline"] - 240 - 120]
    assert result["teardown"] == {"provider_id": request["observed_provider_id"], "state": "ABSENT", "confirmed": True}
    assert endpoint.posts == 1 and len(s.http.purchases) == 2
    assert (s.ledger.get_artifact_root(s.old.result["job_id"]) / "summary.json").read_bytes() == old_bytes
    with runner.State(s.config.trusted_state_directory) as state:
        again = runner.run(
            s.config,
            s.plan,
            s.ledger,
            s.backend,
            None,
            state,
            replacement_reader=lambda *_: pytest.fail("must not replay"),
        )
    assert again == result and len(s.http.purchases) == 2


@pytest.mark.parametrize(
    "kind", ["raw_config", "result", "observation", "artifact", "missing_artifact", "seal", "symlink", "hardlink"]
)
def test_changed_predecessor_evidence_is_refused(predecessor, kind):
    s = predecessor
    old_dir = Path(s.old.config.trusted_state_directory)
    bundle = s.ledger.get_artifact_root(s.old.result["job_id"])
    if kind == "raw_config":
        Path(s.config.replacement.runner_config_path).write_text("{}")
    elif kind == "result":
        (old_dir / "result.json").write_text("{}")
    elif kind == "observation":
        body = json.loads((old_dir / "observations.json").read_text())
        body["cases"][0]["observed_receipt"]["process_stopped"] = False
        (old_dir / "observations.json").write_text(canonical_json(body))
    else:
        bundle.chmod(0o700)
        target = bundle / (".probe-bundle.json" if kind == "seal" else "summary.json")
        target.chmod(0o600)
        if kind in {"seal", "artifact"}:
            target.write_text("{}")
        elif kind == "missing_artifact":
            target.unlink()
        elif kind == "symlink":
            outside = s.root / "copied-summary"
            outside.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(outside)
        elif kind == "hardlink":
            os.link(target, s.root / "shared-summary")
    with pytest.raises((ValueError, OSError, ArtifactError)):
        replacement.verify_predecessor(s.config, s.plan, s.ledger, s.backend, owner=os.geteuid())
    assert len(s.http.purchases) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_id", "wrong-pod"),
        ("approval_id", "wrong-approval"),
        ("worker_id", "wrong-worker"),
        ("attempt_id", "wrong-attempt"),
    ],
)
def test_predecessor_identity_pin_cannot_be_changed(predecessor, field, value):
    s = predecessor
    config = s.config.model_copy(update={"replacement": s.config.replacement.model_copy(update={field: value})})
    with pytest.raises(replacement.ReplacementError):
        replacement.verify_predecessor(config, s.plan, s.ledger, s.backend, owner=os.geteuid())


@pytest.mark.parametrize("fault", ["job_failed", "request_running", "approval_open", "attempt_unstopped"])
def test_authoritative_predecessor_must_be_complete_and_closed(predecessor, fault):
    s = predecessor
    b = s.config.replacement

    def corrupt(connection, _):
        if fault == "job_failed":
            connection.execute("UPDATE jobs SET state='FAILED' WHERE job_id=?", (b.job_id,))
        elif fault == "request_running":
            connection.execute("UPDATE compute_requests SET state='RUNNING' WHERE request_id=?", (b.request_id,))
        elif fault == "approval_open":
            connection.execute("UPDATE approvals SET ended_at=NULL WHERE approval_id=?", (b.approval_id,))
        else:
            connection.execute("UPDATE attempts SET stopped_at=NULL WHERE attempt_id=?", (b.attempt_id,))

    s.ledger._submit(corrupt)
    with pytest.raises(replacement.ReplacementError):
        replacement.verify_predecessor(s.config, s.plan, s.ledger, s.backend, owner=os.geteuid())


@pytest.mark.parametrize(
    "status_change",
    [{"state": WorkerState.STOPPED}, {"provider_id": "wrong"}, {"request_key": None}, {"configuration_hash": None}],
)
def test_provider_absence_requires_seen_exact_durable_mapping(predecessor, status_change):
    s = predecessor
    status = s.backend.status(s.config.replacement.worker_id)
    backend = SimpleNamespace(status=lambda _: replace(status, **status_change))
    with pytest.raises(replacement.ReplacementError, match="PREDECESSOR_NOT_ABSENT"):
        replacement.verify_predecessor(s.config, s.plan, s.ledger, backend, owner=os.geteuid())


@pytest.mark.parametrize(
    "change",
    [
        {"source_commit": "d" * 40},
        {"expected_worker_price_usd_per_hour": 0.75},
        {"worker_config_sha256": "sha256:" + "9" * 64},
    ],
)
def test_replacement_preserves_exact_worker_provenance_and_price(predecessor, change):
    s = predecessor
    with pytest.raises(replacement.ReplacementError, match="SCOPE_CHANGED"):
        replacement.verify_predecessor(
            s.config.model_copy(update=change), s.plan, s.ledger, s.backend, owner=os.geteuid()
        )


def test_startup_only_failure_cannot_become_successful_predecessor_even_with_new_result_pin(predecessor):
    s = predecessor
    path = Path(s.old.config.trusted_state_directory) / "result.json"
    body = json.loads(path.read_text())
    body.update(status="failed", stage="verified_worker_startup", reason="PROVIDER_ENDPOINT_REFUSED")
    path.write_text(canonical_json(body))
    config = s.config.model_copy(
        update={"replacement": s.config.replacement.model_copy(update={"result_sha256": runner.digest(body)})}
    )
    with pytest.raises(replacement.ReplacementError, match="SUCCESSFUL_PREDECESSOR_REQUIRED"):
        replacement.verify_predecessor(config, s.plan, s.ledger, s.backend, owner=os.geteuid())


def test_replacement_failure_after_fresh_parity_cannot_leave_passed_status(predecessor):
    s = predecessor
    approve_replacement(s)
    client = Endpoint(s)
    original = client.download

    def download(*args, **kwargs):
        path = s.ledger.get_artifact_root(s.old.result["job_id"]) / "summary.json"
        path.chmod(0o600)
        path.write_text("tampered after replacement dispatch")
        return original(*args, **kwargs)

    client.download = download
    result, _ = run_case(
        s,
        client_factory=lambda *_a, **_k: client,
        model_inventory=lambda *_a, **_k: {"receipt_sha256": "sha256:" + "f" * 64},
    )
    assert result["status"] == "failed" and result["teardown"]["confirmed"] is True
    assert result.get("replacement", {}).get("complete") is not True
    assert client.posts == 1


def test_existing_config_digests_stay_unchanged_when_no_replacement(setup):
    assert "replacement" not in setup.config.model_dump(mode="json")
    assert runner.matches_request(
        {
            "action": "CREATE",
            "job_ids": ["j"],
            "configuration_hash": setup.config.deployment.digest,
            "configuration": setup.config.deployment.model_dump(mode="json"),
            "max_runtime_seconds": 900,
            "infrastructure": None,
            "replaces_worker_id": None,
        },
        setup.config,
        "j",
    )


@pytest.fixture
def asset_readback(setup, monkeypatch):
    """Hash tiny real files with the same production inventory function."""
    s = setup
    root = s.root / "model"
    root.mkdir()
    contents = {
        "config.json": encoded(
            {
                "model_type": "qwen3",
                "num_hidden_layers": 28,
                "num_attention_heads": 16,
                "num_key_value_heads": 8,
                "hidden_size": 2048,
                "head_dim": 128,
                "vocab_size": 151936,
            }
        ),
        "tokenizer.json": b"{}",
        "tokenizer_config.json": b"{}",
        "model.safetensors": b"synthetic, never loaded",
    }
    for name, body in contents.items():
        (root / name).write_bytes(body)
    lock = ModelLock(
        repo="Qwen/Qwen3-1.7B-Base",
        revision=s.plan.model.revision_sha,
        source_url="https://example.invalid/fixture",
        files=tuple(
            {"path": name, "size_bytes": len(body), "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}
            for name, body in contents.items()
        ),
    )
    actual = inventory(root, lock)
    data = s.plan.model_dump(mode="json")
    data["model"] = actual.model.model_dump(mode="json")
    data["cases"][0]["spec"]["model"] = data["model"]
    s.plan = AcceptancePlan.model_validate(data)
    worker_path = Path(s.config.worker_config_path)
    worker = json.loads(worker_path.read_text())
    worker.update(
        model=data["model"],
        assets=[entry.model_dump(mode="json") for entry in actual.assets],
        model_directory="/opt/probe-assets/models/Qwen3-1.7B-Base/" + lock.revision,
    )
    worker_path.write_bytes(encoded(worker))
    s.config = s.config.model_copy(
        update={"worker_config_sha256": "sha256:" + hashlib.sha256(worker_path.read_bytes()).hexdigest()}
    )
    monkeypatch.setattr(replacement, "canonical_locks", lambda: (lock,))
    s.inventory = {"schema_version": 1, "uid": 10001, "gid": 10001, "inventory": actual.model_dump(mode="json")}
    s.settings = {
        "host": "213.192.2.71",
        "ssh_port": 40125,
        "identity_file": s.root / "key",
        "known_hosts_file": s.root / "known-hosts",
    }
    s.asset_root, s.lock = root, lock
    return s


def test_remote_readback_matches_real_file_hashes_fixed_command_identity_and_deadline(asset_readback):
    s = asset_readback
    seen = []

    def command(argv, *, timeout):
        seen.append((argv, timeout))
        # This result comes from production hashing of actual small files, not
        # copied expected WorkerConfig digests. The remote script is separately
        # inspected for exact identity drop and bounded, inventory-only work.
        observed = inventory(s.asset_root, s.lock).model_dump(mode="json")
        return canonical_json(dict(s.inventory, inventory=observed))

    result = replacement.read_model_inventory(
        s.config, s.plan, s.settings, deadline=1042, clock=lambda: 1000, command=command, owner=os.geteuid()
    )
    assert result["receipt"] == s.inventory and result["receipt_sha256"] == runner.digest(s.inventory)
    argv, timeout = seen[0]
    remote = shlex.split(argv[-1])
    assert remote[:4] == ["/opt/probe-core/venv/bin/python", "-I", "-B", "-c"]
    assert remote[4] == replacement.INVENTORY_SCRIPT
    assert remote[-3:] == ["Qwen/Qwen3-1.7B-Base", "none", "42"] and timeout == 42
    assert "StrictHostKeyChecking=yes" in argv and argv[-2] == "root@213.192.2.71"
    assert "os.environ.clear()" in remote[4] and "os.setgroups([])" in remote[4]
    assert remote[4].index("os.setgid(10001)") < remote[4].index("os.setuid(10001)")
    assert remote[4].index("os.setuid(10001)") < remote[4].index("result=inventory(")
    assert "signal.alarm(int(seconds))" in remote[4] and "RLIMIT_CPU" in remote[4]
    assert "download" not in remote[4] and "bearer" not in remote[4]
    compile(remote[4], "<fixed-readback>", "exec")


@pytest.mark.parametrize(
    "fault", ["uid", "gid", "extra", "hash", "missing", "duplicate", "model", "lock", "invalid_json", "too_large"]
)
def test_inventory_receipt_cannot_substitute_identity_model_or_hashes(asset_readback, fault):
    s = asset_readback
    body = deepcopy(s.inventory)
    if fault in ("uid", "gid"):
        body[fault] = 0
    elif fault == "extra":
        body["unexpected"] = "ignored values are forbidden"
    elif fault == "hash":
        body["inventory"]["assets"][0]["sha256"] = "sha256:" + "0" * 64
    elif fault == "missing":
        body["inventory"]["assets"].pop()
    elif fault == "duplicate":
        body["inventory"]["assets"][1] = body["inventory"]["assets"][0]
    elif fault == "model":
        body["inventory"]["model"]["revision_sha"] = "9" * 40
    elif fault == "lock":
        body["inventory"]["source_lock_hash"] = "sha256:" + "0" * 64
    raw = "not JSON" if fault == "invalid_json" else ("x" * 65537 if fault == "too_large" else canonical_json(body))
    with pytest.raises(ValueError):
        replacement.read_model_inventory(
            s.config,
            s.plan,
            s.settings,
            deadline=1042,
            clock=lambda: 1000,
            command=lambda *_a, **_k: raw,
            owner=os.geteuid(),
        )


def test_changed_actual_asset_fails_hashing_instead_of_copying_expected_values(asset_readback):
    s = asset_readback
    (s.asset_root / "model.safetensors").write_bytes(b"tampered")

    def command(*_a, **_k):
        return canonical_json(dict(s.inventory, inventory=inventory(s.asset_root, s.lock).model_dump(mode="json")))

    with pytest.raises(ValueError, match="frozen source inventory"):
        replacement.read_model_inventory(
            s.config, s.plan, s.settings, deadline=1042, clock=lambda: 1000, command=command, owner=os.geteuid()
        )


def test_inventory_never_starts_or_accepts_after_original_cutoff(asset_readback):
    s = asset_readback
    with pytest.raises(replacement.ReplacementError, match="READBACK_DEADLINE"):
        replacement.read_model_inventory(
            s.config,
            s.plan,
            s.settings,
            deadline=1000,
            clock=lambda: 1000,
            command=lambda *_a, **_k: pytest.fail("deadline already expired"),
            owner=os.geteuid(),
        )
    values = iter([1000, 1042])
    with pytest.raises(replacement.ReplacementError, match="READBACK_DEADLINE"):
        replacement.read_model_inventory(
            s.config,
            s.plan,
            s.settings,
            deadline=1042,
            clock=lambda: next(values),
            command=lambda *_a, **_k: canonical_json(s.inventory),
            owner=os.geteuid(),
        )


def test_already_started_replacement_is_not_dispatched_a_second_time(predecessor):
    s = predecessor
    request = approve_replacement(s)
    first = s.ledger.dispatch_next(request["worker_id"], approval_id=request["approval_id"])
    result, endpoint = run_case(s, model_inventory=lambda *_a, **_k: pytest.fail("no readback"))
    assert result["status"] == "failed" and result["reason"] == "REPLACEMENT_ALREADY_EXECUTED"
    assert result["teardown"]["confirmed"] is True and endpoint.posts == 0
    assert s.ledger.get_job(first.job_id).attempt_id == first.attempt_id


@pytest.mark.parametrize("fault", ["missing", "changed"])
def test_absent_or_changed_authority_free_preflight_deletes_without_dispatch(predecessor, fault):
    s = predecessor
    path = Path(s.config.trusted_state_directory) / "replacement-preflight.json"
    if fault == "missing":
        path.unlink()
    else:
        path.write_text("{}")
    approve_replacement(s)
    result, endpoint = run_case(s)
    assert result["status"] == "failed" and result["reason"] == "REPLACEMENT_PREFLIGHT_MISSING_OR_CHANGED"
    assert result["teardown"]["confirmed"] is True and endpoint.posts == 0


def test_fresh_absence_lookup_timestamps_do_not_invalidate_preflight(predecessor):
    s = predecessor
    original = s.backend.status(s.config.replacement.worker_id)
    observations = []

    def status(worker_id):
        observations.append(worker_id)
        return SimpleNamespace(
            worker_id=original.worker_id,
            state=original.state,
            provider_id=original.provider_id,
            request_key=original.request_key,
            configuration_hash=original.configuration_hash,
            checked_at=len(observations),
        )

    backend = SimpleNamespace(status=status)
    with runner.State(s.config.trusted_state_directory) as state:
        first = replacement.verify_before_approval(s.config, s.plan, s.ledger, backend, state, owner=os.geteuid())
        second = replacement.verify_before_approval(s.config, s.plan, s.ledger, backend, state, owner=os.geteuid())
    assert first == second and observations == [original.worker_id] * 2
    assert len(s.http.purchases) == 1


@pytest.mark.parametrize("field", ["request_id", "worker_id", "approval_id", "observed_provider_id", "job_ids"])
def test_replacement_cannot_reuse_predecessor_authority_or_physical_identity(predecessor, field):
    s = predecessor
    request = approve_replacement(s)
    b = s.config.replacement
    old = [b.job_id] if field == "job_ids" else b.provider_id if field == "observed_provider_id" else getattr(b, field)
    with pytest.raises(replacement.ReplacementError, match="IDENTITIES_REUSED"):
        replacement.fresh_request(s.config, dict(request, **{field: old}), s.ledger)


def test_numerical_success_without_positive_new_deletion_is_not_replacement_acceptance(predecessor):
    s = predecessor
    request = approve_replacement(s)

    def refuse(_):
        raise TimeoutError("synthetic deletion refusal")

    s.http.delete_hook = refuse
    result, endpoint = run_case(s, model_inventory=lambda *_a, **_k: {"receipt_sha256": "sha256:" + "f" * 64})
    assert endpoint.posts == 1 and s.ledger.get_job(request["job_ids"][0]).state == "COMPLETED"
    assert result["status"] == "failed" and result["reason"] == "PROVIDER_DELETION_UNCONFIRMED"
    assert result["replacement"]["complete"] is False


def test_readback_failure_stops_new_pod_without_numerical_submission(predecessor):
    s = predecessor
    approve_replacement(s)

    def mismatch(*_args, **_kwargs):
        raise replacement.ReplacementError("REPLACEMENT_MODEL_READBACK_CHANGED")

    result, endpoint = run_case(s, model_inventory=mismatch)
    assert result["status"] == "failed" and result["reason"] == "REPLACEMENT_MODEL_READBACK_CHANGED"
    assert result["teardown"]["confirmed"] is True and endpoint.posts == 0
