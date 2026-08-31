#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, cast


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from biotech_index.core.calibration_metrics import finite_float  # noqa: E402

from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.portfolio_governance import (  # noqa: E402
    ProfitabilityPromotionRules,
    decide_profitability_promotion,
    evaluate_champion_challenger_monitoring,
)
from biotech_index.core.portfolio_profitability import (  # noqa: E402
    ReplayCostModel,
    ReplayResult,
    TerminalRecovery,
    compare_daily_replays,
    run_daily_portfolio_replay,
    summarize_daily_replay,
    targets_from_selection_rows,
)
from biotech_index.core.portfolio_replay_verification import (  # noqa: E402
    ReplayVerificationSettings,
    compare_replay_payloads,
    replay_normalized_artifacts,
)


LOGGER = logging.getLogger("biotech_portfolio_profitability")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FRAMEWORK_VERSION = "biotech_net_profitability_replay_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay frozen biotech walk-forward selections as daily net-of-cost portfolios, compare "
            "challenger and production terminal wealth, and produce staged promotion evidence."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--calibration-output-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    materialized = [dict(row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for row in materialized:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_iso_date(raw: object, *, label: str) -> date:
    try:
        return date.fromisoformat(str(raw or "").strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {raw!r}") from exc


def bool_value(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def float_value(raw: object, default: float) -> float:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value == value and abs(value) != float("inf") else default


def load_cost_model(config: dict[str, Any]) -> ReplayCostModel:
    raw = cfg_get(config, "calibration.walk_forward.profitability_replay.execution", {}) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("calibration.walk_forward.profitability_replay.execution must be a mapping")
    return ReplayCostModel(
        initial_capital=float_value(raw.get("initial_capital"), 1_000_000.0),
        base_one_way_cost_bps=float_value(raw.get("base_one_way_cost_bps"), 20.0),
        benchmark_one_way_cost_bps=float_value(raw.get("benchmark_one_way_cost_bps"), 2.0),
        market_impact_coefficient_bps=float_value(raw.get("market_impact_coefficient_bps"), 25.0),
        max_market_impact_bps=float_value(raw.get("max_market_impact_bps"), 75.0),
        max_adv_participation_pct=float_value(raw.get("max_adv_participation_pct"), 2.0),
        min_trade_notional=float_value(raw.get("min_trade_notional"), 25.0),
        execution_lag_bars=max(1, int(float_value(raw.get("execution_lag_bars"), 1.0))),
        periods_per_year=max(1, int(float_value(raw.get("periods_per_year"), 252.0))),
        liquidate_at_end=bool_value(raw.get("liquidate_at_fold_end"), True),
    )

def cost_model_from_mapping(raw: Mapping[str, object]) -> ReplayCostModel:
    return ReplayCostModel(
        initial_capital=float_value(raw.get("initial_capital"), 1_000_000.0),
        base_one_way_cost_bps=float_value(raw.get("base_one_way_cost_bps"), 20.0),
        benchmark_one_way_cost_bps=float_value(raw.get("benchmark_one_way_cost_bps"), 2.0),
        market_impact_coefficient_bps=float_value(raw.get("market_impact_coefficient_bps"), 25.0),
        max_market_impact_bps=float_value(raw.get("max_market_impact_bps"), 75.0),
        max_adv_participation_pct=float_value(raw.get("max_adv_participation_pct"), 2.0),
        min_trade_notional=float_value(raw.get("min_trade_notional"), 25.0),
        execution_lag_bars=max(1, int(float_value(raw.get("execution_lag_bars"), 1.0))),
        periods_per_year=max(1, int(float_value(raw.get("periods_per_year"), 252.0))),
        liquidate_at_end=bool_value(raw.get("liquidate_at_end"), True),
    )



def load_promotion_rules(config: dict[str, Any]) -> ProfitabilityPromotionRules:
    raw = cfg_get(config, "calibration.walk_forward.profitability_replay.promotion", {}) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("calibration.walk_forward.profitability_replay.promotion must be a mapping")
    defaults = ProfitabilityPromotionRules()
    kwargs: dict[str, object] = {}
    for field_name, field_value in asdict(defaults).items():
        raw_value = raw.get(field_name, field_value)
        if isinstance(field_value, bool):
            kwargs[field_name] = bool_value(raw_value, field_value)
        elif isinstance(field_value, int):
            kwargs[field_name] = int(float_value(raw_value, float(field_value)))
        else:
            kwargs[field_name] = float_value(raw_value, float(field_value))
    return ProfitabilityPromotionRules(**cast(Any, kwargs))


def effective_trial_count(calibration_dir: Path, config: dict[str, Any]) -> int:
    grid_path = calibration_dir / "walk_forward_candidate_metrics.csv"
    grid_rows = read_csv(grid_path) if grid_path.exists() else []
    base_trials = {
        (
            row.get("calibration_cohort", ""),
            row.get("candidate_id", ""),
            row.get("selection_policy_name", ""),
            row.get("top_n", ""),
        )
        for row in grid_rows
        if row.get("candidate_id")
    }
    adaptive = cfg_get(config, "calibration.walk_forward.adaptive_selection", {}) or {}
    score_candidates = adaptive.get("min_score_pct_of_top_candidates", []) if isinstance(adaptive, Mapping) else []
    name_candidates = adaptive.get("max_name_candidates", []) if isinstance(adaptive, Mapping) else []
    threshold_trials = max(1, len(score_candidates) * len(name_candidates))
    optuna_path = calibration_dir / "optuna_fold_trials.csv"
    optuna_trials = len(read_csv(optuna_path)) if optuna_path.exists() else 0
    return max(1, len(base_trials) * threshold_trials + optuna_trials)


def observation_path(config: dict[str, Any], config_path: Path, calibration_dir: Path) -> Path | None:
    manifest_path = calibration_dir / "walk_forward_run_manifest.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            provenance = payload.get("source_provenance") or {}
            if isinstance(provenance, Mapping):
                candidate = Path(str(provenance.get("observation_csv") or "")).expanduser()
                if candidate.exists():
                    return candidate.resolve()
    contract_path = calibration_dir / "production_policy_contract_candidate.json"
    if contract_path.exists():
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            provenance = payload.get("source_provenance") or {}
            if isinstance(provenance, Mapping):
                candidate = Path(str(provenance.get("observation_csv") or "")).expanduser()
                if candidate.exists():
                    return candidate.resolve()

    return resolve_optional_path(
        cfg_get(config, "calibration.walk_forward.observations_csv", ""),
        base_dir=config_path.parent,
    )


def load_adv_lookup(path: Path | None, needed: set[tuple[str, str]]) -> dict[tuple[str, str], float]:
    if path is None or not path.exists() or not needed:
        return {}
    output: dict[tuple[str, str], float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (str(row.get("asof_date") or "").strip(), str(row.get("ticker") or "").strip().upper())
            if key not in needed:
                continue
            value = float_value(row.get("avg_dollar_volume_20d"), -1.0)
            if value > 0.0:
                output[key] = value
    return output


def chain_results(results: list[tuple[str, ReplayResult]], *, model: ReplayCostModel) -> ReplayResult:
    if not results:
        raise ValueError("Cannot chain an empty replay result list")
    daily_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    wealth = model.initial_capital
    seen_dates: set[str] = set()
    for fold_id, result in results:
        for row in result.daily_rows:
            row_date = str(row.get("date") or "")
            if row_date in seen_dates:
                raise ValueError(f"Profitability fold date overlap: {row_date}")
            seen_dates.add(row_date)
            daily_return = finite_float(row.get("daily_net_return"))
            if daily_return is None:
                raise ValueError(f"Profitability replay row has no daily return: {row}")
            wealth *= 1.0 + daily_return
            daily_rows.append(
                {
                    **dict(row),
                    "fold_id": fold_id,
                    "equity": wealth,
                }
            )
        trade_rows.extend({**dict(row), "fold_id": fold_id} for row in result.trade_rows)
    summary = summarize_daily_replay(
        daily_rows,
        initial_capital=model.initial_capital,
        periods_per_year=model.periods_per_year,
    )
    for field in (
        "total_transaction_cost",
        "gross_traded_notional",
        "trade_count",
        "partial_fill_count",
        "missing_adv_trade_count",
        "missing_target_price_count",
    ):
        summary[field] = sum(float_value(result.summary.get(field), 0.0) for _, result in results)
    total_cost = float_value(summary.get("total_transaction_cost"), 0.0)
    gross_notional = float_value(summary.get("gross_traded_notional"), 0.0)
    summary["total_transaction_cost_pct_initial"] = round(
        100.0 * total_cost / model.initial_capital,
        6,
    )
    summary["gross_turnover_multiple"] = round(
        gross_notional / model.initial_capital,
        6,
    )
    return ReplayResult(tuple(daily_rows), tuple(trade_rows), summary)


def decision_markdown(decision: Mapping[str, object]) -> str:
    fields = (
        ("Status", "profitability_promotion_status"),
        ("Authorized", "profitability_promotion_authorized"),
        ("Provisional", "profitability_provisional_promotion"),
        ("Active weight cap", "profitability_active_weight_cap"),
        ("Composite score", "profitability_composite_score"),
        ("Candidate terminal wealth", "candidate_terminal_wealth"),
        ("Production terminal wealth", "incumbent_terminal_wealth"),
        ("Candidate CAGR", "candidate_cagr_pct"),
        ("Production CAGR", "incumbent_cagr_pct"),
        ("Candidate PF", "candidate_profit_factor"),
        ("Production PF", "incumbent_profit_factor"),
        ("Candidate max drawdown", "candidate_max_drawdown_pct"),
        ("Production max drawdown", "incumbent_max_drawdown_pct"),
        ("Deflated Sharpe probability", "candidate_deflated_sharpe_probability"),
        ("Paired delta bootstrap LCB", "paired_annualized_delta_bootstrap_lcb_pct"),
    )
    lines = ["# Biotech Net-of-Cost Profitability Decision", "", "| Metric | Value |", "|---|---:|"]
    lines.extend(f"| {label} | {decision.get(key, '')} |" for label, key in fields)
    lines.extend(["", f"Reasons: `{decision.get('profitability_reason_codes', '')}`", ""])
    return "\n".join(lines)
def verification_settings_from_manifest(manifest: Mapping[str, object]) -> ReplayVerificationSettings:
    raw = manifest.get("verification_settings") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("Profitability manifest verification_settings must be a mapping")
    return ReplayVerificationSettings(
        benchmark_ticker=str(raw.get("benchmark_ticker") or "XBI").strip().upper(),
        effective_trials=max(1, int(float_value(raw.get("effective_trials"), 1.0))),
        bootstrap_iterations=max(1, int(float_value(raw.get("bootstrap_iterations"), 500.0))),
        bootstrap_block_days=max(1, int(float_value(raw.get("bootstrap_block_days"), 20.0))),
        bootstrap_seed=int(float_value(raw.get("bootstrap_seed"), 1729.0)),
        numeric_tolerance=float_value(raw.get("numeric_tolerance"), 1e-6),
    )


def verify_output_dir(output_dir: Path) -> dict[str, object]:
    manifest_path = output_dir / "portfolio_profitability_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise ValueError("Profitability manifest root must be a mapping")
    cost_raw = manifest.get("cost_model") or {}
    if not isinstance(cost_raw, Mapping):
        raise ValueError("Profitability manifest cost_model must be a mapping")
    settings = verification_settings_from_manifest(manifest)
    actual = replay_normalized_artifacts(
        output_dir,
        model=cost_model_from_mapping(cost_raw),
        settings=settings,
    )
    expected_path = output_dir / "portfolio_profitability_comparison.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if not isinstance(expected, Mapping):
        raise ValueError("Persisted profitability comparison must be a mapping")
    result = compare_replay_payloads(expected, actual, tolerance=settings.numeric_tolerance)
    payload = {
        **result,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "independent_normalized_input_replay": True,
        "database_accessed_by_verifier": False,
        "expected_comparison_sha256": sha256_file(expected_path),
    }
    write_json(output_dir / "portfolio_profitability_verification.json", payload)
    if result["verification_status"] != "pass":
        raise ValueError(f"Independent profitability replay verification failed: {result['mismatches']}")
    return payload


def build_profitability_contract(
    calibration_dir: Path,
    *,
    decision: Mapping[str, object],
    comparison: Mapping[str, object],
    monitoring: Mapping[str, object],
) -> dict[str, object] | None:
    source_path = calibration_dir / "production_policy_contract_candidate.json"
    if not source_path.exists():
        return None
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Base production policy contract must be a JSON object")
    fold = payload.get("latest_primary_fold_contract") or {}
    live_ready = (
        payload.get("production_promotion_authorized") is True
        and payload.get("live_deployment_ready") is True
        and isinstance(fold, Mapping)
        and bool(fold)
    )
    profitability_authorized = decision.get("profitability_promotion_authorized") is True
    authorized = bool(profitability_authorized and live_ready)
    active_weight_cap = max(0.0, min(1.0, float_value(decision.get("profitability_active_weight_cap"), 0.0)))
    if authorized and isinstance(fold, dict):
        threshold = fold.get("threshold") or {}
        if not isinstance(threshold, dict):
            raise ValueError("Profitability contract threshold must be a mapping")
        calibrated_weight = max(0.0, min(1.0, float_value(threshold.get("active_weight"), 0.0)))
        governed_weight = min(calibrated_weight, active_weight_cap)
        threshold["calibrated_active_weight"] = calibrated_weight
        threshold["active_weight"] = governed_weight
        threshold["xbi_residual_weight"] = round(1.0 - governed_weight, 10)
        threshold["deployment_weight_cap_reason"] = (
            "provisional_profitability_cap" if governed_weight < calibrated_weight else "none"
        )
        fold["threshold"] = threshold

    payload["statistical_promotion_decision"] = payload.get("promotion_decision") or {}
    payload["promotion_decision"] = dict(decision)
    payload["profitability_evidence"] = dict(comparison)
    payload["profitability_monitoring_baseline"] = dict(monitoring)
    payload["production_promotion_authorized"] = authorized
    payload["activation_status"] = "candidate_requires_explicit_activation" if authorized else "not_authorized"
    payload["deployment_stage"] = (
        "provisional"
        if authorized and decision.get("profitability_provisional_promotion") is True
        else "full" if authorized else "shadow"
    )
    payload["profitability_active_weight_cap"] = active_weight_cap
    if profitability_authorized and not live_ready:
        payload["profitability_activation_block_reason"] = "live_scorer_parity_required"
    return payload




def main() -> int:
    args = parse_args()
    configure_utc_logging()
    if args.verify_only:
        verification_dir = args.output_dir or args.calibration_output_dir
        if verification_dir is None:
            raise ValueError("--verify-only requires --output-dir")
        output_dir = verification_dir.expanduser().resolve()
        verify_output_dir(output_dir)
        LOGGER.info("Independent normalized-input profitability verification passed: %s", output_dir)
        return 0
    if args.calibration_output_dir is None:
        raise ValueError("--calibration-output-dir is required unless --verify-only is used")

    config_path = args.config.expanduser().resolve()
    calibration_dir = args.calibration_output_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else calibration_dir
    config = load_yaml(config_path)
    model = load_cost_model(config)
    rules = load_promotion_rules(config)
    benchmark = str(
        cfg_get(config, "calibration.walk_forward.profitability_replay.benchmark_ticker", "XBI")
        or "XBI"
    ).strip().upper()
    primary_horizon = int(cfg_get(config, "calibration.walk_forward.primary_horizon", 120))
    bootstrap_raw = cfg_get(config, "calibration.walk_forward.profitability_replay.bootstrap", {}) or {}
    if not isinstance(bootstrap_raw, Mapping):
        raise ValueError("calibration.walk_forward.profitability_replay.bootstrap must be a mapping")

    selected_path = calibration_dir / "walk_forward_selected_tickers.csv"
    sleeve_path = calibration_dir / "adaptive_sleeve_allocation_replay.csv"
    fold_path = calibration_dir / "walk_forward_fold_manifest.csv"
    selected_rows = read_csv(selected_path)
    sleeve_rows = read_csv(sleeve_path)
    fold_rows = read_csv(fold_path)
    explicit_target_weight_field = (
        "portfolio_target_weight" if selected_rows and "portfolio_target_weight" in selected_rows[0] else None
    )
    primary_sleeves = [
        row for row in sleeve_rows if int(float_value(row.get("horizon_days"), 0.0)) == primary_horizon
    ]
    primary_folds = {
        str(row.get("fold_id") or ""): row
        for row in fold_rows
        if int(float_value(row.get("horizon_bars"), 0.0)) == primary_horizon
        and str(row.get("support_status") or "").upper() == "PASS"
    }
    if not primary_folds or not primary_sleeves:
        raise ValueError("No supported primary-horizon folds are available for profitability replay")

    candidate_selected = [
        row for row in selected_rows if row.get("evaluation_split") == "outer_test_candidate"
    ]
    incumbent_selected = [
        row for row in selected_rows if row.get("evaluation_split") == "outer_test_incumbent"
    ]
    needed_adv = {
        (str(row.get("asof_date") or ""), str(row.get("ticker") or "").upper())
        for row in (*candidate_selected, *incumbent_selected)
    }
    observations_path = observation_path(config, config_path, calibration_dir)
    adv_lookup = load_adv_lookup(observations_path, needed_adv)

    calibration_module = __import__("biotech_index.calibration_base", fromlist=["*"])
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    selected_tickers = {
        str(row.get("ticker") or "").strip().upper()
        for row in (*candidate_selected, *incumbent_selected)
        if str(row.get("ticker") or "").strip()
    }
    min_date = min(parse_iso_date(row["test_start"], label="test_start") for row in primary_folds.values())
    with connect(db_path) as conn:
        alias_map = calibration_module.load_calibration_ticker_alias_map(conn)
        market_tickers = {alias_map.get(ticker, ticker) for ticker in selected_tickers}
        market_tickers.add(benchmark)
        bars_by_ticker = calibration_module.load_bars(
            conn,
            tickers=market_tickers,
            min_date=min_date,
            market_sources=calibration_market_sources(config),
        )
        calibration_module.apply_delisted_price_series_overlay(
            conn,
            bars_by_ticker,
            price_ticker_alias=alias_map,
            min_date=min_date,
            config=config,
        )
    prices = {
        ticker: {bar.day: float(bar.close) for bar in bars}
        for ticker, bars in bars_by_ticker.items()
    }
    terminal_raw = calibration_module.load_terminal_events()
    terminal_events: dict[str, TerminalRecovery] = {}
    for ticker, event in terminal_raw.items():
        terminal_events[ticker] = TerminalRecovery(
            terminal_date=event.terminal_date,
            equity_recovery=event.equity_recovery,
            recovery_type=event.recovery_type,
            drop_otc_tape=event.drop_otc_tape,
        )
    for alias, canonical in alias_map.items():
        if canonical in terminal_events:
            terminal_events[alias] = terminal_events[canonical]

    target_input_rows: list[dict[str, object]] = []
    candidate_results: list[tuple[str, ReplayResult]] = []
    incumbent_results: list[tuple[str, ReplayResult]] = []
    fold_comparisons: list[dict[str, object]] = []
    daily_output: list[dict[str, object]] = []
    trade_output: list[dict[str, object]] = []
    fold_input_rows: list[dict[str, object]] = []
    trials = effective_trial_count(calibration_dir, config)

    for fold_id, fold in sorted(primary_folds.items()):
        fold_sleeves = [row for row in primary_sleeves if row.get("fold_id") == fold_id]
        evaluation_dates = sorted({str(row.get("asof_date") or "") for row in fold_sleeves})
        if not evaluation_dates:
            continue
        fold_input_rows.append(
            {
                "fold_id": fold_id,
                "start_date": str(fold["test_start"]),
                "end_date": str(fold["test_end"]),
            }
        )
        candidate_rows = [row for row in candidate_selected if row.get("fold_id") == fold_id]
        incumbent_rows = [row for row in incumbent_selected if row.get("fold_id") == fold_id]
        candidate_weight = {
            str(row.get("asof_date") or ""): float_value(row.get("active_stock_selection_weight"), 0.0)
            for row in fold_sleeves
        }
        incumbent_active_dates = {str(row.get("asof_date") or "") for row in incumbent_rows}
        incumbent_weight = {
            asof_date: (1.0 if asof_date in incumbent_active_dates else 0.0)
            for asof_date in evaluation_dates
        }
        candidate_targets = targets_from_selection_rows(
            candidate_rows,
            evaluation_dates,
            active_weight_by_date=candidate_weight,
            benchmark_ticker=benchmark,
            adv_lookup=adv_lookup,
            target_weight_field=explicit_target_weight_field,
        )
        incumbent_targets = targets_from_selection_rows(
            incumbent_rows,
            evaluation_dates,
            active_weight_by_date=incumbent_weight,
            benchmark_ticker=benchmark,
            adv_lookup=adv_lookup,
            target_weight_field=explicit_target_weight_field,
        )
        for strategy, targets in (("challenger", candidate_targets), ("production", incumbent_targets)):
            for target in targets:
                for ticker, weight in target.weights.items():
                    target_input_rows.append(
                        {
                            "fold_id": fold_id,
                            "strategy": strategy,
                            "signal_date": target.signal_date.isoformat(),
                            "ticker": ticker,
                            "target_weight": weight,
                            "avg_dollar_volume": target.adv_by_ticker.get(ticker, ""),
                        }
                    )
        fold_start = parse_iso_date(fold["test_start"], label="test_start")
        fold_end = parse_iso_date(fold["test_end"], label="test_end")
        candidate_result = run_daily_portfolio_replay(
            prices,
            candidate_targets,
            benchmark_ticker=benchmark,
            model=model,
            terminal_events=terminal_events,
            start_date=fold_start,
            end_date=fold_end,
        )
        incumbent_result = run_daily_portfolio_replay(
            prices,
            incumbent_targets,
            benchmark_ticker=benchmark,
            model=model,
            terminal_events=terminal_events,
            start_date=fold_start,
            end_date=fold_end,
        )
        candidate_results.append((fold_id, candidate_result))
        incumbent_results.append((fold_id, incumbent_result))
        comparison = compare_daily_replays(
            candidate_result,
            incumbent_result,
            effective_trials=trials,
            bootstrap_iterations=int(float_value(bootstrap_raw.get("iterations"), 500.0)),
            bootstrap_block_days=int(float_value(bootstrap_raw.get("block_days"), 20.0)),
            bootstrap_seed=int(float_value(bootstrap_raw.get("seed"), 1729.0)),
            periods_per_year=model.periods_per_year,
        )
        fold_comparisons.append({"fold_id": fold_id, **comparison})
        daily_output.extend(
            {"fold_id": fold_id, "strategy": "challenger", **dict(row)}
            for row in candidate_result.daily_rows
        )
        daily_output.extend(
            {"fold_id": fold_id, "strategy": "production", **dict(row)}
            for row in incumbent_result.daily_rows
        )
        trade_output.extend(
            {"fold_id": fold_id, "strategy": "challenger", **dict(row)}
            for row in candidate_result.trade_rows
        )
        trade_output.extend(
            {"fold_id": fold_id, "strategy": "production", **dict(row)}
            for row in incumbent_result.trade_rows
        )

    aggregate_candidate = chain_results(candidate_results, model=model)
    aggregate_incumbent = chain_results(incumbent_results, model=model)
    aggregate_comparison = compare_daily_replays(
        aggregate_candidate,
        aggregate_incumbent,
        effective_trials=trials,
        bootstrap_iterations=int(float_value(bootstrap_raw.get("iterations"), 500.0)),
        bootstrap_block_days=int(float_value(bootstrap_raw.get("block_days"), 20.0)),
        bootstrap_seed=int(float_value(bootstrap_raw.get("seed"), 1729.0)),
        periods_per_year=model.periods_per_year,
    )
    decision = decide_profitability_promotion(aggregate_comparison, fold_comparisons, rules)
    decision_payload = decision.as_dict()
    base_contract_path = calibration_dir / "production_policy_contract_candidate.json"
    base_contract: Mapping[str, object] = {}
    if base_contract_path.exists():
        loaded_contract = json.loads(base_contract_path.read_text(encoding="utf-8"))
        if not isinstance(loaded_contract, Mapping):
            raise ValueError("Base production policy contract must be a JSON object")
        base_contract = loaded_contract
    primary_fold_contract = base_contract.get("latest_primary_fold_contract") or {}
    contract_activation_authorized = bool(
        decision.authorized
        and base_contract.get("production_promotion_authorized") is True
        and base_contract.get("live_deployment_ready") is True
        and isinstance(primary_fold_contract, Mapping)
        and primary_fold_contract
    )
    monitoring_raw = cfg_get(config, "calibration.walk_forward.monitoring.rollback_triggers", {}) or {}
    monitoring = evaluate_champion_challenger_monitoring(
        aggregate_comparison,
        min_live_paired_days=int(float_value(monitoring_raw.get("min_live_paired_dates"), 20.0)),
        max_drawdown_deterioration_pct=float_value(
            monitoring_raw.get("max_drawdown_deterioration_pct"),
            rules.max_drawdown_deterioration_pct,
        ),
        max_daily_cvar_deterioration_pct=float_value(
            monitoring_raw.get("max_daily_cvar_deterioration_pct"),
            rules.max_daily_cvar_deterioration_pct,
        ),
        policy_hash_consistent=True,
        contract_activation_authorized=contract_activation_authorized,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    replay_tickers = selected_tickers | {benchmark}
    replay_start = min(parse_iso_date(fold["test_start"], label="test_start") for fold in primary_folds.values())
    replay_end = max(parse_iso_date(fold["test_end"], label="test_end") for fold in primary_folds.values())
    price_rows = [
        {"ticker": ticker, "bar_date": day.isoformat(), "close": close}
        for ticker, series in sorted(prices.items())
        for day, close in sorted(series.items())
        if ticker in replay_tickers and replay_start <= day <= replay_end
    ]
    terminal_rows = [
        {
            "ticker": ticker,
            "terminal_date": event.terminal_date.isoformat(),
            "equity_recovery": event.equity_recovery,
            "recovery_type": event.recovery_type,
            "drop_otc_tape": int(event.drop_otc_tape),
        }
        for ticker, event in sorted(terminal_events.items())
        if ticker in replay_tickers
    ]
    write_csv(output_dir / "portfolio_replay_targets.csv", target_input_rows)
    write_csv(output_dir / "portfolio_replay_price_inputs.csv", price_rows)
    write_csv(output_dir / "portfolio_replay_terminal_events.csv", terminal_rows)
    write_csv(output_dir / "portfolio_replay_folds.csv", fold_input_rows)
    write_csv(output_dir / "portfolio_profitability_daily.csv", daily_output)
    write_csv(output_dir / "portfolio_profitability_trades.csv", trade_output)
    write_csv(output_dir / "portfolio_profitability_fold_comparisons.csv", fold_comparisons)
    write_csv(output_dir / "portfolio_profitability_comparison.csv", [aggregate_comparison])
    write_json(output_dir / "portfolio_profitability_comparison.json", aggregate_comparison)
    write_json(output_dir / "profitability_promotion_decision.json", decision_payload)
    write_json(output_dir / "champion_challenger_monitoring_baseline.json", monitoring)
    (output_dir / "profitability_promotion_decision.md").write_text(
        decision_markdown(decision_payload),
        encoding="utf-8",
    )

    profitability_contract = build_profitability_contract(
        calibration_dir,
        decision=decision_payload,
        comparison=aggregate_comparison,
        monitoring=monitoring,
    )
    if profitability_contract is not None:
        write_json(
            output_dir / "production_policy_contract_profitability_candidate.json",
            profitability_contract,
        )

    source_paths = [selected_path, sleeve_path, fold_path]
    normalized_paths = [
        output_dir / "portfolio_replay_targets.csv",
        output_dir / "portfolio_replay_price_inputs.csv",
        output_dir / "portfolio_replay_terminal_events.csv",
        output_dir / "portfolio_replay_folds.csv",
    ]
    result_paths = [
        output_dir / "portfolio_profitability_daily.csv",
        output_dir / "portfolio_profitability_trades.csv",
        output_dir / "portfolio_profitability_fold_comparisons.csv",
        output_dir / "portfolio_profitability_comparison.json",
        output_dir / "profitability_promotion_decision.json",
    ]
    verification_settings = {
        "benchmark_ticker": benchmark,
        "effective_trials": trials,
        "bootstrap_iterations": int(float_value(bootstrap_raw.get("iterations"), 500.0)),
        "bootstrap_block_days": int(float_value(bootstrap_raw.get("block_days"), 20.0)),
        "bootstrap_seed": int(float_value(bootstrap_raw.get("seed"), 1729.0)),
        "numeric_tolerance": 1e-6,
    }

    manifest = {
        "status": "success",
        "framework_version": FRAMEWORK_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "primary_horizon": primary_horizon,
        "benchmark_ticker": benchmark,
        "database_path": str(db_path),
        "database_size": db_path.stat().st_size,
        "database_mtime_ns": db_path.stat().st_mtime_ns,
        "observations_path": "" if observations_path is None else str(observations_path),
        "effective_trial_count": trials,
        "cost_model": asdict(model),
        "promotion_rules": asdict(rules),
        "verification_settings": verification_settings,
        "source_artifacts": {
            path.name: {"path": str(path), "sha256": sha256_file(path)} for path in source_paths
        },
        "normalized_inputs": {
            path.name: {"path": str(path), "sha256": sha256_file(path)} for path in normalized_paths
        },
        "result_artifacts": {
            path.name: {"path": str(path), "sha256": sha256_file(path)} for path in result_paths
        },
        "replay_summary": aggregate_comparison,
        "promotion_decision": decision_payload,
        "monitoring_baseline": monitoring,
    }
    write_json(output_dir / "portfolio_profitability_manifest.json", manifest)
    verification = verify_output_dir(output_dir)
    if profitability_contract is not None:
        profitability_contract["profitability_replay_verification"] = verification
        profitability_contract_path = output_dir / "production_policy_contract_profitability_candidate.json"
        write_json(profitability_contract_path, profitability_contract)
        manifest["result_artifacts"][profitability_contract_path.name] = {
            "path": str(profitability_contract_path),
            "sha256": sha256_file(profitability_contract_path),
        }

    manifest["independent_verification"] = verification
    verification_path = output_dir / "portfolio_profitability_verification.json"
    manifest["result_artifacts"][verification_path.name] = {
        "path": str(verification_path),
        "sha256": sha256_file(verification_path),
    }
    write_json(output_dir / "portfolio_profitability_manifest.json", manifest)
    LOGGER.info(
        "Biotech profitability replay complete: folds=%d authorized=%s provisional=%s output=%s",
        len(fold_comparisons),
        decision.authorized,
        decision.provisional,
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
