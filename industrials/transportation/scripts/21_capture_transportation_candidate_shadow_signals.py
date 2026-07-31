#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.transportation.zero_overlay_monitoring import (  # noqa: E402
    capture_signal_snapshot,
)


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
            "Capture an immutable, outcome-blind transportation candidate "
            "shadow snapshot after a month-end current refresh."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--source-snapshot", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = capture_signal_snapshot(
        asof=args.asof[:10],
        source_snapshot=args.source_snapshot.expanduser().resolve(),
        policy_path=args.policy.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
