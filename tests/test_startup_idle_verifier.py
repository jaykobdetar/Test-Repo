"""Exact watchdog cause verification; simulator state and read-only SQLite."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from test_controller import harness, provision

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('startup_idle_verifier', PROJECT/'deploy/verify-startup-idle.py')
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


@pytest.fixture
def stopped(harness):
    h = harness
    _, started = provision(h)
    h['watcher'].tick()
    h['clock'].advance(300)
    assert h['watcher'].tick() == [{'worker_id': started['worker_id'], 'reason': 'five_minute_idle', 'confirmed_off': True}]
    h['controller'].reconcile()
    h['watcher'].tick()  # A later inactive schedule retains the actual stop cause.
    path = h['watcher'].state_path
    expected = dict(request_id=started['request_id'], worker_id=started['worker_id'], approval_id=started['approval_id'],
                    provider_id=started['observed_provider_id'], job_id=h['job'].job_id, deadline=started['deadline'])
    def verify(**overrides):
        return v.verify(path, owner=os.geteuid(), trusted_root=h['tmp_path'], **{**expected, **overrides})
    def update(*, snapshot=None, **columns):
        with closing(sqlite3.connect(path)) as connection:
            if snapshot is not None:
                raw = connection.execute('SELECT snapshot FROM watch_schedule WHERE request_id=?', (expected['request_id'],)).fetchone()[0]
                columns['snapshot'] = snapshot(json.loads(raw))
            for column, value in columns.items():
                assert column in {'snapshot','idle_since','last_stop_reason','confirmed_off'}
                connection.execute('UPDATE watch_schedule SET '+column+'=? WHERE request_id=?', (value,expected['request_id']))
            connection.commit()
    return SimpleNamespace(h=h,path=path,expected=expected,verify=verify,update=update)


@pytest.mark.parametrize('mode',[0o600,0o640])
def test_real_watchdog_first_idle_stop_matches_exact_pins_without_writes(stopped,monkeypatch,mode):
    f=stopped
    f.path.chmod(mode)
    paths=(f.path,f.h['ledger'].path,f.h['backend'].path)
    before={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in paths}
    statements=[];connect=sqlite3.connect
    def reader(database_uri,**kwargs):
        assert database_uri.endswith('?mode=ro') and kwargs['uri'] is True
        connection=connect(database_uri,**kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(v.sqlite3,'connect',reader)
    assert f.verify() is True
    assert before=={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in paths}
    assert statements[0]=='PRAGMA query_only=ON'
    assert all(statement.startswith(('PRAGMA query_only=ON','PRAGMA trusted_schema=OFF','SELECT request_id,snapshot')) for statement in statements)


@pytest.mark.parametrize('field',['request_id','worker_id','approval_id','provider_id','job_id','deadline'])
def test_any_wrong_pin_refuses(stopped,field):
    value=stopped.expected[field]+1 if field=='deadline' else 'different-'+field
    with pytest.raises(v.VerificationError):stopped.verify(**{field:value})


def test_fractional_epoch_deadline_is_exact_without_rounding_tolerance(stopped):
    f=stopped
    f.expected['deadline']=1789906876.322796
    consumed=f.expected['deadline']-900
    f.update(snapshot=lambda body:json.dumps(dict(body,deadline=f.expected['deadline'],
        absolute_deadline=f.expected['deadline'],consumed_at=consumed)),idle_since=consumed)
    assert f.verify() is True
    with pytest.raises(v.VerificationError):f.verify(deadline=f.expected['deadline']+.000001)


@pytest.mark.parametrize('fault',['reason','unconfirmed','prior_execution','later_idle','snapshot_request','snapshot_worker',
    'snapshot_approval','snapshot_provider','jobs','job_string','extra_job','deadline','absolute_deadline','consumed',
    'runtime','runtime_float','runtime_bool','duplicate_key','nonfinite','oversize','not_object'])
def test_stop_reason_and_never_started_snapshot_are_strict(stopped,fault):
    f=stopped
    if fault in {'reason','unconfirmed','prior_execution','later_idle'}:
        column,value={'reason':('last_stop_reason','absolute_deadline'),'unconfirmed':('confirmed_off',0),
                      'prior_execution':('idle_since',-1),'later_idle':('idle_since',f.expected['deadline']-899)}[fault]
        f.update(**{column:value})
    else:
        def changed(body):
            if fault.startswith('snapshot_'):
                key={'snapshot_request':'request_id','snapshot_worker':'worker_id','snapshot_approval':'approval_id',
                     'snapshot_provider':'observed_provider_id'}[fault]
                body[key]='different'
            elif fault in {'jobs','job_string','extra_job'}:
                body['job_ids']={'jobs':'["different"]','job_string':[f.expected['job_id']],
                                 'extra_job':json.dumps([f.expected['job_id'],'extra'])}[fault]
            elif fault in {'deadline','absolute_deadline','consumed'}:
                body['consumed_at' if fault=='consumed' else fault]+=1
            elif fault in {'runtime','runtime_float','runtime_bool'}:
                body['max_runtime_seconds']={'runtime':901,'runtime_float':900.0,'runtime_bool':True}[fault]
            elif fault=='duplicate_key':return '{"request_id":"duplicate",'+json.dumps(body)[1:]
            elif fault=='nonfinite':return json.dumps(body)[:-1]+',"unexpected":NaN}'
            elif fault=='oversize':body['unexpected']='x'*v.MAX_SNAPSHOT
            elif fault=='not_object':return '[]'
            return json.dumps(body)
        f.update(snapshot=changed)
    with pytest.raises((v.VerificationError,sqlite3.Error)):f.verify()


@pytest.mark.parametrize('fault',['owner','group_write','other_read','symlink','hardlink','parent_symlink','parent_write','sidecar_link','sidecar_write'])
def test_unsafe_database_or_parent_or_sidecar_refuses(stopped,fault):
    f=stopped
    if fault=='owner':
        with pytest.raises(v.VerificationError):v.verify(f.path,owner=os.geteuid()+1,trusted_root=f.h['tmp_path'],**f.expected)
        return
    if fault=='group_write':f.path.chmod(0o660)
    elif fault=='other_read':f.path.chmod(0o644)
    elif fault in {'symlink','hardlink'}:
        original=f.path.with_name('original.sqlite');f.path.rename(original)
        if fault=='symlink':f.path.symlink_to(original)
        else:os.link(original,f.path)
    elif fault=='parent_symlink':
        original=f.path.parent.with_name('original-watchdog');f.path.parent.rename(original);f.path.parent.symlink_to(original,target_is_directory=True)
    elif fault=='parent_write':f.path.parent.chmod(0o770)
    else:
        sidecar=Path(str(f.path)+'-wal')
        if fault=='sidecar_link':sidecar.symlink_to(f.path)
        else:sidecar.write_bytes(b'unsafe');sidecar.chmod(0o660)
    with pytest.raises(v.VerificationError):f.verify()


def test_committed_wal_evidence_is_read_and_not_ignored(stopped):
    f=stopped
    f.update(confirmed_off=0)
    with closing(sqlite3.connect(f.path)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
        writer.execute('UPDATE watch_schedule SET confirmed_off=1 WHERE request_id=?',(f.expected['request_id'],))
        writer.commit()
        assert Path(str(f.path)+'-wal').exists() and Path(str(f.path)+'-shm').exists()
        before=f.path.read_bytes()
        assert f.verify() is True
        assert f.path.read_bytes()==before


def test_duplicate_request_rows_and_invalid_database_refuse(stopped):
    f=stopped
    with closing(sqlite3.connect(f.path)) as connection:
        connection.execute('CREATE TABLE duplicate AS SELECT * FROM watch_schedule')
        connection.execute('INSERT INTO duplicate SELECT * FROM watch_schedule')
        connection.execute('DROP TABLE watch_schedule')
        connection.execute('ALTER TABLE duplicate RENAME TO watch_schedule')
        connection.commit()
    with pytest.raises(v.VerificationError):f.verify()
    f.path.write_bytes(b'not sqlite')
    with pytest.raises(sqlite3.DatabaseError):f.verify()


def arguments(expected):
    return [item for key,value in expected.items() for item in ('--'+key.replace('_','-'),str(value))]


@pytest.mark.parametrize('fault',[None,'non_root','mismatch','private_error','invalid_deadline','unknown_argument'])
def test_cli_is_root_only_and_prints_only_bounded_status(stopped,monkeypatch,capsys,fault):
    f=stopped;seen=[]
    monkeypatch.setattr(v.os,'geteuid',lambda:1000 if fault=='non_root' else 0)
    monkeypatch.setattr(v.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=995))
    def check(path,**kwargs):
        seen.append((path,kwargs))
        if fault in {'mismatch','private_error'}:raise RuntimeError('PRIVATE_SNAPSHOT_WITH_IDS')
        assert path==Path('/var/lib/probe-watchdog/state.sqlite') and kwargs==dict(owner=995,**f.expected)
        return True
    monkeypatch.setattr(v,'verify',check)
    args=arguments(f.expected)
    if fault=='invalid_deadline':args[-1]='PRIVATE_INVALID_DEADLINE'
    if fault=='unknown_argument':args+=['--PRIVATE_ARGUMENT','PRIVATE_VALUE']
    assert v.main(args)==(0 if fault is None else 1)
    output=capsys.readouterr()
    assert output.err==''
    assert json.loads(output.out)=={'schema_version':1,'status':'passed' if fault is None else 'failed','verified':fault is None}
    assert all(value not in output.out for value in ['PRIVATE',*map(str,f.expected.values())])
    if fault in {'non_root','invalid_deadline','unknown_argument'}:assert seen==[]
