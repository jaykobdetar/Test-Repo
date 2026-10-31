"""Real controller/provider decisions with local HTTP fixtures; no cloud calls."""

from copy import deepcopy
import json

import pytest

from probe_core.audit import canonical_json
from probe_core.ledger import Ledger
from probe_core.provider import StopOnlyBackend, WorkerState, WorkerStatus
from probe_core.runpod_provider import ProviderHTTPError, ProviderResponseError
from test_controller import harness, provision
from test_runpod_provider import runpod


def causes(ledger, request_id):
    return [
        record
        for record in ledger.audit_records()
        if record["payload"].get("request_id") == request_id and "reconciliation_cause" in record["payload"]
    ]


@pytest.fixture
def live(harness, runpod):
    backend, http, _, spec = runpod
    clock = harness["clock"]
    backend.clock = clock
    backend.sleep = clock.advance
    harness["controller"].backend = backend
    harness["watcher"].backend = StopOnlyBackend(backend)
    request = harness["controller"].request_provision(spec, [harness["job"].job_id], 300)
    started = harness["controller"].approve_and_start(request["request_id"])
    return dict(harness, backend=backend, http=http, started=started, provider_clock=clock)


def test_original_status_503_survives_delete_503_and_later_shutdown(live, monkeypatch):
    controller, ledger, http, started = (live[key] for key in ("controller", "ledger", "http", "started"))
    path = "/v2/pods/" + started["observed_provider_id"]
    original = http.request
    attempts = []
    headers = {
        "Content-Type": "application/json",
        "Retry-After": "2",
        "Authorization": "PRIVATE_HEADER",
        "Location": "https://PRIVATE_URL.invalid",
    }

    def unavailable(method, current_path, body=None):
        if current_path != path:
            return original(method, current_path, body)
        attempts.append(method)
        if method == "DELETE":
            # The original observation and state are already durably committed
            # when the destructive action begins, even if that action fails.
            record = causes(ledger, started["request_id"])[0]
            assert record["payload"]["reconciliation_cause"]["http"]["http_status"] == 503
            assert controller.status()[0]["state"] == "STOP_REQUESTED"
        error = ProviderHTTPError(503, headers=headers)
        error.args = ("PRIVATE_BODY PRIVATE_ERROR_MESSAGE",)
        raise error

    monkeypatch.setattr(http, "request", unavailable)
    controller.reconcile()
    current = controller.status()[0]
    assert attempts == ["GET", "GET", "DELETE"]  # One bounded read retry, then the unchanged stop path.
    assert current["state"] == "UNCERTAIN" and current["last_error_code"] == "ProviderUncertain"
    record = causes(ledger, started["request_id"])[0]
    assert record["payload"]["decision"] == "compute_stop_requested"
    assert record["payload"]["reconciliation_cause"] == {
        "reason": "provider_status_error",
        "request_state": "RUNNING",
        "exception_type": "ProviderHTTPError",
        "http": {"http_status": 503, "content_type": "json", "retry_after_seconds": 2, "cf_mitigated_challenge": False},
    }
    assert "PRIVATE" not in canonical_json(ledger.audit_records())
    assert json.loads(ledger.audit_path.read_text().splitlines()[record["sequence"] - 1]) == record

    monkeypatch.setattr(http, "request", original)
    controller.reconcile()
    assert controller.status()[0]["state"] == "STOPPED"
    assert controller.status()[0]["deadline"] == started["deadline"]
    assert causes(ledger, started["request_id"])[0] == record
    assert http.pods == [] and len(http.purchases) == 1
    assert all(job.attempt_id is None for job in ledger.list_jobs())
    ledger.sync_audit()
    from probe_core.audit import AuditLog

    assert AuditLog(ledger.audit_path).verify() == ledger.audit_records()


