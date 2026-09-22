"""Simulator-only tests for human-approved budget envelopes."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from pydantic import ValidationError

from probe_core import budget
from probe_core.controller import BudgetError
from probe_core.research_api import ResearchPolicy, ResearchService
from probe_core.schemas import BudgetEnvelope, JobSpec
from test_controller import calls, disposable_deployment, harness  # noqa: F401

FIXTURE_REVISION = "a" * 40


def envelope_fields(**overrides):
    fields = dict(
        envelope_id="m1-exploratory-001",
        max_gpu_usd=3.0,
        max_llm_usd=0.0,
        max_gpu_usd_per_hour=0.80,
        max_wall_seconds_per_pod=900,
        allowed_models=[{"repo": "Qwen/Qwen3-1.7B-Base", "revision_sha": FIXTURE_REVISION}],
        allowed_stages=["exploratory"],
        lifetime_hours=24,
    )
    fields.update(overrides)
    return fields


def issue(harness, **overrides):
    return harness["controller"].admin_dispatch(
        "issue_envelope", envelope_fields(**overrides), admin_identity="uid:1234"
    )


def pending(harness, runtime=300, job_id=None):
    return harness["controller"].request_provision(disposable_deployment(), [job_id or harness["job"].job_id], runtime)


def second_job(harness, key="budget-job-2", **model):
    spec = harness["job"].spec.model_dump(mode="json")
    spec["idempotency_key"] = key
    spec["model"].update(model)
    return harness["ledger"].submit_job(JobSpec.model_validate(spec))


def decisions(harness):
    return [record["payload"].get("decision") for record in harness["ledger"].audit_records()]


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_llm_usd": 0.01},
        {"max_gpu_usd_per_hour": 1.50},
        {"max_wall_seconds_per_pod": 901},
        {"max_gpu_usd": 0.0},
        {"max_gpu_usd": 25.0},
        {"allowed_stages": ["confirmatory"]},
        {"allowed_stages": ["calibration"]},
        {"allowed_models": []},
        {"allowed_models": [{"repo": "probe/testing-tiny-qwen3", "revision_sha": FIXTURE_REVISION}]},
        {"allowed_models": [{"repo": "Qwen/Qwen3-1.7B", "revision_sha": FIXTURE_REVISION}] * 2},
        {"expires_in": timedelta(days=8)},
        {"expires_in": timedelta(seconds=0)},
        {"approved_by": "operator"},
    ],
)
def test_envelope_schema_rejects_out_of_policy_values(overrides):
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    fields = envelope_fields()
    del fields["lifetime_hours"]
    fields.update(issued_at=now, expires_at=now + overrides.pop("expires_in", timedelta(hours=24)))
    fields["approved_by"] = "uid:1000"
    fields.update(overrides)
    with pytest.raises(ValidationError):
        BudgetEnvelope.model_validate(fields)


def test_only_the_admin_socket_issues_envelopes_and_identity_is_not_client_supplied(harness):
    controller = harness["controller"]
    with pytest.raises(PermissionError):
        controller.research_dispatch("issue_envelope", envelope_fields())
    with pytest.raises(PermissionError, match="set by the controller"):
        controller.admin_dispatch("issue_envelope", {**envelope_fields(), "approved_by": "uid:0"})
    summary = issue(harness)
    active = summary["active_envelope"]
    assert active["envelope_id"] == "m1-exploratory-001" and active["gpu_remaining_usd"] == 3.0
    issued = [
        r["payload"]
        for r in harness["ledger"].audit_records()
        if r["payload"].get("decision") == "budget_envelope_issued"
    ]
    assert issued[0]["approved_by"] == "uid:1234"
    with pytest.raises(BudgetError, match="another envelope is still open"):
        issue(harness, envelope_id="m1-exploratory-002")


def test_start_without_an_open_envelope_is_refused_before_any_provider_action(harness):
    request = pending(harness)
    with pytest.raises(BudgetError, match="no open budget envelope"):
        harness["controller"].start_within_envelope(request["request_id"])
    assert calls(harness, "create") == []


def test_reservation_settles_at_measured_time_after_confirmed_deletion(harness):
    controller, clock = harness["controller"], harness["clock"]
    issue(harness)
    request = pending(harness, runtime=300)
    started = controller.start_within_envelope(request["request_id"])
    assert started["state"] == "RUNNING" and len(calls(harness, "create")) == 1
    worst = round((300 + budget.DELETION_RESERVE_SECONDS) / 3600 * 0.80, 6)
    active = controller.budget_status()["active_envelope"]
    assert active["gpu_reserved_usd"] == worst and active["gpu_spent_usd"] == 0
    approved = [
        r["payload"] for r in harness["ledger"].audit_records() if r["payload"].get("decision") == "envelope_approved"
    ]
    assert "human_approved" not in decisions(harness)
    assert approved[0]["price_ceiling_usd_per_hour"] == 0.80 and approved[0]["envelope_id"] == "m1-exploratory-001"
    clock.advance(400)
    controller.stop_gpu(request["worker_id"])
    active = controller.budget_status()["active_envelope"]
    spent = round(400 / 3600 * 0.50, 6)  # simulator live price
    assert active["gpu_reserved_usd"] == 0 and active["gpu_spent_usd"] == spent
    assert active["gpu_remaining_usd"] == round(3.0 - spent, 6)


def test_refused_when_remaining_budget_cannot_cover_worst_case(harness):
    controller = harness["controller"]
    worst = round((300 + budget.DELETION_RESERVE_SECONDS) / 3600 * 0.80, 6)
    issue(harness, max_gpu_usd=round(worst * 1.5, 6))
    first = pending(harness)
    controller.start_within_envelope(first["request_id"])
    other = second_job(harness)
    second = pending(harness, job_id=other.job_id)
    with pytest.raises(BudgetError, match="cannot cover this Pod's worst-case cost"):
        controller.start_within_envelope(second["request_id"])
    assert len(calls(harness, "create")) == 1
    assert [r for r in controller.status() if r["request_id"] == second["request_id"]][0]["state"] == "PENDING"
    assert "budget_reservation_refused" in decisions(harness)
    # Settling the first Pod frees its unused worst case for the second.
    controller.stop_gpu(first["worker_id"])
    assert controller.start_within_envelope(second["request_id"])["state"] == "RUNNING"


@pytest.mark.parametrize(
    "scope,reason",
    [
        ("model", "model is not allowed"),
        ("revision", "model is not allowed"),
        ("runtime", "per-Pod ceiling"),
        ("infrastructure", "disposable research Pods"),
    ],
)
def test_requests_outside_the_envelope_scope_are_refused(harness, scope, reason):
    controller = harness["controller"]
    issue(harness, max_wall_seconds_per_pod=600)
    if scope == "model":
        request = pending(harness, job_id=second_job(harness, repo="Qwen/Qwen3-1.7B", thinking_mode=False).job_id)
    elif scope == "revision":
        request = pending(harness, job_id=second_job(harness, revision_sha="c" * 40).job_id)
    elif scope == "runtime":
        request = pending(harness, runtime=601)
    else:
        request = controller.request_infrastructure_preflight(
            disposable_deployment().model_copy(update={"storage_mode": "ephemeral_preflight"}),
            "sha256:" + "c" * 64,
            300,
        )
    with pytest.raises(BudgetError, match=reason):
        controller.start_within_envelope(request["request_id"])
    assert calls(harness, "create") == []


def test_live_price_above_envelope_ceiling_is_refused(harness):
    controller = harness["controller"]
    issue(harness, max_gpu_usd_per_hour=0.40)  # simulator quotes 0.50
    request = pending(harness)
    with pytest.raises(BudgetError, match="price"):
        controller.start_within_envelope(request["request_id"])
    assert calls(harness, "create") == []


def test_closed_and_expired_envelopes_grant_nothing(harness):
    controller, clock = harness["controller"], harness["clock"]
    issue(harness, lifetime_hours=1)
    request = pending(harness)
    controller.admin_dispatch("close_envelope", {"envelope_id": "m1-exploratory-001"})
    with pytest.raises(BudgetError, match="no open budget envelope"):
        controller.start_within_envelope(request["request_id"])
    issue(harness, envelope_id="m1-exploratory-002", lifetime_hours=1)
    clock.advance(3600)
    with pytest.raises(BudgetError, match="no open budget envelope"):
        controller.start_within_envelope(request["request_id"])
    assert calls(harness, "create") == []
    with pytest.raises(BudgetError, match="already closed"):
        controller.close_envelope("m1-exploratory-001")


def test_uncertain_shutdown_keeps_the_full_reservation(harness, monkeypatch):
    controller, backend = harness["controller"], harness["backend"]
    issue(harness)
    request = pending(harness)
    controller.start_within_envelope(request["request_id"])
    held = controller.budget_status()["active_envelope"]["gpu_reserved_usd"]

    def refuse(worker_id):
        raise TimeoutError("provider did not answer")

    monkeypatch.setattr(backend, "stop", refuse)
    assert controller.stop_gpu(request["worker_id"])[0]["state"] == "UNCERTAIN"
    active = controller.budget_status()["active_envelope"]
    assert active["gpu_reserved_usd"] == held and active["gpu_spent_usd"] == 0


def test_failure_before_provider_action_settles_at_zero(harness, monkeypatch):
    controller = harness["controller"]
    issue(harness)
    request = pending(harness)

    def unavailable(*args, **kwargs):
        raise RuntimeError("watchdog did not acknowledge")

    monkeypatch.setattr(controller, "_await_watchdog_ack", unavailable)
    with pytest.raises(RuntimeError):
        controller.start_within_envelope(request["request_id"])
    active = controller.budget_status()["active_envelope"]
    assert active["gpu_reserved_usd"] == 0 and active["gpu_spent_usd"] == 0 and calls(harness, "create") == []


def test_budget_rows_are_append_only(harness):
    issue(harness)
    request = pending(harness)
    harness["controller"].start_within_envelope(request["request_id"])
    connection = sqlite3.connect(harness["ledger"].path)
    try:
        for statement in (
            "UPDATE budget_reservations SET reserved_usd=0.01",
            "DELETE FROM budget_reservations",
            "DELETE FROM budget_envelopes",
            "UPDATE budget_envelopes SET expires_at=expires_at+86400",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)
    finally:
        connection.close()


def test_research_service_starts_within_envelope_and_reports_spend(harness):
    controller, ledger = harness["controller"], harness["ledger"]
    service = ResearchService(
        ledger,
        ResearchPolicy(discovery_datasets=(harness["job"].spec.inputs.dataset_revision,)),
        cloud=controller,
    )
    status = service.dispatch("lab_status", {})
    assert status["gpu_start_authority"] is False and status["budget"]["active_envelope"] is None
    issue(harness)
    assert service.dispatch("lab_status", {})["gpu_start_authority"] is True
    request = service.dispatch(
        "request_gpu_provision",
        {
            "deployment": disposable_deployment().model_dump(mode="json"),
            "job_ids": [harness["job"].job_id],
            "max_runtime_seconds": 300,
        },
    )
    started = service.dispatch("start_gpu_within_envelope", {"request_id": request["request_id"]})
    assert started["state"] == "RUNNING"
    assert service.dispatch("lab_status", {})["budget"]["active_envelope"]["gpu_reserved_usd"] > 0
    with pytest.raises(PermissionError):
        service.dispatch("start_gpu_within_envelope", {"request_id": "request-unknown"})
    for method in ("issue_envelope", "close_envelope"):
        with pytest.raises(PermissionError):
            service.dispatch(method, {})
