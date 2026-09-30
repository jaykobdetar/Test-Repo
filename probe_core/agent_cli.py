"""Run a local task service, connect to it, or explicitly recover owned state."""

import argparse
import json
import os
from pathlib import Path

from .rpc import UnixRPCClient, UnixRPCServer, decode
from .task_service import TaskService
from .tasks import AgentWorkspace


def demo(state: Path):
    """Execute a fixed offline example in a new, dedicated caller-owned workspace."""
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    with AgentWorkspace(state) as workspace:
        analysis = workspace.submit("demo-analysis", "Count words in: agents coordinate useful work")
        report = workspace.submit("demo-report", "Report the saved analysis", [analysis["task_id"]])
        claim = workspace.claim("analyst")
        workspace.complete(
            claim["task_id"], claim["claim_token"], {"word_count": len("agents coordinate useful work".split())}
        )
        claim = workspace.claim("reporter")
        source = workspace.show(analysis["task_id"])
        workspace.complete(
            claim["task_id"],
            claim["claim_token"],
            {"summary": f"The source contains {source['result']['word_count']} words."},
        )
    with AgentWorkspace(state) as reopened:
        return reopened.show(report["task_id"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve tasks through an authenticated local Unix socket")
    serve.add_argument("--state", type=Path, required=True)
    serve.add_argument("--socket", type=Path, required=True)
    identity = serve.add_mutually_exclusive_group(required=True)
    identity.add_argument(
        "--local", action="store_true", help="trust this OS account; not isolation between same-UID agents"
    )
    identity.add_argument("--agent-uid", type=int, help="allow a separate agent OS identity")
    serve.add_argument("--socket-gid", type=int, help="group for separate identities to access the socket")
    call = sub.add_parser("call", help="call a task method with JSON parameters")
    call.add_argument("--socket", required=True)
    call.add_argument("--service-uid", type=int, required=True)
    call.add_argument("method")
    call.add_argument("params", nargs="?", default="{}")
    mcp = sub.add_parser("mcp", help="expose the task service through stdio MCP")
    mcp.add_argument("--socket", required=True)
    mcp.add_argument("--service-uid", type=int, required=True)
    release = sub.add_parser("release", help="owner recovery: revoke the old claim and explicitly requeue a task")
    release.add_argument("--state", type=Path, required=True)
    release.add_argument("task_id")
    example = sub.add_parser("demo", help="run a fixed offline two-agent dependency/result example")
    example.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "serve":
        with AgentWorkspace(args.state) as workspace:
            with UnixRPCServer(
                args.socket,
                TaskService(workspace).dispatch,
                allowed_uids={os.geteuid() if args.local else args.agent_uid},
                allow_service_uid=args.local,
                socket_gid=args.socket_gid,
            ) as server:
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass  # Task state and claims survive service shutdown.
    elif args.command == "mcp":
        from .agent_mcp import main as mcp_main

        mcp_main(["--socket", args.socket, "--service-uid", str(args.service_uid)])
    elif args.command == "call":
        params = decode(args.params.encode())
        if type(params) is not dict:
            parser.error("params must be a JSON object")
        print(json.dumps(UnixRPCClient(args.socket, expected_server_uid=args.service_uid).call(args.method, params)))
    elif args.command == "release":
        with AgentWorkspace(args.state) as workspace:
            print(json.dumps(workspace.release(args.task_id)))
    else:
        print(json.dumps(demo(args.state), indent=2))


if __name__ == "__main__":
    main()
