"""Real research-facade -> sandbox -> retained-input -> sandbox integration."""

import hashlib
import json

from probe_core.artifact_store import ArtifactStore
from probe_core.ledger import Ledger
from probe_core.research_api import ResearchPolicy, ResearchService
from probe_core.schemas import TensorArtifact

from test_sandbox import real_sandbox  # Reuse the mandatory rootless runtime gate.


def test_real_facade_retains_cpu_tensor_and_reuses_it_in_a_second_sandbox(real_sandbox, tmp_path):
    store = ArtifactStore(tmp_path / "private-inputs")
    with Ledger(tmp_path / "research.sqlite") as ledger:
        service = ResearchService(ledger, ResearchPolicy(), sandbox=real_sandbox, artifact_store=store)
        first = service.dispatch(
            "run_sandboxed_experiment",
            {
                "code": """import torch
from safetensors.torch import save_file
save_file({"direction": torch.tensor([1., 2., 3.], dtype=torch.float32)}, "direction.safetensors")
print("direction created")
"""
            },
        )
        assert first["returncode"] == 0, first
        assert first["termination_reason"] is None
        assert len(first["artifacts"]) == 1
        artifact = first["artifacts"][0]
        assert artifact["name"] == "direction.safetensors"
        artifact_id = artifact["artifact_id"]
        record, content = store.read(artifact_id, max_bytes=1024 * 1024)
        assert artifact_id == hashlib.sha256(content).hexdigest()
        assert record["sha256"] == "sha256:" + artifact_id
        assert artifact["tensor_refs"] == [
            {
                "path": f"{artifact_id}/tensor.safetensors",
                "sha256": "sha256:" + artifact_id,
                "tensor_name": "direction",
            }
        ]
        assert TensorArtifact.model_validate(artifact["tensor_refs"][0]).tensor_name == "direction"
        assert store.register(store.root / record["path"])["artifact_id"] == artifact_id

        # Retention must not depend on the temporary sandbox output remaining.
        sources = list(real_sandbox.workspace.glob("run-*/output/direction.safetensors"))
        assert len(sources) == 1
        sources[0].unlink()

        second = service.dispatch(
            "run_sandboxed_experiment",
            {
                "input_artifacts": [{"artifact_id": artifact_id}],
                "code": """import json
from pathlib import Path
from safetensors.torch import load_file
direction = load_file("/input/input-0.safetensors")["direction"]
assert direction.tolist() == [1., 2., 3.]
Path("result.json").write_text(json.dumps({"sum": float(direction.sum()), "count": direction.numel()}))
print("retained direction reused")
""",
            },
        )
        assert second["returncode"] == 0, second
        assert second["termination_reason"] is None
        assert "retained direction reused" in second["stdout"]
        assert len(second["artifacts"]) == 1
        _, result_bytes = store.read(second["artifacts"][0]["artifact_id"], max_bytes=4096)
        assert json.loads(result_bytes) == {"sum": 6.0, "count": 3}
        assert store.describe(artifact_id)["artifact_id"] == artifact_id
        assert len(ledger.list_jobs()) == 0
        assert len([event for event in ledger.audit_records() if event["event_type"] == "tool_call"]) == 2
