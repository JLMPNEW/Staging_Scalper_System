#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.transportation.financial_contract import (  # noqa: E402
    load_metric_registry,
)
from industrials.transportation.monitoring_source import (  # noqa: E402
    export_source_snapshot,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_zero_overlay_monitoring_policy.yaml"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "zero_overlay_monitoring"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export the outcome-blind transportation monitoring source from "
            "one point-in-time complete feature panel."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--complete-panel", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = cfg_get(config, "model_families.transportation", {}) or {}
    registry_path = resolve_path(
        (family.get("financial") or {})["metric_registry"],
        base_dir=config_path.parent,
    )
    _, definitions = load_metric_registry(registry_path)
    weights = {
        str(key): float(value)
        for key, value in (
            (family.get("scoring") or {}).get("component_weights") or {}
        ).items()
    }
    result = export_source_snapshot(
        asof=args.asof[:10],
        complete_panel=args.complete_panel.expanduser().resolve(),
        policy_path=args.policy.expanduser().resolve(),
        registry_path=registry_path,
        definitions=definitions,
        component_weights=weights,
        output_root=args.output_root.expanduser().resolve(),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
