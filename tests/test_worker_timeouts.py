from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import time

import pytest

from probe_core.worker import Supervisor, _cpu_time_exceeded
from probe_core.worker_contracts import ExecutionReceipt, WorkerState
from test_worker import tiny_bundle, make_request


@pytest.mark.parametrize("expired,exitcode,kind,code",[
    (True,-signal.SIGKILL,"timeout","ExecutionDeadlineExceeded"),
    (False,-signal.SIGXCPU,"timeout","CPUTimeLimitExceeded"),
    (False,-signal.SIGKILL,"infrastructure","ProcessExitedWithoutResult"),
])
def test_unreported_exit_uses_observed_deadline_or_signal_evidence(tiny_bundle,make_request,tmp_path,expired,exitcode,kind,code):
    request=make_request(key="exit-evidence")
    root=tmp_path/"attempts"
    directory=root/request.attempt_id
    directory.mkdir(parents=True)
    receipt=ExecutionReceipt(job_id=request.job_id,attempt_id=request.attempt_id,state=WorkerState.RUNNING,started_at=datetime.now(timezone.utc))
    (directory/"request.json").write_text(request.model_dump_json())
    (directory/"receipt.json").write_text(receipt.model_dump_json())
    (directory/"process.json").write_text(json.dumps({"pid":999999999,"identity":"absent-fixture","boot_id":"absent-fixture", "deadline":time.time()+(-1 if expired else 60), "monotonic_deadline":time.monotonic()+(-1 if expired else 60),"cgroup":None}))
    supervisor=Supervisor(tiny_bundle[0].model_copy(update={"output_directory":str(root)}),monitor_interval=60)
    class Reaped:
        def join(self,timeout):pass
    process=Reaped()
    process.exitcode=exitcode
    supervisor._processes[request.attempt_id]=process
    try:
        observed=supervisor.status(request.attempt_id)
        assert observed.failure_kind==kind
        assert observed.error_code==code
        assert observed.process_stopped
    finally:
        supervisor.close()


def test_cpu_soft_limit_handler_records_timeout_not_generic_failure():
    with pytest.raises(TimeoutError,match="CPU time limit"):
        _cpu_time_exceeded(signal.SIGXCPU,None)


def test_actual_supervisor_restart_adopts_existing_attempt(tiny_bundle,make_request,tmp_path):
    config=tiny_bundle[0].model_copy(update={"output_directory":str(tmp_path/"restarted")})
    first=Supervisor(config)
    request=make_request(key="actual-supervisor-restart")
    receipt=first.submit(request)
    first.close(terminate=False)
    restarted=Supervisor(config)
    try:
        assert restarted.submit(request).attempt_id==receipt.attempt_id
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            result=restarted.status(request.attempt_id)
            if result.process_stopped:
                break
            time.sleep(0.05)
        assert result.state==WorkerState.SUCCEEDED,result
        assert result.process_stopped
        assert len(restarted._requests)==1
        assert not restarted._processes  # No replacement execution was spawned.
    finally:
        restarted.close()
        for process in first._processes.values():
            process.join(timeout=5)