def test_one_transient_read_recovers_without_revoking_the_original_allowance(live, monkeypatch):
    controller, ledger, http, started = (live[key] for key in ("controller", "ledger", "http", "started"))
    original = http.request
    path = "/v2/pods/" + started["observed_provider_id"]
    reads = []
    before = live["clock"]()

    def transient(method, current_path, body=None):
        if method == "GET" and current_path == path:
            reads.append(live["clock"]().timestamp())
            if len(reads) == 1:
                raise ProviderHTTPError(503, headers={"Retry-After": "2"})
        return original(method, current_path, body)

    monkeypatch.setattr(http, "request", transient)
    controller.reconcile()
    current = controller.status()[0]
    assert current["state"] == "RUNNING"
    assert (current["approval_id"], current["deadline"], current["observed_provider_id"]) == (
        started["approval_id"],
        started["deadline"],
        started["observed_provider_id"],
    )
    assert reads == [before.timestamp(), before.timestamp() + 2]
    assert causes(ledger, started["request_id"]) == []
    assert live["watcher"].tick() == []
    assert len(http.purchases) == 1 and not any(call[0] == "DELETE" for call in http.calls)
    assert all(job.attempt_id is None for job in ledger.list_jobs())
    assert len([entry for entry in ledger.audit_records() if entry["payload"].get("decision") == "human_approved"]) == 1
    # The normal deadline still terminates the same resource without renewal.
    live["clock"].advance(current["deadline"] - live["clock"]().timestamp())
    assert live["watcher"].tick()[0]["reason"] == "absolute_deadline"
    controller.reconcile()
    assert controller.status()[0]["state"] == "STOPPED" and http.pods == []


@pytest.mark.parametrize("state", ["STARTING", "STOP_REQUESTED", "UNCERTAIN"])
def test_non_running_request_keeps_its_existing_stop_policy_and_cause(harness, state):
    request, started = provision(harness)
    controller, ledger = harness["controller"], harness["ledger"]
    controller._state(request["request_id"], state)
    controller.reconcile()
    cause = causes(ledger, request["request_id"])[0]["payload"]["reconciliation_cause"]
    assert cause == {
        "reason": "request_not_running",
        "request_state": state,
        "provider_state": "RUNNING",
        "provider_identity_changed": False,
    }
    assert controller.status()[0]["state"] == "STOPPED"
    assert controller.status()[0]["deadline"] == started["deadline"]


@pytest.mark.parametrize("reason", ["deadline_missing", "approval_deadline"])
def test_deadline_stop_branches_record_fixed_cause(harness, reason):
    request, _ = provision(harness)
    if reason == "deadline_missing":
        harness["ledger"]._submit(
            lambda conn, now: conn.execute(
                "UPDATE compute_requests SET deadline=NULL WHERE request_id=?", (request["request_id"],)
            )
        )
    else:
        harness["clock"].advance(900)
    harness["controller"].reconcile()
    cause = causes(harness["ledger"], request["request_id"])[0]["payload"]["reconciliation_cause"]
    assert cause == {
        "reason": reason,
        "request_state": "RUNNING",
        "provider_state": "RUNNING",
        "provider_identity_changed": False,
    }
    assert harness["controller"].status()[0]["state"] == "STOPPED"


@pytest.mark.parametrize("state", [WorkerState.STARTING, WorkerState.UNKNOWN, WorkerState.STOPPED, WorkerState.ABSENT])
def test_provider_state_stop_branches_preserve_bounded_observation(harness, monkeypatch, state):
    request, started = provision(harness)
    backend = harness["backend"]
    original = backend.status
    seen = []

    def observe(worker_id):
        seen.append(worker_id)
        if len(seen) == 1:
            return WorkerStatus(worker_id, state, started["observed_provider_id"])
        return original(worker_id)

    monkeypatch.setattr(backend, "status", observe)
    harness["controller"].reconcile()
    assert len(seen) == 2
    cause = causes(harness["ledger"], request["request_id"])[0]["payload"]["reconciliation_cause"]
    assert cause == {
        "reason": "provider_not_running",
        "request_state": "RUNNING",
        "provider_state": state.value,
        "provider_identity_changed": False,
    }


