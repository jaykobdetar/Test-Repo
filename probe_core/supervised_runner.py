"""One approved ledger job over SSH, using the supervised public Pod boundary.

This runner consumes no approval and creates no compute. The installed controller
and watchdog retain those responsibilities. It changes only the worker transport.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from typing import Literal

from pydantic import model_validator

from .audit import canonical_json
from .controller import ControllerClient
from .dispatcher import Dispatcher, DispatcherService, TransportError
from .gpu_acceptance import AcceptancePlan, collect
from .gpu_acceptance_runner import (
    CONTROLLER_SOCKET,
    LEDGER,
    RESEARCH_SOCKET,
    RunnerConfig,
    RunnerError,
    State,
    digest,
    find_request,
    read_file,
    require,
    stop_and_observe,
    submit,
    validate_authority,
    validate_plan,
    verified_endpoint,
)
from .ledger import JobState, Ledger
from .rpc import UnixRPCClient, decode
from .runpod_provider import RunPodConfig, RunPodProvider
from .schemas import SHA256
from .ssh_job_client import SSHJobClient

PUBLIC_FIELDS = frozenset(
    {"model", "assets", "datasets", "code_git_commit", "container_image_digest", "region", "live_price_usd_per_hour"}
)
PUBLIC_DATASET = "sha256:6867f3b38b8587c8f7b71955bbb597eac27445ee69b2aa033904619df4df5bbe"


class SupervisedRunnerConfig(RunnerConfig):
    profile: Literal["supervised_fixed_public"] = "supervised_fixed_public"
    helper_path: str
    helper_sha256: SHA256

    @model_validator(mode="after")
    def supervised_scope(self):
        require(
            Path(self.helper_path).is_absolute() and ".." not in Path(self.helper_path).parts,
            "ABSOLUTE_HELPER_PATH_REQUIRED",
        )
        # Replacement needs a new verifier for this execution profile. Never
        # reinterpret an older managed-worker receipt as equivalent evidence.
        require(self.replacement is None, "SUPERVISED_REPLACEMENT_NOT_IMPLEMENTED")
        return self


def load_inputs(path, *, mode, owner=0):
    config = SupervisedRunnerConfig.model_validate_json(read_file(path, owner=owner))
    require(os.geteuid() == (config.research_uid if mode == "submit" else config.service_uid), "WRONG_PROCESS_IDENTITY")
    raw = read_file(config.plan_path, owner=owner)
    require("sha256:" + hashlib.sha256(raw).hexdigest() == config.plan_sha256, "PLAN_HASH_MISMATCH")
    plan = AcceptancePlan.model_validate_json(raw)
    validate_plan(config, plan)
    case = plan.cases[0]
    require(
        case.action in {"wait", "cancel_after_running"} and case.name != "public-direction-transfer",
        "SUPERVISED_ACTION_NOT_IMPLEMENTED",
    )
    raw = read_file(config.worker_config_path, owner=owner)
    require("sha256:" + hashlib.sha256(raw).hexdigest() == config.worker_config_sha256, "WORKER_CONFIG_HASH_MISMATCH")
    worker = decode(raw)
    require(type(worker) is dict and set(worker) == PUBLIC_FIELDS, "PUBLIC_CONFIGURATION_REQUIRED")
    require(
        worker["model"] == plan.model.model_dump(mode="json")
        and worker["container_image_digest"] == config.deployment.image_digest
        and worker["code_git_commit"] == config.source_commit
        and worker["region"] == config.deployment.region
        and worker["live_price_usd_per_hour"] == config.expected_worker_price_usd_per_hour,
        "PUBLIC_CONFIGURATION_BINDING_INVALID",
    )
    require(
        len(worker["datasets"]) == 1
        and worker["datasets"][0]["sha256"] == PUBLIC_DATASET
        and worker["datasets"][0]["path"] == "/opt/probe-assets/datasets/public-calibration-prompts.json"
        and case.spec.inputs.dataset_revision == PUBLIC_DATASET
        and case.spec.inputs.prompt_ids == ("public-short", "public-long"),
        "FIXED_PUBLIC_DATASET_REQUIRED",
    )
    helper = read_file(config.helper_path, owner=owner, bound=256 * 1024)
    require("sha256:" + hashlib.sha256(helper).hexdigest() == config.helper_sha256, "HELPER_HASH_MISMATCH")
    return config, plan, worker


def wait_for_approval(config, plan, ledger, *, clock=time.time, sleep=time.sleep):
    until = time.monotonic() + config.approval_wait_seconds
    while time.monotonic() < until:
        request = find_request(ledger, config, plan)
        if request is not None and request["state"] == "RUNNING":
            return request
        require(
            request is None or request["state"] in {"PENDING", "PREPARING", "STARTING"},
            "REQUEST_TERMINATED_BEFORE_ACCEPTANCE",
        )
        sleep(0.25)
    raise RunnerError("APPROVAL_WAIT_EXPIRED")


def cancellation_step(dispatcher, client, job, state, worker, deadline):
    """Reconcile one durable cancellation intent across coordinator interruption."""
    before = state.read("cancellation-before.json")
    if state.read("cancellation-after.json") is not None:
        return
    if job.attempt_id is None:
        return
    execution = dispatcher._request(job)
    if before is None:
        if job.state != JobState.RUNNING:
            return
        before = client.inspect(job.attempt_id)
        observed = before.get("receipt", {})
        if not (before.get("execution_started") and observed.get("state") == "RUNNING"):
            return
        started = before["execution_started"]
        require(
            observed.get("job_id") == job.job_id
            and observed.get("attempt_id") == job.attempt_id
            and before.get("request_sha256") == digest(execution.model_dump(mode="json"))
            and before.get("config_sha256") == digest(worker)
            and isinstance(before.get("child_identity"), dict)
            and datetime.fromisoformat(before["original_deadline"]).timestamp() == deadline
            and type(started) is dict
            and all(
                started.get(key) == getattr(execution, key)
                for key in ("job_id", "attempt_id", "worker_id", "approval_id")
            )
            and all(started.get(key) == before[key] for key in ("request_sha256", "config_sha256", "child_identity"))
            and datetime.fromisoformat(started["deadline"]) == execution.deadline,
            "CANCELLATION_ATTEMPT_MISMATCH",
        )
        state.publish("cancellation-before.json", before)
    current = client.inspect(job.attempt_id)
    if current["receipt"]["state"] == "RUNNING":
        # cancel() is idempotent for this exact attempt. It never starts work.
        client.cancel(job.attempt_id)
        # A completion racing cancellation must retain its actual outcome.
        dispatcher.reconcile(job.job_id)
        current = client.inspect(job.attempt_id)
    proof = current.get("cancellation") or {}
    require(
        current["receipt"]["state"] == "CANCELLED"
        and current["receipt"]["process_stopped"] is True
        and current["receipt"]["job_id"] == job.job_id
        and current["receipt"]["attempt_id"] == job.attempt_id
        and proof.get("attempt_id") == job.attempt_id
        and all(
            proof.get(key) == before.get(key) == current.get(key)
            for key in ("request_sha256", "config_sha256", "child_identity", "original_deadline")
        )
        and proof.get("request_sha256") == digest(execution.model_dump(mode="json"))
        and proof.get("config_sha256") == digest(worker)
        and proof.get("process_stopped") is True
        and proof.get("descendants_stopped") is True
        and proof.get("result_present") is False
        and proof.get("signal") == "SIGKILL"
        and proof.get("signal_scope") == "fenced_monitor_descendants"
        and before.get("child_identity") in proof.get("signalled", []),
        "CANCELLATION_STOP_UNCONFIRMED",
    )
    state.publish("cancellation-after.json", current)


def run(
    config,
    plan,
    worker,
    ledger,
    backend,
    cloud,
    state,
    *,
    clock=time.time,
    sleep=time.sleep,
    endpoint=verified_endpoint,
    client_factory=SSHJobClient,
    progress=lambda stage: None,
):
    """Resume the persisted attempt; never manufacture a new approval or deadline."""
    validate_plan(config, plan)
    state.publish(
        "binding.json", {"config_sha256": digest(config.model_dump(mode="json")), "plan_sha256": config.plan_sha256}
    )
    if state.read("result.json") is not None:
        return state.read("result.json")
    progress("waiting_for_installed_approval")
    request = wait_for_approval(config, plan, ledger, clock=clock, sleep=sleep)
    report = {
        "schema_version": 1,
        "kind": "supervised_ledger_acceptance",
        "status": "failed",
        "profile": config.profile,
        "request_id": request["request_id"],
        "worker_id": request["worker_id"],
        "provider_id": request["observed_provider_id"],
        "plan_sha256": config.plan_sha256,
        "approval_consumed_by_runner": False,
        "scientific_evidence": False,
        "nested_cgroup_enforcement": False,
        "lifecycle_acceptance_complete": False,
    }
    try:
        deadline = validate_authority(ledger, config, plan, request, now=clock())
        state.publish(
            "bound-request.json",
            {
                k: request[k]
                for k in ("request_id", "worker_id", "approval_id", "observed_provider_id", "deadline", "batch_hash")
            },
        )
        # Reattachment reserves collection/deletion time. Only a never-dispatched
        # job must also reserve its entire job runtime before starting.
        job = ledger.get_job(request["job_ids"][0])
        startup_cutoff = (
            deadline - 120 - (plan.cases[0].spec.limits.max_runtime_seconds if job.attempt_id is None else 0)
        )
        progress("verified_ssh_startup")
        while clock() < startup_cutoff:
            current = find_request(ledger, config, plan)
            validate_authority(ledger, config, plan, current, now=clock())
            try:
                settings = endpoint(config, current, backend, state)
                break
            except RunnerError as error:
                if str(error) not in {
                    "DIRECT_SSH_ENDPOINT_UNAVAILABLE",
                    "PROVIDER_ENDPOINT_UNAVAILABLE",
                    "PROVIDER_LOGS_UNAVAILABLE",
                    "HOST_FINGERPRINT_UNAVAILABLE",
                    "SSH_VERIFICATION_COMMAND_FAILED",
                    "SCANNED_HOST_KEY_AMBIGUOUS",
                }:
                    raise
                sleep(1)
        else:
            raise RunnerError("WORKER_STARTUP_DEADLINE")
        client = client_factory(settings, worker, datetime.fromtimestamp(deadline, timezone.utc), timeout_seconds=10)
        staged = client.stage_helper(Path(config.helper_path), config.helper_sha256)
        state.publish("helper.json", staged)
        dispatcher = Dispatcher(
            ledger,
            client,
            worker_id=request["worker_id"],
            transfer_directory=state.directory / "transfers",
            input_artifact_root=LEDGER.parent / "input-artifacts",
            lease_seconds=30,
        )
        service = DispatcherService(dispatcher)
        progress("approved_ssh_dispatch")
        while clock() < deadline - 45:
            current = find_request(ledger, config, plan)
            validate_authority(ledger, config, plan, current, now=clock())
            job = ledger.get_job(request["job_ids"][0])
            require(job.attempt_id is not None or clock() < startup_cutoff, "WORKER_STARTUP_DEADLINE")
            service.tick()
            job = ledger.get_job(job.job_id)
            if plan.cases[0].action == "cancel_after_running":
                cancellation_step(dispatcher, client, job, state, worker, deadline)
                job = ledger.get_job(job.job_id)
            if job.state in {JobState.COMPLETED, JobState.FAILED}:
                break
            sleep(0.25)
        else:
            raise RunnerError("APPROVED_DEADLINE_REACHED")
        observation = collect(ledger, plan, client)
        state.publish("observations.json", observation)
        require(observation["case_results_passed"] is True, "CALIBRATION_DID_NOT_PASS")
        if plan.cases[0].action == "cancel_after_running":
            require(
                state.read("cancellation-before.json") is not None
                and state.read("cancellation-after.json") is not None,
                "CANCELLATION_EVIDENCE_MISSING",
            )
            report["cancellation_proven"] = True
        job = ledger.get_job(job.job_id)
        if job.state == JobState.COMPLETED:
            manifest = ledger.get_manifest(job.job_id)
            require(
                manifest.model == plan.model
                and manifest.software.container_image_digest == config.deployment.image_digest
                and manifest.software.probe_mcp_git_commit == config.source_commit
                and manifest.hardware.region == config.deployment.region,
                "MANIFEST_PROVENANCE_MISMATCH",
            )
            report["manifest_sha256"] = digest(manifest.model_dump(mode="json"))
        report.update(
            status="passed",
            case=plan.cases[0].name,
            job_id=job.job_id,
            attempt_id=job.attempt_id,
            observed_state=job.state.value,
            observed_failure_kind=job.failure_kind,
            attempt_count=job.attempt_count,
        )
    except Exception as error:
        report.update(
            status="failed",
            error_type=type(error).__name__,
            reason=str(error) if isinstance(error, RunnerError) else "SUPERVISED_RUN_FAILED",
        )
    finally:
        progress("provider_deletion")
        report["teardown"] = stop_and_observe(cloud, backend, request, sleep=sleep)
        if not report["teardown"]["confirmed"]:
            report.update(status="failed", reason="PROVIDER_DELETION_UNCONFIRMED")
        state.publish("result.json", report)
    return report


def configuration_paths(config_path, suite_path):
    if config_path is not None:
        return (config_path,)
    body = decode(read_file(suite_path, owner=0))
    require(
        type(body) is dict
        and set(body) == {"schema_version", "configurations"}
        and body["schema_version"] == 1
        and type(body["configurations"]) is list
        and 1 <= len(body["configurations"]) <= 16,
        "SUITE_INVALID",
    )
    paths = []
    for value in body["configurations"]:
        require(
            type(value) is str
            and "/" not in value
            and value.endswith("-acceptance.json")
            and value not in {".", ".."}
            and len(value) <= 128,
            "SUITE_PATH_INVALID",
        )
        paths.append(suite_path.parent / value)
    require(len(set(paths)) == len(paths), "DUPLICATE_SUITE_CONFIGURATION")
    return tuple(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("submit", "run"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path)
    source.add_argument("--suite", type=Path)
    args = parser.parse_args()

    def interrupted(*_):
        raise RunnerError("RUNNER_INTERRUPTED")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    paths = configuration_paths(args.config, args.suite)
    # Validate the entire pinned suite before proposing or starting its first
    # case. Each case still needs a separate exact ledger approval.
    inputs = [(path, *load_inputs(path, mode=args.mode)) for path in paths]
    for path, config, plan, worker in inputs:
        directory = config.submission_state_directory if args.mode == "submit" else config.trusted_state_directory
        with State(directory) as state:
            if args.mode == "submit":
                result = submit(
                    config, plan, UnixRPCClient(RESEARCH_SOCKET, expected_server_uid=config.service_uid), state
                )
            else:
                require(LEDGER.is_file() and not LEDGER.is_symlink(), "INSTALLED_LEDGER_REQUIRED")
                provider = RunPodProvider(RunPodConfig.load(config.provider_config_path))
                with Ledger(LEDGER) as ledger:
                    result = run(
                        config,
                        plan,
                        worker,
                        ledger,
                        provider,
                        ControllerClient(CONTROLLER_SOCKET, expected_server_uid=config.service_uid),
                        state,
                        progress=lambda stage: print(canonical_json({"case": path.name, "stage": stage}), flush=True),
                    )
        print(canonical_json({"configuration": path.name, "result": result}), flush=True)
        if result.get("status") == "failed":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
