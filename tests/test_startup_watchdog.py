"""Cold startup is bounded by immutable approved work, never a renewed timer."""
from contextlib import closing
import json
import sqlite3

import pytest

from probe_core.compute_timing import COLLECTION_DELETION_RESERVE_SECONDS, startup_dispatch_cutoff
from probe_core.controller import StopWatchdog, _calibration_startup_deadline
from probe_core.provider import StopOnlyBackend, WorkerState
from probe_core.schemas import JobSpec
from test_controller import harness, disposable_deployment


def calibration(h, *, runtime=900, job_runtime=240, count=1):
    h['ledger'].cancel_job(h['job'].job_id)
    specs = []
    for number in range(count):
        body = h['job'].spec.model_dump(mode='json')
        body.update(experiment_stage='calibration', idempotency_key='startup-job-' + str(number))
        body['limits']['max_runtime_seconds'] = job_runtime
        specs.append(h['ledger'].submit_job(JobSpec.model_validate(body)))
    request = h['controller'].request_provision(disposable_deployment(), [job.job_id for job in specs], runtime)
    started = h['controller'].approve_and_start(request['request_id'])
    return request, started, specs


def stored(h, request):
    with closing(sqlite3.connect(h['watcher'].state_path)) as connection:
        row = connection.execute('SELECT snapshot,idle_since,last_stop_reason FROM watch_schedule WHERE request_id=?',
                                 (request['request_id'],)).fetchone()
    return json.loads(row[0]), row[1], row[2]


def test_pull_longer_than_five_minutes_is_allowed_only_until_shared_cutoff(harness):
    h = harness
    request, started, _ = calibration(h)
    original = h['clock']().timestamp()
    assert h['watcher'].tick() == []
    h['clock'].advance(301)
    assert h['watcher'].tick() == []
    snapshot, idle, reason = stored(h, request)
    assert idle == original and reason is None
    assert snapshot['startup_deadline'] == startup_dispatch_cutoff(started['deadline'], 240) == original + 540
    assert snapshot['absolute_deadline'] == original + 900
    h['clock'].advance(238)
    assert h['watcher'].tick() == []
    h['clock'].advance(1)
    assert h['watcher'].tick() == [{'worker_id': request['worker_id'], 'reason': 'startup_deadline', 'confirmed_off': True}]
    assert h['backend'].status(request['worker_id']).state == WorkerState.STOPPED
    assert h['controller'].status()[-1]['deadline'] == original + 900


def test_crashed_runner_and_restarted_watchdog_cannot_renew_startup(harness):
    h = harness
    request, started, _ = calibration(h)
    h['clock'].advance(350)
    restarted = StopWatchdog(h['ledger'].path, StopOnlyBackend(h['backend']),
        state_path=h['watcher'].state_path, health_path=h['health'], clock=h['clock'])
    assert restarted.tick() == []
    assert stored(h, request)[0]['startup_deadline'] == started['deadline'] - 360
    # No worker request/readiness or runner progress is needed to enforce this.
    h['clock'].advance(191)
    assert restarted.tick()[0]['reason'] == 'startup_deadline'


def test_first_tick_after_startup_cutoff_stops_without_fresh_grace(harness):
    h = harness
    request, _, _ = calibration(h)
    # Use a fresh local watchdog state after an independently supervised restart.
    h['clock'].advance(600)
    restarted = StopWatchdog(h['ledger'].path, StopOnlyBackend(h['backend']),
        state_path=h['tmp_path']/'new-watch-state.sqlite', health_path=h['health'], clock=h['clock'])
    assert restarted.tick() == [{'worker_id': request['worker_id'], 'reason': 'startup_deadline', 'confirmed_off': True}]


@pytest.mark.parametrize('dispatched', [False, True])
def test_absolute_approval_deadline_keeps_priority(harness, dispatched):
    h = harness
    request, started, _ = calibration(h)
    if dispatched:
        h['ledger'].dispatch_next(request['worker_id'], approval_id=started['approval_id'])
    h['clock'].advance(900)
    assert h['watcher'].tick()[0]['reason'] == 'absolute_deadline'


