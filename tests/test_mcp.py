import asyncio
import os
from pathlib import Path
import sys
import threading

from mcp import Client, StdioServerParameters

from probe_core.rpc import UnixRPCServer
from test_research_api import research


def test_real_stdio_mcp_surface_and_persistent_submission(tmp_path, research):
    service, spec = research
    server = UnixRPCServer(tmp_path / "mcp.sock", service.dispatch,
                           allowed_uids={os.geteuid()}, allow_service_uid=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "probe_core.mcp_server", "--socket", str(server.path), "--service-uid", str(os.geteuid())],
        cwd=str(Path(__file__).parent.parent))

    async def exercise():
        async with Client(parameters) as client:
            assert client.protocol_version == "2026-07-28"
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"submit_job", "cancel_job", "request_gpu_start", "request_gpu_provision",
                    "import_run_artifact", "run_sandboxed_experiment"} <= names
            assert not ({"approve", "approve_gpu_start", "complete_job", "confirm_stopped", "evaluate"} & names)
            result = await client.call_tool("submit_job", {"spec": spec.model_dump(mode="json")})
            assert not result.is_error
            job_id = result.structured_content["job_id"]
        # New MCP session, same durable state. Disconnect never cancels the job.
        async with Client(parameters) as client:
            result = await client.call_tool("job_status", {"job_id": job_id})
            assert result.structured_content["state"] == "PENDING"
            denied = await client.call_tool("approve_gpu_start", {"job_id": job_id})
            assert denied.is_error
            result = await client.call_tool("cancel_job", {"job_id": job_id})
            assert result.structured_content["state"] == "FAILED"

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
