"""Small black-box checks for the local agent transport and its trust boundary."""

import asyncio
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

from probe_core.rpc import RPCError, UnixRPCClient, UnixRPCServer


@pytest.fixture
def rpc_service(tmp_path):
    root = tmp_path / "transport"
    root.mkdir(mode=0o700)
    calls = []

    def dispatch(method, params):
        calls.append((method, params))
        return {"method": method, "params": params}

    server = UnixRPCServer(root / "agent.sock", dispatch, allowed_uids={os.geteuid()}, allow_service_uid=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_rpc_authenticates_both_peers_before_dispatch(rpc_service):
    server, calls = rpc_service
    client = UnixRPCClient(server.path, expected_server_uid=os.geteuid())
    with pytest.raises(RPCError, match="untrusted service identity"):
        UnixRPCClient(server.path, expected_server_uid=os.geteuid() + 1).call("status")
    server.allowed_uids = frozenset({os.geteuid() + 1})
    with pytest.raises(RPCError):
        client.call("status")
    assert calls == []
    server.allowed_uids = frozenset({os.geteuid()})
    assert client.call("status", {"task_id": "task-1"})["params"] == {"task_id": "task-1"}
    assert len(calls) == 1
    with pytest.raises(ValueError, match="different OS identity"):
        UnixRPCServer(server.path.with_name("other.sock"), server.dispatch, allowed_uids={os.geteuid()})


def test_rpc_failure_does_not_expose_service_data(rpc_service):
    server, _ = rpc_service

    def fail(method, params):
        raise RuntimeError("private-service-token-must-not-leak")

    server.dispatch = fail
    with pytest.raises(RPCError) as error:
        UnixRPCClient(server.path, expected_server_uid=os.geteuid()).call("status")
    assert str(error.value) == "request rejected by trusted service"

    for index, response in enumerate((b"{private-service-token}\n", b'{"ok":true}\n')):
        # Exercise malformed trusted-peer responses through an actual socket.
        path = server.path.with_name(f"malformed-{index}.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            listener.settimeout(5)

            def respond(payload):
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(payload)

            thread = threading.Thread(target=respond, args=(response,), daemon=True)
            thread.start()
            try:
                with pytest.raises(RPCError, match="invalid service response"):
                    UnixRPCClient(path, expected_server_uid=os.geteuid()).call("status")
            finally:
                thread.join(timeout=5)


def test_rpc_preserves_live_socket_and_existing_files(rpc_service):
    server, _ = rpc_service
    inode = server.path.stat().st_ino
    with pytest.raises(FileExistsError, match="running instance"):
        UnixRPCServer(server.path, server.dispatch, allowed_uids={os.geteuid()}, allow_service_uid=True)
    assert server.path.stat().st_ino == inode
    assert UnixRPCClient(server.path, expected_server_uid=os.geteuid()).call("status")["method"] == "status"
    occupied = server.path.with_name("existing.sock")
    occupied.write_text("existing user data")
    with pytest.raises(FileExistsError, match="refusing to replace"):
        UnixRPCServer(occupied, server.dispatch, allowed_uids={os.geteuid()}, allow_service_uid=True)
    assert occupied.read_text() == "existing user data"


def test_mcp_task_work_survives_reconnect_and_supports_manual_cancellation(tmp_path):
    from mcp import Client, StdioServerParameters

    from probe_core.task_service import TaskService
    from probe_core.tasks import AgentWorkspace

    root = tmp_path / "agent"
    root.mkdir(mode=0o700)
    project = Path(__file__).resolve().parents[1]
    with AgentWorkspace(root / "state") as workspace:
        service = TaskService(workspace)
        server = UnixRPCServer(
            root / "tasks.sock", service.dispatch, allowed_uids={os.geteuid()}, allow_service_uid=True
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "probe_core.agent_mcp", "--socket", str(server.path), "--service-uid", str(os.geteuid())],
            cwd=str(project),
            env={"PYTHONPATH": str(project)},
        )

        async def call(client, method, arguments):
            result = await client.call_tool(method, arguments)
            assert not result.is_error, result
            content = result.structured_content
            assert isinstance(content, dict), result
            # MCP wraps nullable return annotations in a result object.
            return content["result"] if method == "claim_task" else content

        async def exercise():
            async with Client(parameters) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "submit_task",
                    "claim_task",
                    "complete_task",
                    "fail_task",
                    "cancel_task",
                    "list_tasks",
                    "task_status",
                }
                submission = {"idempotency_key": "first", "instruction": "Summarize the offline finding."}
                first = await call(client, "submit_task", submission)
                repeated = await call(client, "submit_task", submission)
                assert first["task_id"] == repeated["task_id"]
                second = await call(
                    client,
                    "submit_task",
                    {
                        "idempotency_key": "followup",
                        "instruction": "Review the summary.",
                        "depends_on": [first["task_id"]],
                    },
                )
                claim = await call(client, "claim_task", {"agent_id": "author"})
                assert claim["task_id"] == first["task_id"]
                completed = await call(
                    client,
                    "complete_task",
                    {
                        "task_id": claim["task_id"],
                        "claim_token": claim["claim_token"],
                        "result": {"summary": "The offline task produced a useful result."},
                    },
                )
                assert completed["state"] == "completed"

            async with Client(parameters) as client:
                stored = await call(client, "task_status", {"task_id": first["task_id"]})
                assert stored["result"] == completed["result"]
                assert "claim_token" not in stored
                review = await call(client, "claim_task", {"agent_id": "reviewer"})
                assert review["task_id"] == second["task_id"]
                cancelled = await call(client, "cancel_task", {"task_id": second["task_id"]})
                assert cancelled["state"] == "cancelled"
                late_result = await client.call_tool(
                    "complete_task",
                    {"task_id": second["task_id"], "claim_token": review["claim_token"], "result": {"late": True}},
                )
                assert late_result.is_error
                denied = await client.call_tool("release_task", {"task_id": second["task_id"]})
                assert denied.is_error
                return first["task_id"], second["task_id"]

        try:
            first_id, second_id = asyncio.run(exercise())
            assert workspace.show(first_id)["state"] == "completed"
            assert workspace.show(second_id)["state"] == "cancelled"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
