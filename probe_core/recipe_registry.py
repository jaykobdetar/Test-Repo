"""Registry of approved fixed suites for the supervised public GPU command.

The standalone command runs only a suite named here. A recipe suite pins its
frozen recipe file by the SHA-256 of its canonical JSON, its public prompt
dataset by content hash, and the checkpoints it may run on. Adding or changing
an entry is a reviewed code change; nothing at run time can register a suite.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .recipe_runner import recipe_hash
from .schemas import ExperimentStage, Recipe

RESOURCES = Path(__file__).parent / "resources" / "recipes"


class UnknownSuite(ValueError):
    """The requested suite is not registered or its pinned files changed."""


@dataclass(frozen=True)
class RegisteredSuite:
    name: str
    kind: str
    stage: ExperimentStage
    dataset_path: str
    dataset_sha256: str
    prompt_ids: tuple[str, ...]
    models: frozenset[str]
    recipe: Recipe | None = None
    recipe_sha256: str | None = None


def _load(entry: dict) -> RegisteredSuite:
    kind = entry["kind"]
    recipe = recipe_sha256 = None
    if kind == "recipe":
        name = entry["recipe_file"]
        if Path(name).name != name or not name.endswith(".json"):
            raise UnknownSuite("recipe files must be plain names inside the registry")
        recipe = Recipe.model_validate_json((RESOURCES / name).read_text())
        recipe_sha256 = recipe_hash(recipe)
        if recipe_sha256 != entry["recipe_sha256"]:
            raise UnknownSuite("registered recipe file does not match its pinned hash")
        if tuple(p.prompt_id for p in recipe.prompts) != tuple(entry["prompt_ids"]):
            raise UnknownSuite("registered recipe prompts do not match the suite")
    elif kind != "backend_parity":
        raise UnknownSuite("unknown suite kind")
    stage = ExperimentStage(entry["stage"])
    if (kind == "backend_parity") != (stage == ExperimentStage.CALIBRATION) or stage not in {
        ExperimentStage.CALIBRATION,
        ExperimentStage.EXPLORATORY,
    }:
        raise UnknownSuite("parity suites are calibration and recipe suites are exploratory")
    return RegisteredSuite(
        name=entry["name"],
        kind=kind,
        stage=stage,
        dataset_path=entry["dataset"]["path"],
        dataset_sha256=entry["dataset"]["sha256"],
        prompt_ids=tuple(entry["prompt_ids"]),
        models=frozenset(entry["models"]),
        recipe=recipe,
        recipe_sha256=recipe_sha256,
    )


def suites() -> dict[str, RegisteredSuite]:
    index = json.loads((RESOURCES / "index.json").read_text())
    if index.get("schema_version") != 1:
        raise UnknownSuite("unsupported registry version")
    loaded = [_load(entry) for entry in index["suites"]]
    names = [suite.name for suite in loaded]
    if len(set(names)) != len(names):
        raise UnknownSuite("suite names must be distinct")
    return {suite.name: suite for suite in loaded}


def registered(name: str) -> RegisteredSuite:
    if type(name) is not str:
        raise UnknownSuite("suite name must be a string")
    found = suites().get(name)
    if found is None:
        raise UnknownSuite("suite is not registered")
    return found
