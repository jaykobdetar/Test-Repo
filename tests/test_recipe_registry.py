"""Registry of approved suites and envelope charging in the supervised standalone runner."""

import base64
import hashlib
import json
import os
import shutil
import time

import pytest

from probe_core import budget, recipe_registry
from probe_core.recipe_runner import recipe_hash
from probe_core.schemas import Recipe
from test_public_calibration import asset_environment, calibration, config, public_data  # noqa: F401
from test_public_calibration_launch import DeletedProvider, runner
from test_recipes import recipe as tiny_recipe

BASE = {"repo": "Qwen/Qwen3-1.7B-Base", "revision_sha": "ea980cb0a6c2ae4b936e82123acc929f1cec04c1"}
RECIPE_DATASET = b'{"prompts":[{"prompt_id":"short","text":"a"},{"prompt_id":"long","text":"b"}],"schema_version":1}'


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A temporary registry holding the real parity entry plus one recipe suite."""
    root = tmp_path / "recipes"
    shutil.copytree(recipe_registry.RESOURCES, root)
    index = json.loads((recipe_registry.RESOURCES / "index.json").read_text())
    body = Recipe.model_validate(tiny_recipe())
    (root / "tiny_recipe_v1.json").write_text(body.model_dump_json())
    index["suites"].append(
        {
            "name": "tiny_recipe_v1",
            "kind": "recipe",
            "stage": "exploratory",
            "dataset": {
                "path": "datasets/tiny-recipe.json",
                "sha256": "sha256:" + hashlib.sha256(RECIPE_DATASET).hexdigest(),
            },
            "prompt_ids": ["short", "long"],
            "models": [BASE["repo"]],
            "recipe_file": "tiny_recipe_v1.json",
            "recipe_sha256": recipe_hash(body),
        }
    )
    (root / "index.json").write_text(json.dumps(index))
    monkeypatch.setattr(recipe_registry, "RESOURCES", root)
    return root


def test_parity_is_the_first_registered_suite_and_unknown_suites_are_refused():
    parity = recipe_registry.registered("backend_parity_v1")
    assert parity.kind == "backend_parity" and parity.stage.value == "calibration"
    assert parity.dataset_sha256 == calibration.DATASET_SHA and parity.prompt_ids == calibration.PROMPTS
    for name in ("backend_parity_v2", "../index", 3):
        with pytest.raises(recipe_registry.UnknownSuite):
            recipe_registry.registered(name)


def test_recipe_suite_is_pinned_by_hash(registry):
    suite = recipe_registry.registered("tiny_recipe_v1")
    assert suite.kind == "recipe" and suite.recipe.recipe_id == "tiny_recipe"
    changed = tiny_recipe(seed=2)
    (registry / "tiny_recipe_v1.json").write_text(json.dumps(changed))
    with pytest.raises(recipe_registry.UnknownSuite, match="pinned hash"):
        recipe_registry.registered("tiny_recipe_v1")


def test_standalone_command_refuses_an_unknown_suite(config, tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, "process_boundary", lambda: None)
    monkeypatch.setattr(calibration, "private_output", lambda output: output)
    with pytest.raises(recipe_registry.UnknownSuite):
        calibration.run(config, absolute_deadline=None, run_id="run", output=tmp_path, suite_name="unregistered")


def test_images_may_bake_other_registered_datasets_but_nothing_unregistered(
    config, asset_environment, registry, monkeypatch
):
    s = asset_environment
    extra = str(calibration.ASSETS / "datasets/tiny-recipe.json")
    original = calibration.Path.lstat
    monkeypatch.setattr(
        calibration.Path,
        "lstat",
        lambda path: (
            type("Info", (), {"st_mode": 0o100444, "st_uid": 0, "st_nlink": 1})()
            if str(path) == extra
            else original(path)
        ),
    )
    s.baked["assets"].append(
        {
            "path": "datasets/tiny-recipe.json",
            "sha256": "sha256:" + hashlib.sha256(RECIPE_DATASET).hexdigest(),
            "inline_base64": base64.b64encode(RECIPE_DATASET).decode(),
        }
    )
    s.documents[str(calibration.BAKED_MANIFEST)] = json.dumps(s.baked).encode()
    assert tuple(p.prompt_id for p in calibration.validate_assets(config).prompts) == calibration.PROMPTS
    s.baked["assets"].append({"path": "datasets/unreviewed.json", "sha256": "sha256:" + "b" * 64})
    s.documents[str(calibration.BAKED_MANIFEST)] = json.dumps(s.baked).encode()
    with pytest.raises(ValueError, match="BAKED_ASSET_HASHES_MISMATCH"):
        calibration.validate_assets(config)


def test_recipe_suite_rejects_a_model_it_is_not_registered_for(config, registry):
    posttrained = config.model_copy(update={"model": config.model.model_copy(update={"repo": "Qwen/Qwen3-1.7B"})})
    with pytest.raises(ValueError, match="SUITE_NOT_REGISTERED_FOR_MODEL"):
        calibration.validate_assets(posttrained, recipe_registry.registered("tiny_recipe_v1"))


def issue_envelope(ledger_path, **overrides):
    fields = {
        "envelope_id": "m1-test",
        "max_gpu_usd": 3.0,
        "max_llm_usd": 0.0,
        "max_gpu_usd_per_hour": 0.8,
        "max_wall_seconds_per_pod": 900,
        "allowed_models": [BASE],
        "allowed_stages": ["exploratory"],
        "lifetime_hours": 24,
    }
    fields.update(overrides)
    envelope_file = ledger_path.parent / "envelope.json"
    envelope_file.write_text(json.dumps(fields))
    assert budget.main(["--ledger", str(ledger_path), "issue", "--envelope-file", str(envelope_file)]) == 0


def run_directory(tmp_path, *, suite="tiny_recipe_v1", ledger=None):
    directory = tmp_path / "run"
    directory.mkdir()
    deadline = time.time() + 600
    (directory / "guard-ready.json").write_text(json.dumps({"deadline": deadline, "pid": os.getpid()}))
    (directory / "calibration.json").write_text(json.dumps({"model": BASE, "live_price_usd_per_hour": 0.74}))
    key = tmp_path / "key"
    key.write_text("fixture")
    key.chmod(0o600)
    record = {
        "deadline": deadline,
        "worker_id": "worker",
        "request_id": "request-recipe-1",
        "deployment": {},
        "ssh_key": str(key),
        "suite": suite,
    }
    if ledger is not None:
        record["budget_ledger"] = str(ledger)
    return directory, record


def failing_provider(created):
    provider = DeletedProvider()
    provider._pods = lambda: []

    def create(*args, **kwargs):
        created.append(kwargs)
        raise RuntimeError("uncertain create")

    provider.create = create
    return provider


def test_recipe_run_without_a_budget_ledger_never_creates_a_pod(tmp_path, registry):
    created = []
    directory, record = run_directory(tmp_path)
    with pytest.raises(ValueError, match="charged to a budget envelope"):
        runner.run(directory, record, failing_provider(created))
    assert created == [] and not (directory / "create-started.json").exists()


def test_recipe_run_without_an_open_envelope_never_creates_a_pod(tmp_path, registry):
    created = []
    directory, record = run_directory(tmp_path, ledger=tmp_path / "budget.sqlite")
    with pytest.raises(budget.BudgetRefused, match="no open budget envelope"):
        runner.run(directory, record, failing_provider(created))
    assert created == [] and not (directory / "create-started.json").exists()


def test_recipe_run_reserves_before_creation_and_settles_after_confirmed_deletion(tmp_path, registry, monkeypatch):
    ledger_path = tmp_path / "budget.sqlite"
    issue_envelope(ledger_path, max_gpu_usd_per_hour=0.75)
    monkeypatch.setattr(runner.DeploymentSpec, "model_validate", lambda _: object())
    created = []
    directory, record = run_directory(tmp_path, ledger=ledger_path)
    assert runner.run(directory, record, failing_provider(created)) == 1
    assert created[0]["price_ceiling_usd_per_hour"] == 0.75  # the envelope's ceiling bounds creation
    reservation = json.loads((directory / "budget-reservation.json").read_text())
    assert reservation["envelope_id"] == "m1-test" and reservation["reserved_usd"] > 0
    report = json.loads((directory / "result.json").read_text())
    assert report["teardown"]["confirmed"] and report["budget_after"]["gpu_reserved_usd"] == 0
    assert 0 <= report["budget_after"]["gpu_spent_usd"] < reservation["reserved_usd"]


def test_unconfirmed_deletion_keeps_the_reservation_held(tmp_path, registry, monkeypatch):
    ledger_path = tmp_path / "budget.sqlite"
    issue_envelope(ledger_path)
    monkeypatch.setattr(runner.DeploymentSpec, "model_validate", lambda _: object())
    monkeypatch.setattr(runner, "cleanup", lambda *args, **kwargs: {"confirmed": False})
    directory, record = run_directory(tmp_path, ledger=ledger_path)
    assert runner.run(directory, record, failing_provider([])) == 1
    report = json.loads((directory / "result.json").read_text())
    reservation = json.loads((directory / "budget-reservation.json").read_text())
    assert report["budget_after"]["gpu_reserved_usd"] == reservation["reserved_usd"]
    assert report["budget_after"]["gpu_spent_usd"] == 0


def test_model_outside_the_envelope_is_refused_before_creation(tmp_path, registry):
    ledger_path = tmp_path / "budget.sqlite"
    issue_envelope(ledger_path, allowed_models=[{"repo": "Qwen/Qwen3-1.7B", "revision_sha": "7" * 40}])
    created = []
    directory, record = run_directory(tmp_path, ledger=ledger_path)
    with pytest.raises(budget.BudgetRefused, match="model is not allowed"):
        runner.run(directory, record, failing_provider(created))
    assert created == []


def recipe_results(directory, record, suite, *, mutate=None):
    import hashlib as hashing
    from datetime import datetime, timezone

    from probe_core.audit import canonical_json

    config = {
        "model": BASE,
        "container_image_digest": "sha256:" + "a" * 64,
        "code_git_commit": "b" * 40,
        "region": "EU-RO-1",
        "live_price_usd_per_hour": 0.74,
    }
    (directory / "calibration.json").write_text(json.dumps(config))
    recipe = suite.recipe
    rows = lambda: [{"prompt_id": p, "baseline": 0.0, "condition": 0.0, "delta": 0.0} for p in suite.prompt_ids]  # noqa: E731
    summary = {
        "suite": "recipe",
        "recipe_sha256": suite.recipe_sha256,
        "scientific_evidence": False,
        "evidence_label": "exploratory",
        "noop_checks": [{"name": "unhooked_forward_matches_baseline", "passed": True}],
        "prompt_ids": list(suite.prompt_ids),
        "steps": [
            {"name": step.name, "metrics": {m.name: {"per_prompt": rows()} for m in recipe.metrics}}
            for step in recipe.steps
        ],
    }
    names = ["baseline_logits", "clean__layer_1_mlp_output", "zero__logits"]
    if mutate:
        mutate(summary, names)
    (directory / "summary.json").write_text(json.dumps(summary))
    header = json.dumps(
        {n: {"dtype": "F32", "shape": [1], "data_offsets": [4 * i, 4 * i + 4]} for i, n in enumerate(names)}
    ).encode()
    (directory / "tensors.safetensors").write_bytes(len(header).to_bytes(8, "little") + header + b"\0" * 4 * len(names))
    (directory / "process.json").write_text(json.dumps({"returncode": 0, "process_stopped": True}))
    manifest = {
        "kind": "standalone_public_recipe",
        "status": "completed",
        "suite": suite.name,
        "scientific_evidence": False,
        "installed_ledger_used": False,
        "heldout_data_used": False,
        "lifecycle_acceptance": False,
        "nested_cgroup_limits_enforced": False,
        "model": BASE,
        "operation": {"kind": "recipe", "recipe": recipe.model_dump(mode="json")},
        "recipe_sha256": suite.recipe_sha256,
        "dataset_sha256": suite.dataset_sha256,
        "config_sha256": "sha256:" + hashing.sha256(canonical_json(config).encode()).hexdigest(),
        "run_id": "public-calibration",
        "absolute_deadline": datetime.fromtimestamp(record["deadline"], timezone.utc).isoformat(),
        "software": {"container_image_digest": config["container_image_digest"], "probe_mcp_git_commit": "b" * 40},
        "hardware": {"region": "EU-RO-1", "live_price_usd_per_hour": 0.74, "gpu_count": 1},
        "calibration_script_sha256": record["calibration_script_sha256"],
        "artifacts": [
            {"path": n, "sha256": hashing.sha256((directory / n).read_bytes()).hexdigest()}
            for n in ("summary.json", "tensors.safetensors")
        ],
    }
    (directory / "standalone-manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "mutate,message",
    [
        (None, None),
        (lambda s, n: s["noop_checks"][0].update(passed=False), "no-op checks"),
        (lambda s, n: s["steps"][2]["metrics"]["agreement"]["per_prompt"].pop(), "every prompt"),
        (lambda s, n: n.append("unexpected"), "tensor inventory"),
        (lambda s, n: s.update(evidence_label="validated"), "no-op checks or identity"),
    ],
)
def test_host_verifies_recipe_results_per_suite(tmp_path, registry, mutate, message):
    suite = recipe_registry.registered("tiny_recipe_v1")
    record = {"deadline": time.time() + 600, "calibration_script_sha256": "sha256:" + "c" * 64}
    recipe_results(tmp_path, record, suite, mutate=mutate)
    if message is None:
        evidence = runner.verify_results(tmp_path, record, suite)
        assert evidence["prompts"] == 2 and evidence["retained_tensors"] == 3
    else:
        with pytest.raises(ValueError, match=message):
            runner.verify_results(tmp_path, record, suite)


def test_budget_cli_sets_identity_and_closes(tmp_path, capsys):
    ledger_path = tmp_path / "budget.sqlite"
    issue_envelope(ledger_path)
    status = json.loads(capsys.readouterr().out)
    assert status["active_envelope"]["envelope_id"] == "m1-test"
    envelope_file = tmp_path / "forged.json"
    envelope_file.write_text(json.dumps({"approved_by": "uid:0"}))
    with pytest.raises(SystemExit):
        budget.main(["--ledger", str(ledger_path), "issue", "--envelope-file", str(envelope_file)])
    budget.main(["--ledger", str(ledger_path), "close", "--envelope-id", "m1-test"])
    assert json.loads(capsys.readouterr().out)["active_envelope"] is None
    ledger = budget.open_ledger(ledger_path)
    try:
        issued = [r for r in ledger.audit_records() if r["payload"].get("decision") == "budget_envelope_issued"]
        assert issued[0]["payload"]["approved_by"] == f"uid:{os.geteuid()}"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "name,manifest,repo",
    [
        ("subject_verb_l14_mlp_base_v1", "public-assets.json", "Qwen/Qwen3-1.7B-Base"),
        ("subject_verb_l14_mlp_posttrained_v1", "public-assets-posttrained.json", "Qwen/Qwen3-1.7B"),
    ],
)
def test_first_experiment_recipes_are_baked_into_their_model_images(name, manifest, repo):
    from pathlib import Path

    from probe_core.worker import prompt_set_hash  # noqa: F401
    from probe_core.worker_contracts import PromptDataset

    suite = recipe_registry.registered(name)
    assert suite.models == {repo} and suite.stage.value == "exploratory" and len(suite.prompt_ids) == 24
    baked = json.loads((Path(__file__).resolve().parents[1] / "deploy/gpu" / manifest).read_text())
    entry = next(item for item in baked["assets"] if item["path"] == suite.dataset_path)
    raw = base64.b64decode(entry["inline_base64"])
    assert entry["sha256"] == suite.dataset_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    dataset = PromptDataset.model_validate_json(raw)
    assert tuple(p.prompt_id for p in dataset.prompts) == suite.prompt_ids
    assert baked["assets"][-1]["path"] == "datasets/public-calibration-prompts.json"
    recipe = suite.recipe
    assert recipe.primary_step == "zero_l14_mlp" and len(recipe.control_steps) == 3
    for prompt, row in zip(recipe.prompts, dataset.prompts):
        singular = prompt.prompt_id.endswith("singular")
        verbs = (374, 525) if repo.endswith("Base") else (285, 546)
        assert (prompt.target_token_id, prompt.alternative_token_id) == (verbs if singular else verbs[::-1])


def test_prepare_public_run_binds_image_suite_and_fresh_deadline(tmp_path):
    from datetime import datetime, timezone

    from probe_core.runpod_provider import RunPodConfig, RunPodLaunchConfig, StorageRates
    from test_public_calibration_launch import load

    prepare = load("prepare-public-run.py")
    launch = RunPodLaunchConfig(image_repository="ghcr.io/test/worker", ports=("22/tcp",))
    provider = RunPodConfig(
        state_path=str(tmp_path / "state.sqlite"),
        api_key_file=str(tmp_path / "never-read"),
        launch=launch,
        storage_rates=StorageRates(checked_at=datetime.now(timezone.utc)),
        mode="supervised_acceptance",
    )
    template = tmp_path / "old-run"
    template.mkdir()
    old = {
        "deadline": 0,
        "worker_id": "old",
        "request_id": "old",
        "ssh_key": "/keys/id",
        "provider": json.loads(provider.model_dump_json()),
        "deployment": {
            "gpu_model": "NVIDIA GeForce RTX 4090",
            "image_digest": "sha256:" + "1" * 64,
            "region": "EU-RO-1",
            "volume_gb": 0,
            "image_repository": "ghcr.io/test/worker",
            "launch_config_hash": launch.digest,
            "storage_mode": "disposable_research",
        },
        "calibration_script_sha256": "sha256:" + "0" * 64,
    }
    (template / "run.json").write_text(json.dumps(old))
    (template / "calibration.json").write_text(json.dumps({"model": BASE, "live_price_usd_per_hour": 0.74}))
    with pytest.raises(ValueError, match="budget ledger"):
        prepare.prepare(
            template,
            tmp_path / "x",
            suite_name="subject_verb_l14_mlp_base_v1",
            image_digest="sha256:" + "2" * 64,
            code_commit="a" * 40,
        )
    with pytest.raises(ValueError, match="not registered"):
        prepare.prepare(
            template,
            tmp_path / "y",
            suite_name="subject_verb_l14_mlp_posttrained_v1",
            image_digest="sha256:" + "2" * 64,
            code_commit="a" * 40,
            budget_ledger=tmp_path / "b",
        )
    result = prepare.prepare(
        template,
        tmp_path / "new",
        suite_name="subject_verb_l14_mlp_base_v1",
        image_digest="sha256:" + "2" * 64,
        code_commit="a" * 40,
        budget_ledger=tmp_path / "budget.sqlite",
    )
    record = json.loads((tmp_path / "new" / "run.json").read_text())
    config = json.loads((tmp_path / "new" / "calibration.json").read_text())
    suite = recipe_registry.registered("subject_verb_l14_mlp_base_v1")
    assert record["deployment"]["image_digest"] == config["container_image_digest"] == "sha256:" + "2" * 64
    assert record["suite"] == suite.name and record["worker_id"] != "old" and record["request_id"] != "old"
    assert 0 < record["deadline"] - time.time() <= 900 and result["suite"] == suite.name
    assert config["datasets"] == [{"path": "/opt/probe-assets/" + suite.dataset_path, "sha256": suite.dataset_sha256}]
    assert config["code_git_commit"] == "a" * 40 and record["provider"] == old["provider"]
    assert (
        record["calibration_script_sha256"]
        == "sha256:"
        + hashlib.sha256(
            (recipe_registry.RESOURCES.parents[2] / "deploy/gpu/public-calibration.py").read_bytes()
        ).hexdigest()
    )