@pytest.mark.parametrize("failure", ["auth", "malformed", "identity", "custom_type"])
def test_refused_or_malformed_status_retains_only_safe_diagnostics(live, monkeypatch, failure):
    http, ledger, controller, started = (live[key] for key in ("http", "ledger", "controller", "started"))
    original = http.request
    reads = 0

    def observe(method, path, body=None):
        nonlocal reads
        if method == "GET" and path == "/v2/pods/" + started["observed_provider_id"]:
            reads += 1
            if reads == 1:
                if failure == "auth":
                    raise ProviderHTTPError(
                        403,
                        headers={
                            "Content-Type": "text/html",
                            "cf-mitigated": "challenge",
                            "Authorization": "PRIVATE_HEADER",
                        },
                    )
                if failure == "malformed":
                    raise ProviderResponseError("PRIVATE_JSON_BODY")
                if failure == "custom_type":
                    raise type("PRIVATE_EXCEPTION_TYPE", (Exception,), {})("PRIVATE_MESSAGE")
                pod = deepcopy(original(method, path, body))
                pod["env"]["PROBE_REQUEST_ID"] = "PRIVATE_PROVIDER_IDENTITY"
                return pod
        return original(method, path, body)

    monkeypatch.setattr(http, "request", observe)
    controller.reconcile()
    cause = causes(ledger, started["request_id"])[0]["payload"]["reconciliation_cause"]
    assert cause["reason"] == "provider_status_error"
    assert (
        cause["exception_type"]
        == {
            "auth": "ProviderHTTPError",
            "malformed": "ProviderResponseError",
            "identity": "ProviderUncertain",
            "custom_type": "Exception",
        }[failure]
    )
    if failure == "auth":
        assert cause["http"] == {
            "http_status": 403,
            "content_type": "html",
            "retry_after_seconds": None,
            "cf_mitigated_challenge": True,
        }
    else:
        assert "http" not in cause
    assert "PRIVATE" not in canonical_json(ledger.audit_records())
    assert controller.status()[0]["state"] == "STOPPED"
    assert len(http.purchases) == 1 and http.pods == []


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {"http_status": True},
        {
            "http_status": 503,
            "content_type": "PRIVATE_VALUE",
            "retry_after_seconds": 2,
            "cf_mitigated_challenge": False,
        },
        {
            "http_status": 503,
            "content_type": "json",
            "retry_after_seconds": float("nan"),
            "cf_mitigated_challenge": False,
        },
    ],
)
def test_invalid_http_metadata_is_omitted_without_blocking_shutdown(harness, monkeypatch, metadata):
    request, _ = provision(harness)
    backend = harness["backend"]
    original = backend.status
    calls = 0

    def broken(worker_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            error = ProviderHTTPError(503)
            error.metadata = metadata
            raise error
        return original(worker_id)

    monkeypatch.setattr(backend, "status", broken)
    harness["controller"].reconcile()
    cause = causes(harness["ledger"], request["request_id"])[0]["payload"]["reconciliation_cause"]
    assert cause == {
        "reason": "provider_status_error",
        "request_state": "RUNNING",
        "exception_type": "ProviderHTTPError",
    }
    assert harness["controller"].status()[0]["state"] == "STOPPED"


def test_audit_failure_rolls_back_stop_transition_before_provider_action(harness, monkeypatch):
    request, _ = provision(harness)
    original = Ledger._event
    stops = []

    def fail_event(conn, now, event_type, payload):
        if payload.get("reconciliation_cause") is not None:
            raise RuntimeError("synthetic audit write failure")
        return original(conn, now, event_type, payload)

    harness["clock"].advance(900)
    monkeypatch.setattr(Ledger, "_event", staticmethod(fail_event))
    monkeypatch.setattr(harness["backend"], "stop", stops.append)
    with pytest.raises(RuntimeError, match="synthetic audit write failure"):
        harness["controller"].reconcile()
    assert harness["controller"].status()[0]["state"] == "RUNNING"
    assert stops == [] and causes(harness["ledger"], request["request_id"]) == []


def test_normal_running_and_explicit_stop_do_not_gain_reconciliation_causes(harness):
    request, _ = provision(harness)
    before = harness["ledger"].audit_records()
    harness["controller"].reconcile()
    assert harness["ledger"].audit_records() == before
    harness["controller"].stop_gpu(request["worker_id"])
    assert causes(harness["ledger"], request["request_id"]) == []
    assert harness["controller"].status()[0]["state"] == "STOPPED"


def test_non_running_request_reason_precedes_expired_deadline_and_provider_state(harness, monkeypatch):
    request, _ = provision(harness)
    harness["controller"]._state(request["request_id"], "UNCERTAIN")
    harness["clock"].advance(900)
    original = harness["backend"].status
    seen = []

    def observe(worker_id):
        seen.append(worker_id)
        return (
            WorkerStatus(worker_id, WorkerState.UNKNOWN, "different-known-id")
            if len(seen) == 1
            else original(worker_id)
        )

    monkeypatch.setattr(harness["backend"], "status", observe)
    harness["controller"].reconcile()
    cause = causes(harness["ledger"], request["request_id"])[0]["payload"]["reconciliation_cause"]
    assert cause == {
        "reason": "request_not_running",
        "request_state": "UNCERTAIN",
        "provider_state": "UNKNOWN",
        "provider_identity_changed": True,
    }


def test_http_metadata_extra_fields_never_enter_audit(harness, monkeypatch):
    request, _ = provision(harness)
    original = harness["backend"].status
    first = True

    def observe(worker_id):
        nonlocal first
        if first:
            first = False
            error = ProviderHTTPError(503)
            error.metadata.update(body="PRIVATE_BODY", authorization="PRIVATE_AUTHORIZATION")
            raise error
        return original(worker_id)

    monkeypatch.setattr(harness["backend"], "status", observe)
    harness["controller"].reconcile()
    cause = causes(harness["ledger"], request["request_id"])[0]["payload"]["reconciliation_cause"]
    assert set(cause["http"]) == {"http_status", "content_type", "retry_after_seconds", "cf_mitigated_challenge"}
    assert "PRIVATE" not in canonical_json(harness["ledger"].audit_records())


def test_optional_reconciliation_read_receives_exact_bound_identity_and_deadline(harness, monkeypatch):
    request, started = provision(harness)
    backend = harness["backend"]
    observed = backend.status(request["worker_id"])
    calls = []

    def reconcile_read(worker_id, **kwargs):
        calls.append((worker_id, kwargs))
        return observed

    def ordinary_status(_):
        raise AssertionError("eligible reconciliation must select its dedicated read")

    monkeypatch.setattr(backend, "reconcile_status", reconcile_read, raising=False)
    monkeypatch.setattr(backend, "status", ordinary_status)
    harness["controller"].reconcile()
    assert calls == [
        (request["worker_id"], {"provider_id": started["observed_provider_id"], "deadline": started["deadline"]})
    ]
    assert causes(harness["ledger"], request["request_id"]) == []


@pytest.mark.parametrize(
    "change", ["STARTING", "UNCERTAIN", "STOP_REQUESTED", "missing_id", "missing_deadline", "expired", "nan", "boolean"]
)
def test_reconciliation_read_hook_is_not_used_outside_bound_live_request(harness, monkeypatch, change):
    request, _ = provision(harness)
    if change in {"STARTING", "UNCERTAIN", "STOP_REQUESTED"}:
        harness["controller"]._state(request["request_id"], change)
    elif change == "missing_id":
        harness["ledger"]._submit(
            lambda conn, now: conn.execute(
                "UPDATE compute_requests SET observed_provider_id=NULL WHERE request_id=?", (request["request_id"],)
            )
        )
    elif change == "missing_deadline":
        harness["ledger"]._submit(
            lambda conn, now: conn.execute(
                "UPDATE compute_requests SET deadline=NULL WHERE request_id=?", (request["request_id"],)
            )
        )
    elif change == "expired":
        harness["clock"].advance(900)
    else:
        # SQLite normalizes NaN/booleans, so inject only the public snapshot for
        # this malformed-input branch without changing persisted authority.
        original_status = harness["controller"].status

        def snapshot():
            result = original_status()
            for row in result:
                row["deadline"] = float("nan") if change == "nan" else True
            return result

        monkeypatch.setattr(harness["controller"], "status", snapshot)
    calls = []
    monkeypatch.setattr(
        harness["backend"], "reconcile_status", lambda *args, **kwargs: calls.append(args), raising=False
    )
    harness["controller"].reconcile()
    assert calls == []


def test_failed_reconciliation_hook_records_final_error_and_stop_readback_stays_one_shot(harness, monkeypatch):
    request, _ = provision(harness)
    backend = harness["backend"]
    original_status, original_stop = backend.status, backend.stop
    calls = []

    def read(worker_id, **kwargs):
        calls.append("reconcile_read")
        raise ProviderHTTPError(503, headers={"Retry-After": "2"})

    def stop(worker_id):
        calls.append("stop")
        return original_stop(worker_id)

    def status(worker_id):
        calls.append("ordinary_status")
        return original_status(worker_id)

    monkeypatch.setattr(backend, "reconcile_status", read, raising=False)
    monkeypatch.setattr(backend, "stop", stop)
    monkeypatch.setattr(backend, "status", status)
    harness["controller"].reconcile()
    assert calls == ["reconcile_read", "stop", "ordinary_status"]
    cause = causes(harness["ledger"], request["request_id"])[0]["payload"]["reconciliation_cause"]
    assert cause["http"]["http_status"] == 503
    assert harness["controller"].status()[0]["state"] == "STOPPED"
