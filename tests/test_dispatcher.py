"""Real loopback HTTP, spawned worker and authoritative-ledger integration."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import threading
import time

import pytest

from probe_core.dispatcher import Dispatcher, SSHTunnel, TransportError, WorkerClient
from probe_core.ledger import Ledger
from probe_core.schemas import ApprovalNonce
from probe_core.worker import Supervisor, WorkerHTTPServer
from probe_core.worker_contracts import WorkerBusyError, WorkerConfig
from test_worker import tiny_bundle, make_request


@pytest.fixture
def service(tiny_bundle,tmp_path):
    config=tiny_bundle[0].model_copy(update={"output_directory":str(tmp_path/"worker")})
    supervisor=Supervisor(config)
    secret=secrets.token_urlsafe(32)
    server=WorkerHTTPServer(supervisor,secret)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    client=WorkerClient(f"http://127.0.0.1:{server.server_port}",secret)
    yield supervisor,server,client
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    supervisor.close()


def approve(ledger,jobs):
    now=datetime.now(timezone.utc)
    token=secrets.token_urlsafe(32)
    nonce=ApprovalNonce(approval_id="wake-1",token=token,pod_id="worker-1",batch_hash=ledger.batch_hash([job.job_id for job in jobs]),max_runtime_seconds=180,price_ceiling_usd_per_hour=1.2,issued_at=now,expires_at=now+timedelta(minutes=5))
    ledger.register_approval(nonce)
    return ledger.consume_approval("wake-1",token,pod_id="worker-1",job_ids=[job.job_id for job in jobs],live_price_usd_per_hour=1.0,requested_runtime_seconds=180)


def wait_stopped(client,attempt_id,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        receipt=client.status(attempt_id)
        if receipt.process_stopped:return receipt
        time.sleep(0.05)
    raise AssertionError("worker did not stop within acceptance deadline")


def test_http_requires_authentication_and_loopback(service,make_request):
    supervisor,server,client=service
    bad=WorkerClient(f"http://127.0.0.1:{server.server_port}","x"*40)
    with pytest.raises(TransportError):bad.submit(make_request())
    assert not supervisor._requests
    with pytest.raises(ValueError):WorkerClient("http://example.com:8080","x"*40)
    with pytest.raises(ValueError):WorkerHTTPServer(supervisor,"x"*40,host="0.0.0.0")


def test_full_dispatch_records_real_manifest_and_seals_downloaded_artifacts(service,make_request,tmp_path):
    _,_,client=service
    with Ledger(tmp_path/"ledger.sqlite") as ledger:
        job=ledger.submit_job(make_request().spec)
        approve(ledger,[job])
        dispatcher=Dispatcher(ledger,client,worker_id="worker-1",transfer_directory=tmp_path/"transfers")
        running=dispatcher.dispatch_next(approval_id="wake-1")
        assert running.state.value=="RUNNING"
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            record=dispatcher.reconcile(job.job_id)
            if record.state.value in {"COMPLETED","FAILED"}:break
            time.sleep(0.1)
        assert record.state.value=="COMPLETED",record
        manifest=ledger.get_manifest(job.job_id)
        assert manifest.hardware.provider_backend=="local_cpu"
        assert manifest.software.nnsight_version=="0.7.0"
        assert (ledger.get_artifact_root(job.job_id)/"tensors.safetensors").is_file()
        assert dispatcher.reconcile(job.job_id)==record
        assert any(event["payload"].get("to")=="COMPLETED" for event in ledger.audit_records())


def test_cancel_kills_worker_and_acknowledges_stop_to_ledger(service,make_request,tmp_path):
    supervisor,_,client=service
    with Ledger(tmp_path/"ledger.sqlite") as ledger:
        job=ledger.submit_job(make_request().spec)
        approve(ledger,[job])
        dispatcher=Dispatcher(ledger,client,worker_id="worker-1",transfer_directory=tmp_path/"transfers")
        running=dispatcher.dispatch_next(approval_id="wake-1")
        cancelled=dispatcher.cancel(job.job_id)
        assert cancelled.state.value=="FAILED"
        assert cancelled.failure_kind=="cancelled"
        assert client.status(running.attempt_id).process_stopped
        with ledger.read_connection() as reader:
            assert reader.execute("SELECT stopped_at FROM attempts WHERE attempt_id=?",(running.attempt_id,)).fetchone()[0] is not None


def test_ledger_side_cancellation_is_enforced_by_reconciliation(service,make_request,tmp_path):
    _,_,client=service
    with Ledger(tmp_path/"ledger.sqlite") as ledger:
        job=ledger.submit_job(make_request().spec)
        approve(ledger,[job])
        dispatcher=Dispatcher(ledger,client,worker_id="worker-1",transfer_directory=tmp_path/"transfers")
        running=dispatcher.dispatch_next(approval_id="wake-1")
        ledger.cancel_job(job.job_id,"research service cancellation")
        dispatcher.reconcile(job.job_id)
        assert client.status(running.attempt_id).process_stopped


def test_hard_wall_deadline_terminates_an_actual_process(service,make_request):
    _,_,client=service
    request=make_request()
    data=request.model_dump(mode="json")
    data["spec"]["limits"]["max_runtime_seconds"]=1
    from probe_core.worker_contracts import ExecutionRequest
    request=ExecutionRequest.model_validate(data)
    client.submit(request)
    receipt=wait_stopped(client,request.attempt_id,timeout=10)
    assert receipt.state.value=="FAILED"
    assert receipt.failure_kind=="timeout"


def test_single_worker_rejects_parallel_jobs_and_submission_is_idempotent(service,make_request):
    supervisor,_,client=service
    request=make_request()
    first=client.submit(request)
    assert client.submit(request).attempt_id==first.attempt_id
    with pytest.raises(TransportError):client.submit(make_request(key="job-2"))
    assert len(supervisor._requests)==1
    client.cancel(request.attempt_id)


def test_uncertain_submission_preserves_attempt_and_can_reconcile_same_execution(service,make_request,tmp_path,monkeypatch):
    _,_,client=service
    with Ledger(tmp_path/"ledger.sqlite") as ledger:
        job=ledger.submit_job(make_request().spec)
        approve(ledger,[job])
        dispatcher=Dispatcher(ledger,client,worker_id="worker-1",transfer_directory=tmp_path/"transfers")
        original=client.submit
        def lose_response(request):
            original(request)
            raise TransportError("simulated lost response after server accepted")
        with monkeypatch.context() as patch:
            patch.setattr(client,"submit",lose_response)
            with pytest.raises(TransportError):dispatcher.dispatch_next(approval_id="wake-1")
        stranded=ledger.get_job(job.job_id)
        assert stranded.state.value=="DISPATCHED"
        resumed=dispatcher.resume_submission(job.job_id)
        assert resumed.attempt_id==stranded.attempt_id
        assert resumed.attempt_count==1
        dispatcher.cancel(job.job_id)


def test_ssh_command_pins_trust_and_never_uses_host_shell(tmp_path):
    key=tmp_path/"private key"
    hosts=tmp_path/"known_hosts"
    key.write_text("synthetic fixture; no live SSH")
    hosts.write_text("synthetic fixture; no live SSH")
    key.chmod(0o600)
    hosts.chmod(0o600)
    tunnel=SSHTunnel("worker.example",user="root",identity_file=key,known_hosts_file=hosts,local_port=40000)
    command=tunnel.command()
    assert "StrictHostKeyChecking=yes" in command
    assert "BatchMode=yes" in command
    assert "IdentitiesOnly=yes" in command
    assert "127.0.0.1:40000:127.0.0.1:8080" in command
    assert command[command.index("-F")+1]=="/dev/null"
    assert str(key) in command
    with pytest.raises(ValueError):SSHTunnel("host; touch /tmp/invalid",user="root",identity_file=key,known_hosts_file=hosts)


def test_controller_direction_is_staged_by_hash_before_steering(service,make_request,tmp_path):
    import hashlib
    import torch
    from safetensors.torch import save_file
    _,_,client=service
    raw=tmp_path/"direction.safetensors"
    save_file({"direction":torch.arange(32,dtype=torch.float32)/32},str(raw))
    digest=hashlib.sha256(raw.read_bytes()).hexdigest()
    registry=tmp_path/"registry"
    directory=registry/digest
    directory.mkdir(parents=True)
    (directory/"tensor.safetensors").write_bytes(raw.read_bytes())
    operation={"kind":"steer","target":{"layer":0,"component":"residual"},"positions":["last"],"direction":{"path":digest+"/tensor.safetensors","sha256":"sha256:"+digest,"tensor_name":"direction"},"strength":0.2}
    with Ledger(tmp_path/"steering.sqlite") as ledger:
        job=ledger.submit_job(make_request(operation,key="steer-upload").spec)
        approve(ledger,[job])
        dispatcher=Dispatcher(ledger,client,worker_id="worker-1",transfer_directory=tmp_path/"transfers",input_artifact_root=registry)
        running=dispatcher.dispatch_next(approval_id="wake-1")
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            result=dispatcher.reconcile(job.job_id)
            if result.state.value in {"COMPLETED","FAILED"}:break
            time.sleep(0.1)
        assert result.state.value=="COMPLETED", result
        assert ledger.get_manifest(job.job_id).experiment.tool=="steer_direction"
        assert client.status(running.attempt_id).process_stopped


def test_capture_output_can_be_uploaded_and_replayed_by_patch(service,make_request,tmp_path):
    import hashlib
    import torch
    from safetensors.torch import load_file
    _,_,client=service
    capture=make_request(key="capture-for-patch")
    client.submit(capture)
    observed=wait_stopped(client,capture.attempt_id)
    assert observed.state.value=="SUCCEEDED", observed
    client.download(observed,tmp_path/"capture",max_bytes=1000000)
    source=tmp_path/"capture"/"tensors.safetensors"
    digest="sha256:"+hashlib.sha256(source.read_bytes()).hexdigest()
    uploaded=client.upload_tensor(source,expected_sha256=digest)
    assert client.upload_tensor(source,expected_sha256=digest)==uploaded
    operation={"kind":"patch","target":{"layer":0,"component":"residual"},"positions":["last"],"source":{"path":uploaded["path"],"sha256":uploaded["sha256"],"tensor_name":"layer_0_residual"}}
    patch=make_request(operation,key="patch-from-capture")
    client.submit(patch)
    replay=wait_stopped(client,patch.attempt_id)
    assert replay.state.value=="SUCCEEDED", replay
    client.download(replay,tmp_path/"patch",max_bytes=1000000)
    a=load_file(str(source))["next_token_logits"]
    b=load_file(str(tmp_path/"patch"/"tensors.safetensors"))["next_token_logits"]
    torch.testing.assert_close(a,b,rtol=1e-5,atol=1e-6)


def test_non_safetensors_and_wrong_digest_uploads_are_rejected(service,tmp_path):
    import hashlib
    _,_,client=service
    path=tmp_path/"not-a-tensor"
    path.write_bytes(b"not executable pickle or safetensors")
    digest="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(TransportError):client.upload_tensor(path,expected_sha256=digest)
    with pytest.raises(TransportError):client.upload_tensor(path,expected_sha256="sha256:"+"0"*64)


def _independent_watchdog(ledger_path,provider_path,state_path,health_path,stop_event):
    from probe_core.controller import StopWatchdog
    from probe_core.provider import SimulatedProvider, StopOnlyBackend
    watcher=StopWatchdog(ledger_path,StopOnlyBackend(SimulatedProvider(provider_path)),state_path=state_path,health_path=health_path)
    while not stop_event.is_set():
        watcher.tick()
        stop_event.wait(0.05)


@pytest.mark.parametrize("stop_while_running", [False, True])
def test_daemon_runs_controller_approved_job_with_independent_watchdog(service,make_request,tmp_path,stop_while_running):
    from contextlib import closing
    import multiprocessing
    import sqlite3
    import subprocess
    import sys
    from probe_core.controller import Controller
    from probe_core.provider import DeploymentSpec,SimulatedProvider
    _,server,client=service
    database=tmp_path/"daemon-ledger.sqlite"
    provider_path=tmp_path/"simulated-provider.sqlite"
    health=tmp_path/"watchdog"/"health.json"
    context=multiprocessing.get_context("spawn")
    stop=context.Event()
    watcher=None
    daemon=None
    with Ledger(database) as ledger:
        backend=SimulatedProvider(provider_path)
        controller=Controller(ledger,backend,watchdog_health_path=health,controller_idle_usd_per_day=0.20)
        job=ledger.submit_job(make_request(key="daemon-calibration").spec)
        # The provider is deliberately a simulator. Only the local CPU Qwen
        # worker executes; no provision/start/stop call reaches a cloud account.
        deployment=DeploymentSpec(gpu_model="SIMULATED",image_digest="sha256:"+"c"*64,volume_id="simulated-volume",volume_gb=100,region="simulation")
        request=controller.request_provision(deployment,[job.job_id],180)
        watcher=context.Process(target=_independent_watchdog,args=(str(database),str(provider_path),str(tmp_path/"watchdog"/"state.sqlite"),str(health),stop))
        watcher.start()
        secret_file=tmp_path/"worker-secret"
        secret_file.write_text(client._secret.get_secret_value())
        secret_file.chmod(0o600)
        config=tmp_path/"dispatcher.json"
        config.write_text(json.dumps({"ledger_path":str(database),"worker_id":request["worker_id"],"transfer_directory":str(tmp_path/"daemon-transfers"),"input_artifact_root":str(tmp_path/"registry"),"bearer_secret_file":str(secret_file),"base_url":f"http://127.0.0.1:{server.server_port}","poll_seconds":0.1}))
        config.chmod(0o600)
        try:
            deadline=time.monotonic()+25
            while not health.exists() and time.monotonic()<deadline:
                assert watcher.is_alive()
                time.sleep(0.05)
            assert health.exists()
            daemon=subprocess.Popen([sys.executable,"-m","probe_core.dispatcher","--config",str(config)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            controller.approve_and_start(request["request_id"])
            # Completion may use the whole approved job budget. Allow bounded
            # receipt/reconciliation time without shortening that contract in
            # the test observer; the worker's enforced limit stays unchanged.
            observation_seconds = 45 if stop_while_running else job.spec.limits.max_runtime_seconds + 10
            deadline=time.monotonic()+observation_seconds
            while time.monotonic()<deadline:
                record=ledger.get_job(job.job_id)
                if record.state.value in ({"RUNNING", "COMPLETED", "FAILED"} if stop_while_running else {"COMPLETED","FAILED"}):break
                assert daemon.poll() is None
                time.sleep(0.1)
            if stop_while_running:
                assert record.state.value == "RUNNING", record
                # Own SQLite's writer lock before pausing the dispatcher. Otherwise
                # SIGSTOP can freeze its writer mid-transaction and prevent the
                # controller from recording its independent stop request.
                import signal
                try:
                    with closing(sqlite3.connect(database, isolation_level=None, timeout=5)) as gate:
                        gate.execute("BEGIN IMMEDIATE")
                        try:
                            assert gate.execute("SELECT state FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()[0] == "RUNNING"
                            daemon.send_signal(signal.SIGSTOP)
                            stop_deadline = time.monotonic() + 3
                            observed_stop = None
                            while time.monotonic() < stop_deadline:
                                observed_stop = os.waitid(os.P_PID, daemon.pid, os.WSTOPPED | os.WNOHANG | os.WNOWAIT)
                                if observed_stop is not None:
                                    break
                                assert daemon.poll() is None
                                time.sleep(0.01)
                            assert observed_stop is not None
                            assert observed_stop.si_code == os.CLD_STOPPED and observed_stop.si_status == signal.SIGSTOP
                        finally:
                            gate.rollback()
                    # The stopped daemon owns no write transaction, so inspect
                    # the simulator's stop before a real receipt is reconciled.
                    stopped = controller.stop_gpu(request["worker_id"])
                    assert stopped[0]["state"] == "STOP_REQUESTED"
                    assert stopped[0]["last_error_code"] == "WorkerStopPending"
                    with ledger.read_connection() as reader:
                        assert reader.execute("SELECT ended_at FROM approvals WHERE approval_id=?", (request["approval_id"],)).fetchone()[0] is None
                        assert reader.execute("SELECT stopped_at FROM attempts WHERE attempt_id=?", (record.attempt_id,)).fetchone()[0] is None
                finally:
                    daemon.send_signal(signal.SIGCONT)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    controller.reconcile()
                    if controller.status()[0]["state"] == "STOPPED":
                        break
                    assert daemon.poll() is None
                    time.sleep(0.1)
                assert controller.status()[0]["state"] == "STOPPED"
                assert ledger.get_job(job.job_id).state.value == "FAILED"
                assert client.status(record.attempt_id).process_stopped
                with ledger.read_connection() as reader:
                    assert reader.execute("SELECT stopped_at FROM attempts WHERE attempt_id=?", (record.attempt_id,)).fetchone()[0] is not None
            else:
                assert record.state.value=="COMPLETED",record
                assert ledger.get_manifest(job.job_id).hardware.provider_backend=="local_cpu"
                assert (ledger.get_artifact_root(job.job_id)/"tensors.safetensors").is_file()
                controller.stop_gpu(request["worker_id"])
            assert backend.status(request["worker_id"]).state.value=="STOPPED"
        finally:
            if daemon is not None:
                daemon.terminate()
                try:daemon.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.communicate(timeout=5)
            stop.set()
            watcher.join(timeout=10)
            if watcher.is_alive():
                watcher.kill()
                watcher.join(timeout=5)
