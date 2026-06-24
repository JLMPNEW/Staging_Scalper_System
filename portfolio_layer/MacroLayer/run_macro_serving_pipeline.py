#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from macro_raw_config import cfg_get, configure_pipeline_logging, load_macro_raw_config, parse_boolish, resolve_path

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the MacroLayer serving build DAG in dependency order: "
            "calendar -> PIT -> metric latest -> country coverage -> feature -> composite "
            "-> probability -> regime raw -> regime smoothed -> regime decision -> industry macro -> country macro "
            "-> stock macro overlay -> portfolio inputs -> stock sleeve targets -> foreign sleeve budget "
            "-> final optimizer integration."
        )
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--python-executable", type=str, default=sys.executable, help="Python executable to use for child steps.")
    parser.add_argument("--raw-db-path", type=Path, default=None, help="Optional raw SQLite path override.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--policy-csv", type=Path, default=None, help="Optional metric policy CSV override.")
    parser.add_argument("--feature-policy-csv", type=Path, default=None, help="Optional feature policy CSV override.")
    parser.add_argument("--composite-policy-csv", type=Path, default=None, help="Optional composite policy CSV override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional shared build start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional shared build end YYYY-MM-DD override.")
    parser.add_argument("--metric-keys", nargs="*", default=None, help="Optional metric_key filter for PIT/feature steps.")
    parser.add_argument("--composite-keys", nargs="*", default=None, help="Optional composite_key filter for the composite step.")
    parser.add_argument("--probability-keys", nargs="*", default=None, help="Optional probability_key filter for the probability step.")
    parser.add_argument("--pit-workers", type=int, default=0, help="Optional PIT worker count override.")
    parser.add_argument("--feature-workers", type=int, default=0, help="Optional feature worker count override.")
    parser.add_argument("--rebuild-policies", action="store_true", help="Rebuild metric, feature, and composite policy CSVs before serving steps.")
    parser.add_argument("--skip-metric-latest", action="store_true", help="Skip rebuilding macro_metric_latest.")
    parser.add_argument("--skip-country-coverage", action="store_true", help="Skip rebuilding macro_country_coverage_daily.")
    parser.add_argument("--skip-feature", action="store_true", help="Skip rebuilding macro features.")
    parser.add_argument("--skip-composites", action="store_true", help="Skip rebuilding macro composites.")
    parser.add_argument("--skip-probabilities", action="store_true", help="Skip rebuilding macro probabilities.")
    parser.add_argument("--skip-regime-raw", action="store_true", help="Skip rebuilding macro_regime_raw_daily.")
    parser.add_argument("--skip-regime-smoothed", action="store_true", help="Skip rebuilding macro_regime_smoothed_daily.")
    parser.add_argument("--skip-regime-decision", action="store_true", help="Skip rebuilding macro_regime_decision_daily.")
    parser.add_argument("--skip-industry-macro", action="store_true", help="Skip rebuilding Stage 9 industry macro fits.")
    parser.add_argument("--skip-country-macro", action="store_true", help="Skip rebuilding Stage 10 country macro fits.")
    parser.add_argument("--skip-stock-macro-overlay", action="store_true", help="Skip rebuilding Stage 11 stock macro overlay scores.")
    parser.add_argument("--skip-portfolio-inputs", action="store_true", help="Skip rebuilding Stage 12A portfolio optimizer inputs.")
    parser.add_argument("--skip-stock-sleeve-targets", action="store_true", help="Skip rebuilding Stage 12B stock sleeve target weights.")
    parser.add_argument("--skip-foreign-sleeve-budget", action="store_true", help="Skip rebuilding Stage 12C foreign sleeve budget.")
    parser.add_argument("--skip-final-optimizer", action="store_true", help="Skip running Stage 12D final optimizer integration cases.")
    return parser.parse_args()


def _append_path_arg(cmd: list[str], flag: str, value: Path | None) -> None:
    if value is not None:
        cmd.extend([flag, str(Path(value).expanduser().resolve())])


def _append_text_arg(cmd: list[str], flag: str, value: str | None) -> None:
    if value is not None and str(value).strip():
        cmd.extend([flag, str(value).strip()])


def _append_multi_arg(cmd: list[str], flag: str, values: list[str] | None) -> None:
    items = [str(item).strip() for item in (values or []) if str(item).strip()]
    if items:
        cmd.append(flag)
        cmd.extend(items)


