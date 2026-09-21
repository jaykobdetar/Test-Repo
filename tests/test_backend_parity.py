"""The actual fixed suite runs on tiny CPU Qwen; live BF16 remains an explicit gate."""
import json

import pytest

from probe_core.schemas import JobSpec
from probe_core.worker import WorkerEngine
from test_worker import tiny_bundle, make_request


def test_actual_fixed_backend_suite_produces_honest_calibration(tiny_bundle,make_request,tmp_path):
    request=make_request({"kind":"backend_parity"})
    receipt=WorkerEngine(tiny_bundle[0]).execute(request,tmp_path/"parity")
    report=json.loads((tmp_path/"parity"/"summary.json").read_text())
    assert report["suite"] == "backend_parity_v1"
    assert report["passed"] and not report["scientific_evidence"]
    assert len(report["checks"]) >= 20
    assert all(check["passed"] for check in report["checks"])
    assert report["generated_tokens_including_reference"] == receipt.generated_tokens
    assert receipt.manifest.experiment.tool == "backend_parity"
    assert not receipt.manifest.results.heldout
    assert receipt.manifest.results.replication_status == "not_applicable"


@pytest.mark.parametrize("change",[
    {"experiment_stage":"exploratory"}, {"hypothesis_id":"cannot-claim-evidence"},
    {"operation":{"kind":"backend_parity","rtol":1.0}},
])
def test_parity_contract_cannot_expand_into_research_or_relax_thresholds(make_request,change):
    data=make_request({"kind":"backend_parity"}).spec.model_dump(mode="json")
    data.update(change)
    with pytest.raises(ValueError):JobSpec.model_validate(data)


def test_parity_token_budget_counts_both_native_and_traced_generation(make_request):
    data=make_request({"kind":"backend_parity"}).spec.model_dump(mode="json")
    data["limits"]["max_generated_tokens"]=6
    with pytest.raises(ValueError,match="generation limit"):
        JobSpec.model_validate(data)
