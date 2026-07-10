#!/usr/bin/env python3
"""Diagnose failed biotech OOS calibration without changing production scoring.

The Tier-1 candidate calibration can correctly reject every challenger while
still leaving an important question unanswered: did the model have no signal,
or did it only fail the absolute-return promotion gate in a hostile biotech
regime?  This script turns completed calibration outputs into focused
diagnostics by return basis, cohort, and time regime.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_SEQUENCE_DIR = PROJECT_ROOT / "output" / "biotech_index_reports" / "clean_historical_sequence_20190104_20260702"
DEFAULT_CALIBRATION_DIR = DEFAULT_SEQUENCE_DIR / "candidate_calibration"
RETURN_BASIS_COLUMNS = {
    "absolute": "net_forward_return_pct",
    "xbi_alpha": "net_benchmark_alpha_return_pct",
    "equal_weight_alpha": "net_equal_weight_alpha_return_pct",
}
SELECTED_REQUIRED_COLUMNS = {
    "sample",
    "evaluation_split",
    "horizon_days",
    "top_n",
    "candidate_name",
    "selection_policy_name",
    "asof_date",
    "ticker",
    "profile_name",
    *RETURN_BASIS_COLUMNS.values(),
}


@dataclass
class ReturnStats:
    n: int = 0
    return_n: int = 0
    total: float = 0.0
    total_sq: float = 0.0
    positive_n: int = 0
    loss20_n: int = 0
    loss40_n: int = 0
    best: float | None = None
    worst: float | None = None

    def add(self, value: float | None) -> None:
        self.n += 1
        if value is None:
            return
        self.return_n += 1
        self.total += value
        self.total_sq += value * value
        if value > 0.0:
            self.positive_n += 1
        if value <= -20.0:
            self.loss20_n += 1
        if value <= -40.0:
            self.loss40_n += 1
        self.best = value if self.best is None else max(self.best, value)
        self.worst = value if self.worst is None else min(self.worst, value)

    def mean(self) -> float | None:
        return self.total / self.return_n if self.return_n else None

    def stdev(self) -> float | None:
        if self.return_n < 2:
            return None
        numerator = self.total_sq - (self.total * self.total / self.return_n)
        return math.sqrt(max(0.0, numerator / (self.return_n - 1)))

    def lcb(self, z_score: float) -> float | None:
        avg = self.mean()
        if avg is None:
            return None
        sigma = self.stdev()
        if sigma is None:
            return avg
        return avg - max(0.0, z_score) * sigma / math.sqrt(float(self.return_n))

    def as_row(self, z_score: float) -> dict[str, Any]:
        avg = self.mean()
        return {
            "n": self.n,
            "return_n": self.return_n,
            "mean_return_pct": rounded(avg),
            "lcb_return_pct": rounded(self.lcb(z_score)),
            "hit_rate_pct": rounded(100.0 * self.positive_n / self.return_n if self.return_n else None),
            "loss20_rate_pct": rounded(100.0 * self.loss20_n / self.return_n if self.return_n else None),
            "loss40_rate_pct": rounded(100.0 * self.loss40_n / self.return_n if self.return_n else None),
            "best_return_pct": rounded(self.best),
            "worst_return_pct": rounded(self.worst),
            "stdev_return_pct": rounded(self.stdev()),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose biotech OOS calibration failures by cohort/regime/return basis.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--lcb-z", type=float, default=1.0)
    return parser.parse_args()


def filesystem_path(path: Path) -> str:
    text = str(path.resolve())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    try:
        text = str(raw).strip().replace(",", "")
        if not text:
            return default
        value = float(text)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def float_or_default(raw: object, default: float) -> float:
    value = to_float(raw, None)
    return default if value is None else value


def as_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return text in {"1", "1.0", "true", "yes", "y"}


def rounded(value: float | None, digits: int = 6) -> float | str:
    return "" if value is None else round(value, digits)


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper()


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        seen: set[str] = set()
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
    with open(filesystem_path(path), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(filesystem_path(path), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with open(filesystem_path(path), newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def gather_selected_pairs(path: Path) -> tuple[set[str], set[str]]:
    dates: set[str] = set()
    tickers: set[str] = set()
    with open(filesystem_path(path), newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = SELECTED_REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing selected-diagnostics columns: {sorted(missing)}")
        for row in reader:
            date_text = str(row.get("asof_date") or "").strip()
            ticker = normalize_ticker(row.get("ticker"))
            if date_text:
                dates.add(date_text)
            if ticker:
                tickers.add(ticker)
    return dates, tickers


def load_cohort_map(
    db_path: Path,
    *,
    dates: set[str],
    tickers: set[str],
) -> dict[tuple[str, str], str]:
    if not db_path.exists() or not dates or not tickers:
        return {}
    min_date = min(dates)
    max_date = max(dates)
    ticker_set = {normalize_ticker(item) for item in tickers}
    out: dict[tuple[str, str], str] = {}
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(daily_scores)")}
        if "ticker" not in columns or "asof_date" not in columns:
            return {}
        cohort_expr = (
            "biotech_calibration_cohort"
            if "biotech_calibration_cohort" in columns
            else "biotech_primary_cohort"
            if "biotech_primary_cohort" in columns
            else "''"
        )
        query = f"""
            SELECT asof_date, ticker, {cohort_expr} AS cohort
            FROM daily_scores
            WHERE asof_date BETWEEN ? AND ?
        """
        for row in conn.execute(query, (min_date, max_date)):
            ticker = normalize_ticker(row["ticker"])
            if ticker not in ticker_set:
                continue
            cohort = str(row["cohort"] or "").strip()
            if cohort:
                out[(str(row["asof_date"]), ticker)] = cohort
    return out


def calendar_regime(asof_date: str) -> tuple[str, str]:
    year = int(str(asof_date)[:4]) if len(str(asof_date)) >= 4 else 0
    if year <= 2020:
        return str(year or "unknown"), "2019_2020"
    if year <= 2022:
        return str(year), "2021_2022"
    if year <= 2024:
        return str(year), "2023_2024"
    return str(year), "2025_2026"


def add_stat(
    stats: dict[tuple[str, ...], ReturnStats],
    key: tuple[str, ...],
    value: float | None,
) -> None:
    stats[key].add(value)


def stats_rows(
    stats: dict[tuple[str, ...], ReturnStats],
    key_fields: list[str],
    *,
    lcb_z: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value in stats.items():
        row = dict(zip(key_fields, key))
        row.update(value.as_row(lcb_z))
        rows.append(row)
    rows.sort(key=lambda row: tuple(str(row.get(field, "")) for field in key_fields))
    return rows


def aggregate_selected_diagnostics(
    path: Path,
    *,
    cohort_map: dict[tuple[str, str], str],
    lcb_z: float,
) -> dict[str, list[dict[str, Any]]]:
    by_basis: dict[tuple[str, ...], ReturnStats] = defaultdict(ReturnStats)
    by_cohort: dict[tuple[str, ...], ReturnStats] = defaultdict(ReturnStats)
    by_period: dict[tuple[str, ...], ReturnStats] = defaultdict(ReturnStats)
    by_profile: dict[tuple[str, ...], ReturnStats] = defaultdict(ReturnStats)

    with open(filesystem_path(path), newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            asof_date = str(row.get("asof_date") or "").strip()
            ticker = normalize_ticker(row.get("ticker"))
            candidate = str(row.get("candidate_name") or "").strip()
            policy = str(row.get("selection_policy_name") or "").strip()
            base = (
                str(row.get("sample") or "").strip(),
                str(row.get("evaluation_split") or "").strip(),
                str(row.get("horizon_days") or "").strip(),
                str(row.get("top_n") or "").strip(),
                candidate,
                policy,
            )
            cohort = cohort_map.get((asof_date, ticker)) or str(row.get("profile_name") or "unknown").strip() or "unknown"
            year, regime = calendar_regime(asof_date)
            profile = str(row.get("profile_name") or "unknown").strip() or "unknown"
            for basis, column in RETURN_BASIS_COLUMNS.items():
                value = to_float(row.get(column))
                add_stat(by_basis, (*base, basis), value)
                add_stat(by_cohort, (*base, cohort, basis), value)
                add_stat(by_profile, (*base, profile, basis), value)
                add_stat(by_period, (*base, "calendar_year", year, basis), value)
                add_stat(by_period, (*base, "two_year_regime", regime, basis), value)

    return {
        "selected_return_by_basis.csv": stats_rows(
            by_basis,
            ["sample", "evaluation_split", "horizon_days", "top_n", "candidate_name", "selection_policy_name", "return_basis"],
            lcb_z=lcb_z,
        ),
        "selected_return_by_cohort.csv": stats_rows(
            by_cohort,
            [
                "sample",
                "evaluation_split",
                "horizon_days",
                "top_n",
                "candidate_name",
                "selection_policy_name",
                "cohort",
                "return_basis",
            ],
            lcb_z=lcb_z,
        ),
        "selected_return_by_profile.csv": stats_rows(
            by_profile,
            [
                "sample",
                "evaluation_split",
                "horizon_days",
                "top_n",
                "candidate_name",
                "selection_policy_name",
                "profile_name",
                "return_basis",
            ],
            lcb_z=lcb_z,
        ),
        "selected_return_by_period.csv": stats_rows(
            by_period,
            [
                "sample",
                "evaluation_split",
                "horizon_days",
                "top_n",
                "candidate_name",
                "selection_policy_name",
                "period_type",
                "period",
                "return_basis",
            ],
            lcb_z=lcb_z,
        ),
    }


def holdout_diagnostic_rows(holdout_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in holdout_rows:
        train_pass = as_bool(row.get("train_calibration_pass"))
        test_pass = as_bool(row.get("test_calibration_pass"))
        if train_pass and test_pass:
            diagnosis = "promotion_candidate"
        elif train_pass:
            diagnosis = "failed_holdout_after_train_pass"
        else:
            diagnosis = "failed_train_gate"
        train_lcb = to_float(row.get("train_selected_lcb_return_pct"))
        test_lcb = to_float(row.get("test_selected_lcb_return_pct"))
        out.append(
            {
                "sample": row.get("sample", ""),
                "horizon_days": row.get("horizon_days", ""),
                "top_n": row.get("top_n", ""),
                "candidate_name": row.get("candidate_name", ""),
                "selection_policy_name": row.get("selection_policy_name", ""),
                "diagnosis": diagnosis,
                "train_calibration_pass": train_pass,
                "test_calibration_pass": test_pass,
                "train_lcb_return_pct": rounded(train_lcb),
                "test_lcb_return_pct": rounded(test_lcb),
                "test_minus_train_lcb_return_pct": rounded(
                    test_lcb - train_lcb if test_lcb is not None and train_lcb is not None else None
                ),
                "train_mean_return_pct": rounded(to_float(row.get("train_selected_mean_return_pct"))),
                "test_mean_return_pct": rounded(to_float(row.get("test_selected_mean_return_pct"))),
                "train_hit_rate_pct": rounded(to_float(row.get("train_selected_hit_rate_pct"))),
                "test_hit_rate_pct": rounded(to_float(row.get("test_selected_hit_rate_pct"))),
                "train_loss20_rate_pct": rounded(to_float(row.get("train_selected_large_loss_20pct_rate_pct"))),
                "test_loss20_rate_pct": rounded(to_float(row.get("test_selected_large_loss_20pct_rate_pct"))),
                "train_loss40_rate_pct": rounded(to_float(row.get("train_selected_large_loss_40pct_rate_pct"))),
                "test_loss40_rate_pct": rounded(to_float(row.get("test_selected_large_loss_40pct_rate_pct"))),
                "train_profit_factor": rounded(to_float(row.get("train_selected_profit_factor"))),
                "test_profit_factor": rounded(to_float(row.get("test_selected_profit_factor"))),
                "train_fail_reasons": row.get("train_calibration_fail_reasons", ""),
                "test_fail_reasons": row.get("test_calibration_fail_reasons", ""),
            }
        )
    out.sort(
        key=lambda item: (
            int(to_float(item.get("horizon_days"), 0.0) or 0.0),
            int(to_float(item.get("top_n"), 0.0) or 0.0),
            str(item.get("sample", "")),
            -float_or_default(item.get("test_lcb_return_pct"), -1e9),
        )
    )
    return out


def best_by_scope_rows(holdout_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in holdout_rows:
        grouped[(str(row.get("horizon_days", "")), str(row.get("top_n", "")), str(row.get("sample", "")))].append(row)
    out: list[dict[str, Any]] = []
    for (horizon, top_n, sample), rows in sorted(grouped.items()):
        best = max(rows, key=lambda item: float_or_default(item.get("test_selected_lcb_return_pct"), -1e9))
        out.append(
            {
                "horizon_days": horizon,
                "top_n": top_n,
                "sample": sample,
                "candidate_name": best.get("candidate_name", ""),
                "selection_policy_name": best.get("selection_policy_name", ""),
                "train_pass": as_bool(best.get("train_calibration_pass")),
                "test_pass": as_bool(best.get("test_calibration_pass")),
                "test_lcb_return_pct": rounded(to_float(best.get("test_selected_lcb_return_pct"))),
                "test_mean_return_pct": rounded(to_float(best.get("test_selected_mean_return_pct"))),
                "test_hit_rate_pct": rounded(to_float(best.get("test_selected_hit_rate_pct"))),
                "test_loss20_rate_pct": rounded(to_float(best.get("test_selected_large_loss_20pct_rate_pct"))),
                "test_profit_factor": rounded(to_float(best.get("test_selected_profit_factor"))),
                "test_fail_reasons": best.get("test_calibration_fail_reasons", ""),
            }
        )
    return out


def holdout_gate_summary_rows(holdout_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in holdout_rows:
        grouped[(str(row.get("horizon_days", "")), str(row.get("top_n", "")), str(row.get("sample", "")))].append(row)
    out: list[dict[str, Any]] = []
    for (horizon, top_n, sample), rows in sorted(grouped.items()):
        train_pass = sum(1 for row in rows if as_bool(row.get("train_calibration_pass")))
        test_pass = sum(1 for row in rows if as_bool(row.get("test_calibration_pass")))
        both_pass = sum(
            1 for row in rows if as_bool(row.get("train_calibration_pass")) and as_bool(row.get("test_calibration_pass"))
        )
        test_lcbs = [
            value
            for value in (to_float(row.get("test_selected_lcb_return_pct")) for row in rows)
            if value is not None
        ]
        best_test_lcb = max(test_lcbs) if test_lcbs else None
        out.append(
            {
                "horizon_days": horizon,
                "top_n": top_n,
                "sample": sample,
                "candidate_rows": len(rows),
                "train_pass_rows": train_pass,
                "test_pass_rows": test_pass,
                "train_and_test_pass_rows": both_pass,
                "best_test_lcb_return_pct": rounded(best_test_lcb),
            }
        )
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(var_x * var_y)
    return cov / denom if denom else None


def exposure_correlation_rows(holdout_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not holdout_rows:
        return []
    candidate_columns = [
        column
        for column in holdout_rows[0]
        if (
            column.endswith("_weight")
            or column.startswith("selection_policy_")
            or (
                column.startswith("test_selected_")
                and any(token in column for token in ("exposure", "avg_", "mean_", "borrow", "short_interest"))
            )
        )
    ]
    out: list[dict[str, Any]] = []
    for column in candidate_columns:
        xs: list[float] = []
        ys: list[float] = []
        for row in holdout_rows:
            x = to_float(row.get(column))
            y = to_float(row.get("test_selected_lcb_return_pct"))
            if x is None or y is None:
                continue
            xs.append(x)
            ys.append(y)
        corr = pearson(xs, ys)
        if corr is None:
            continue
        out.append(
            {
                "feature": column,
                "n": len(xs),
                "pearson_corr_to_test_lcb": rounded(corr),
                "abs_corr": rounded(abs(corr)),
            }
        )
    out.sort(key=lambda row: to_float(row.get("abs_corr"), 0.0) or 0.0, reverse=True)
    return out


def write_markdown_summary(
    path: Path,
    *,
    holdout_rows: list[dict[str, str]],
    gate_rows: list[dict[str, Any]],
    best_rows: list[dict[str, Any]],
    selected_basis_rows: list[dict[str, Any]],
) -> None:
    both_pass = sum(
        1 for row in holdout_rows if as_bool(row.get("train_calibration_pass")) and as_bool(row.get("test_calibration_pass"))
    )
    train_pass = sum(1 for row in holdout_rows if as_bool(row.get("train_calibration_pass")))
    test_pass = sum(1 for row in holdout_rows if as_bool(row.get("test_calibration_pass")))
    current_basis = [
        row
        for row in selected_basis_rows
        if str(row.get("candidate_name") or "").startswith("current_config")
        and row.get("evaluation_split") == "test"
        and row.get("return_basis") == "xbi_alpha"
    ]
    lines = [
        "# Biotech OOS Calibration Diagnostic",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Gate Result",
        "",
        f"- Holdout rows: {len(holdout_rows)}",
        f"- Train pass rows: {train_pass}",
        f"- Test pass rows: {test_pass}",
        f"- Train and test pass rows: {both_pass}",
        "",
        "No production promotion is supported when train_and_test_pass_rows is zero.",
        "",
        "## Best Test LCB By Scope",
        "",
        "| Horizon | Top N | Sample | Candidate | Policy | Test LCB | Test Mean | Test Hit | Test Loss20 | Test PF |",
        "|---:|---:|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in best_rows:
        lines.append(
            "| {horizon_days} | {top_n} | {sample} | {candidate_name} | {selection_policy_name} | "
            "{test_lcb_return_pct} | {test_mean_return_pct} | {test_hit_rate_pct} | "
            "{test_loss20_rate_pct} | {test_profit_factor} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## Current Config Test XBI-Alpha Summary",
            "",
            "| Horizon | Top N | Sample | Policy | XBI-alpha LCB | XBI-alpha Mean | Hit | Loss20 |",
            "|---:|---:|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(
        current_basis,
        key=lambda item: (
            int(to_float(item.get("horizon_days"), 0.0) or 0.0),
            int(to_float(item.get("top_n"), 0.0) or 0.0),
            str(item.get("sample", "")),
            str(item.get("selection_policy_name", "")),
        ),
    ):
        lines.append(
            "| {horizon_days} | {top_n} | {sample} | {selection_policy_name} | "
            "{lcb_return_pct} | {mean_return_pct} | {hit_rate_pct} | {loss20_rate_pct} |".format(**row)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    calibration_dir = args.calibration_dir.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else calibration_dir.parent / "oos_calibration_diagnostics"
    )
    holdout_path = calibration_dir / "tier1_weight_calibration_holdout.csv"
    selected_path = calibration_dir / "tier1_selected_ticker_diagnostics.csv"
    if not holdout_path.exists():
        raise FileNotFoundError(holdout_path)
    if not selected_path.exists():
        raise FileNotFoundError(selected_path)

    holdout_rows = read_csv_rows(holdout_path)
    dates, tickers = gather_selected_pairs(selected_path)
    cohort_map = load_cohort_map(db_path, dates=dates, tickers=tickers)
    selected_outputs = aggregate_selected_diagnostics(selected_path, cohort_map=cohort_map, lcb_z=float(args.lcb_z))

    holdout_diagnostics = holdout_diagnostic_rows(holdout_rows)
    gate_summary = holdout_gate_summary_rows(holdout_rows)
    best_by_scope = best_by_scope_rows(holdout_rows)
    exposure_corr = exposure_correlation_rows(holdout_rows)

    write_csv(output_dir / "holdout_diagnostics.csv", holdout_diagnostics)
    write_csv(output_dir / "holdout_gate_summary.csv", gate_summary)
    write_csv(output_dir / "holdout_best_by_scope.csv", best_by_scope)
    write_csv(output_dir / "holdout_test_lcb_exposure_correlations.csv", exposure_corr)
    for filename, rows in selected_outputs.items():
        write_csv(output_dir / filename, rows)
    write_markdown_summary(
        output_dir / "calibration_failure_diagnostic_summary.md",
        holdout_rows=holdout_rows,
        gate_rows=gate_summary,
        best_rows=best_by_scope,
        selected_basis_rows=selected_outputs["selected_return_by_basis.csv"],
    )

    both_pass = sum(
        1 for row in holdout_rows if as_bool(row.get("train_calibration_pass")) and as_bool(row.get("test_calibration_pass"))
    )
    manifest = {
        "status": "success",
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "calibration_dir": str(calibration_dir),
        "selected_ticker_diagnostics": str(selected_path),
        "holdout_csv": str(holdout_path),
        "db_path": str(db_path),
        "output_dir": str(output_dir),
        "holdout_rows": len(holdout_rows),
        "train_and_test_pass_rows": both_pass,
        "cohort_enrichment_rows": len(cohort_map),
        "selected_dates": len(dates),
        "selected_tickers": len(tickers),
        "production_promotion_supported": both_pass > 0,
        "notes": [
            "Diagnostic-only report; no production config or scoring formula is changed.",
            "Negative absolute LCB with less-negative or positive alpha rows indicates regime/objective issues, not automatically no edge.",
            "Production promotion remains blocked when train_and_test_pass_rows is zero.",
        ],
    }
    write_json(output_dir / "oos_calibration_diagnostic_manifest.json", manifest)
    print(f"oos_calibration_diagnostics_written={output_dir} train_and_test_pass_rows={both_pass}")


if __name__ == "__main__":
    main()
