"""Trusted Unix service entry point, supervised independently of MCP sessions."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat

from pydantic import Field

from .ledger import Ledger
from .artifact_store import ArtifactStore
from .research_api import ResearchPolicy, ResearchService
from .rpc import UnixRPCServer
from .schemas import FrozenModel, SHA256


class ServiceConfig(FrozenModel):
    ledger_path: str
    socket_path: str
    service_uid: int = Field(strict=True, ge=0)
    research_uid: int = Field(strict=True, ge=0)
    admin_uid: int = Field(strict=True, ge=0)
    socket_gid: int = Field(strict=True, ge=0)
    policy: ResearchPolicy
    controller_socket: str | None = None
    controller_uid: int | None = None
    sandbox_image: SHA256 | None = None
    sandbox_workspace: str | None = None
    sandbox_seccomp_profile: str | None = None
    input_artifact_root: str
    podman_path: str = "/usr/bin/podman"


def load_config(path: Path) -> ServiceConfig:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
        raise PermissionError("configuration must be a trusted, non-writable regular file")
    config = ServiceConfig.model_validate_json(path.read_bytes())
    if config.service_uid != os.geteuid() or len({config.service_uid, config.research_uid, config.admin_uid}) != 3:
        raise PermissionError("service and research identities must be configured separately")
    if config.controller_socket is not None and config.controller_uid is None:
        raise ValueError("controller identity must be pinned")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    cloud = None
    if config.controller_socket is not None:
        from .controller import ControllerClient

        cloud = ControllerClient(config.controller_socket, expected_server_uid=config.controller_uid)
    sandbox = None
    if config.sandbox_image is not None:
        from .sandbox import PodmanSandbox

        if config.sandbox_workspace is None or config.sandbox_seccomp_profile is None:
            raise ValueError("sandbox workspace and seccomp profile are required")
        sandbox = PodmanSandbox(
            image=config.sandbox_image,
            workspace=Path(config.sandbox_workspace),
            podman=config.podman_path,
            seccomp_profile=Path(config.sandbox_seccomp_profile),
        )
    with Ledger(config.ledger_path) as ledger:
        service = ResearchService(
            ledger,
            config.policy,
            cloud=cloud,
            sandbox=sandbox,
            artifact_store=ArtifactStore(config.input_artifact_root),
        )
        with UnixRPCServer(
            config.socket_path,
            service.dispatch,
            allowed_uids={config.research_uid},
            socket_gid=config.socket_gid,
            timeout_seconds=60,
        ) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
