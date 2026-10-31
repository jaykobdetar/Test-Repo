"""Evidence for one separately approved replacement of a successful public run.

This module cannot approve, create, dispatch, or delete compute. It verifies the
old accepted bundle, the fresh authority, and a bounded read of the new model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import time

from pydantic import model_validator

from .audit import canonical_json
from .ledger import JobState, Ledger
from .model_assets import PreparedBundle, canonical_locks, lock_hash
from .provider import WorkerState
from .schemas import FrozenModel, Identifier, SHA256
from .worker_contracts import ExecutionReceipt, WorkerConfig, WorkerState as ExecutionState


class ReplacementError(ValueError):
    """Fixed public refusal codes only."""


def require(condition, code):
    if not condition:
        raise ReplacementError(code)


def digest(value):
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ReplacementBinding(FrozenModel):
    request_id: Identifier
    worker_id: Identifier
    provider_id: Identifier
    approval_id: Identifier
    job_id: Identifier
    attempt_id: Identifier
    manifest_sha256: SHA256
    runner_config_path: str
    runner_config_sha256: SHA256
    result_sha256: SHA256  # canonical parsed JSON, not a raw-file checksum

    @model_validator(mode="after")
    def fixed_path(self):
        require(
            self.runner_config_path.startswith("/")
            and all(part not in ("", ".", "..") for part in self.runner_config_path.split("/")[1:]),
            "REPLACEMENT_CONFIG_PATH_INVALID",
        )
        return self


def submission_predecessor(config, rpc):
    """Research-visible checks only; the trusted runner revalidates full evidence."""
    binding = config.replacement
    job = rpc.call("job_status", {"job_id": binding.job_id})
    require(
        job.get("job_id") == binding.job_id and job.get("state") == "COMPLETED" and job.get("attempt_count") == 1,
        "REPLACEMENT_PREDECESSOR_NOT_COMPLETED",
    )
    manifest = rpc.call("read_manifest", {"job_id": binding.job_id})
    require(digest(manifest) == binding.manifest_sha256, "REPLACEMENT_MANIFEST_CHANGED")
    rows = rpc.call("gpu_status").get("requests", [])
    matches = [row for row in rows if row.get("request_id") == binding.request_id]
    require(len(matches) == 1, "REPLACEMENT_PREDECESSOR_REQUEST_MISSING")
    validate_old_request(config, matches[0])


def validate_old_request(config, row):
    b = config.replacement
    require(
        row.get("worker_id") == b.worker_id
        and row.get("observed_provider_id") == b.provider_id
        and row.get("approval_id") == b.approval_id
        and row.get("job_ids") == [b.job_id]
        and row.get("state") == "STOPPED"
        and row.get("action") in {"CREATE", "REPLACE"}
        and row.get("infrastructure") is None
        and row.get("configuration") == config.deployment.model_dump(mode="json")
        and row.get("configuration_hash") == config.deployment.digest
        and row.get("max_runtime_seconds") == config.max_runtime_seconds,
        "REPLACEMENT_PREDECESSOR_REQUEST_INVALID",
    )


def retained_bundle(ledger, binding):
    from .gpu_acceptance_runner import read_file

    manifest = ledger.get_manifest(binding.job_id)
    require(digest(manifest.model_dump(mode="json")) == binding.manifest_sha256, "REPLACEMENT_MANIFEST_CHANGED")
    root = ledger.get_artifact_root(binding.job_id)
    seal = json.loads(read_file(root / ".probe-bundle.json", owner=os.geteuid(), bound=4096))
    require(
        seal == {"job_id": binding.job_id, "attempt_id": binding.attempt_id, "manifest_hash": binding.manifest_sha256},
        "REPLACEMENT_BUNDLE_SEAL_INVALID",
    )
    require(bool(manifest.artifacts), "REPLACEMENT_ARTIFACTS_MISSING")
    # Existing descriptor-based verifier rejects links, changes during reads,
    # missing bytes, wrong checksums and an incorrect total byte count.
    Ledger._copy_artifacts(manifest, root, None, manifest.cost.bytes_persisted)
    artifacts = [
        {"path": item.path, "sha256": "sha256:" + item.sha256}
        for item in sorted(manifest.artifacts, key=lambda item: item.path)
    ]
    require("summary.json" in {item.path for item in manifest.artifacts}, "REPLACEMENT_PARITY_SUMMARY_MISSING")
    summary = json.loads(read_file(root / "summary.json", owner=os.geteuid()))
    require(
        summary.get("suite") == "backend_parity_v1"
        and summary.get("passed") is True
        and summary.get("scientific_evidence") is False,
        "REPLACEMENT_PREDECESSOR_PARITY_FAILED",
    )
    return {
        "manifest_sha256": binding.manifest_sha256,
        "artifacts": artifacts,
        "bytes_persisted": manifest.cost.bytes_persisted,
        "artifacts_sha256": digest(artifacts),
    }


def verify_predecessor(config, plan, ledger, backend, *, owner=0):
    """Read authoritative old state and independently re-read its accepted bytes."""
    from .gpu_acceptance_runner import RunnerConfig, load_worker_config, read_file
    from .gpu_acceptance import AcceptancePlan

    b = config.replacement
    raw = read_file(b.runner_config_path, owner=owner)
    require("sha256:" + hashlib.sha256(raw).hexdigest() == b.runner_config_sha256, "REPLACEMENT_CONFIG_CHANGED")
    previous = RunnerConfig.model_validate_json(raw)
    unchanged = (
        "service_uid",
        "research_uid",
        "admin_uid",
        "worker_config_path",
        "worker_config_sha256",
        "deployment",
        "source_commit",
        "expected_worker_price_usd_per_hour",
        "max_runtime_seconds",
        "provider_config_path",
        "ssh_identity_file",
        "bearer_secret_file",
    )
    require(all(getattr(config, name) == getattr(previous, name) for name in unchanged), "REPLACEMENT_SCOPE_CHANGED")
    require(
        config.trusted_state_directory != previous.trusted_state_directory
        and config.submission_state_directory != previous.submission_state_directory
        and config.plan_path != previous.plan_path,
        "REPLACEMENT_STATE_REUSED",
    )
    old_raw = read_file(previous.plan_path, owner=owner)
    require("sha256:" + hashlib.sha256(old_raw).hexdigest() == previous.plan_sha256, "REPLACEMENT_PLAN_CHANGED")
    old_plan = AcceptancePlan.model_validate_json(old_raw)
    require(
        len(old_plan.cases) == 1
        and old_plan.cases[0].name == "backend-parity"
        and old_plan.cases[0].action == "wait"
        and old_plan.cases[0].expected_state == "COMPLETED"
        and old_plan.cases[0].expected_failure_kind is None
        and old_plan.model == plan.model
        and old_plan.label != plan.label
        and old_plan.cases[0].spec.idempotency_key != plan.cases[0].spec.idempotency_key
        and old_plan.cases[0].spec.model_dump(exclude={"idempotency_key"})
        == plan.cases[0].spec.model_dump(exclude={"idempotency_key"}),
        "REPLACEMENT_PARITY_SPEC_CHANGED",
    )
    worker = WorkerConfig.model_validate(load_worker_config(config, plan, owner=owner))
    directory = Path(previous.trusted_state_directory)
    result = json.loads(read_file(directory / "result.json", owner=config.service_uid, private=True))
    require(digest(result) == b.result_sha256, "REPLACEMENT_RESULT_CHANGED")
    require(
        result.get("schema_version") == 1
        and result.get("kind") == "single_public_gpu_calibration"
        and result.get("status") == "passed"
        and result.get("stage") == "completed"
        and result.get("scientific_evidence") is False
        and result.get("approval_consumed_by_runner") is False
        and result.get("case") == "backend-parity"
        and result.get("observed_state") == "COMPLETED"
        and result.get("observed_failure_kind") is None
        and all(
            result.get(name) == getattr(b, name)
            for name in ("request_id", "worker_id", "provider_id", "job_id", "attempt_id", "manifest_sha256")
        )
        and result.get("plan_sha256") == previous.plan_sha256,
        "REPLACEMENT_SUCCESSFUL_PREDECESSOR_REQUIRED",
    )
    teardown = result.get("teardown", {})
    require(
        teardown.get("confirmed") is True
        and teardown.get("state") == "ABSENT"
        and teardown.get("provider_id") == b.provider_id,
        "REPLACEMENT_PRIOR_DELETION_UNCONFIRMED",
    )
    observations = json.loads(read_file(directory / "observations.json", owner=config.service_uid, private=True))
    cases = observations.get("cases", [])
    require(
        observations.get("case_results_passed") is True
        and len(cases) == 1
        and cases[0].get("case") == "backend-parity"
        and cases[0].get("passed") is True,
        "REPLACEMENT_PRIOR_RECEIPT_MISSING",
    )
    receipt = ExecutionReceipt.model_validate(cases[0].get("observed_receipt"))
    require(
        digest(receipt.model_dump(mode="json")) == result.get("worker_receipt_sha256")
        and receipt.job_id == b.job_id
        and receipt.attempt_id == b.attempt_id
        and receipt.state == ExecutionState.SUCCEEDED
        and receipt.process_stopped
        and receipt.manifest is not None
        and digest(receipt.manifest.model_dump(mode="json")) == b.manifest_sha256,
        "REPLACEMENT_PRIOR_RECEIPT_INVALID",
    )
    job = ledger.get_job(b.job_id)
    require(
        job.state == JobState.COMPLETED
        and job.attempt_id == b.attempt_id
        and job.worker_id == b.worker_id
        and job.approval_id == b.approval_id
        and job.attempt_count == 1
        and job.retry_count == 0
        and job.spec == old_plan.cases[0].spec,
        "REPLACEMENT_PREDECESSOR_NOT_COMPLETED",
    )
    with ledger.read_connection() as connection:
        old = connection.execute("SELECT * FROM compute_requests WHERE request_id=?", (b.request_id,)).fetchone()
        attempt = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (b.attempt_id,)).fetchone()
        approval = connection.execute("SELECT * FROM approvals WHERE approval_id=?", (b.approval_id,)).fetchone()
        jobs = [
            row[0]
            for row in connection.execute("SELECT job_id FROM approval_jobs WHERE approval_id=?", (b.approval_id,))
        ]
        count = connection.execute(
            "SELECT count(*) FROM compute_requests WHERE worker_id=?", (b.worker_id,)
        ).fetchone()[0]
    require(
        old is not None and attempt is not None and approval is not None and count == 1,
        "REPLACEMENT_PREDECESSOR_AUTHORITY_MISSING",
    )
    old = dict(old)
    for name in ("configuration", "job_ids", "infrastructure"):
        old[name] = json.loads(old[name]) if old[name] else None
    validate_old_request(config, old)
    public = json.loads(approval["document"])
    require(
        approval["consumed_at"] is not None
        and approval["ended_at"] is not None
        and approval["ended_at"] >= approval["consumed_at"]
        and jobs == [b.job_id],
        "REPLACEMENT_PREDECESSOR_APPROVAL_OPEN",
    )
    require(
        public.get("purpose", "research") == "research"
        and public["pod_id"] == b.worker_id
        and public["batch_hash"] == old["batch_hash"]
        and public["max_runtime_seconds"] == config.max_runtime_seconds
        # end_approval deliberately shortens the core deadline to the
        # positive stop time; the controller retains the original cutoff.
        and math.isfinite(approval["deadline"])
        and math.isfinite(old["deadline"])
        and approval["consumed_at"] <= approval["deadline"] <= old["deadline"]
        and old["deadline"] - approval["consumed_at"] <= config.max_runtime_seconds
        and old["batch_hash"] == ledger.batch_hash([b.job_id]),
        "REPLACEMENT_PREDECESSOR_APPROVAL_INVALID",
    )
    require(
        attempt["job_id"] == b.job_id
        and attempt["worker_id"] == b.worker_id
        and attempt["approval_id"] == b.approval_id
        and attempt["stopped_at"] is not None
        and approval["ended_at"] >= attempt["stopped_at"]
        and attempt["outcome"] == "completed",
        "REPLACEMENT_PREDECESSOR_AUTHORITY_INVALID",
    )
    manifest = ledger.get_manifest(b.job_id)
    require(
        manifest.model == worker.model
        and manifest.software.container_image_digest == config.deployment.image_digest
        and manifest.software.probe_mcp_git_commit == config.source_commit
        and manifest.hardware.region == config.deployment.region
        and manifest.hardware.live_price_usd_per_hour == config.expected_worker_price_usd_per_hour
        and manifest.hardware.provider_backend == "runpod"
        and manifest.hardware.gpu_count == 1
        and manifest.run.approval_id == b.approval_id,
        "REPLACEMENT_PRIOR_PROVENANCE_CHANGED",
    )
    observed = backend.status(b.worker_id)
    require(
        observed.state == WorkerState.ABSENT
        and observed.worker_id == b.worker_id
        and observed.provider_id == b.provider_id
        and observed.request_key == b.request_id
        and observed.configuration_hash == config.deployment.digest,
        "REPLACEMENT_PREDECESSOR_NOT_ABSENT",
    )
    # Evidence equality intentionally excludes volatile provider timestamps or
    # other adapter metadata. Every call still performs a fresh absence lookup.
    provider = {
        "worker_id": observed.worker_id,
        "state": observed.state.value,
        "provider_id": observed.provider_id,
        "request_key": observed.request_key,
        "configuration_hash": observed.configuration_hash,
    }
    return {
        "binding": b.model_dump(mode="json"),
        "provider": provider,
        "bundle": retained_bundle(ledger, b),
        "worker_config_sha256": config.worker_config_sha256,
        "assets_sha256": digest([asset.model_dump(mode="json") for asset in worker.assets]),
    }


def fresh_request(config, request, ledger):
    b = config.replacement
    require(
        all(request[name] != getattr(b, name) for name in ("request_id", "worker_id", "approval_id"))
        and request["observed_provider_id"] != b.provider_id
        and request["job_ids"] != [b.job_id]
        and request["action"] == "REPLACE"
        and request["replaces_worker_id"] == b.worker_id,
        "REPLACEMENT_IDENTITIES_REUSED",
    )
    job = ledger.get_job(request["job_ids"][0])
    require(
        job.state == JobState.PENDING and job.attempt_id is None and job.attempt_count == 0 and job.retry_count == 0,
        "REPLACEMENT_ALREADY_EXECUTED",
    )


def preflight_record(config, predecessor):
    return {
        "schema_version": 1,
        "kind": "replacement_preflight",
        "config_sha256": digest(config.model_dump(mode="json")),
        "plan_sha256": config.plan_sha256,
        "predecessor": predecessor,
        "approval_consumed_by_runner": False,
    }


def verify_before_approval(config, plan, ledger, backend, state, *, owner=0):
    """Preparation gate: reads the old provider only, never requests new compute."""
    require(config.replacement is not None, "REPLACEMENT_BINDING_REQUIRED")
    predecessor = verify_predecessor(config, plan, ledger, backend, owner=owner)
    record = preflight_record(config, predecessor)
    state.publish("replacement-preflight.json", record)
    return {
        "status": "passed",
        "kind": "replacement_preflight",
        "receipt_sha256": digest(record),
        "approval_consumed_by_runner": False,
    }


# Fixed code, not an operator-supplied remote program. It uses the existing
# inventory implementation/resource installed in worker image 6c9bec0. The alarm
# and CPU ceiling terminate this read even if the local SSH connection is lost.
INVENTORY_SCRIPT = """import os,resource,signal,sys
assert os.geteuid()==0
repo,thinking,seconds=sys.argv[1:]
assert repo in ("Qwen/Qwen3-1.7B-Base","Qwen/Qwen3-1.7B")
assert thinking in ("none","true","false") and seconds.isdecimal() and 1<=int(seconds)<=60
os.environ.clear()
os.environ.update(PATH="/usr/bin:/bin",LANG="C",HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",PYTHONDONTWRITEBYTECODE="1")
os.chdir("/")
resource.setrlimit(resource.RLIMIT_CPU,(int(seconds),int(seconds)))
signal.alarm(int(seconds))
os.setgroups([])
os.setgid(10001)
os.setuid(10001)
from pathlib import Path
from probe_core.model_assets import canonical_locks,inventory
from probe_core.audit import canonical_json
lock=next(item for item in canonical_locks() if item.repo==repo)
result=inventory(Path("/opt/probe-assets/models")/repo.split("/")[-1]/lock.revision,lock,thinking_mode=None if thinking=="none" else thinking=="true")
print(canonical_json({"schema_version":1,"uid":os.geteuid(),"gid":os.getegid(),"inventory":result.model_dump(mode="json")}),flush=True)
"""


def read_model_inventory(config, plan, settings, *, deadline, clock=time.time, command=None, owner=0):
    from .gpu_acceptance_runner import command_bytes, load_worker_config
    from .rpc import decode

    worker = WorkerConfig.model_validate(load_worker_config(config, plan, owner=owner))
    require(
        worker.model_directory
        == "/opt/probe-assets/models/" + worker.model.repo.split("/")[-1] + "/" + worker.model.revision_sha,
        "REPLACEMENT_MODEL_PATH_INVALID",
    )
    seconds = min(60, int(deadline - clock()))
    require(seconds >= 1, "REPLACEMENT_READBACK_DEADLINE")
    thinking = "none" if worker.model.thinking_mode is None else str(worker.model.thinking_mode).lower()
    remote = shlex.join(
        [
            "/opt/probe-core/venv/bin/python",
            "-I",
            "-B",
            "-c",
            INVENTORY_SCRIPT,
            worker.model.repo,
            thinking,
            str(seconds),
        ]
    )
    options = [
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "UserKnownHostsFile=" + str(settings["known_hosts_file"]),
        "-i",
        str(settings["identity_file"]),
    ]
    raw = (command or command_bytes)(
        ["/usr/bin/ssh", *options, "-T", "-p", str(settings["ssh_port"]), "root@" + settings["host"], remote],
        timeout=seconds,
    )
    require(clock() < deadline and isinstance(raw, str) and len(raw) <= 65536, "REPLACEMENT_READBACK_DEADLINE")
    observed = decode(raw.encode())
    require(
        type(observed) is dict
        and set(observed) == {"schema_version", "uid", "gid", "inventory"}
        and type(observed["schema_version"]) is int
        and observed["schema_version"] == 1
        and type(observed["uid"]) is int
        and observed["uid"] == 10001
        and type(observed["gid"]) is int
        and observed["gid"] == 10001,
        "REPLACEMENT_MODEL_READBACK_INVALID",
    )
    inventory = PreparedBundle.model_validate(observed["inventory"])
    lock = next(item for item in canonical_locks() if item.repo == worker.model.repo)
    expected = {asset.path: asset.sha256 for asset in worker.assets}
    require(
        inventory.model == worker.model
        and inventory.source_lock_hash == lock_hash(lock)
        and len(inventory.assets) == len(expected)
        and {asset.path: asset.sha256 for asset in inventory.assets} == expected,
        "REPLACEMENT_MODEL_READBACK_CHANGED",
    )
    return {
        "schema_version": 1,
        "observed_at": clock(),
        "receipt": observed,
        "receipt_sha256": digest(observed),
        "worker_config_sha256": config.worker_config_sha256,
    }
