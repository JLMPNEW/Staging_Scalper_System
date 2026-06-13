#!/usr/bin/env python3
"""Embargoed walk-forward diagnostics for semiconductor scoring signals.

The script reuses the read-only point-in-time feature construction from
07_run_semiconductor_signal_diagnostics.py, emits date-level ICs, and evaluates
rolling train/test folds. It does not write to technology.sqlite and does not
apply any suggested weights.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import logging
import math
import sqlite3
import sys
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.scoring_features import (  # noqa: E402
    COMPONENT_SPECS,
    SUBFEATURE_SPECS,
    percentile_scores,
    safe_float,
    weighted_available_score,
)
from technology.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("semiconductor_walk_forward_diagnostics")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_signal_diagnostics"
DIAGNOSTICS_SCRIPT = Path(__file__).with_name("07_run_semiconductor_signal_diagnostics.py")


def load_diagnostics_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("semiconductor_signal_diagnostics_module", DIAGNOSTICS_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {DIAGNOSTICS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run embargoed walk-forward IC diagnostics for semiconductor signals.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--train-dates", type=int, default=36, help="Rolling train window in panel dates.")
    parser.add_argument("--test-dates", type=int, default=6, help="Forward test window in panel dates.")
    parser.add_argument("--embargo-days", type=int, default=90, help="Calendar-day embargo between train and test.")
    parser.add_argument("--min-train-dates", type=int, default=24)
    parser.add_argument("--min-test-dates", type=int, default=4)
    return parser.parse_args()


def stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    if not values:
        return {"n": 0, "mean": "", "t_stat": "", "hit_rate": ""}
    mean = sum(values) / n
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / (n - 1)) if n > 2 else None
    t_stat = mean / std * math.sqrt(n) if std and std > 0 else None
    return {
        "n": n,
        "mean": round(mean, 4),
        "t_stat": round(t_stat, 2) if t_stat is not None else "",
        "hit_rate": round(sum(1 for value in values if value > 0) / n, 3),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def unique_dates(records: list[dict[str, Any]]) -> list[date]:
    values = {record["asof_date"] for record in records}
    return sorted(values)


def date_set(values: list[date]) -> set[str]:
    return {value.isoformat() for value in values}


def build_fold_rows(
    records: list[dict[str, Any]],
    *,
    group: str,
    panel_dates: list[date],
    train_dates: int,
    test_dates: int,
    embargo_days: int,
    min_train_dates: int,
    min_test_dates: int,
    min_t: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    signals = sorted({str(record["signal"]) for record in records if str(record.get("group")) == group})
    horizons = sorted({int(record["horizon_days"]) for record in records if str(record.get("group")) == group})
    if not signals or not horizons:
        return rows
    fold_no = 0
    for test_start_idx in range(train_dates, len(panel_dates), test_dates):
        test_window = panel_dates[test_start_idx : test_start_idx + test_dates]
        if len(test_window) < min_test_dates:
            continue
        train_cutoff = test_window[0] - timedelta(days=embargo_days)
        train_pool = [value for value in panel_dates[:test_start_idx] if value < train_cutoff]
        train_window = train_pool[-train_dates:]
        if len(train_window) < min_train_dates:
            continue
        fold_no += 1
        train_keys = date_set(train_window)
        test_keys = date_set(test_window)
        for signal in signals:
            for horizon in horizons:
                train_values = [
                    float(record["ic"])
                    for record in records
                    if record["signal"] == signal
                    and int(record["horizon_days"]) == horizon
                    and record["asof_date"].isoformat() in train_keys
                    and record["ic"] != ""
                ]
                test_values = [
                    float(record["ic"])
                    for record in records
                    if record["signal"] == signal
                    and int(record["horizon_days"]) == horizon
                    and record["asof_date"].isoformat() in test_keys
                    and record["ic"] != ""
                ]
                if len(train_values) < min_train_dates or len(test_values) < min_test_dates:
                    continue
                train_stat = stats(train_values)
                test_stat = stats(test_values)
                train_mean = train_stat["mean"] if train_stat["mean"] != "" else 0.0
                train_t = train_stat["t_stat"] if train_stat["t_stat"] != "" else 0.0
                keep = float(train_mean) > 0 and abs(float(train_t)) >= min_t
                rows.append(
                    {
                        "fold": fold_no,
                        "signal": signal,
                        "group": group,
                        "horizon_days": horizon,
                        "train_start": train_window[0].isoformat(),
                        "train_end": train_window[-1].isoformat(),
                        "test_start": test_window[0].isoformat(),
                        "test_end": test_window[-1].isoformat(),
                        "embargo_days": embargo_days,
                        "train_n": train_stat["n"],
                        "train_mean_ic": train_stat["mean"],
                        "train_t_stat": train_stat["t_stat"],
                        "train_hit_rate": train_stat["hit_rate"],
                        "train_keep_candidate": int(keep),
                        "test_n": test_stat["n"],
                        "test_mean_ic": test_stat["mean"],
                        "test_t_stat": test_stat["t_stat"],
                        "test_hit_rate": test_stat["hit_rate"],
                    }
                )
    return rows


def summarize_folds(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    keys = sorted({(row["signal"], row["group"], int(row["horizon_days"])) for row in rows})
    for signal, group, horizon in keys:
        signal_rows = [
            row for row in rows
            if row["signal"] == signal and row["group"] == group and int(row["horizon_days"]) == horizon
        ]
        kept = [row for row in signal_rows if int(row["train_keep_candidate"]) == 1]
        kept_test_values = [float(row["test_mean_ic"]) for row in kept if row["test_mean_ic"] != ""]
        all_test_values = [float(row["test_mean_ic"]) for row in signal_rows if row["test_mean_ic"] != ""]
        kept_stats = stats(kept_test_values)
        all_stats = stats(all_test_values)
        out.append(
            {
                "signal": signal,
                "group": group,
                "horizon_days": horizon,
                "folds": len(signal_rows),
                "kept_folds": len(kept),
                "train_keep_rate": round(len(kept) / len(signal_rows), 3) if signal_rows else "",
                "all_fold_test_mean_ic": all_stats["mean"],
                "all_fold_test_hit_rate": all_stats["hit_rate"],
                "kept_fold_test_mean_ic": kept_stats["mean"],
                "kept_fold_test_hit_rate": kept_stats["hit_rate"],
                "walk_forward_keep": int(len(kept) > 0 and kept_stats["mean"] != "" and float(kept_stats["mean"]) > 0 and kept_stats["hit_rate"] != "" and float(kept_stats["hit_rate"]) >= 0.5),
            }
        )
    return out


def main() -> int:
    configure_utc_logging()
    diag = load_diagnostics_module()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/signal_diagnostics"),
        base_dir=base_dir,
    ) / "walk_forward"
    output_dir.mkdir(parents=True, exist_ok=True)

    start = diag.parse_date(args.start) or diag.parse_date(cfg_get(config, f"{CONFIG_KEY}.start_date", "2018-01-01")) or date(2018, 1, 1)
    end = diag.parse_date(args.end) or date.today()
    step = int(cfg_get(config, f"{CONFIG_KEY}.step_trading_days", 21))
    horizons = [int(h) for h in cfg_get(config, f"{CONFIG_KEY}.horizons_trading_days", [21, 63])]
    bench_ticker = normalize_ticker(cfg_get(config, f"{CONFIG_KEY}.benchmark_ticker", "SMH"))
    beta_lookback = int(cfg_get(config, f"{CONFIG_KEY}.beta_lookback_days", 252))
    min_cross_section = int(cfg_get(config, f"{CONFIG_KEY}.min_cross_section", 30))
    min_t = float(cfg_get(config, f"{CONFIG_KEY}.min_abs_t_stat_for_keep", 1.5))
    price_sources = diag.research_price_source_ids(config)
    fin_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    mp_source = str(cfg_get(config, "positioning_import.market_positioning_source_id", "market_positioning_upstream"))
    direct_source = str(cfg_get(config, "positioning_import.direct_ownership_source_id", "sec_ownership_direct"))
    upstream_source = str(cfg_get(config, "positioning_import.form4_source_id", "sec_insider_upstream"))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    short_change_days = int(cfg_get(config, "positioning_import.lookback_days.short_change", 92))

    with diag.ro_connect(db_path) as conn:
        assert isinstance(conn, sqlite3.Connection)
        universe = [
            normalize_ticker(row["ticker"])
            for row in conn.execute(
                """
                SELECT c.ticker FROM dim_company c
                JOIN dim_technology_taxonomy t ON t.ticker = c.ticker AND t.model_family = ?
                WHERE c.is_active = 1 ORDER BY c.ticker
                """,
                (model_family,),
            ).fetchall()
            if normalize_ticker(row["ticker"])
        ]
        prices = diag.load_prices(conn, price_sources, universe + [bench_ticker, "SOXX"])
        bench = prices.get(bench_ticker, diag.PriceSeries())
        soxx = prices.get("SOXX", diag.PriceSeries())
        if not bench.dates:
            LOGGER.error("No benchmark prices for %s; cannot build panel.", bench_ticker)
            return 1
        fin_rows = diag.load_financial_rows(conn, fin_source, model_family)
        form4 = diag.load_form4(conn, direct_source, upstream_source)
        inst = diag.load_13f(conn, mp_source)
        short = diag.load_short(conn, mp_source)
        borrow = diag.load_borrow(conn, mp_source)
        signal_birthdates, _signal_birthdate_rows = diag.load_positioning_signal_birthdates(
            conn,
            direct_source=direct_source,
            upstream_source=upstream_source,
            market_positioning_source=mp_source,
            short_change_days=short_change_days,
        )

    max_h = max(horizons)
    start_idx = bisect_right(bench.dates, start)
    panel_indices = list(range(max(start_idx, 260), len(bench.dates) - max_h, step))
    panel_indices = [idx for idx in panel_indices if bench.dates[idx] <= end]
    panel_dates = [bench.dates[idx] for idx in panel_indices]
    LOGGER.info("Panel dates=%d from %s to %s", len(panel_dates), panel_dates[0] if panel_dates else "-", panel_dates[-1] if panel_dates else "-")

    score_to_component = {score: comp for comp, specs in COMPONENT_SPECS.items() for score, _weight in specs}
    sub_rows: list[dict[str, Any]] = []
    comp_rows: list[dict[str, Any]] = []

    for panel_idx in panel_indices:
        asof = bench.dates[panel_idx]
        asof_iso = asof.isoformat()
        feature_rows: list[dict[str, Any]] = []
        fwd_resid: dict[int, dict[str, float]] = {h: {} for h in horizons}
        for ticker in universe:
            series = prices.get(ticker)
            if series is None or not series.dates:
                continue
            feats = diag.market_subfeatures(series, asof, soxx)
            if not feats:
                continue
            feats.update(diag.financial_subfeatures(fin_rows.get(ticker, []), asof_iso))
            feats.update(diag.positioning_subfeatures(ticker, asof_iso, form4=form4, inst=inst, short=short, borrow=borrow))
            diag.apply_signal_birthdates(feats, signal_birthdates, asof)
            feats["ticker"] = ticker
            idx = series.idx_at(asof)
            beta = diag.trailing_beta(series, bench, asof, beta_lookback)
            usable = False
            for horizon in horizons:
                target_date = bench.dates[panel_idx + horizon]
                target_idx = series.idx_at(target_date)
                fwd = series.ret_between(idx, target_idx)
                bench_fwd = bench.ret_between(panel_idx, panel_idx + horizon)
                if fwd is None or bench_fwd is None:
                    continue
                fwd_resid[horizon][ticker] = fwd - beta * bench_fwd
                usable = True
            if usable:
                feature_rows.append(feats)
        if len(feature_rows) < min_cross_section:
            continue

        for raw_key, _score_key, higher_is_better, valid in SUBFEATURE_SPECS:
            group = score_to_component.get(f"{raw_key}_score", "")
            for horizon in horizons:
                pairs: list[tuple[float, float]] = []
                for row in feature_rows:
                    value = safe_float(row.get(raw_key))
                    resid = fwd_resid[horizon].get(str(row["ticker"]))
                    if value is None or resid is None:
                        continue
                    if valid is not None and not valid(value):
                        continue
                    pairs.append((value if higher_is_better else -value, resid))
                if len(pairs) < min_cross_section:
                    continue
                ic = diag.spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                if ic is None:
                    continue
                spread = diag.quintile_spread([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                sub_rows.append(
                    {
                        "asof_date": asof,
                        "signal": raw_key,
                        "group": group,
                        "horizon_days": horizon,
                        "ic": round(ic, 6),
                        "coverage": len(pairs),
                        "q5_minus_q1_fwd_resid": round(spread, 6) if spread is not None else "",
                    }
                )

        for raw_key, score_key, higher_is_better, valid in SUBFEATURE_SPECS:
            scores = percentile_scores(feature_rows, raw_key, higher_is_better=higher_is_better, valid=valid)
            for row in feature_rows:
                row[score_key] = scores.get(str(row["ticker"]))
        for component, specs in COMPONENT_SPECS.items():
            for horizon in horizons:
                pairs = []
                for row in feature_rows:
                    resid = fwd_resid[horizon].get(str(row["ticker"]))
                    if resid is None:
                        continue
                    score, quality, _available, _missing, _default = weighted_available_score(row, specs, neutral_score=50.0)
                    if quality <= 0:
                        continue
                    pairs.append((score, resid))
                if len(pairs) < min_cross_section:
                    continue
                ic = diag.spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                if ic is None:
                    continue
                spread = diag.quintile_spread([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                comp_rows.append(
                    {
                        "asof_date": asof,
                        "signal": component,
                        "group": "component",
                        "horizon_days": horizon,
                        "ic": round(ic, 6),
                        "coverage": len(pairs),
                        "q5_minus_q1_fwd_resid": round(spread, 6) if spread is not None else "",
                    }
                )

    panel_dates = unique_dates(comp_rows)
    component_folds = build_fold_rows(
        comp_rows,
        group="component",
        panel_dates=panel_dates,
        train_dates=args.train_dates,
        test_dates=args.test_dates,
        embargo_days=args.embargo_days,
        min_train_dates=args.min_train_dates,
        min_test_dates=args.min_test_dates,
        min_t=min_t,
    )
    subfeature_groups = sorted({str(row["group"]) for row in sub_rows})
    subfeature_folds: list[dict[str, Any]] = []
    for group in subfeature_groups:
        subfeature_folds.extend(
            build_fold_rows(
                sub_rows,
                group=group,
                panel_dates=panel_dates,
                train_dates=args.train_dates,
                test_dates=args.test_dates,
                embargo_days=args.embargo_days,
                min_train_dates=args.min_train_dates,
                min_test_dates=args.min_test_dates,
                min_t=min_t,
            )
        )

    component_summary = summarize_folds(component_folds)
    subfeature_summary = summarize_folds(subfeature_folds)
    write_csv(output_dir / "date_level_component_ic.csv", comp_rows)
    write_csv(output_dir / "date_level_subfeature_ic.csv", sub_rows)
    write_csv(output_dir / "walk_forward_component_ic.csv", component_folds)
    write_csv(output_dir / "walk_forward_subfeature_ic.csv", subfeature_folds)
    write_csv(output_dir / "walk_forward_component_summary.csv", component_summary)
    write_csv(output_dir / "walk_forward_subfeature_summary.csv", subfeature_summary)

    LOGGER.info("Wrote walk-forward diagnostics to %s", output_dir)
    for row in component_summary:
        if int(row["horizon_days"]) == horizons[0]:
            LOGGER.info(
                "component=%s h=%s folds=%s kept=%s test_mean_when_kept=%s wf_keep=%s",
                row["signal"],
                row["horizon_days"],
                row["folds"],
                row["kept_folds"],
                row["kept_fold_test_mean_ic"],
                row["walk_forward_keep"],
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
