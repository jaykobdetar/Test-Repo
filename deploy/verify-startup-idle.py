#!/usr/bin/python3
"""Read-only confirmation of one pinned 900-second acceptance's startup idle stop.

This checks watchdog evidence only; the ordinary recovery helper still verifies
closed approval, zero attempts and provider absence. No credentials, services,
provider APIs or writable Ledger connections are used here.
"""
import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import time

DATABASE = Path('/var/lib/probe-watchdog/state.sqlite')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
MAX_DATABASE = 16 * 1024**2
MAX_SNAPSHOT = 65536


class VerificationError(ValueError):
    pass


def require(condition):
    if not condition:
        raise VerificationError('STARTUP_IDLE_NOT_VERIFIED')


def decode(raw, limit):
    require(type(raw) is str and len(raw.encode('utf-8')) <= limit)
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: require(False))


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def metadata(path, owner, trusted_root):
    """CLI trusts the whole fixed host path; tests can supply a temporary root."""
    path, trusted_root = Path(path).absolute(), Path(trusted_root).absolute()
    require('..' not in path.parts and (path.parent == trusted_root or trusted_root in path.parents))
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, owner} and not info.st_mode & 0o022)
        if parent == trusted_root:
            break
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) in {0o600, 0o640} and 0 < info.st_size <= MAX_DATABASE)
    # SQLite WAL and SHM are legitimate alongside the database. Refuse links,
    # unrelated ownership and writable groups before SQLite can follow them.
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if os.path.lexists(sidecar):
            extra = sidecar.lstat()
            require(stat.S_ISREG(extra.st_mode) and extra.st_uid == owner and extra.st_nlink == 1
                    and stat.S_IMODE(extra.st_mode) in {0o600, 0o640} and extra.st_size <= MAX_DATABASE)
    return path, (info.st_dev, info.st_ino)


def verify(path, *, owner, request_id, worker_id, approval_id, provider_id, job_id, deadline,
           trusted_root=Path('/')):
    identities = dict(request_id=request_id, worker_id=worker_id, approval_id=approval_id,
                      observed_provider_id=provider_id)
    require(all(type(value) is str and IDENT.fullmatch(value) for value in (*identities.values(), job_id))
            and number(deadline))
    path, inode = metadata(path, owner, trusted_root)
    until = time.monotonic() + 5
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.set_progress_handler(lambda: int(time.monotonic() > until), 1000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * MAX_SNAPSHOT)
        connection.row_factory = sqlite3.Row
        rows = connection.execute('''SELECT request_id,snapshot,idle_since,last_stop_reason,confirmed_off
            FROM watch_schedule WHERE request_id=? LIMIT 2''', (request_id,)).fetchall()
    require(metadata(path, owner, trusted_root)[1] == inode and len(rows) == 1)
    row = rows[0]
    require(row['request_id'] == request_id and row['last_stop_reason'] == 'five_minute_idle'
            and type(row['confirmed_off']) is int and row['confirmed_off'] == 1)
    snapshot = decode(row['snapshot'], MAX_SNAPSHOT)
    require(type(snapshot) is dict and all(snapshot.get(key) == value for key, value in identities.items()))
    require(decode(snapshot.get('job_ids'), 1024) == [job_id]
            and type(snapshot.get('max_runtime_seconds')) is int and snapshot['max_runtime_seconds'] == 900)
    consumed = snapshot.get('consumed_at')
    require(number(consumed) and number(snapshot.get('deadline')) and number(snapshot.get('absolute_deadline'))
            and snapshot['deadline'] == snapshot['absolute_deadline'] == deadline
            and consumed + 900 == deadline and number(row['idle_since']) and row['idle_since'] == consumed)
    return True


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics echo rejected values and arguments.
        raise VerificationError('STARTUP_IDLE_NOT_VERIFIED')


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        for name in ('request-id', 'worker-id', 'approval-id', 'provider-id', 'job-id', 'deadline'):
            parser.add_argument('--' + name, required=True)
        args = parser.parse_args(argv)
        require(os.geteuid() == 0)
        owner = pwd.getpwnam('probe-watchdog').pw_uid
        require(owner > 0)
        verify(DATABASE, owner=owner, **{**vars(args), 'deadline': float(args.deadline)})
        passed = True
    except Exception:
        passed = False
    print(json.dumps({'schema_version': 1, 'status': 'passed' if passed else 'failed', 'verified': passed}, sort_keys=True))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
