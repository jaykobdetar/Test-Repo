"""Prepare exact approved GPU calibration batches and collect actual receipts.

This CLI never consumes an approval, provisions compute, or runs an unqueued GPU
operation. Controller/dispatcher services execute the prepared typed jobs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from .dispatcher import TransportError, WorkerClient
from .direction_transfer import direction_reference, verify_direction_evidence
from .ledger import Ledger
from .schemas import FrozenModel, Identifier, JobSpec, ModelIdentity
from .worker import prompt_set_hash, sha256_file
from .worker_contracts import PromptDataset, WorkerConfig, WorkerState


class AcceptanceCase(FrozenModel):
    name: Identifier
    action: Literal["wait", "cancel_after_running", "restart_supervisor_after_running", "reconnect_tunnel_after_running"]
    expected_state: Literal["COMPLETED", "FAILED"]
    expected_failure_kind: Literal["timeout", "cancelled", "policy", "oom"] | None = None
    spec: JobSpec


class AcceptancePlan(FrozenModel):
    schema_version: Literal[1] = 1
    kind: Literal["gpu_runtime_acceptance"] = "gpu_runtime_acceptance"
    label: Identifier
    model: ModelIdentity
    cases: tuple[AcceptanceCase, ...]
    scientific_evidence: Literal[False] = False


# A generic policy error or host RAM exhaustion does not prove the named limit.
# These are the existing worker's specific, stopped terminal outcomes.
LIMIT_FAILURE_CODES = {
    "hard-deadline": frozenset({"ExecutionDeadlineExceeded"}),
    "output-limit": frozenset({"OutputLimitExceeded"}),
    "vram-limit": frozenset({"OutOfMemoryError"}),
}


def make_plan(config: WorkerConfig, label: str) -> AcceptancePlan:
    if config.device != "cuda:0" or config.model.dtype != "bfloat16" or config.model.repo == "probe/testing-tiny-qwen3":
        raise ValueError("live acceptance requires a canonical unquantized BF16/CUDA worker configuration")
    asset = config.datasets[0]
    path = Path(asset.path)
    if path.is_symlink() or sha256_file(path) != asset.sha256:
        raise ValueError("acceptance dataset must match the registered immutable bytes")
    dataset = PromptDataset.model_validate_json(path.read_text())
    prompt_ids = tuple(prompt.prompt_id for prompt in dataset.prompts[:2])
    if len(prompt_ids) < 2:
        raise ValueError("acceptance requires two distinct prompts to exercise padding")
    return fixed_plan(config.model, label, asset.sha256, prompt_set_hash(dataset, prompt_ids), prompt_ids)


def fixed_plan(model: ModelIdentity, label: str, dataset_revision: str,
               prompts_sha256: str, prompt_ids: tuple[str, ...]) -> AcceptancePlan:
    """The fixed case vocabulary, shared by preparation and runner validation.

    Dataset bytes are verified by ``make_plan`` and the pinned worker. This pure
    constructor lets the controller check a case without loading model assets.
    It does not submit jobs or grant execution authority.
    """
    base = {"experiment_stage": "calibration", "model": model.model_dump(mode="json"),
            "inputs": {"dataset_revision": dataset_revision, "prompt_set_hash": prompts_sha256,
                       "prompt_ids": prompt_ids, "random_seed": 123, "generation": {"temperature": 0.0, "max_new_tokens": 4}},
            "operation": {"kind": "capture", "modules": [{"layer": 14, "component": "residual"}], "positions": ["last"]},
            "limits": {"max_runtime_seconds": 120, "max_output_bytes": 32*1024**2, "max_cpu_cores": 4,
                       "max_ram_bytes": 24*1024**3, "max_vram_bytes": 20*1024**3, "max_generated_tokens": 32}}
    definitions = [
        ("backend-parity", "wait", "COMPLETED", None, {"kind": "backend_parity"}, {"max_runtime_seconds": 240}),
        ("capture-retention", "wait", "COMPLETED", None, None, {}),
        ("cancel-running", "cancel_after_running", "FAILED", "cancelled", None, {}),
        ("hard-deadline", "wait", "FAILED", "timeout", None, {"max_runtime_seconds": 1}),
        ("output-limit", "wait", "FAILED", "policy", None, {"max_output_bytes": 1}),
        ("vram-limit", "wait", "FAILED", "oom", None, {"max_vram_bytes": 1024**3}),
        ("supervisor-restart", "restart_supervisor_after_running", "COMPLETED", None, None, {}),
        ("tunnel-reconnect", "reconnect_tunnel_after_running", "COMPLETED", None, None, {}),
    ]
    cases = []
    for name, action, state, kind, operation, limits in definitions:
        values = dict(base, idempotency_key=label+"-"+name,
                      operation=operation or base["operation"], limits=dict(base["limits"], **limits))
        cases.append(AcceptanceCase(name=name, action=action, expected_state=state,
                                    expected_failure_kind=kind, spec=JobSpec.model_validate(values)))
    return AcceptancePlan(label=label, model=model, cases=cases)


def fixed_direction_plan(model: ModelIdentity, label: str, dataset_revision: str,
                         prompts_sha256: str, prompt_ids: tuple[str, ...]) -> AcceptancePlan:
    """Preserve the separate, already prepared public e_0 transfer recipe."""
    ordinary = fixed_plan(model, label, dataset_revision, prompts_sha256, prompt_ids).cases[1]
    values = ordinary.spec.model_dump(mode='json')
    values.update(idempotency_key=label, operation={'kind': 'steer',
        'target': {'layer': 14, 'component': 'residual'}, 'positions': ['last'],
        'direction': direction_reference(), 'strength': 0.5})
    case = AcceptanceCase(name='public-direction-transfer', action='wait', expected_state='COMPLETED',
                          spec=JobSpec.model_validate(values))
    return AcceptancePlan(label=label, model=model, cases=(case,))


def submit(ledger: Ledger, plan: AcceptancePlan):
    jobs = [ledger.submit_job(case.spec) for case in plan.cases]
    return {"job_ids": [job.job_id for job in jobs], "batch_hash": ledger.batch_hash([job.job_id for job in jobs]),
            "approval_consumed": False, "compute_started": False,
            "operator_actions": [{"job_id": job.job_id, "action": case.action} for case, job in zip(plan.cases, jobs)]}


def collect(ledger: Ledger, plan: AcceptancePlan, client: WorkerClient | None = None, *, action_directory: Path | None = None,
            direction_evidence: dict | None = None, input_artifact_root: Path | None = None):
    jobs = {job.spec.idempotency_key: job for job in ledger.list_jobs()}
    reports = []
    for case in plan.cases:
        job = jobs.get(case.spec.idempotency_key)
        row = {"case": case.name, "passed": False, "action": case.action}
        receipt = None
        cancellation_case = case.name == "cancel-running" or case.action == "cancel_after_running"
        if cancellation_case:
            row["cancellation_observation"] = "inconclusive"
        reports.append(row)
        if job is None or job.spec != case.spec:
            row["reason"] = "exact approved job is missing"
            continue
        row.update(job_id=job.job_id, attempt_id=job.attempt_id, state=job.state.value, failure_kind=job.failure_kind)
        if job.attempt_id is None:
            row["reason"] = "no execution attempt exists"
            continue
        with ledger.read_connection() as connection:
            stopped = connection.execute("SELECT stopped_at FROM attempts WHERE attempt_id=?", (job.attempt_id,)).fetchone()[0]
        row["process_stopped_at"] = stopped
        if job.state.value != case.expected_state or job.failure_kind != case.expected_failure_kind or stopped is None:
            row["reason"] = "expected terminal state and positive stop evidence are absent"
            continue
        if case.expected_state == "FAILED" and client is None:
            # Local failure and stopped_at alone do not preserve the worker's
            # outcome: its actual failure may differ, or it may have succeeded.
            row["reason"] = "failure observation is inconclusive without an actual worker receipt"
            continue
        if client is not None:
            try:
                receipt = client.status(job.attempt_id)
            except TransportError:
                row["reason"] = "worker receipt is unavailable or invalid"
                continue
            if receipt.job_id != job.job_id or receipt.attempt_id != job.attempt_id or not receipt.process_stopped:
                row["reason"] = "worker receipt identity or stop evidence mismatches"
                continue
            row["observed_receipt"] = receipt.model_dump(mode="json")
            if cancellation_case:
                if receipt.state != WorkerState.CANCELLED or receipt.failure_kind != "cancelled":
                    row["reason"] = "worker receipt does not confirm cancellation of the exact attempt"
                    continue
                row["cancellation_observation"] = "confirmed"
            elif receipt.state != (WorkerState.SUCCEEDED if case.expected_state == "COMPLETED" else WorkerState.FAILED) or receipt.failure_kind != case.expected_failure_kind:
                row["reason"] = "worker receipt does not confirm the expected terminal outcome"
                continue
            if case.name in LIMIT_FAILURE_CODES:
                if receipt.error_code not in LIMIT_FAILURE_CODES[case.name]:
                    row["reason"] = "worker receipt does not confirm the specific resource limit"
                    continue
                # Dispatcher recovers an expired lease before persisting the
                # receipt. The one-second case's lease ends at its execution
                # deadline, so its authoritative local reason can be derived
                # from that deadline rather than copied from the worker.
                local_deadline = (case.name == "hard-deadline"
                                  and job.failure_reason == "execution deadline elapsed")
                if job.failure_reason != receipt.error_code and not local_deadline:
                    row["reason"] = "worker failure code differs from the recorded terminal outcome"
                    continue
        if job.state.value == "COMPLETED":
            manifest = ledger.get_manifest(job.job_id)
            if manifest.model != plan.model or manifest.hardware.provider_backend != "runpod" or manifest.hardware.gpu_count != 1 or manifest.software.container_image_digest is None:
                row["reason"] = "canonical GPU/container provenance is absent"
                continue
            directory = ledger.get_artifact_root(job.job_id)
            if case.name == "capture-retention" and "tensors.safetensors" not in {item.path for item in manifest.artifacts}:
                row["reason"] = "captured tensor artifact is absent"
                continue
            row["retained_artifacts"] = [{"path": item.path, "sha256": sha256_file(directory/item.path)} for item in manifest.artifacts]
            if any(item["sha256"] != "sha256:"+expected.sha256 for item, expected in zip(row["retained_artifacts"], manifest.artifacts)):
                row["reason"] = "retained artifact bytes changed"
                continue
            row["manifest"] = manifest.model_dump(mode="json")
            if case.name == 'public-direction-transfer':
                try:
                    inputs = case.spec.inputs
                    if case != fixed_direction_plan(plan.model, plan.label, inputs.dataset_revision,
                                                    inputs.prompt_set_hash, inputs.prompt_ids).cases[0]:
                        raise ValueError('fixed direction recipe differs')
                    row['direction_transfer'] = verify_direction_evidence(ledger, job, direction_evidence,
                                                                          input_artifact_root, receipt, manifest)
                except (ValueError, OSError, KeyError, TypeError):
                    row['reason'] = 'controller direction transfer evidence is absent or mismatched'
                    continue
            if case.name == "backend-parity":
                summary = json.loads((directory/"summary.json").read_text())
                if summary.get("suite") != "backend_parity_v1" or summary.get("passed") is not True or summary.get("scientific_evidence") is not False:
                    row["reason"] = "the real fixed backend suite did not pass"
                    continue
                row["calibration"] = summary
        row["passed"] = True
    actions = {"complete": False, "cases": []}
    if action_directory is not None:
        from .gpu_acceptance_actions import collect_action_evidence
        actions = collect_action_evidence(ledger, plan, action_directory)
    proven_actions = {row["action"] for row in actions["cases"] if row["passed"]}
    remaining = []
    if "cancel_after_running" not in proven_actions:
        remaining.append("cancellation requested after the exact attempt was observed running")
    if "restart_supervisor_after_running" not in proven_actions:
        remaining.append("supervisor PID changed while the same attempt remained fenced")
    if "reconnect_tunnel_after_running" not in proven_actions:
        remaining.append("owned SSH tunnel reconnected to the same endpoint while preserving the exact live attempt")
    remaining.extend(["provider-confirmed stop and separately approved restart/replacement",
                      "model and retained-artifact hash readback after replacement"])
    # Action transcripts can prove cancellation, supervisor restart or local SSH replacement.
    # They cannot prove provider shutdown, asset persistence or Pod replacement.
    return {"schema_version": 1, "kind": "gpu_runtime_acceptance_observations", "model": plan.model.model_dump(mode="json"),
            "case_results_passed": all(row["passed"] for row in reports), "cases": reports,
            "scientific_evidence": False, "lifecycle_acceptance_complete": False,
            "action_evidence": actions, "remaining_evidence": remaining}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "submit", "collect"])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--worker-config", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--worker-url")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--action-directory", type=Path)
    args = parser.parse_args()
    if args.command == "plan":
        if args.worker_config is None or args.label is None:
            parser.error("plan requires --worker-config and --label")
        result = make_plan(WorkerConfig.model_validate_json(args.worker_config.read_text()), args.label).model_dump(mode="json")
    else:
        if args.plan is None or args.ledger is None:
            parser.error("submit/collect requires --plan and --ledger")
        plan = AcceptancePlan.model_validate_json(args.plan.read_text())
        client = None
        if args.worker_url:
            if args.token_file is None:
                parser.error("--worker-url requires --token-file")
            info = args.token_file.stat()
            if args.token_file.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                parser.error("token file must be owned and mode0600")
            client = WorkerClient(args.worker_url, args.token_file.read_text().strip())
        with Ledger(args.ledger) as ledger:
            result = submit(ledger, plan) if args.command == "submit" else collect(ledger, plan, client, action_directory=args.action_directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2)+"\n")
    print(json.dumps({"output": str(args.output), "compute_started": False}))


if __name__ == "__main__":
    main()
