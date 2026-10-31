"""Append-only accounting for human-approved research budget envelopes.

Every function here runs inside the ledger's single writer transaction (or a
read connection for ``summary``). Rows are insert-only; state is derived from
them, so a failed or uncertain run can never erase its reservation.

A Pod's reservation is its worst case, priced at the envelope's hourly ceiling:
``(runtime ceiling + deletion reserve) × max_gpu_usd_per_hour``. It is settled
only when the provider confirms the resource is gone, at the measured interval
from reservation to confirmation times the quoted live price. That interval
starts before creation and ends at the controller's confirmation, so it is an
upper bound on billed time. A request that never reached a provider action
settles at zero. An uncertain request keeps its full reservation.

The supervised standalone GPU command, which runs outside the installed
controller, charges the same way through ``reserve_standalone`` and
``settle_standalone`` against an operator-owned ledger. Run
``python -m probe_core.budget --help`` to issue or inspect envelopes there.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from .schemas import BudgetEnvelope, ExperimentStage, JobSpec

DELETION_RESERVE_SECONDS = 300

_TABLES = (
    """CREATE TABLE IF NOT EXISTS budget_envelopes (
        envelope_id TEXT PRIMARY KEY, body TEXT NOT NULL,
        issued_at REAL NOT NULL, expires_at REAL NOT NULL, recorded_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS budget_closures (
        envelope_id TEXT PRIMARY KEY REFERENCES budget_envelopes(envelope_id),
        closed_at REAL NOT NULL, reason TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS budget_reservations (
        request_id TEXT PRIMARY KEY, envelope_id TEXT NOT NULL REFERENCES budget_envelopes(envelope_id),
        reserved_usd REAL NOT NULL CHECK(reserved_usd > 0), reserved_at REAL NOT NULL,
        runtime_seconds INTEGER NOT NULL, price_ceiling_usd_per_hour REAL NOT NULL,
        quoted_usd_per_hour REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS budget_settlements (
        request_id TEXT PRIMARY KEY REFERENCES budget_reservations(request_id),
        settled_usd REAL NOT NULL CHECK(settled_usd >= 0), billed_seconds REAL NOT NULL,
        settled_at REAL NOT NULL, basis TEXT NOT NULL
            CHECK(basis IN ('provider_deletion_confirmed','no_provider_resource')))""",
)


class BudgetRefused(Exception):
    """The request is outside the active envelope or would exceed it."""


def initialize(connection: sqlite3.Connection) -> None:
    for statement in _TABLES:
        connection.execute(statement)
    for table in ("budget_envelopes", "budget_closures", "budget_reservations", "budget_settlements"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"""
            )


def worst_case_usd(runtime_seconds: int, usd_per_hour: float) -> float:
    return round((runtime_seconds + DELETION_RESERVE_SECONDS) / 3600 * usd_per_hour, 6)


def _event(connection, now, payload):
    from .ledger import Ledger

    Ledger._event(connection, now, "policy_evaluation", payload)


def _envelope(row) -> BudgetEnvelope:
    return BudgetEnvelope.model_validate_json(row["body"])


def _is_open(connection, row, now: datetime) -> bool:
    closed = connection.execute("SELECT 1 FROM budget_closures WHERE envelope_id=?", (row["envelope_id"],)).fetchone()
    return closed is None and now.timestamp() < row["expires_at"]


def active_envelope(connection: sqlite3.Connection, now: datetime) -> BudgetEnvelope | None:
    for row in connection.execute("SELECT * FROM budget_envelopes ORDER BY issued_at DESC"):
        if _is_open(connection, row, now):
            return _envelope(row)
    return None


def issue(connection: sqlite3.Connection, now: datetime, envelope: BudgetEnvelope) -> None:
    if active_envelope(connection, now) is not None:
        raise BudgetRefused("another envelope is still open; close it before issuing a new one")
    if envelope.expires_at <= now:
        raise BudgetRefused("the envelope has already expired")
    connection.execute(
        "INSERT INTO budget_envelopes VALUES(?,?,?,?,?)",
        (
            envelope.envelope_id,
            envelope.model_dump_json(),
            envelope.issued_at.timestamp(),
            envelope.expires_at.timestamp(),
            now.timestamp(),
        ),
    )
    _event(connection, now, {"decision": "budget_envelope_issued", **envelope.model_dump(mode="json")})


def close(connection: sqlite3.Connection, now: datetime, envelope_id: str, reason: str) -> None:
    row = connection.execute("SELECT * FROM budget_envelopes WHERE envelope_id=?", (envelope_id,)).fetchone()
    if row is None:
        raise BudgetRefused("envelope does not exist")
    if connection.execute("SELECT 1 FROM budget_closures WHERE envelope_id=?", (envelope_id,)).fetchone():
        raise BudgetRefused("envelope is already closed")
    connection.execute("INSERT INTO budget_closures VALUES(?,?,?)", (envelope_id, now.timestamp(), reason))
    _event(connection, now, {"decision": "budget_envelope_closed", "envelope_id": envelope_id, "reason": reason})


def committed_usd(connection: sqlite3.Connection, envelope_id: str) -> tuple[float, float]:
    """Return (settled spend, reservations still held) for an envelope."""
    settled = held = 0.0
    for row in connection.execute(
        """SELECT r.reserved_usd, s.settled_usd FROM budget_reservations r
        LEFT JOIN budget_settlements s USING(request_id) WHERE r.envelope_id=?""",
        (envelope_id,),
    ):
        if row["settled_usd"] is None:
            held += row["reserved_usd"]
        else:
            settled += row["settled_usd"]
    return round(settled, 6), round(held, 6)


def check_model_and_stage(envelope: BudgetEnvelope, repo: str, revision_sha: str, stage: ExperimentStage) -> None:
    if (repo, revision_sha) not in {(model.repo, model.revision_sha) for model in envelope.allowed_models}:
        raise BudgetRefused("a job's model is not allowed by the envelope")
    if ExperimentStage(stage) not in {ExperimentStage(value) for value in envelope.allowed_stages}:
        raise BudgetRefused("a job's scientific stage is not allowed by the envelope")


def check_scope(envelope: BudgetEnvelope, specs: list[JobSpec], runtime_seconds: int) -> None:
    if runtime_seconds > envelope.max_wall_seconds_per_pod:
        raise BudgetRefused("requested runtime exceeds the envelope's per-Pod ceiling")
    for spec in specs:
        check_model_and_stage(envelope, spec.model.repo, spec.model.revision_sha, spec.experiment_stage)


def reserve(
    connection: sqlite3.Connection,
    now: datetime,
    envelope_id: str,
    *,
    request_id: str,
    runtime_seconds: int,
    quoted_usd_per_hour: float,
) -> float:
    row = connection.execute("SELECT * FROM budget_envelopes WHERE envelope_id=?", (envelope_id,)).fetchone()
    if row is None or not _is_open(connection, row, now):
        raise BudgetRefused("the envelope is closed, expired or unknown")
    envelope = _envelope(row)
    if runtime_seconds > envelope.max_wall_seconds_per_pod:
        raise BudgetRefused("requested runtime exceeds the envelope's per-Pod ceiling")
    if not 0 < quoted_usd_per_hour <= envelope.max_gpu_usd_per_hour:
        raise BudgetRefused("the live price exceeds the envelope's hourly ceiling")
    amount = worst_case_usd(runtime_seconds, envelope.max_gpu_usd_per_hour)
    settled, held = committed_usd(connection, envelope_id)
    if settled + held + amount > envelope.max_gpu_usd + 1e-9:
        raise BudgetRefused("the remaining envelope cannot cover this Pod's worst-case cost")
    connection.execute(
        "INSERT INTO budget_reservations VALUES(?,?,?,?,?,?,?)",
        (
            request_id,
            envelope_id,
            amount,
            now.timestamp(),
            runtime_seconds,
            envelope.max_gpu_usd_per_hour,
            quoted_usd_per_hour,
        ),
    )
    _event(
        connection,
        now,
        {
            "decision": "budget_reserved",
            "envelope_id": envelope_id,
            "request_id": request_id,
            "reserved_usd": amount,
            "runtime_seconds": runtime_seconds,
            "deletion_reserve_seconds": DELETION_RESERVE_SECONDS,
            "quoted_usd_per_hour": quoted_usd_per_hour,
        },
    )
    return amount


def settle(connection: sqlite3.Connection, now: datetime, request_id: str, *, provider_resource: bool) -> None:
    """Settle a reservation once; no-op for requests without one."""
    row = connection.execute("SELECT * FROM budget_reservations WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        return
    if connection.execute("SELECT 1 FROM budget_settlements WHERE request_id=?", (request_id,)).fetchone():
        return
    billed = max(0.0, now.timestamp() - row["reserved_at"]) if provider_resource else 0.0
    usd = round(billed / 3600 * row["quoted_usd_per_hour"], 6)
    basis = "provider_deletion_confirmed" if provider_resource else "no_provider_resource"
    connection.execute(
        "INSERT INTO budget_settlements VALUES(?,?,?,?,?)", (request_id, usd, billed, now.timestamp(), basis)
    )
    _event(
        connection,
        now,
        {
            "decision": "budget_settled",
            "envelope_id": row["envelope_id"],
            "request_id": request_id,
            "settled_usd": usd,
            "billed_seconds": round(billed, 3),
            "basis": basis,
        },
    )


def summary(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    envelopes = []
    active = None
    for row in connection.execute("SELECT * FROM budget_envelopes ORDER BY issued_at"):
        envelope = _envelope(row)
        settled, held = committed_usd(connection, envelope.envelope_id)
        view = {
            "envelope_id": envelope.envelope_id,
            "open": _is_open(connection, row, now),
            "expires_at": envelope.expires_at.isoformat(),
            "max_gpu_usd": envelope.max_gpu_usd,
            "max_llm_usd": envelope.max_llm_usd,
            "max_gpu_usd_per_hour": envelope.max_gpu_usd_per_hour,
            "max_wall_seconds_per_pod": envelope.max_wall_seconds_per_pod,
            "allowed_models": [model.model_dump(mode="json") for model in envelope.allowed_models],
            "allowed_stages": [str(stage) for stage in envelope.allowed_stages],
            "gpu_spent_usd": settled,
            "gpu_reserved_usd": held,
            "gpu_remaining_usd": round(envelope.max_gpu_usd - settled - held, 6),
        }
        envelopes.append(view)
        if view["open"]:
            active = view
    return {"active_envelope": active, "envelopes": envelopes, "deletion_reserve_seconds": DELETION_RESERVE_SECONDS}


def open_ledger(path: str | Path):
    from .ledger import Ledger

    ledger = Ledger(path)
    ledger._submit(lambda connection, now: initialize(connection))
    return ledger


def reserve_standalone(
    ledger,
    *,
    request_id: str,
    runtime_seconds: int,
    quoted_usd_per_hour: float,
    repo: str,
    revision_sha: str,
    stage: ExperimentStage,
) -> dict[str, Any]:
    """Reserve a standalone run's worst case against the open envelope, or refuse."""

    def reserve_one(connection, now):
        envelope = active_envelope(connection, now)
        if envelope is None:
            raise BudgetRefused("no open budget envelope")
        check_model_and_stage(envelope, repo, revision_sha, stage)
        amount = reserve(
            connection,
            now,
            envelope.envelope_id,
            request_id=request_id,
            runtime_seconds=runtime_seconds,
            quoted_usd_per_hour=quoted_usd_per_hour,
        )
        return {
            "envelope_id": envelope.envelope_id,
            "reserved_usd": amount,
            "price_ceiling_usd_per_hour": envelope.max_gpu_usd_per_hour,
        }

    return ledger._submit(reserve_one)


def settle_standalone(ledger, request_id: str, *, provider_resource: bool) -> None:
    ledger._submit(lambda connection, now: settle(connection, now, request_id, provider_resource=provider_resource))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Issue, inspect or close a budget envelope in a local ledger")
    parser.add_argument("--ledger", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    issue_command = commands.add_parser("issue")
    issue_command.add_argument("--envelope-file", required=True, type=Path)
    commands.add_parser("status")
    close_command = commands.add_parser("close")
    close_command.add_argument("--envelope-id", required=True)
    close_command.add_argument("--reason", default="closed by operator")
    args = parser.parse_args(argv)
    ledger = open_ledger(args.ledger)
    try:
        if args.command == "issue":
            fields = json.loads(args.envelope_file.read_text())
            if {"approved_by", "issued_at", "expires_at"} & set(fields):
                raise SystemExit("approval identity and timestamps are set by this command")
            lifetime = fields.pop("lifetime_hours")
            now = datetime.now(timezone.utc)
            envelope = BudgetEnvelope.model_validate(
                {
                    **fields,
                    "issued_at": now,
                    "expires_at": now + timedelta(hours=lifetime),
                    "approved_by": f"uid:{os.geteuid()}",
                }
            )
            ledger._submit(lambda connection, now: issue(connection, now, envelope))
        elif args.command == "close":
            ledger._submit(lambda connection, now: close(connection, now, args.envelope_id, args.reason))
        with ledger.read_connection() as connection:
            print(json.dumps(summary(connection, datetime.now(timezone.utc)), indent=2))
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