def test_attempt_entirely_between_ticks_permanently_ends_startup(harness):
    h = harness
    request, started, _ = calibration(h)
    h['watcher'].tick()
    h['clock'].advance(100)
    active = h['ledger'].dispatch_next(request['worker_id'], approval_id=started['approval_id'])
    h['ledger'].start_job(active.job_id, active.attempt_id, request['worker_id'])
    h['clock'].advance(1)
    h['ledger'].cancel_job(active.job_id)
    h['ledger'].confirm_stopped(active.job_id, active.attempt_id)
    assert h['watcher'].tick() == []
    snapshot, idle, _ = stored(h, request)
    assert snapshot['startup_deadline'] is None and idle == h['clock']().timestamp()
    h['clock'].advance(299)
    assert h['watcher'].tick() == []
    h['clock'].advance(1)
    assert h['watcher'].tick()[0]['reason'] == 'five_minute_idle'  # At401s, before original startup cutoff540.


def test_cancelled_undispatched_job_has_no_startup_exception(harness):
    h = harness
    request, _, jobs = calibration(h)
    h['ledger'].cancel_job(jobs[0].job_id)
    h['clock'].advance(301)
    assert h['watcher'].tick()[0]['reason'] == 'five_minute_idle'
    assert stored(h, request)[0]['startup_deadline'] is None


@pytest.mark.parametrize('invalid_stop', ['future', 'infinite'])
def test_invalid_stop_timestamp_cannot_create_startup_or_future_idle(harness, invalid_stop):
    h = harness
    request, started, _ = calibration(h)
    original = h['clock']().timestamp()
    active = h['ledger'].dispatch_next(request['worker_id'], approval_id=started['approval_id'])
    h['ledger'].cancel_job(active.job_id)
    h['ledger'].confirm_stopped(active.job_id, active.attempt_id)
    value = original + 10_000 if invalid_stop == 'future' else float('inf')
    h['ledger']._submit(lambda c, _: c.execute('UPDATE attempts SET stopped_at=? WHERE attempt_id=?',
                                              (value, active.attempt_id)))
    h['clock'].advance(301)
    assert h['watcher'].tick()[0]['reason'] == 'five_minute_idle'
    snapshot, idle, _ = stored(h, request)
    assert snapshot['startup_deadline'] is None and snapshot['latest_stopped_at'] is None
    assert idle == original


def test_multiple_approved_jobs_keep_original_five_minute_policy(harness):
    h = harness
    calibration(h, count=2)
    h['clock'].advance(300)
    assert h['watcher'].tick()[0]['reason'] == 'five_minute_idle'


def test_five_minute_infrastructure_probe_keeps_absolute_deadline(harness):
    h = harness
    spec = disposable_deployment().model_copy(update={'storage_mode': 'ephemeral_preflight'})
    request = h['controller'].request_infrastructure_preflight(spec, script_sha256='sha256:'+'c'*64,
                                                               max_runtime_seconds=300)
    h['controller'].approve_and_start(request['request_id'])
    h['clock'].advance(299)
    assert h['watcher'].tick() == []
    h['clock'].advance(1)
    assert h['watcher'].tick()[0]['reason'] == 'absolute_deadline'


@pytest.mark.parametrize('reason', ['uncertain_action', 'five_minute_idle'])
def test_durable_stop_latch_cannot_be_cleared_by_new_startup_rule(harness, reason):
    h = harness
    request, _, _ = calibration(h)
    with closing(sqlite3.connect(h['watcher'].state_path)) as connection:
        connection.execute('UPDATE watch_schedule SET last_stop_reason=? WHERE request_id=?', (reason, request['request_id']))
        connection.commit()
    h['clock'].advance(50)
    assert h['watcher'].tick()[0]['reason'] == reason


@pytest.mark.parametrize('state', ['UNCERTAIN', 'STOP_REQUESTED'])
def test_uncertain_state_wins_over_startup(harness, state):
    h = harness
    request, _, _ = calibration(h)
    h['controller']._state(request['request_id'], state)
    assert h['watcher'].tick()[0]['reason'] == 'uncertain_action'


