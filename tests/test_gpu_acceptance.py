from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from probe_core.gpu_acceptance import collect, make_plan, submit
from probe_core.ledger import Ledger
from probe_core.worker_contracts import WorkerConfig
from test_schemas import manifest_data


def test_plan_requires_live_canonical_config_and_submits_no_approval(manifest_data,tmp_path):
    import hashlib
    dataset=tmp_path/"public-calibration-prompts.json"
    dataset.write_text(json.dumps({"prompts":[{"prompt_id":"short","text":"The capital of France is"},{"prompt_id":"long","text":"A quiet garden has three red flowers. Describe their color."}]}))
    config=WorkerConfig(model_directory=str(tmp_path/"models"),model=manifest_data["model"],assets=[{"path":"config.json","sha256":"sha256:"+"a"*64}],
                        datasets=[{"path":str(dataset),"sha256":"sha256:"+hashlib.sha256(dataset.read_bytes()).hexdigest()}],
                        tensor_directory=str(tmp_path/"tensors"),output_directory=str(tmp_path/"output"),device="cuda:0",backend="nnsight",
                        code_git_commit="a"*40,container_image_digest="sha256:"+"b"*64,provider_backend="runpod",region="test-fixture",live_price_usd_per_hour=0.74,cgroup_directory="/not-a-real-cgroup-test-fixture")
    plan=make_plan(config,"base-acceptance")
    assert len(plan.cases)==7
    assert plan.cases[0].spec.operation.kind=="backend_parity"
    assert all(case.spec.experiment_stage.value=="calibration" for case in plan.cases)
    with Ledger(tmp_path/"ledger.sqlite") as ledger:
        queued=submit(ledger,plan)
        assert not queued["approval_consumed"] and not queued["compute_started"]
        assert len(queued["job_ids"])==7
        observed=collect(ledger,plan)
        assert not observed["case_results_passed"]
        assert not observed["lifecycle_acceptance_complete"]
        assert not observed["scientific_evidence"]
        assert all(case["reason"]=="no execution attempt exists" for case in observed["cases"])


def test_worker_config_bootstrap_rejects_unsafe_ownership(tmp_path,monkeypatch):
    from probe_core.gpu_launch import _private_worker_file
    path=tmp_path/"config"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError,match="private"):
        _private_worker_file(path)


def test_path_preflight_rejects_worker_owned_model_tree_before_loading(tmp_path):
    from types import SimpleNamespace
    from probe_core.gpu_launch import check_worker_paths
    model=tmp_path/"model"
    model.mkdir()
    with pytest.raises(ValueError,match="must not own"):
        check_worker_paths(SimpleNamespace(model_directory=str(model)), token_path=tmp_path/"unused-token")
