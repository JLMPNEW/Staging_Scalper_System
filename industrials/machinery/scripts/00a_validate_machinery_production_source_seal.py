#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.machinery.stage12_activation import (  # noqa: E402
    PRODUCTION_POLICY_STATUS_ACTIVE,
    _active_cycle_root,
    _sealed_governance,
    changed_production_policy_sources,
)
from industrials.machinery.scoring import file_sha256  # noqa: E402
from industrials.machinery.stage12_governance import Stage12Paths  # noqa: E402
from industrials.machinery.stage8_calibration import parse_date  # noqa: E402

DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
NON_RETRYABLE_POLICY_FAILURE = 78


def validate_source_seal(
    config: dict[str, Any], *, config_path: Path, asof: str
) -> dict[str, Any]:
    target = parse_date(asof, field="asof")
    governance_root = resolve_path(
        cfg_get(config, "machinery_stage12.output_root"),
        base_dir=config_path.parent,
    )
    state_path = Stage12Paths(governance_root).activation_state_json
    if not state_path.is_file():
        return {
            "acceptance": "PASS",
            "asof_date": target.isoformat(),
            "production_policy_status": "SHADOW_NOT_ACTIVATED",
            "activation_state": str(state_path),
            "changed_source_files": [],
        }
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if (
        state.get("acceptance") != "PASS"
        or state.get("production_policy_status") != PRODUCTION_POLICY_STATUS_ACTIVE
    ):
        raise ValueError("Machinery production activation state is not active")
    activation_asof = parse_date(
        str(state.get("activation_asof") or ""), field="activation_asof"
    )
    if target < activation_asof:
        return {
            "acceptance": "PASS",
            "asof_date": target.isoformat(),
            "production_policy_status": "SHADOW_BEFORE_ACTIVATION",
            "activation_asof": activation_asof.isoformat(),
            "activation_state": str(state_path),
            "changed_source_files": [],
        }
    expected = state.get("production_source_sha256")
    if not isinstance(expected, Mapping):
        raise ValueError("Machinery production activation has no valid source seal")
    changed = changed_production_policy_sources(expected)
    if changed:
        raise ValueError(
            "Machinery production policy source changed; run a new governed "
            "Stage 8/9/12 calibration and activation before daily scoring: "
            + ",".join(changed)
        )
    active_root = _active_cycle_root(state, default_root=governance_root)
    _, active_paths = _sealed_governance(
        config,
        config_path=config_path,
        governance_root=active_root,
    )
    expected_lock_sha256 = str(state.get("governance_lock_sha256") or "").strip()
    if not expected_lock_sha256:
        raise ValueError("Machinery production activation has no governance lock seal")
    actual_lock_sha256 = file_sha256(active_paths.lock_json)
    if actual_lock_sha256 != expected_lock_sha256:
        raise ValueError("Machinery activation governance lock changed")
    return {
        "acceptance": "PASS",
        "asof_date": target.isoformat(),
        "production_policy_status": PRODUCTION_POLICY_STATUS_ACTIVE,
        "activation_asof": activation_asof.isoformat(),
        "activation_state": str(state_path),
        "governance_root": str(active_root),
        "governance_lock": str(active_paths.lock_json),
        "governance_lock_sha256": actual_lock_sha256,
        "changed_source_files": [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail fast when machinery production model sources changed."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    try:
        result = validate_source_seal(
            load_yaml(config_path), config_path=config_path, asof=args.asof
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "acceptance": "FAIL",
                    "asof_date": args.asof,
                    "failure_class": "NON_RETRYABLE_PRODUCTION_POLICY",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                indent=2,
            )
        )
        return NON_RETRYABLE_POLICY_FAILURE
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
