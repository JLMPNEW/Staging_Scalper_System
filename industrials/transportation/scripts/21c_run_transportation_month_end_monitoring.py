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
    source_paths,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402
from industrials.transportation.zero_overlay_monitoring import (  # noqa: E402
    audit_monitoring_state,
    capture_signal_snapshot,
    load_monitoring_policy,
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
            "Run the outcome-blind month-end transportation monitoring chain: "
            "source export, immutable signal capture, and DP16 audit."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--complete-panel", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--dp15", type=Path, default=DEFAULT_DP15)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = args.asof[:10]
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
    output_root = args.output_root.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    policy = load_monitoring_policy(policy_path)
    first_signal_date = str(policy["first_signal_date"])
    if asof < first_signal_date:
        raise ValueError(
            f"{asof}: precedes frozen start {first_signal_date}; "
            "no monitoring source or signal artifact was written"
        )
    source = export_source_snapshot(
        asof=asof,
        complete_panel=args.complete_panel.expanduser().resolve(),
        policy_path=policy_path,
        registry_path=registry_path,
        definitions=definitions,
        component_weights=weights,
        output_root=output_root,
    )
    source_path, _ = source_paths(output_root, asof)
    signals = capture_signal_snapshot(
        asof=asof,
        source_snapshot=source_path,
        policy_path=policy_path,
        output_root=output_root,
    )
    monitor = audit_monitoring_state(
        asof=asof,
        policy_path=policy_path,
        dp15_path=args.dp15.expanduser().resolve(),
        output_root=output_root,
    )
    result = {
        "acceptance": (
            "PASS"
            if source.get("acceptance") == "PASS"
            and signals.get("acceptance") == "PASS"
            and monitor.get("acceptance") == "PASS"
            else "FAIL"
        ),
        "asof_date": asof,
        "source_export": source,
        "signal_capture": signals,
        "monitor_status": monitor,
        "outcomes_accessed": False,
        "calibration_executed": False,
        "production_promotion_authorized": False,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
