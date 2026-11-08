"""Keep the released integration version synchronized across repository files."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_repository_versions_match() -> None:
    """Keep release metadata and the locked project package synchronized."""
    manifest = json.loads(
        (ROOT / "custom_components/narwal/manifest.json").read_text(encoding="utf-8")
    )
    with (ROOT / "pyproject.toml").open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]
    with (ROOT / "uv.lock").open("rb") as lock_file:
        locked_packages = tomllib.load(lock_file)["package"]

    locked_project = next(
        package
        for package in locked_packages
        if package["name"] == project["name"] and package.get("source") == {"virtual": "."}
    )

    assert project["version"] == manifest["version"]
    assert locked_project["version"] == manifest["version"]
