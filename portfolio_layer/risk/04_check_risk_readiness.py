#!/usr/bin/env python3
"""Stage 2 - readiness gate: is the sealed Stage 1 run usable for building the risk panel?"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import check_stage1_readiness, latest_run_with, readiness_passed  # noqa: E402


LOGGER = logging.getLogger("check_risk_readiness")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 2 readiness gate over sealed Stage 1 artifacts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=iso_date_arg, default=None)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        resolve_database_path(paths, args.db)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No sealed Stage 1 run (manifest.json) found under %s", runs_root)
        return 1
    tolerance = int(cfg_get(config, "score_contract.staleness_tolerance_days", 10))
    expected = []
    per_pipeline_tolerance = {}
    optional_pipelines: set[str] = set()
    for sector in cfg_get(config, "score_contract.sectors", []):
        if not bool(sector.get("enabled", True)):
            continue
        pipe = str(sector["model_family"])
        per_pipeline_tolerance[pipe] = int(sector.get("staleness_tolerance_days", tolerance))
        if bool(sector.get("required", True)):
            expected.append(pipe)
        else:
            # shadow-only sectors (required:false) never block the production sleeves: absent from
            # the presence check, and their staleness downgrades to WARN in readiness
            optional_pipelines.add(pipe)
    stale_status = str(cfg_get(config, "risk_panel.readiness_stale_status", "FAIL"))
    checks = check_stage1_readiness(
        runs_root,
        run_as_of,
        staleness_tolerance=tolerance,
        per_pipeline_staleness_tolerance=per_pipeline_tolerance,
        expected_pipelines=expected,
        optional_pipelines=optional_pipelines,
        stale_status=stale_status,
    )
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if readiness_passed(checks):
        LOGGER.info("STAGE 2 READINESS: PASS (Stage 1 run %s is usable)", run_as_of)
        return 0
    LOGGER.error("STAGE 2 READINESS: FAIL (refresh Stage 1 before building the risk panel)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
