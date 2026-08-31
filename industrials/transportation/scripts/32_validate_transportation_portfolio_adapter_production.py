#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import load_yaml, resolve_path  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.legacy_production_routes import (  # noqa: E402
    route_diagnostic,
)
from industrials.transportation.scripts._shared import MODEL_FAMILY  # noqa: E402
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


DEFAULT_PORTFOLIO_CONFIG = PROJECT_ROOT / "portfolio_layer" / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an activated transportation production rank through "
            "the dedicated portfolio-layer Transportation subgroup adapter."
        )
    )
    parser.add_argument(
        "--portfolio-config",
        type=Path,
        default=DEFAULT_PORTFOLIO_CONFIG,
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--sector-output-root", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def _legacy_active_validation_never_called() -> int:
    args = parse_args()
    asof = datetime.strptime(args.asof[:10], "%Y-%m-%d").date().isoformat()
    config_path = args.portfolio_config.expanduser().resolve()
    config = load_yaml(config_path)
    sources = [
        item
        for item in config.get("score_contract", {}).get("sectors", [])
        if str(item.get("model_family") or "") == MODEL_FAMILY
    ]
    errors: list[str] = []
    result = None
    if len(sources) != 1:
        errors.append(
            "expected exactly one portfolio transportation source, "
            f"found {len(sources)}"
        )
    else:
        source = sources[0]
        if source.get("adapter") != "transportation_subgroup":
            errors.append(
                "transportation portfolio source must use transportation_subgroup"
            )
        if not bool(source.get("required")):
            errors.append(
                "activated transportation source must be required"
            )
        if not bool(source.get("require_oos_score_valid")):
            errors.append(
                "transportation source must fail closed on OOS validity"
            )
        root = (
            args.sector_output_root.expanduser().resolve()
            if args.sector_output_root
            else resolve_path(
                config["score_contract"]["sector_output_root"],
                base_dir=config_path.parent,
            )
        )
        try:
            result = run_adapter(source, root, asof)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(
                f"portfolio adapter failed: {type(exc).__name__}: {exc}"
            )

    rows = result.rows if result is not None else []
    if result is not None:
        if result.source_pipeline != MODEL_FAMILY:
            errors.append(f"source_pipeline={result.source_pipeline!r}")
        if result.adapter != "transportation_subgroup":
            errors.append(f"adapter={result.adapter!r}")
        if result.source_asof_date != asof:
            errors.append(
                f"source_asof_date={result.source_asof_date!r} "
                f"expected={asof}"
            )
    if not rows:
        errors.append("portfolio adapter returned no transportation rows")
    investable = [row for row in rows if row.investable_eligible]
    oos_valid = [row for row in rows if row.oos_score_valid_flag]
    if not investable:
        errors.append("activated transportation produced no investable rows")
    if not oos_valid:
        errors.append("activated transportation produced no OOS-valid rows")
    if any(
        row.investable_eligible and not row.oos_score_valid_flag
        for row in rows
    ):
        errors.append("investable row bypassed the OOS validity gate")
    if any(row.calibration_research_eligible for row in rows):
        errors.append(
            "current production snapshot must not be research-calibration eligible"
        )
    if any(row.survivorship_corrected_panel_flag for row in rows):
        errors.append(
            "current production snapshot must not claim survivorship correction"
        )
    if any(row.source_pipeline != MODEL_FAMILY for row in rows):
        errors.append(
            "adapter emitted a non-transportation source_pipeline"
        )

    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else result.source_file.parent
        / "transportation_portfolio_adapter_production_validation.json"
        if result is not None
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / "transportation"
        / "portfolio_adapter_production_validation.json"
    )
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "adapter": result.adapter if result is not None else "",
        "source_pipeline": result.source_pipeline if result is not None else "",
        "source_asof_date": (
            result.source_asof_date if result is not None else ""
        ),
        "rows": len(rows),
        "investable_rows": len(investable),
        "oos_score_valid_rows": len(oos_valid),
        "errors": errors,
    }
    write_manifest(output_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


def main() -> int:
    """Audit that the superseded production adapter route remains inactive."""
    args = parse_args()
    asof = datetime.strptime(args.asof[:10], "%Y-%m-%d").date().isoformat()
    config_path = args.portfolio_config.expanduser().resolve()
    config = load_yaml(config_path)
    sources = [
        item
        for item in config.get("score_contract", {}).get("sectors", [])
        if str(item.get("model_family") or "") == MODEL_FAMILY
    ]
    errors: list[str] = []
    source: dict[str, object] = {}
    if len(sources) != 1:
        errors.append(
            "expected exactly one portfolio transportation source, "
            f"found {len(sources)}"
        )
    else:
        source = dict(sources[0])
        if source.get("adapter") != "transportation_subgroup":
            errors.append("reserved Transportation adapter mapping changed")
        if source.get("enabled") is not False:
            errors.append("legacy Transportation source must remain disabled")
        if source.get("required") is not False:
            errors.append("legacy Transportation source must remain optional")
        if source.get("require_oos_score_valid") is not True:
            errors.append("OOS validity must remain mandatory")
        calibration = source.get("calibration") or {}
        if not isinstance(calibration, dict) or abs(
            float(calibration.get("expected_alpha_at_full") or 0.0)
        ) > 1e-12:
            errors.append("legacy Transportation expected alpha must be zero")
    cap = float(
        ((config.get("optimizer") or {}).get("sector_weight_caps") or {}).get(
            MODEL_FAMILY, 0.0
        )
    )
    if abs(cap) > 1e-12:
        errors.append("legacy Transportation optimizer cap must be zero")
    diagnostic = route_diagnostic(
        "32_validate_transportation_portfolio_adapter_production"
    )
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "acceptance_scope": "INACTIVE_ROUTE_SAFETY_ONLY",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "source": source,
        "sector_weight_cap": cap,
        "production_promotion_eligible": False,
        "production_activation_authorized": False,
        "portfolio_allocation_authorized": False,
        "legacy_route": diagnostic,
        "errors": errors,
    }
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / "transportation"
        / "portfolio_adapter_production_validation.json"
    )
    write_manifest(output_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
