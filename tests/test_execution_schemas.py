import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from probe_core.schemas import HardwareIdentity, JobSpec, RunManifest, TensorSlice


def test_cpu_hardware_cannot_claim_gpu_cost_or_count():
    base = dict(provider_backend="local_cpu", gpu_model="CPU", gpu_count=0, region="local", live_price_usd_per_hour=0.0)
    assert HardwareIdentity(**base).gpu_count == 0
    for change in ({"gpu_count": 1}, {"live_price_usd_per_hour": 0.1}, {"provider_backend": "runpod"}):
        with pytest.raises(ValidationError):
            HardwareIdentity(**(base | change))


def test_fixture_model_cannot_be_canonical_confirmation():
    data = json.loads((Path(__file__).parent / "fixtures/manifest.json").read_text())
    data["model"]["repo"] = "probe/testing-tiny-qwen3"
    with pytest.raises(ValidationError, match="calibration"):
        RunManifest.model_validate(data)
    spec = dict(
        idempotency_key="cpu",
        model=data["model"],
        inputs=data["inputs"],
        operation={"kind": "generate"},
        limits={"max_runtime_seconds": 10, "max_output_bytes": 1024},
    )
    with pytest.raises(ValidationError, match="calibration"):
        JobSpec.model_validate(spec)
    assert JobSpec.model_validate(spec | {"experiment_stage": "calibration"}).operation.kind == "generate"


@pytest.mark.parametrize("sizes", [(4096, 4096), (0,), (-1,), (1, 2, 3, 4, 5)])
def test_inspection_slices_have_element_and_rank_limits(sizes):
    with pytest.raises(ValidationError):
        TensorSlice(kind="tensor_slice", parameter="model.embed_tokens.weight", starts=(0,) * len(sizes), sizes=sizes)