def test_ledger_outage_stops_cached_startup_immediately(harness, monkeypatch):
    h = harness
    request, _, _ = calibration(h)
    original = sqlite3.connect
    def connect(path, *args, **kwargs):
        if str(path).startswith(h['ledger'].path.as_uri()):
            raise sqlite3.OperationalError('fixture read outage')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    assert h['watcher'].tick() == [{'worker_id': request['worker_id'], 'reason': 'ledger_unavailable', 'confirmed_off': True}]


def query_request(h, request):
    with h['ledger'].read_connection() as connection:
        row = connection.execute('SELECT r.*, a.consumed_at,a.deadline AS absolute_deadline,a.document AS approval_document '
            'FROM compute_requests r JOIN approvals a ON a.approval_id=r.approval_id WHERE request_id=?',
            (request['request_id'],)).fetchone()
    return dict(row)


@pytest.mark.parametrize('field,value', [('state', 'PREPARING'), ('state', 'UNKNOWN'), ('action', 'START'),
    ('configuration_hash', 'sha256:'+'0'*64), ('job_ids', '[]'), ('job_ids', '["unknown"]'),
    ('job_ids', '{}'), ('approval_document', '[]'), ('approval_document', 'invalid'),
    ('max_runtime_seconds', 901), ('max_runtime_seconds', True), ('consumed_at', float('nan')),
    ('consumed_at', float('inf')), ('absolute_deadline', float('nan')), ('absolute_deadline', 1e12),
    ('deadline', 0), ('infrastructure', '{}')])
def test_malformed_or_different_binding_never_qualifies(harness, field, value):
    h = harness
    request, _, _ = calibration(h)
    row = dict(query_request(h, request), **{field: value})
    with h['ledger'].read_connection() as connection:
        assert _calibration_startup_deadline(connection, row, now=h['clock']().timestamp()) is None


@pytest.mark.parametrize('fault', ['purpose', 'worker', 'batch', 'runtime', 'unapproved_job', 'old_attempt'])
def test_only_exact_approved_unexecuted_job_set_qualifies(harness, fault):
    h = harness
    request, started, jobs = calibration(h)
    row = query_request(h, request)
    if fault in {'purpose', 'worker', 'batch', 'runtime'}:
        document = json.loads(row['approval_document'])
        key = {'purpose': 'purpose', 'worker': 'pod_id', 'batch': 'batch_hash', 'runtime': 'max_runtime_seconds'}[fault]
        document[key] = 899 if fault == 'runtime' else 'wrong'
        row['approval_document'] = json.dumps(document)
    elif fault == 'unapproved_job':
        h['ledger']._submit(lambda c, _: c.execute('DELETE FROM approval_jobs WHERE approval_id=?', (started['approval_id'],)))
    else:
        active = h['ledger'].dispatch_next(request['worker_id'], approval_id=started['approval_id'])
        h['ledger'].cancel_job(active.job_id)
        h['ledger'].confirm_stopped(active.job_id, active.attempt_id)
        # Even fabricated reset counters must not hide immutable attempt history.
        h['ledger']._submit(lambda c, _: c.execute("UPDATE jobs SET state='PENDING',attempt_id=NULL,worker_id=NULL,approval_id=NULL,"
            'lease_expires_at=NULL,attempt_count=0,retry_count=0 WHERE job_id=?', (jobs[0].job_id,)))
    with h['ledger'].read_connection() as connection:
        assert _calibration_startup_deadline(connection, row, now=h['clock']().timestamp()) is None


@pytest.mark.parametrize('deadline,runtime', [(float('nan'), 240), (float('inf'), 240), (0, 240), (True, 240),
                                             (1000, True), (1000, 0), (1000, 86401)])
def test_shared_startup_arithmetic_rejects_invalid_values(deadline, runtime):
    with pytest.raises(ValueError):
        startup_dispatch_cutoff(deadline, runtime)


def test_shared_reservation_never_depends_on_poll_time():
    assert COLLECTION_DELETION_RESERVE_SECONDS == 120
    assert startup_dispatch_cutoff(1900, 240) == 1540
