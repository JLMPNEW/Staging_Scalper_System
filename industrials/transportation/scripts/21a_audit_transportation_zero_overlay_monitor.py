#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.zero_overlay_monitoring import (  # noqa: E402
    audit_monitoring_state,
)


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_zero_overlay_monitoring_policy.yaml"
)
DEFAULT_DP15 = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "historical_features"
    / "v3_conflict_resolved"
    / "transportation_zero_overlay_portfolio_shadow_gate.json"
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
            "Audit zero-overlay transportation shadow-monitoring progress "
            "without reading outcomes or authorizing recalibration."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--dp15", type=Path, default=DEFAULT_DP15)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = args.output_root.expanduser().resolve()
    result = audit_monitoring_state(
        asof=args.asof[:10],
        policy_path=args.policy.expanduser().resolve(),
        dp15_path=args.dp15.expanduser().resolve(),
        output_root=output_root,
    )
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else output_root / "transportation_zero_overlay_monitor_status.json"
    )
    write_manifest(output_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
