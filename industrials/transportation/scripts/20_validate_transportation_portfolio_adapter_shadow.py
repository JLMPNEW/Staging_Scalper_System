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
from industrials.transportation.scripts._shared import MODEL_FAMILY  # noqa: E402
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


DEFAULT_PORTFOLIO_CONFIG = PROJECT_ROOT / "portfolio_layer" / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate transportation shadow ranks through portfolio adapter.")
    parser.add_argument("--portfolio-config", type=Path, default=DEFAULT_PORTFOLIO_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--sector-output-root", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
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
        errors.append(f"expected exactly one portfolio transportation source, found {len(sources)}")
    else:
        source = sources[0]
        if source.get("adapter") != "industrial_family":
            errors.append("transportation portfolio source must use industrial_family")
        if bool(source.get("required")):
            errors.append("shadow transportation source must remain optional")
        if not bool(source.get("require_oos_score_valid")):
            errors.append("transportation source must fail closed on OOS validity")
        root = args.sector_output_root.expanduser().resolve() if args.sector_output_root else resolve_path(
            config["score_contract"]["sector_output_root"], base_dir=config_path.parent
        )
        try:
            result = run_adapter(source, root, asof)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(f"portfolio adapter failed: {type(exc).__name__}: {exc}")
    rows = result.rows if result is not None else []
    if result is not None:
        if result.source_pipeline != MODEL_FAMILY:
            errors.append(f"source_pipeline={result.source_pipeline!r}")
        if result.adapter != "industrial_family":
            errors.append(f"adapter={result.adapter!r}")
        if result.source_asof_date != asof:
            errors.append(f"source_asof_date={result.source_asof_date!r} expected={asof}")
        if not rows:
            errors.append("portfolio adapter returned no transportation rows")
    if any(row.investable_eligible for row in rows):
        errors.append("shadow transportation rows must not be investable")
    if any(row.oos_score_valid_flag for row in rows):
        errors.append("shadow transportation rows must not be OOS valid")
    if any(row.calibration_research_eligible for row in rows):
        errors.append("current shadow snapshot must not be research-calibration eligible")
    if any(row.survivorship_corrected_panel_flag for row in rows):
        errors.append("current shadow snapshot must not claim survivorship correction")
    if any(row.source_pipeline != MODEL_FAMILY for row in rows):
        errors.append("adapter emitted a non-transportation source_pipeline")
    output_path = args.output_json.expanduser().resolve() if args.output_json else (
        result.source_file.parent / "transportation_portfolio_adapter_validation.json"
        if result is not None
        else PROJECT_ROOT / "output" / "industrials" / "transportation" / "portfolio_adapter_validation.json"
    )
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "adapter": result.adapter if result is not None else "",
        "source_pipeline": result.source_pipeline if result is not None else "",
        "source_asof_date": result.source_asof_date if result is not None else "",
        "rows": len(rows),
        "investable_rows": sum(row.investable_eligible for row in rows),
        "oos_score_valid_rows": sum(row.oos_score_valid_flag for row in rows),
        "research_eligible_rows": sum(row.calibration_research_eligible for row in rows),
        "survivorship_corrected_rows": sum(row.survivorship_corrected_panel_flag for row in rows),
        "errors": errors,
    }
    write_manifest(output_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
