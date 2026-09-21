"""Execution-start evidence must follow release and a verified child identity."""
import json
import multiprocessing
from pathlib import Path

from probe_core.worker import _child_entry
from probe_core.worker_contracts import ExecutionReceipt
from test_worker import tiny_bundle, make_request


def entry_without_identity(config_json, request_json, directory, ready, release):
    # Fault injection is restricted to this disposable spawned child. The real
    # entry path still applies its limits and network denial before this check.
    import probe_core.worker as worker

    def missing_identity(pid):
        (Path(directory) / "identity-check.json").write_text(json.dumps({"pid": pid}))
        return None

    worker._process_identity = missing_identity
    worker._child_entry(config_json, request_json, directory, ready, release)


def finish_child(process):
    if process.is_alive():
        process.kill()
    process.join(timeout=5)
    assert not process.is_alive(), "execution-start test child did not stop"
    process.close()


def test_pre_release_child_has_no_execution_start_marker(tiny_bundle, make_request, tmp_path):
    request = make_request(key="waiting-for-release")
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_child_entry, args=(
        tiny_bundle[0].model_dump_json(), request.model_dump_json(), str(tmp_path), ready, release,
    ))
    process.start()
    try:
        assert ready.wait(timeout=10), "child did not reach its pre-release wait"
        assert process.is_alive() and not release.is_set()
        assert not (tmp_path / "execution-started.json").exists()
        assert not (tmp_path / "execution-started.json.partial").exists()
        assert not (tmp_path / "artifacts").exists()
    finally:
        finish_child(process)


def test_missing_child_identity_fails_before_execution_start_marker(tiny_bundle, make_request, tmp_path):
    request = make_request(key="missing-process-identity")
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=entry_without_identity, args=(
        tiny_bundle[0].model_dump_json(), request.model_dump_json(), str(tmp_path), ready, release,
    ))
    process.start()
    try:
        assert ready.wait(timeout=10)
        assert not (tmp_path / "execution-started.json").exists()
        release.set()
        process.join(timeout=10)
        assert not process.is_alive() and process.exitcode == 0
        assert json.loads((tmp_path / "identity-check.json").read_text()) == {"pid": process.pid}
        receipt = ExecutionReceipt.model_validate_json((tmp_path / "result.json").read_text())
        assert receipt.job_id == request.job_id and receipt.attempt_id == request.attempt_id
        assert receipt.state.value == "FAILED" and receipt.failure_kind == "policy"
        assert receipt.error_code == "WorkerRequestError"
        assert not (tmp_path / "execution-started.json").exists()
        assert not (tmp_path / "execution-started.json.partial").exists()
        assert not (tmp_path / "artifacts").exists()
    finally:
        finish_child(process)
