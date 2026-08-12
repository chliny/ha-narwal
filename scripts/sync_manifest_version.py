#!/usr/bin/env python3
"""Validate or synchronize the integration manifest version with a Git tag."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_MANIFEST = Path("custom_components/narwal/manifest.json")
SEMVER_TAG = re.compile(
    r"^v?(?P<version>"
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r")$"
)
VERSION_LINE = re.compile(
    r'^(?P<prefix>\s*"version"\s*:\s*)"[^"]*"(?P<suffix>\s*,?\s*)$',
    re.MULTILINE,
)


class ManifestVersionError(ValueError):
    """Raised when a tag or manifest cannot be processed safely."""


def version_from_tag(tag: str) -> str:
    """Return a manifest-compatible version extracted from a SemVer tag."""
    match = SEMVER_TAG.fullmatch(tag)
    if match is None:
        raise ManifestVersionError(
            f"tag {tag!r} is not a valid semantic version (expected vX.Y.Z)"
        )
    return match.group("version")


def read_manifest_version(path: Path) -> tuple[str, str]:
    """Read and validate the manifest while retaining its original formatting."""
    try:
        content = path.read_text(encoding="utf-8")
        manifest = json.loads(content)
    except FileNotFoundError as err:
        raise ManifestVersionError(f"manifest not found: {path}") from err
    except (OSError, json.JSONDecodeError) as err:
        raise ManifestVersionError(f"cannot read valid JSON from {path}: {err}") from err

    version = manifest.get("version")
    if not isinstance(version, str):
        raise ManifestVersionError(f"manifest has no string version field: {path}")
    return content, version


def update_manifest_version(path: Path, target_version: str) -> bool:
    """Update only the manifest version line. Return whether the file changed."""
    content, current_version = read_manifest_version(path)
    if current_version == target_version:
        return False

    replacement = rf'\g<prefix>"{target_version}"\g<suffix>'
    updated, count = VERSION_LINE.subn(replacement, content)
    if count != 1:
        raise ManifestVersionError(
            f"expected exactly one version field in {path}, found {count}"
        )

    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as err:
        raise ManifestVersionError(f"cannot update {path}: {err}") from err
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Git tag to validate, for example v0.6.0")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"manifest path (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only validate; do not update a mismatched manifest",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        target_version = version_from_tag(args.tag)
        _, current_version = read_manifest_version(args.manifest)
        if current_version == target_version:
            return 0

        if args.check:
            print(
                f"error: {args.manifest} version is {current_version!r}, "
                f"but tag {args.tag!r} requires {target_version!r}",
                file=sys.stderr,
            )
            return 1

        update_manifest_version(args.manifest, target_version)
        print(
            f"updated {args.manifest}: {current_version} -> {target_version}",
            file=sys.stderr,
        )
        return 2
    except ManifestVersionError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
