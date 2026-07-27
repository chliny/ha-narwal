"""Small release-structure checks not covered by behavioral tests."""

from __future__ import annotations

import filecmp
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "narwal"


def _json_key_tree(value: object) -> object:
    if isinstance(value, dict):
        return {key: _json_key_tree(child) for key, child in sorted(value.items())}
    return None


def test_translation_resources_match_current_schema() -> None:
    expected = _json_key_tree(
        json.loads((INTEGRATION / "strings.json").read_text())
    )
    for path in sorted((INTEGRATION / "translations").glob("*.json")):
        assert _json_key_tree(json.loads(path.read_text())) == expected, path.name


def test_embedded_client_matches_canonical_copy() -> None:
    canonical = ROOT / "narwal_client"
    embedded = INTEGRATION / "narwal_client"
    canonical_files = sorted(canonical.rglob("*.py"))
    embedded_relative = {
        path.relative_to(embedded) for path in embedded.rglob("*.py")
    }

    assert {path.relative_to(canonical) for path in canonical_files} == embedded_relative
    for path in canonical_files:
        relative = path.relative_to(canonical)
        assert filecmp.cmp(path, embedded / relative, shallow=False), relative
