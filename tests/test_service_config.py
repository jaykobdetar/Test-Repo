import os

import pytest

from probe_core.research_service import ServiceConfig, load_config


def make_config(tmp_path, **changes):
    values = dict(ledger_path=str(tmp_path / "research.sqlite"), socket_path=str(tmp_path / "research.sock"),
                  service_uid=os.geteuid(), research_uid=os.geteuid() + 1, socket_gid=os.getegid(),
                  policy={"discovery_datasets": []})
    values["input_artifact_root"] = str(tmp_path / "inputs")
    values["admin_uid"] = os.geteuid() + 2
    values.update(changes)
    path = tmp_path / "config.json"
    path.write_text(ServiceConfig(**values).model_dump_json())
    path.chmod(0o600)
    return path


def test_service_requires_distinct_research_identity(tmp_path):
    path = make_config(tmp_path, research_uid=os.geteuid())
    with pytest.raises(PermissionError, match="separately"):
        load_config(path)


def test_agent_cannot_share_human_approval_identity(tmp_path):
    path = make_config(tmp_path, research_uid=os.geteuid() + 2)
    with pytest.raises(PermissionError, match="separately"):
        load_config(path)


def test_config_rejects_research_writable_file_or_symlink(tmp_path):
    path = make_config(tmp_path)
    assert load_config(path).policy.discovery_datasets == ()
    path.chmod(0o666)
    with pytest.raises(PermissionError):
        load_config(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(PermissionError):
        load_config(link)


def test_upstream_controller_identity_must_be_pinned(tmp_path):
    path = make_config(tmp_path, controller_socket=str(tmp_path / "controller.sock"))
    with pytest.raises(ValueError, match="pinned"):
        load_config(path)
