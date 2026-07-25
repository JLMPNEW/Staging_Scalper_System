#!/usr/bin/env python3
"""Stage 11 rank-reconstitution long replay.

At each signal close, incumbents are retained only inside a wider rank buffer.
Exits and replacements execute at the next adjusted open. The replay supports
unconditional, V1-gated, and H1-gated score selection without changing the
production macro source or the fixed-entry 16g baseline.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.backtest.rank_reconstitution import (  # noqa: E402
    circular_block_mean_ci,
    effective_sample_size,
    run_rank_reconstitution,
    selftest as engine_selftest,
)
from portfolio_layer.backtest.walkforward_common import perf_stats  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import (  # noqa: E402
    open_macro_serving_db,
    single_latest_regime_row,
    single_latest_row,
)
from portfolio_layer.research.stage11_common import (  # noqa: E402
    load_lockbox,
    manifest_file_errors,
    mean_t_hac,
)


LOGGER = logging.getLogger("rank_reconstitution_long")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
H1_MODEL_VERSION = "macro_regime_h1_hybrid_v1"
DEFAULT_PIPELINES = [
    "semiconductors",
    "software_infrastructure",
    "technology_hardware",
    "biotech",
    "med_devices",
    "defense",
]
TRADE_FIELDS = [
    "ticker",
    "source_pipeline",
    "signal_date",
    "entry_date",
    "exit_date",
    "entry_score_z",
    "entry_rank_pct",
    "entry_weight",
    "holding_days",
    "exit_reason",
    "net_return",
    "selection_alpha_net",
    "selection_alpha_equal_weight_net",
]
COVERAGE_DETAIL_FIELDS = [
    "source_pipeline",
    "ticker",
    "candidate_entries",
    "candidate_entries_with_open",
    "missing_open_with_sealed_close",
    "missing_open_without_sealed_close",
    "execution_panel_has_ticker",
    "first_missing_date",
    "last_missing_date",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the rank-reconstitution long arm.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument("--parameter-file", type=Path, default=None)
    parser.add_argument("--signal-from", default=None)
    parser.add_argument("--signal-to", default=None)
    parser.add_argument("--evaluation-json", type=Path, default=None)
    parser.add_argument("--pipelines", default=",".join(DEFAULT_PIPELINES))
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _latest(root: Path, marker: str, wanted: str | None) -> Path | None:
    if wanted:
        candidate = root / wanted
        return candidate if (candidate / marker).exists() else None
    if not root.exists():
        return None
    builds = sorted(
        path for path in root.iterdir() if path.is_dir() and (path / marker).exists()
    )
    return builds[-1] if builds else None


def _read_ohlcv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"date", "ticker", "adj_open"}
    missing = required - set(frame)
    if missing:
        raise ValueError(f"Execution panel lacks {sorted(missing)}")
    frame["date"] = frame["date"].astype(str).str.slice(0, 10)
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    if frame.duplicated(["date", "ticker"]).any():
        raise ValueError("Execution panel has duplicate date/ticker rows")
    frame["adj_open"] = pd.to_numeric(frame["adj_open"], errors="coerce")
    return frame.pivot(index="date", columns="ticker", values="adj_open").sort_index()


def _covered_label(row: Any) -> str:
    if row is None:
        return ""
    try:
        if float(row["coverage_flag"]) != 1.0:
            return ""
        return str(row["active_current_regime"] or "").strip().upper()
    except (TypeError, ValueError, IndexError, KeyError):
        return ""


def _parameter_overrides(path: Path | None) -> tuple[dict[str, Any], str | None]:
    if path is None:
        return {}, None
    resolved = path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if payload.get("acceptance") != "PASS" or not isinstance(
        payload.get("parameters"), dict
    ):
        raise ValueError(f"Rejected rank parameter artifact: {resolved}")
    return dict(payload["parameters"]), sha256_file(resolved)


def _exact_spreads(panel: pd.DataFrame) -> dict[tuple[str, str], float]:
    if "liquidity_half_spread_bps" not in panel:
        return {}
    values = pd.Series(
        pd.to_numeric(panel["liquidity_half_spread_bps"], errors="coerce"),
        index=panel.index,
        dtype=float,
    )
    valid = values.map(lambda value: bool(np.isfinite(value) and value >= 0))
    if "liquidity_join_available" in panel:
        valid &= panel["liquidity_join_available"].astype(str).isin(
            ("1", "1.0", "true", "True")
        )
    output: dict[tuple[str, str], float] = {}
    rows = panel.loc[valid, ["as_of_date", "ticker"]].copy()
    rows["spread"] = values.loc[valid]
    for key, group in rows.groupby(["as_of_date", "ticker"]):
        output[(str(key[0]), str(key[1]))] = float(group["spread"].median())
    return output


def _load_inputs(
    config_path: Path,
    *,
    panel_build: str | None,
    pipelines: list[str],
) -> tuple[
    dict[str, Any],
    Any,
    Path,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    list[str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    lockbox = load_lockbox(config, config_path)
    panel_dir = _latest(
        paths.output_dir / str(cfg_get(config, "calibration_panel.dir", "calibration_panel")),
        "calibration_panel_manifest.json",
        panel_build,
    )
    if panel_dir is None:
        raise FileNotFoundError("No sealed calibration panel")
    panel_path = panel_dir / "calibration_panel.csv"
    panel_manifest_path = panel_dir / "calibration_panel_manifest.json"
    panel_manifest = json.loads(panel_manifest_path.read_text(encoding="utf-8"))
    panel_errors = manifest_file_errors(
        panel_manifest, {"calibration_panel.csv": panel_path}
    )
    if panel_manifest.get("acceptance") != "PASS" or panel_errors:
        raise ValueError(f"Calibration panel rejected/stale: {panel_errors}")
    wanted = {
        "as_of_date",
        "ticker",
        "source_pipeline",
        "score_z_pipeline_date",
        "calibration_research_eligible",
        "sidecar_stage11_eligible",
        "survivorship_complete",
        "in_lockbox",
        "liquidity_join_available",
        "liquidity_half_spread_bps",
    }
    panel = pd.read_csv(panel_path, usecols=lambda column: column in wanted)
    panel["as_of_date"] = panel["as_of_date"].astype(str).str.slice(0, 10)
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()
    panel["score_z_pipeline_date"] = pd.to_numeric(
        panel["score_z_pipeline_date"], errors="coerce"
    )
    truthy = ("1", "1.0", "true", "True")
    eligible = panel["calibration_research_eligible"].astype(str).isin(truthy)
    if "sidecar_stage11_eligible" in panel:
        eligible |= panel["sidecar_stage11_eligible"].astype(str).isin(truthy)
    panel = panel.loc[
        eligible
        & panel["survivorship_complete"].astype(str).isin(truthy)
        & ~panel["in_lockbox"].astype(str).isin(truthy)
        & panel["source_pipeline"].isin(pipelines)
    ].copy()
    if panel.empty or panel.duplicated(["as_of_date", "ticker"]).any():
        raise ValueError("Admitted rank panel is empty or has duplicate date/ticker rows")

    survivorship_build = str(panel_manifest["survivorship_panel_build"])
    survivorship_dir = (
        paths.output_dir
        / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
        / survivorship_build
    )
    survivorship_manifest_path = survivorship_dir / "survivorship_manifest.json"
    prices_path = survivorship_dir / "prices_adjclose.csv"
    coverage_path = survivorship_dir / "ticker_coverage.csv"
    survivorship_manifest = json.loads(
        survivorship_manifest_path.read_text(encoding="utf-8")
    )
    survivorship_errors = manifest_file_errors(
        survivorship_manifest,
        {
            "prices_adjclose.csv": prices_path,
            "ticker_coverage.csv": coverage_path,
        },
    )
    if (
        survivorship_manifest.get("acceptance") != "PASS"
        or sha256_file(survivorship_manifest_path)
        != str(panel_manifest.get("survivorship_panel_manifest_sha256", ""))
        or survivorship_errors
    ):
        raise ValueError(f"Survivorship panel rejected/stale: {survivorship_errors}")
    prices = pd.read_csv(prices_path, index_col=0)
    prices.index = prices.index.astype(str).str.slice(0, 10)
    prices.columns = [str(column).upper().strip() for column in prices]
    prices = prices.loc[
        (prices.index >= lockbox["dev_window_start"])
        & (prices.index <= lockbox["dev_window_end"])
    ].sort_index()
    coverage = pd.read_csv(coverage_path).fillna("")
    terminal_dates = {
        str(row["ticker"]).strip().upper(): str(row["delist_date"])[:10]
        for _, row in coverage.iterrows()
        if str(row.get("status", "")) == "delisted_covered"
        and len(str(row.get("delist_date", ""))) >= 10
    }
    execution_dir = (
        paths.output_dir
        / str(cfg_get(config, "execution_ohlcv_panel.dir", "execution_ohlcv_panel"))
        / survivorship_build
    )
    execution_path = execution_dir / "prices_adjusted_ohlcv.csv.gz"
    execution_manifest_path = execution_dir / "execution_ohlcv_manifest.json"
    execution_manifest = json.loads(
        execution_manifest_path.read_text(encoding="utf-8")
    )
    execution_errors = manifest_file_errors(
        execution_manifest, {"prices_adjusted_ohlcv.csv.gz": execution_path}
    )
    if (
        execution_manifest.get("acceptance") != "PASS"
        or str(execution_manifest.get("survivorship_manifest_sha256", ""))
        != sha256_file(survivorship_manifest_path)
        or execution_errors
    ):
        raise ValueError(f"Execution panel rejected/stale: {execution_errors}")
    opens = _read_ohlcv(execution_path)
    calendar = list(prices.index)
    snapshots = sorted(set(panel["as_of_date"]) & set(calendar))

    before = sha256_file(paths.macro_serving_db_path)
    conn = open_macro_serving_db(paths.macro_serving_db_path)
    try:
        v1 = {
            day: _covered_label(
                single_latest_row(conn, "macro_regime_decision_daily", day)
            )
            for day in snapshots
        }
        h1 = {
            day: _covered_label(
                single_latest_regime_row(
                    conn,
                    source="h1",
                    run_as_of=day,
                    model_version=H1_MODEL_VERSION,
                )
            )
            for day in snapshots
        }
    finally:
        conn.close()
    after = sha256_file(paths.macro_serving_db_path)
    if before != after:
        raise RuntimeError("Macro serving DB changed while labels were loaded")
    inputs = {
        "config.yaml": sha256_file(config_path),
        "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
        "survivorship_manifest.json": sha256_file(survivorship_manifest_path),
        "execution_ohlcv_manifest.json": sha256_file(execution_manifest_path),
        "macro_serving.sqlite": after,
    }
    return (
        config,
        paths,
        panel_dir,
        panel,
        prices,
        opens,
        calendar,
        v1,
        h1,
        terminal_dates,
        inputs,
    )


def _summarize(
    result: dict[str, Any],
    *,
    max_holding_days: int,
    bootstrap_confidence: float,
    bootstrap_replications: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    daily = result["daily"]
    selection = [float(row["selection_alpha_net"]) for row in daily]
    net = [float(row["net_return"]) for row in daily]
    stress = [float(row["stress_net_return"]) for row in daily]
    years = max(len(selection) / 252.0, 1e-9)
    _mean, _se, active_t = mean_t_hac(selection, max_lag=max_holding_days)
    low, high = circular_block_mean_ci(
        selection,
        block_length=max_holding_days,
        confidence=bootstrap_confidence,
        replications=bootstrap_replications,
        seed=bootstrap_seed,
    )
    trade_values = np.asarray(
        [float(row["net_return"]) for row in result["trades"]], dtype=float
    )
    gains = float(trade_values[trade_values > 0].sum())
    losses = abs(float(trade_values[trade_values < 0].sum()))
    minimum_coverage = min(
        float(row["coverage_fraction"]) for row in result["coverage_by_sector"]
    )
    net_sharpe = float(perf_stats(net, ppy=252)["sharpe"])
    return {
        "trades": len(result["trades"]),
        "net_ann": sum(net) / years,
        "selection_alpha_ann": sum(selection) / years,
        "selection_alpha_equal_weight_ann": sum(
            float(value)
            for value in result["sector_equal_weight_selection_alpha"].values()
        )
        / years,
        "stress_net_ann": sum(stress) / years,
        "active_t": active_t,
        "selection_ci_low_ann": low * 252 if low is not None else None,
        "selection_ci_high_ann": high * 252 if high is not None else None,
        "effective_daily_observations": effective_sample_size(
            selection, max_lag=max_holding_days
        ),
        "profit_factor": gains / losses if losses > 0 else None,
        "positive_sectors": sum(
            float(value) > 0 for value in result["sector_selection_alpha"].values()
        ),
        "minimum_execution_coverage": minimum_coverage,
        "net_sharpe": net_sharpe if np.isfinite(net_sharpe) else None,
    }


def main() -> int:  # noqa: C901, PLR0912, PLR0915
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        engine_selftest()
        print("rank-reconstitution replay self-test: PASS")
        return 0
    config_path = args.config.expanduser().resolve()
    pipelines = [value.strip() for value in args.pipelines.split(",") if value.strip()]
    try:
        overrides, parameter_sha = _parameter_overrides(args.parameter_file)
        (
            config,
            paths,
            panel_dir,
            panel,
            prices,
            opens,
            calendar,
            v1,
            h1,
            terminal_dates,
            input_hashes,
        ) = _load_inputs(config_path, panel_build=args.panel_build, pipelines=pipelines)
        cfg = dict(cfg_get(config, "rank_reconstitution_long", {}) or {})
        cfg.update(overrides)
        entry_fraction = float(cfg.get("entry_fraction", 0.10))
        exit_fraction = float(cfg.get("exit_fraction", entry_fraction + 0.10))
        base_max_holding_days = int(cfg.get("max_holding_days", 126))
        holding_schedule = {
            str(day): int(value)
            for day, value in (
                cfg.get("max_holding_days_by_signal", {}) or {}
            ).items()
        }
        max_holding_days = max(
            [base_max_holding_days, *holding_schedule.values()]
        )
        regime_mode = str(cfg.get("regime_mode", "unconditional"))
        signal_every = max(1, int(cfg.get("signal_every_n_snapshots", 5)))
        snapshots = sorted(set(panel["as_of_date"]) & set(calendar))
        signals = snapshots[::signal_every]
        if args.signal_from:
            signals = [day for day in signals if day >= args.signal_from]
        if args.signal_to:
            signals = [day for day in signals if day <= args.signal_to]
        last_valid_pos = len(calendar) - max_holding_days - 2
        signals = [
            day for day in signals if calendar.index(day) <= last_valid_pos
        ]
        if not signals:
            raise ValueError("No outcome-complete rank signal dates")
        exact = _exact_spreads(panel)
        cost_cfg = cfg.get("long_costs", {}) or {}
        fallback = float(cost_cfg.get("historical_half_spread_fallback_bps", 15.0))
        stress_fallback = float(cost_cfg.get("stress_half_spread_fallback_bps", 30.0))
        stress_multiplier = float(
            cost_cfg.get("stress_observed_spread_multiplier", 1.5)
        )

        def resolve_spread(day: str, ticker: str) -> tuple[float, str]:
            value = exact.get((day, ticker))
            return (value, "panel_exact") if value is not None else (
                fallback,
                "conservative_fallback",
            )

        def stress_spread(value: float, source: str) -> float:
            return (
                value * stress_multiplier
                if source == "panel_exact"
                else max(value, stress_fallback)
            )

        aum = float(
            cost_cfg.get(
                "research_aum_usd",
                cfg_get(config, "transaction_costs.aum_usd", 300000),
            )
        )
        commission = float(
            cost_cfg.get(
                "commission_per_order_usd",
                cfg_get(
                    config,
                    "transaction_costs.commission_per_order.worst_case",
                    1.25,
                ),
            )
        )
        parameters = {
            "entry_fraction": entry_fraction,
            "exit_fraction": exit_fraction,
            "max_holding_days": max_holding_days,
            "max_holding_days_by_signal": holding_schedule,
            "regime_mode": regime_mode,
            "target_long_gross": float(cfg.get("target_long_gross", 0.95)),
            "max_position_weight": float(cfg.get("max_position_weight", 0.05)),
            "minimum_names_per_sector": int(cfg.get("minimum_names_per_sector", 2)),
            "supportive_regimes": list(cfg.get("supportive_regimes", ["HEATING_UP"])),
        }
        result = run_rank_reconstitution(
            panel=panel,
            prices=prices,
            opens=opens,
            calendar=calendar,
            signal_dates=signals,
            pipelines=pipelines,
            sector_etfs={
                str(key): str(value).upper().strip()
                for key, value in (
                    cfg_get(config, "risk_panel.sector_etf_map", {}) or {}
                ).items()
            },
            v1_labels=v1,
            h1_labels=h1,
            terminal_dates=terminal_dates,
            parameters=parameters,
            spread_resolver=resolve_spread,
            stressed_spread=stress_spread,
            commission_fraction=commission / aum,
            aum_usd=aum,
            commission_usd=commission,
            min_commission_fraction=float(
                cfg_get(config, "transaction_costs.min_position_commission_fraction", 0.005)
            ),
            absolute_name_cap=int(cfg.get("absolute_max_names_per_sector", 200)),
        )
        validation_cfg = cfg_get(config, "rank_reconstitution_calibration", {}) or {}
        summary = _summarize(
            result,
            max_holding_days=max_holding_days,
            bootstrap_confidence=float(
                validation_cfg.get("bootstrap_confidence", 0.90)
            ),
            bootstrap_replications=int(
                validation_cfg.get("bootstrap_replications", 1000)
            ),
            bootstrap_seed=int(validation_cfg.get("bootstrap_seed", 1729)),
        )
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        LOGGER.error("%s", exc)
        return 1

    input_hashes["backtest/rank_reconstitution.py"] = sha256_file(
        PACKAGE_ROOT / "backtest" / "rank_reconstitution.py"
    )
    input_hashes["backtest/16i_rank_reconstitution_long.py"] = sha256_file(
        Path(__file__).resolve()
    )
    if parameter_sha:
        input_hashes["parameter_artifact.json"] = parameter_sha
    payload = {
        "acceptance": "PASS",
        "panel_build": panel_dir.name,
        "signal_window": {"from": signals[0], "to": signals[-1]},
        "parameters": parameters,
        "summary": summary,
        "sector_selection_alpha": result["sector_selection_alpha"],
        "sector_equal_weight_selection_alpha": result[
            "sector_equal_weight_selection_alpha"
        ],
        "coverage_by_sector": result["coverage_by_sector"],
        "h1_fallback_signal_dates": result["h1_fallback_signal_dates"],
        "regime_signal_summary": result["regime_signal_summary"],
        "economic_max_names_per_sector": result["economic_max_names_per_sector"],
        "inputs_sha256": input_hashes,
        "daily_selection_returns": [
            float(row["selection_alpha_net"]) for row in result["daily"]
        ],
        "daily_dates": [str(row["date"]) for row in result["daily"]],
        "daily_stress_returns": [
            float(row["stress_net_return"]) for row in result["daily"]
        ],
        "trade_net_returns": [
            float(row["net_return"]) for row in result["trades"]
        ],
        "trade_records": result["trades"],
    }
    if args.evaluation_json:
        write_manifest(args.evaluation_json.expanduser().resolve(), payload)
        return 0

    out_dir = (
        paths.output_dir
        / str(cfg.get("dir", "rank_reconstitution_long"))
        / panel_dir.name
    )
    trades_path = out_dir / "rank_reconstitution_trades.csv"
    daily_path = out_dir / "rank_reconstitution_daily.csv"
    coverage_path = out_dir / "rank_reconstitution_coverage.csv"
    coverage_detail_path = out_dir / "rank_reconstitution_coverage_detail.csv"
    summary_path = out_dir / "rank_reconstitution_summary.json"
    manifest_path = out_dir / "rank_reconstitution_manifest.json"
    artifacts = [
        trades_path,
        daily_path,
        coverage_path,
        coverage_detail_path,
        summary_path,
        manifest_path,
    ]
    if args.force:
        for path in artifacts:
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(artifacts, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        trades_path,
        list(result["trades"][0]) if result["trades"] else TRADE_FIELDS,
        result["trades"],
    )
    write_csv(daily_path, list(result["daily"][0]), result["daily"])
    write_csv(
        coverage_path,
        list(result["coverage_by_sector"][0]),
        result["coverage_by_sector"],
    )
    write_csv(
        coverage_detail_path,
        (
            list(result["coverage_detail"][0])
            if result["coverage_detail"]
            else COVERAGE_DETAIL_FIELDS
        ),
        result["coverage_detail"],
    )
    published_payload = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "daily_selection_returns",
            "daily_dates",
            "daily_benchmark_returns",
            "daily_active_returns",
            "daily_costs",
            "daily_stress_returns",
            "trade_active_returns",
            "trade_net_returns",
            "trade_records",
        }
    }
    write_manifest(summary_path, published_payload)
    write_manifest(
        manifest_path,
        {
            "stage": "stage11_rank_reconstitution_long",
            "generated_at": utc_now(),
            "acceptance": "PASS",
            "diagnostic_only": True,
            "panel_build": panel_dir.name,
            "parameters": parameters,
            "summary": summary,
            "inputs_sha256": input_hashes,
            "files": {
                path.name: {"sha256": sha256_file(path)}
                for path in (
                    trades_path,
                    daily_path,
                    coverage_path,
                    coverage_detail_path,
                    summary_path,
                )
            },
        },
    )
    LOGGER.info(
        "RANK RECONSTITUTION PASS mode=%s entry=%.0f%% hold=%d alpha=%+.4f t=%s",
        regime_mode,
        entry_fraction * 100,
        max_holding_days,
        summary["selection_alpha_ann"],
        summary["active_t"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
