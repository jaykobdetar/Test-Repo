"""Trusted local execution example; see --help. No process supervision or time limits."""

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from probe_core.data import canonical_json
from probe_core.tasks import AgentWorkspace


def now():
    return datetime.now(UTC).isoformat()


def save(path, value):
    """Replace one private receipt atomically, including flushing it to disk."""
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_plan(path):
    jobs = json.loads(path.read_text())
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Plan must be a nonempty list of jobs")
    ids = set()
    for job in jobs:
        if not isinstance(job, dict) or set(job) != {"id", "command", "outputs"}:
            raise ValueError("Each job needs exactly id, command and outputs")
        if not isinstance(job["id"], str) or not job["id"] or job["id"] in ids:
            raise ValueError("Job IDs must be unique nonempty strings")
        ids.add(job["id"])
        if (not isinstance(job["command"], list) or not job["command"]
                or any(not isinstance(arg, str) or "\0" in arg for arg in job["command"])):
            raise ValueError("command must be a nonempty argv list, without NUL bytes")
        if not isinstance(job["outputs"], list):
            raise TypeError("outputs must be a list of relative file paths")
        for output in job["outputs"]:
            if (not isinstance(output, str) or not output or "\0" in output
                    or Path(output).is_absolute() or ".." in Path(output).parts):
                raise ValueError("Outputs must stay inside the attempt directory")
    return jobs


def output_paths(receipt):
    directory = Path(receipt["cwd"])
    paths = [directory / output for output in receipt["outputs"]]
    if any(not path.is_file() or not path.resolve().is_relative_to(directory) for path in paths):
        raise ValueError("One or more declared output files are missing or outside the attempt directory")
    return [str(path) for path in paths]


def publish(board, claim, receipt_path, receipt):
    board.complete(claim["task_id"], claim["claim_token"], {
        "job_id": receipt["job_id"], "command": receipt["command"],
        "returncode": receipt["returncode"], "outputs": output_paths(receipt),
        "receipt": str(receipt_path),
    })


def execute(board, claim, job, root):
    directory = root / claim["task_id"] / str(claim["attempt_count"])
    directory.mkdir(parents=True, mode=0o700)
    work = directory / "work"
    work.mkdir(mode=0o700)
    receipt_path = directory / "receipt.json"
    receipt = {"job_id": job["id"], "command": job["command"], "cwd": str(work),
               "outputs": job["outputs"], "started_at": now(), "finished_at": None,
               "pid": None, "returncode": None, "phase": "prepared",
               "stdout": str(directory / "stdout.log"), "stderr": str(directory / "stderr.log")}
    save(receipt_path, receipt)
    try:
        with open(receipt["stdout"], "xb") as stdout, open(receipt["stderr"], "xb") as stderr:
            # A separate session lets an interrupted wrapper leave external work alone.
            process = subprocess.Popen(job["command"], cwd=work, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            receipt.update(pid=process.pid, phase="running")
            save(receipt_path, receipt)
            returncode = process.wait()
    except OSError as error:
        receipt.update(phase="launch_or_io_error", error=str(error))
        save(receipt_path, receipt)
        board.fail(claim["task_id"], claim["claim_token"], "Launch or receipt I/O failed; inspect the receipt")
        raise
    receipt.update(phase="exited", returncode=returncode, finished_at=now())
    save(receipt_path, receipt)  # If publication is interrupted, recovery can reuse this evidence.
    if returncode != 0:
        board.fail(claim["task_id"], claim["claim_token"], f"Observed command exit status {returncode}")
        raise ValueError(f"{job['id']}: command exited with {returncode}; see {receipt_path}")
    try:
        publish(board, claim, receipt_path, receipt)
    except ValueError as error:
        if board.show(claim["task_id"])["state"] == "running":
            board.fail(claim["task_id"], claim["claim_token"], str(error))
        raise


def run(plan_path, state, recover_stopped=()):
    jobs = load_plan(plan_path)
    recovery = set(recover_stopped)
    if recovery - {job["id"] for job in jobs}:
        raise ValueError("Recovery names a job absent from this plan")
    with AgentWorkspace(state) as board:
        root = board.root.resolve()
        # Cooperating runners share one compute owner. This is not a sandbox.
        fd = os.open(root / "runner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another runner owns this directory") from None
            frozen = root / "plan.json"
            if frozen.exists() and json.loads(frozen.read_text()) != jobs:
                raise ValueError("Plan changed; use a new state directory and preserve this run")
            if not frozen.exists():
                if board.list():
                    raise ValueError("Use a dedicated empty workspace for this example")
                save(frozen, jobs)
            tasks = [board.submit(job["id"], canonical_json(job)) for job in jobs]
            task_ids = {task["task_id"] for task in tasks}
            for offset in range(0, len(tasks) + 1, 100):
                if any(task["task_id"] not in task_ids for task in board.list(offset=offset, limit=100)):
                    raise ValueError("This example must not share its board with unrelated tasks")
            for job, task in zip(jobs, tasks):
                if task["state"] not in {"pending", "completed"} and job["id"] not in recovery:
                    raise ValueError(f"{job['id']}: {task['state']}; inspect receipts and external work, "
                                     "then explicitly use --recover-stopped JOB")
            for job, task in zip(jobs, tasks):
                if task["state"] == "completed":
                    continue
                if task["state"] != "pending":
                    board.release(task["task_id"])
                claim = board.claim("compute-owner")
                if claim is None or claim["task_id"] != task["task_id"]:
                    if claim is not None:
                        board.release(claim["task_id"])
                    raise ValueError("Unexpected claim; stop other clients of this dedicated board")
                previous = sorted((root / task["task_id"]).glob("*/receipt.json"),
                                  key=lambda path: int(path.parent.name))
                receipt = json.loads(previous[-1].read_text()) if previous else None
                if receipt and receipt["phase"] == "exited" and receipt["returncode"] == 0:
                    try:
                        output_paths(receipt)
                    except ValueError:
                        pass  # Preserve incomplete artifacts and create a new attempt.
                    else:
                        publish(board, claim, previous[-1], receipt)
                        continue
                execute(board, claim, job, root)
            return [board.show(task["task_id"]) for task in tasks]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="JSON list of {id, command: argv, outputs: relative paths}")
    parser.add_argument("--state", type=Path, required=True, help="Dedicated private run directory")
    parser.add_argument("--recover-stopped", action="append", default=[], metavar="JOB",
                        help="Explicitly confirm this unfinished job's old processes have stopped; reuse observed "
                             "success or retry in a fresh directory. Repeat for multiple jobs.")
    args = parser.parse_args()
    try:
        tasks = run(args.plan, args.state, args.recover_stopped)
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted: child processes may still run. Inspect receipts; recovery is manual.\n")
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(tasks, indent=2))


if __name__ == "__main__":
    main()
