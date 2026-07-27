#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import family_config, load_yaml, resolve_path  # noqa: E402
from industrials.transportation.discovery_contract import build_and_write_contract  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the sealed transportation DP0 metric/archetype applicability contract. "
            "This does not invoke the dedicated parser or modify v2 scoring artifacts."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    base_dir = config_path.parent
    universe = family["universe"]
    parser_cfg = family["dedicated_parser"]
    baseline_paths = [resolve_path(value, base_dir=base_dir) for value in parser_cfg.get("baseline_artifacts", [])]
    manifest = build_and_write_contract(
        project_root=PROJECT_ROOT,
        active_path=resolve_path(universe["seed_csv"], base_dir=base_dir),
        delisted_path=resolve_path(universe["delisted_seed_csv"], base_dir=base_dir),
        metric_registry_path=resolve_path(parser_cfg["discovery_registry_csv"], base_dir=base_dir),
        supporting_registry_path=resolve_path(parser_cfg["supporting_registry_csv"], base_dir=base_dir),
        archetype_policy_path=resolve_path(parser_cfg["archetype_policy_yaml"], base_dir=base_dir),
        archetype_output_path=resolve_path(parser_cfg["archetype_map_csv"], base_dir=base_dir),
        scope_output_path=resolve_path(parser_cfg["scope_manifest_csv"], base_dir=base_dir),
        supporting_scope_output_path=resolve_path(parser_cfg["supporting_scope_manifest_csv"], base_dir=base_dir),
        manifest_output_path=resolve_path(parser_cfg["dp0_manifest_json"], base_dir=base_dir),
        registry_version=str(parser_cfg["discovery_registry_version"]),
        scope_version=str(parser_cfg["scope_version"]),
        supporting_registry_version=str(parser_cfg["supporting_registry_version"]),
        supporting_scope_version=str(parser_cfg["supporting_scope_version"]),
        baseline_paths=baseline_paths,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
