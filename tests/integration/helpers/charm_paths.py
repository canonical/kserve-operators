#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import yaml


def resolve_charm_path(charms_path: str, charm_name: str) -> Path:
    base = Path(charms_path)
    expected = base / charm_name / f"{charm_name}_ubuntu@24.04-amd64.charm"
    if expected.exists():
        return expected.absolute()

    candidates = list((base / charm_name).glob(f"{charm_name}_*.charm"))
    if len(candidates) == 1:
        return candidates[0].absolute()
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple charm artifacts found for {charm_name} under {(base / charm_name)!s}: "
            f"{[str(c) for c in candidates]}"
        )

    raise RuntimeError(
        f"No charm artifact found for {charm_name}. Expected {expected!s} or a single "
        f"{charm_name}_*.charm under {(base / charm_name)!s}."
    )


def resolve_charm_resources(charm_name: str) -> dict[str, str]:
    metadata_path = Path(__file__).resolve().parents[3] / "charms" / charm_name / "metadata.yaml"
    if not metadata_path.exists():
        raise RuntimeError(f"Charm metadata file not found: {metadata_path!s}")

    metadata = yaml.safe_load(metadata_path.read_text())
    resources = metadata.get("resources", {})
    if not resources:
        raise RuntimeError(f"No resources found in metadata: {metadata_path!s}")

    resolved_resources = {}
    for resource_name, resource_data in resources.items():
        upstream_source = resource_data.get("upstream-source")
        if not upstream_source:
            raise RuntimeError(
                f"Resource '{resource_name}' in {metadata_path!s} is missing upstream-source"
            )
        resolved_resources[resource_name] = upstream_source

    return resolved_resources


def resolve_test_charm_path(test_charm_name: str) -> Path:
    base = Path(__file__).resolve().parents[1] / test_charm_name
    candidates = list(base.glob(f"{test_charm_name}_*.charm"))
    if len(candidates) == 1:
        return candidates[0].absolute()
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple charm artifacts found for {test_charm_name} under {base!s}: "
            f"{[str(c) for c in candidates]}"
        )
    raise RuntimeError(
        f"No charm artifact found for {test_charm_name} under {base!s}. "
        f"Expected a single {test_charm_name}_*.charm file."
    )
