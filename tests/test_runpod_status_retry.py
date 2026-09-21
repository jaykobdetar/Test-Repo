"""Exact-Pod reconciliation reads use fake HTTP; no cloud calls or real waits."""
from datetime import timedelta

import pytest

from probe_core.provider import WorkerState
from probe_core.runpod_provider import ProviderHTTPError, ProviderResponseError, ProviderUncertain
from test_runpod_provider import create, runpod


def observe(runpod, monkeypatch, failures, *, headers=None):
    backend, http, clock, _ = runpod
    started = create(runpod)
    deadline = backend._intent("worker1")["deadline"]
    original = http.request
    reads = []

    def request(method, path, body=None):
        if method == "GET" and path == "/v2/pods/" + started.provider_id:
            reads.append(clock().timestamp())
            if failures:
                failure = failures.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                raise ProviderHTTPError(failure, headers=headers)
        return original(method, path, body)

    monkeypatch.setattr(http, "request", request)
    arguments = {"provider_id": started.provider_id, "deadline": deadline}
    return backend, http, clock, reads, arguments


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_read_recovers_once_without_paid_mutation(runpod, monkeypatch, status):
    backend, http, clock, reads, args = observe(runpod, monkeypatch, [status], headers={"Retry-After": "2"})
    before = clock()
    result = backend.reconcile_status("worker1", **args)
    assert result.state == WorkerState.RUNNING and result.provider_id == args["provider_id"]
    assert reads == [before.timestamp(), before.timestamp() + 2]
    assert backend._intent("worker1")["deadline"] == args["deadline"]
    assert len(http.purchases) == 1 and not any(call[0] == "DELETE" for call in http.calls)


def test_second_failure_escapes_without_another_wait_or_cached_success(runpod, monkeypatch):
    backend, http, clock, reads, args = observe(runpod, monkeypatch, [503, 502, 503])
    before = clock()
    with pytest.raises(ProviderHTTPError) as error:
        backend.reconcile_status("worker1", **args)
    assert error.value.status == 502 and len(reads) == 2
    assert clock() == before + timedelta(seconds=2) and len(http.purchases) == 1


@pytest.mark.parametrize("error", [ProviderHTTPError(401), ProviderHTTPError(403),
    ProviderHTTPError(500), ProviderResponseError("invalid response"),
    ProviderUncertain("configuration mismatch"), TimeoutError("uncertain transport")])
def test_non_transient_errors_remain_one_shot(runpod, monkeypatch, error):
    backend, _, clock, reads, args = observe(runpod, monkeypatch, [error])
    before = clock()
    with pytest.raises(type(error)) as caught:
        backend.reconcile_status("worker1", **args)
    assert caught.value is error and len(reads) == 1 and clock() == before


def test_confirmed_absence_is_not_retried(runpod, monkeypatch):
    backend, _, clock, reads, args = observe(runpod, monkeypatch, [404])
    before = clock()
    assert backend.reconcile_status("worker1", **args).state == WorkerState.ABSENT
    assert len(reads) == 1 and clock() == before


@pytest.mark.parametrize("headers", [{"Retry-After": "3"}, {"Retry-After": "86401"},
    {"Retry-After": "Sun, 20 Sep 2026 16:00:00 GMT"}, {"Retry-After": "invalid"},
    {"cf-mitigated": "challenge"}])
def test_long_backoff_or_challenge_does_not_receive_an_early_retry(runpod, monkeypatch, headers):
    backend, _, clock, reads, args = observe(runpod, monkeypatch, [503], headers=headers)
    before = clock()
    with pytest.raises(ProviderHTTPError):
        backend.reconcile_status("worker1", **args)
    assert len(reads) == 1 and clock() == before


@pytest.mark.parametrize("remaining", [0, 1, 17])
def test_retry_never_uses_time_reserved_past_original_deadline(runpod, monkeypatch, remaining):
    backend, _, clock, reads, args = observe(runpod, monkeypatch, [503])
    clock.advance(300 - remaining)
    before = clock()
    with pytest.raises(ProviderUncertain if remaining == 0 else ProviderHTTPError):
        backend.reconcile_status("worker1", **args)
    assert len(reads) == (0 if remaining == 0 else 1) and clock() == before


def test_oversleep_does_not_retry_or_renew_allowance(runpod, monkeypatch):
    backend, _, clock, reads, args = observe(runpod, monkeypatch, [503])
    monkeypatch.setattr(backend, "sleep", lambda _: clock.advance(300))
    with pytest.raises(ProviderHTTPError):
        backend.reconcile_status("worker1", **args)
    assert len(reads) == 1 and backend._intent("worker1")["deadline"] == args["deadline"]


@pytest.mark.parametrize("change", ["provider_id", "deadline", "provider_seen"])
def test_durable_binding_must_remain_identical_across_wait(runpod, monkeypatch, change):
    backend, _, clock, reads, args = observe(runpod, monkeypatch, [503])

    def interrupted(_):
        clock.advance(2)
        value = {"provider_id": "different", "deadline": args["deadline"] + 1, "provider_seen": 0}[change]
        with backend._connect() as connection:
            connection.execute(f"UPDATE runpod_intents SET {change}=? WHERE worker_id=?", (value, "worker1"))

    monkeypatch.setattr(backend, "sleep", interrupted)
    with pytest.raises(ProviderHTTPError):
        backend.reconcile_status("worker1", **args)
    assert len(reads) == 1


def test_deadline_crossed_during_response_cannot_return_running(runpod, monkeypatch):
    backend, http, clock, reads, args = observe(runpod, monkeypatch, [503])
    original = http.request

    def slow_response(method, path, body=None):
        response = original(method, path, body)
        if len(reads) == 2:
            clock.advance(300)
        return response

    monkeypatch.setattr(http, "request", slow_response)
    with pytest.raises(ProviderUncertain, match="deadline elapsed during observation"):
        backend.reconcile_status("worker1", **args)
    assert len(reads) == 2


@pytest.mark.parametrize("changed", [{"provider_id": "other"}, {"deadline": 0}])
def test_unmatched_controller_binding_keeps_one_shot_behavior(runpod, monkeypatch, changed):
    backend, _, clock, reads, args = observe(runpod, monkeypatch, [503])
    args.update(changed)
    before = clock()
    with pytest.raises(ProviderHTTPError):
        backend.reconcile_status("worker1", **args)
    assert len(reads) == 1 and clock() == before


def test_ordinary_status_remains_one_shot_for_create_and_stop_callers(runpod, monkeypatch):
    backend, _, clock, reads, _ = observe(runpod, monkeypatch, [503])
    before = clock()
    with pytest.raises(ProviderHTTPError):
        backend.status("worker1")
    assert len(reads) == 1 and clock() == before