def _run_step(*, step_name: str, command: list[str]) -> None:
    logger.info("Running serving step=%s command=%s", step_name, subprocess.list2cmdline(command))
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Serving step failed: {step_name} (exit_code={exc.returncode})") from exc


def _resolve_configured_path(
    *,
    config_path: Path,
    cfg: dict,
    override: Path | None,
    default_value: str,
    cfg_keys: tuple[str, ...],
) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    raw_value = cfg_get(cfg, *cfg_keys, default=default_value)
    resolved = resolve_path(config_path, str(raw_value)) if raw_value is not None and str(raw_value).strip() else None
    if resolved is None:
        raise ValueError(f"Unable to resolve configured path for keys={cfg_keys!r}.")
    return resolved


def _resolve_worker_count(*, cli_value: int, cfg_value: object) -> int:
    if int(cli_value) > 0:
        return int(cli_value)
    try:
        resolved = int(cfg_value) if cfg_value is not None else 0
    except (TypeError, ValueError):
        resolved = 0
    return max(0, resolved)


def _policy_builder_commands(
    *,
    python_executable: str,
    config_path: Path,
    cfg: dict,
    policy_csv: Path | None,
    feature_policy_csv: Path | None,
    composite_policy_csv: Path | None,
) -> list[tuple[str, list[str]]]:
    registry_path = _resolve_configured_path(
        config_path=config_path,
        cfg=cfg,
        override=None,
        default_value="MacroLayer/macro_metric_registry_full.csv",
        cfg_keys=("registry_csv",),
    )
    country_metadata_path = _resolve_configured_path(
        config_path=config_path,
        cfg=cfg,
        override=None,
        default_value="MacroLayer/macro_country_metadata_seed.csv",
        cfg_keys=("country_metadata_csv",),
    )
    metric_policy_path = _resolve_configured_path(
        config_path=config_path,
        cfg=cfg,
        override=policy_csv,
        default_value="MacroLayer/macro_metric_policy.csv",
        cfg_keys=("metric_policy_csv",),
    )
    feature_policy_path = _resolve_configured_path(
        config_path=config_path,
        cfg=cfg,
        override=feature_policy_csv,
        default_value="MacroLayer/macro_feature_policy.csv",
        cfg_keys=("feature_policy_csv",),
    )
    composite_policy_path = _resolve_configured_path(
        config_path=config_path,
        cfg=cfg,
        override=composite_policy_csv,
        default_value="MacroLayer/macro_composite_policy.csv",
        cfg_keys=("composite_policy_csv",),
    )
    return [
        (
            "metric_policy",
            [
                python_executable,
                str(SCRIPT_DIR / "build_macro_metric_policy.py"),
                "--registry",
                str(registry_path),
                "--country-metadata",
                str(country_metadata_path),
                "--output",
                str(metric_policy_path),
            ],
        ),
        (
            "feature_policy",
            [
                python_executable,
                str(SCRIPT_DIR / "build_macro_feature_policy.py"),
                "--metric-policy",
                str(metric_policy_path),
                "--country-metadata",
                str(country_metadata_path),
                "--output",
                str(feature_policy_path),
            ],
        ),
        (
            "composite_policy",
            [
                python_executable,
                str(SCRIPT_DIR / "build_macro_composite_policy.py"),
                "--feature-policy",
                str(feature_policy_path),
                "--output",
                str(composite_policy_path),
            ],
        ),
    ]


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    # Load once so the wrapper follows the same default-config resolution as the child scripts
    # and can use config-backed defaults for wrapper-level behavior.
    config_path, cfg = load_macro_raw_config(args.config)

    if args.metric_keys:
        logger.info(
            "metric_keys filter supplied. The wrapper will rebuild PIT/features for the selected metrics, "
            "then downstream aggregate tables will be rebuilt from the current full serving state."
        )

    if args.rebuild_policies:
        for step_name, command in _policy_builder_commands(
            python_executable=str(args.python_executable),
            config_path=config_path,
            cfg=cfg,
            policy_csv=args.policy_csv,
            feature_policy_csv=args.feature_policy_csv,
            composite_policy_csv=args.composite_policy_csv,
        ):
            _run_step(step_name=step_name, command=command)

    common_cfg: list[str] = ["--config", str(config_path)]
    pit_workers = _resolve_worker_count(
        cli_value=int(args.pit_workers),
        cfg_value=cfg_get(cfg, "serving", "pit_workers", default=0),
    )
    feature_workers = _resolve_worker_count(
        cli_value=int(args.feature_workers),
        cfg_value=cfg_get(cfg, "serving", "feature_workers", default=0),
    )

    calendar_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_calendar_daily.py"), *common_cfg]
    _append_path_arg(calendar_cmd, "--raw-db-path", args.raw_db_path)
    _append_path_arg(calendar_cmd, "--serving-db-path", args.serving_db_path)
    _append_text_arg(calendar_cmd, "--start-date", args.start_date)
    _append_text_arg(calendar_cmd, "--end-date", args.end_date)
    _run_step(step_name="calendar", command=calendar_cmd)

    pit_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_observation_daily_pit.py"), *common_cfg]
    _append_path_arg(pit_cmd, "--raw-db-path", args.raw_db_path)
    _append_path_arg(pit_cmd, "--serving-db-path", args.serving_db_path)
    _append_path_arg(pit_cmd, "--policy-csv", args.policy_csv)
    _append_text_arg(pit_cmd, "--start-date", args.start_date)
    _append_text_arg(pit_cmd, "--end-date", args.end_date)
    _append_multi_arg(pit_cmd, "--metric-keys", args.metric_keys)
    if pit_workers > 0:
        pit_cmd.extend(["--workers", str(pit_workers)])
    _run_step(step_name="observation_daily_pit", command=pit_cmd)

    if not args.skip_metric_latest:
        latest_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_metric_latest.py"), *common_cfg]
        _append_path_arg(latest_cmd, "--serving-db-path", args.serving_db_path)
        _run_step(step_name="metric_latest", command=latest_cmd)

    if not args.skip_country_coverage:
        coverage_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_country_coverage_daily.py"), *common_cfg]
        _append_path_arg(coverage_cmd, "--raw-db-path", args.raw_db_path)
        _append_path_arg(coverage_cmd, "--serving-db-path", args.serving_db_path)
        _append_path_arg(coverage_cmd, "--policy-csv", args.policy_csv)
        _append_text_arg(coverage_cmd, "--start-date", args.start_date)
        _append_text_arg(coverage_cmd, "--end-date", args.end_date)
        _run_step(step_name="country_coverage_daily", command=coverage_cmd)

    if not args.skip_feature:
        feature_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_features.py"), *common_cfg]
        _append_path_arg(feature_cmd, "--raw-db-path", args.raw_db_path)
        _append_path_arg(feature_cmd, "--serving-db-path", args.serving_db_path)
        _append_path_arg(feature_cmd, "--feature-policy-csv", args.feature_policy_csv)
        _append_text_arg(feature_cmd, "--start-date", args.start_date)
        _append_text_arg(feature_cmd, "--end-date", args.end_date)
        _append_multi_arg(feature_cmd, "--metric-keys", args.metric_keys)
        if feature_workers > 0:
            feature_cmd.extend(["--workers", str(feature_workers)])
        _run_step(step_name="feature_layer", command=feature_cmd)

    if not args.skip_composites:
        composite_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_composites.py"), *common_cfg]
        _append_path_arg(composite_cmd, "--serving-db-path", args.serving_db_path)
        _append_path_arg(composite_cmd, "--composite-policy-csv", args.composite_policy_csv)
        _append_text_arg(composite_cmd, "--start-date", args.start_date)
        _append_text_arg(composite_cmd, "--end-date", args.end_date)
        _append_multi_arg(composite_cmd, "--composite-keys", args.composite_keys)
        _run_step(step_name="composite_layer", command=composite_cmd)

    if not args.skip_probabilities:
        probability_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_probabilities.py"), *common_cfg]
        _append_path_arg(probability_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(probability_cmd, "--start-date", args.start_date)
        _append_text_arg(probability_cmd, "--end-date", args.end_date)
        _append_multi_arg(probability_cmd, "--probability-keys", args.probability_keys)
        _run_step(step_name="probability_layer", command=probability_cmd)

    if not args.skip_regime_raw:
        regime_raw_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_regime_raw.py"), *common_cfg]
        _append_path_arg(regime_raw_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(regime_raw_cmd, "--start-date", args.start_date)
        _append_text_arg(regime_raw_cmd, "--end-date", args.end_date)
        _run_step(step_name="regime_raw_layer", command=regime_raw_cmd)

    if not args.skip_regime_smoothed:
        regime_smoothed_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_regime_smoothed.py"), *common_cfg]
        _append_path_arg(regime_smoothed_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(regime_smoothed_cmd, "--start-date", args.start_date)
        _append_text_arg(regime_smoothed_cmd, "--end-date", args.end_date)
        _run_step(step_name="regime_smoothed_layer", command=regime_smoothed_cmd)

    if not args.skip_regime_decision:
        regime_decision_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_regime_decision.py"), *common_cfg]
        _append_path_arg(regime_decision_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(regime_decision_cmd, "--start-date", args.start_date)
        _append_text_arg(regime_decision_cmd, "--end-date", args.end_date)
        _run_step(step_name="regime_decision_layer", command=regime_decision_cmd)

    if not args.skip_industry_macro:
        industry_macro_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_industry_fit.py"), *common_cfg]
        _append_path_arg(industry_macro_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(industry_macro_cmd, "--start-date", args.start_date)
        _append_text_arg(industry_macro_cmd, "--end-date", args.end_date)
        _run_step(step_name="industry_macro_layer", command=industry_macro_cmd)

    if not args.skip_country_macro:
        country_macro_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_country_fit.py"), *common_cfg]
        _append_path_arg(country_macro_cmd, "--raw-db-path", args.raw_db_path)
        _append_path_arg(country_macro_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(country_macro_cmd, "--start-date", args.start_date)
        _append_text_arg(country_macro_cmd, "--end-date", args.end_date)
        _run_step(step_name="country_macro_layer", command=country_macro_cmd)

    if not args.skip_stock_macro_overlay:
        stock_overlay_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_stock_overlay.py"), *common_cfg]
        _append_path_arg(stock_overlay_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(stock_overlay_cmd, "--start-date", args.start_date)
        _append_text_arg(stock_overlay_cmd, "--end-date", args.end_date)
        _run_step(step_name="stock_macro_overlay", command=stock_overlay_cmd)

    if not args.skip_portfolio_inputs:
        portfolio_input_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_portfolio_inputs.py"), *common_cfg]
        _append_path_arg(portfolio_input_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(portfolio_input_cmd, "--start-date", args.start_date)
        _append_text_arg(portfolio_input_cmd, "--end-date", args.end_date)
        _run_step(step_name="portfolio_input_layer", command=portfolio_input_cmd)

    if not args.skip_stock_sleeve_targets:
        stock_sleeve_target_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_stock_sleeve_targets.py"), *common_cfg]
        _append_path_arg(stock_sleeve_target_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(stock_sleeve_target_cmd, "--start-date", args.start_date)
        _append_text_arg(stock_sleeve_target_cmd, "--end-date", args.end_date)
        _run_step(step_name="stock_sleeve_target_layer", command=stock_sleeve_target_cmd)

    if not args.skip_foreign_sleeve_budget:
        foreign_sleeve_budget_cmd = [str(args.python_executable), str(SCRIPT_DIR / "build_macro_foreign_sleeve_budget.py"), *common_cfg]
        _append_path_arg(foreign_sleeve_budget_cmd, "--serving-db-path", args.serving_db_path)
        _append_text_arg(foreign_sleeve_budget_cmd, "--start-date", args.start_date)
        _append_text_arg(foreign_sleeve_budget_cmd, "--end-date", args.end_date)
        _run_step(step_name="foreign_sleeve_budget_layer", command=foreign_sleeve_budget_cmd)

    optimizer_cfg = dict(cfg_get(cfg, "optimizer_integration_layer", default={}) or {})
    optimizer_enabled = parse_boolish(optimizer_cfg.get("enabled"), default=False)
    if optimizer_enabled and not args.skip_final_optimizer:
        final_optimizer_cmd = [str(args.python_executable), str(SCRIPT_DIR / "run_macro_optimizer_integration.py"), *common_cfg]
        _run_step(step_name="optimizer_integration_layer", command=final_optimizer_cmd)
    elif not optimizer_enabled:
        logger.info("Skipping optimizer_integration_layer because optimizer_integration_layer.enabled=false.")

    logger.info("Macro serving pipeline completed successfully.")


if __name__ == "__main__":
    main()
