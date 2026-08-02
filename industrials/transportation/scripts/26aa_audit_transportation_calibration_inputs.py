#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.oos_research import artifact_sha256  # noqa: E402
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.calibration_preflight import (  # noqa: E402
    audit_candidate_component_coverage,
)
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.prebuild_contract import (  # noqa: E402
    load_prebuild_contract,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
)


REPORT_FIELDS = [
    "candidate_id",
    "component_field",
    "configured_weight",
    "eligible_row_count",
    "available_row_count",
    "coverage",
    "status",
    "reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail closed when transportation calibration would silently "
            "renormalize positive weights around missing components."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, "transportation")
    prebuild = load_prebuild_contract(config_path, family)
    standards = cfg_get(
        config,
        "oos_calibration_standards.families.transportation",
    )
    if not isinstance(standards, dict):
        raise ValueError("Missing transportation OOS standards")
    root = resolve_path(standards["research_output_root"], base_dir=base_dir)
    panel_path = root / "transportation_generic_oos_panel.csv"
    panel_validation_path = (
        root / "transportation_generic_oos_panel_validation.json"
    )
    report_path = root / "transportation_calibration_input_preflight.csv"
    manifest_path = root / "transportation_calibration_input_preflight.json"
    if not panel_path.is_file() or not panel_validation_path.is_file():
        raise FileNotFoundError("Validated generic OOS panel is required")
    validation = json.loads(
        panel_validation_path.read_text(encoding="utf-8")
    )
    if (
        validation.get("acceptance") != "PASS"
        or validation.get("panel_sha256") != artifact_sha256(panel_path)
    ):
        raise ValueError("Generic OOS panel has not passed independent validation")
    if (
        not args.allow_overwrite
        and (report_path.exists() or manifest_path.exists())
    ):
        raise FileExistsError(
            "Calibration input preflight is sealed; use --allow-overwrite"
        )
    candidates = {
        str(candidate_id): {
            str(field): float(weight) for field, weight in weights.items()
        }
        for candidate_id, weights in prebuild["enabled_candidate_registry"].items()
    }
    audit = audit_candidate_component_coverage(
        read_rows(panel_path),
        candidates=candidates,
        horizon_sessions=63,
        production_universe_policy="frozen_24_surface_freight_only",
        minimum_complete_row_coverage=float(
            standards["minimum_complete_component_row_coverage"]
        ),
    )
    write_csv_atomic(report_path, REPORT_FIELDS, audit["report_rows"])
    result = {
        key: value
        for key, value in audit.items()
        if key != "report_rows"
    }
    result.update(
        {
            "artifact_family": "transportation_calibration_input_preflight",
            "model_family": "transportation",
            "panel_path": str(panel_path),
            "panel_sha256": artifact_sha256(panel_path),
            "candidate_registry": candidates,
            "prebuild_contract_path": prebuild["manifest_path"],
            "prebuild_contract_sha256": prebuild["manifest_sha256"],
            "report_path": str(report_path),
            "report_sha256": artifact_sha256(report_path),
        }
    )
    write_text_atomic(
        manifest_path,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
