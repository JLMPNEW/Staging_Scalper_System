from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import sqlite3
import subprocess
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from industrials.core.config import cfg_get, resolve_path
from industrials.core.reports import write_csv_atomic
from industrials.machinery.production_universe import (
    configured_universe_policy,
    production_universe_eligible,
)
from industrials.machinery.scoring import (
    COMPONENT_FIELDS as SCORING_COMPONENT_FIELDS,
)
from industrials.machinery.scoring import (
    METRIC_DIRECTIONS,
    file_sha256,
    write_json_atomic,
)


MODEL_FAMILY = "machinery"
CONFIG_KEY = "machinery_stage8"
PANEL_SOURCE = "survivorship_corrected_pit_membership_score_recompute"
COMPONENT_FIELDS = tuple(
    field
    for field in SCORING_COMPONENT_FIELDS
    if field != "development_stage_risk_score"
)
RAW_SIGNAL_FIELDS = tuple(sorted(METRIC_DIRECTIONS))
SIGNAL_FIELDS = (*COMPONENT_FIELDS, *RAW_SIGNAL_FIELDS)
PANEL_BASE_FIELDS = (
    "ticker",
    "asof_date",
    "model_family",
    "company_name",
    "calibration_cohort",
    "calibration_cohort_name",
    "calibration_use",
    "development_stage",
    "membership_status",
    "membership_start_date",
    "membership_end_date",
    "rank_ready_flag",
    "model_status",
    "score_confidence",
    "final_score",
    *SIGNAL_FIELDS,
    "latest_adj_close",
    "avg_dollar_volume_60d",
    "market_cap",
    "market_cap_source",
    "liquidity_capacity_reason",
    "market_feature_asof_date",
    "financial_feature_asof_date",
    "positioning_feature_asof_date",
    "financial_metric_availability_asof_date",
    "latest_bar_date",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "source_sidecar_sha256",
    "source_manifest_sha256",
    "source_sidecar_path",
    "base_panel_eligible_flag",
    "base_panel_eligible_reason",
    "core_model_eligible_flag",
    "core_model_eligible_reason",
    "execution_universe_eligible_flag",
    "execution_universe_eligible_reason",
    "split_name",
)
SOURCE_INDEX_FIELDS = (
    "asof_date",
    "sidecar_path",
    "sidecar_sha256",
    "manifest_path",
    "manifest_sha256",
    "row_count",
)
SPLIT_FIELDS = (
    "split_name",
    "start_date",
    "end_date",
    "snapshot_count",
    "role",
)
DIAGNOSTIC_FIELDS = (
    "signal",
    "signal_role",
    "split_name",
    "horizon_days",
    "direction",
    "observation_count",
    "date_count",
    "mean_cross_section",
    "mean_ic",
    "ic_std",
    "ic_hit_rate",
    "newey_west_t",
    "mean_quintile_spread",
)
TRIAL_FIELDS = (
    "trial_number",
    "candidate_id",
    "pre_registered_flag",
    "candidate_registry_sha256",
    "search_method",
    "train_objective",
    "train_avg_top_turnover",
    "train_avg_top_cohort_share",
    "weights_json",
)
WALK_FORWARD_FIELDS = (
    "block",
    "candidate_id",
    "train_start",
    "train_end",
    "test_start",
    "test_end",
    "train_date_count",
    "test_date_count",
    "candidate_objective",
    "baseline_objective",
    "objective_improvement",
    "candidate_wins",
    "candidate_gate_pass",
    "candidate_fold_product_pass",
    "candidate_avg_top_turnover",
    "candidate_avg_top_cohort_share",
    "weights_json",
)
RETURN_RECONCILIATION_FIELDS = (
    "split_name",
    "horizon_days",
    "core_observation_count",
    "close_available_count",
    "execution_available_count",
    "both_available_count",
    "execution_coverage",
    "terminal_outcome_count",
    "unresolved_terminal_count",
    "mean_close_excess_return",
    "mean_execution_excess_return",
    "mean_execution_minus_close",
    "close_execution_correlation",
)
MODEL_DATE_DIAGNOSTIC_FIELDS = (
    "model",
    "split_name",
    "asof_date",
    "calendar_year",
    "horizon_days",
    "ranked_cross_section",
    "outcome_coverage",
    "ic",
    "top_excess",
    "top_excess_net",
    "spread",
    "spread_net",
    "top_turnover",
    "top_transaction_cost",
    "bottom_transaction_cost",
)
QUANTILE_DIAGNOSTIC_FIELDS = (
    "model",
    "split_name",
    "horizon_days",
    "universe_policy",
    "rank_direction",
    "bucket_count",
    "quantile",
    "date_count",
    "observation_count",
    "missing_outcome_count",
    "mean_execution_excess_return",
    "hit_rate",
)
COMPONENT_ABLATION_FIELDS = (
    "model",
    "split_name",
    "component",
    "ablation_method",
    "baseline_objective",
    "ablated_objective",
    "objective_contribution",
    "standalone_objective",
)
SLEEVE_MEMBERSHIP_FIELDS = (
    "model",
    "split_name",
    "asof_date",
    "calendar_year",
    "horizon_days",
    "universe_policy",
    "rank_direction",
    "ticker",
    "calibration_cohort",
    "score",
    "rank",
    "ranked_cross_section",
    "quantile",
    "configured_sleeve",
    "sleeve_weight",
    "outcome_available_flag",
    "execution_excess_return",
    "gross_return_contribution",
)
CANDIDATE_FOLD_FIELDS = (
    "candidate_id",
    "candidate_role",
    "block",
    "test_start",
    "test_end",
    "test_date_count",
    "objective",
    "product_gate_pass",
    "gate_reasons",
    "weights_json",
)
REGIME_DIAGNOSTIC_FIELDS = (
    "model",
    "split_name",
    "regime_type",
    "regime_value",
    "horizon_days",
    "date_count",
    "mean_top_excess_net",
    "median_top_excess_net",
    "top_excess_net_hit_rate",
    "top_excess_net_newey_west_t",
    "top_excess_net_lower_confidence_bound",
    "mean_ic",
    "mean_spread_net",
)
TICKER_ATTRIBUTION_FIELDS = (
    "model",
    "split_name",
    "horizon_days",
    "configured_sleeve",
    "ticker",
    "membership_dates",
    "mean_execution_excess_return",
    "mean_gross_return_contribution",
    "total_mean_sleeve_contribution",
)


@dataclass(frozen=True)
class PricePoint:
    bar_date: date
    value: float
    source_id: str
    price_basis: str
    open_value: float | None = None


@dataclass(frozen=True)
class SnapshotArtifact:
    asof_date: str
    sidecar_path: Path
    manifest_path: Path
    sidecar_sha256: str
    manifest_sha256: str
    rows: list[dict[str, str]]


@dataclass(frozen=True)
class RankedEvaluationPopulation:
    ordered: tuple[tuple[Mapping[str, str], float], ...]
    top: tuple[tuple[Mapping[str, str], float], ...]
    bottom: tuple[tuple[Mapping[str, str], float], ...]


@dataclass(frozen=True)
class Stage8Paths:
    root: Path
    panel_csv: Path
    source_index_csv: Path
    splits_csv: Path
    panel_manifest_json: Path
    diagnostics_csv: Path
    return_reconciliation_csv: Path
    model_date_diagnostics_csv: Path
    quantile_diagnostics_csv: Path
    component_ablation_csv: Path
    sleeve_membership_csv: Path
    candidate_registry_json: Path
    candidate_fold_comparison_csv: Path
    regime_diagnostics_csv: Path
    ticker_attribution_csv: Path
    trials_csv: Path
    static_summary_json: Path
    walk_forward_csv: Path
    walk_forward_summary_json: Path
    acceptance_json: Path
    run_manifest_json: Path
    validation_csv: Path
    validation_json: Path


def stage8_paths(root: Path) -> Stage8Paths:
    return Stage8Paths(
        root=root,
        panel_csv=root / "machinery_stage8_panel.csv",
        source_index_csv=root / "machinery_stage8_source_index.csv",
        splits_csv=root / "machinery_stage8_splits.csv",
        panel_manifest_json=root / "machinery_stage8_panel_manifest.json",
        diagnostics_csv=root / "machinery_stage8_signal_diagnostics.csv",
        return_reconciliation_csv=(
            root / "machinery_stage8_return_reconciliation.csv"
        ),
        model_date_diagnostics_csv=(
            root / "machinery_stage8_model_date_diagnostics.csv"
        ),
        quantile_diagnostics_csv=(
            root / "machinery_stage8_quantile_diagnostics.csv"
        ),
        component_ablation_csv=(
            root / "machinery_stage8_component_ablation.csv"
        ),
        sleeve_membership_csv=(
            root / "machinery_stage8_sleeve_membership.csv"
        ),
        candidate_registry_json=(
            root / "machinery_stage8_candidate_registry.json"
        ),
        candidate_fold_comparison_csv=(
            root / "machinery_stage8_candidate_fold_comparison.csv"
        ),
        regime_diagnostics_csv=(
            root / "machinery_stage8_regime_diagnostics.csv"
        ),
        ticker_attribution_csv=(
            root / "machinery_stage8_ticker_attribution.csv"
        ),
        trials_csv=root / "machinery_stage8_calibration_trials.csv",
        static_summary_json=root / "machinery_stage8_static_summary.json",
        walk_forward_csv=root / "machinery_stage8_walk_forward_blocks.csv",
        walk_forward_summary_json=(
            root / "machinery_stage8_walk_forward_summary.json"
        ),
        acceptance_json=root / "machinery_stage8_acceptance.json",
        run_manifest_json=root / "machinery_stage8_run_manifest.json",
        validation_csv=root / "machinery_stage8_validation.csv",
        validation_json=root / "machinery_stage8_validation.json",
    )


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def stage8_config_sha256(config: dict[str, Any]) -> str:
    payload = {
        "machinery_stage8": cfg_get(config, CONFIG_KEY, {}),
        "component_weights": cfg_get(
            config,
            "machinery_scoring.component_weights",
            {},
        ),
        "market_price_sources": {
            "primary": cfg_get(
                config,
                "market_data_policy.scoring_primary_source",
                "",
            ),
            "fallbacks": cfg_get(
                config,
                "market_data_policy.scoring_fallback_sources",
                [],
            ),
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_date(raw: object, *, field: str = "date") -> date:
    text = str(raw or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid {field}: {raw!r}") from exc


def as_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(raw: object, digits: int = 10) -> str:
    value = as_float(raw)
    if value is None:
        return ""
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        if len(fields) != len(set(fields)):
            raise ValueError(f"CSV contains duplicate columns: {path}")
        return [
            {str(key): str(value or "") for key, value in row.items()}
            for row in reader
        ]


def rank_values(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index + 1
        while end < len(indexed) and indexed[end][1] == indexed[index][1]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for original, _ in indexed[index:end]:
            ranks[original] = rank
        index = end
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    x_dev = [value - x_mean for value in xs]
    y_dev = [value - y_mean for value in ys]
    x_scale = math.sqrt(sum(value * value for value in x_dev))
    y_scale = math.sqrt(sum(value * value for value in y_dev))
    if x_scale <= 0 or y_scale <= 0:
        return None
    return sum(x * y for x, y in zip(x_dev, y_dev)) / (
        x_scale * y_scale
    )


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    return pearson(rank_values(xs), rank_values(ys))


def mean(values: Iterable[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return sum(clean) / len(clean) if clean else None


def stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    average = sum(values) / len(values)
    return math.sqrt(
        sum((value - average) ** 2 for value in values)
        / (len(values) - 1)
    )


def median(values: Sequence[float]) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    midpoint = len(clean) // 2
    if len(clean) % 2:
        return clean[midpoint]
    return (clean[midpoint - 1] + clean[midpoint]) / 2.0


def newey_west_mean_standard_error(
    values: Sequence[float],
    lags: int,
) -> float | None:
    if len(values) < 3:
        return None
    average = sum(values) / len(values)
    centered = [value - average for value in values]
    gamma0 = sum(value * value for value in centered) / len(values)
    long_run_variance = gamma0
    max_lag = min(max(0, lags), len(values) - 1)
    for lag in range(1, max_lag + 1):
        covariance = sum(
            centered[index] * centered[index - lag]
            for index in range(lag, len(values))
        ) / len(values)
        long_run_variance += (
            2.0 * (1.0 - lag / (max_lag + 1.0)) * covariance
        )
    if long_run_variance <= 0:
        return None
    return math.sqrt(long_run_variance / len(values))


def newey_west_t(values: Sequence[float], lags: int) -> float | None:
    if len(values) < 3:
        return None
    average = sum(values) / len(values)
    standard_error = newey_west_mean_standard_error(values, lags)
    if standard_error is None:
        return None
    return average / standard_error if standard_error > 0 else None


def _bootstrap_mean_lower_bound(
    values: Sequence[float],
    *,
    confidence: float,
    simulations: int,
    seed: int,
) -> float | None:
    if len(values) < 2 or simulations < 1 or not 0.5 < confidence < 1.0:
        return None
    rng = random.Random(seed)
    sample_size = len(values)
    means = sorted(
        sum(values[rng.randrange(sample_size)] for _ in range(sample_size))
        / sample_size
        for _ in range(simulations)
    )
    index = max(0, math.floor((1.0 - confidence) * simulations) - 1)
    return means[index]


def quintile_spread(
    scores: Sequence[float],
    returns: Sequence[float],
) -> float | None:
    if len(scores) != len(returns) or len(scores) < 10:
        return None
    ordered_scores = sorted(scores)
    lower = ordered_scores[math.floor((len(scores) - 1) * 0.20)]
    upper = ordered_scores[math.ceil((len(scores) - 1) * 0.80)]
    if lower >= upper:
        return None
    bottom = [
        outcome
        for score, outcome in zip(scores, returns)
        if score <= lower
    ]
    top = [
        outcome
        for score, outcome in zip(scores, returns)
        if score >= upper
    ]
    if not bottom or not top:
        return None
    return sum(top) / len(top) - sum(bottom) / len(bottom)


def _snapshot_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.name)
    except ValueError:
        return None


def _weekly_paths(
    root: Path,
    *,
    start: date,
    end: date,
    anchor: date,
    selection: str,
) -> list[Path]:
    buckets: dict[int, list[Path]] = defaultdict(list)
    for path in root.iterdir():
        if not path.is_dir():
            continue
        asof = _snapshot_date(path)
        if asof is None or asof < start or asof > end:
            continue
        bucket = (asof - anchor).days // 7
        buckets[bucket].append(path)
    output: list[Path] = []
    for bucket in sorted(buckets):
        members = sorted(buckets[bucket], key=lambda item: item.name)
        output.append(members[0] if selection == "first" else members[-1])
    return output


def _load_snapshot(path: Path) -> SnapshotArtifact:
    asof = path.name
    sidecar = path / "machinery_stage11_survivorship_calibration_panel.csv"
    manifest_path = path / "machinery_final_rank_table_manifest.json"
    if not sidecar.exists() or not manifest_path.exists():
        raise FileNotFoundError(
            f"Missing Stage 8 source artifact under {path}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = read_csv_rows(sidecar)
    sidecar_sha = file_sha256(sidecar)
    if manifest.get("acceptance") != "PASS":
        raise ValueError(f"Source manifest did not pass: {manifest_path}")
    if manifest.get("model_family") not in {None, MODEL_FAMILY}:
        raise ValueError(f"Source manifest family mismatch: {manifest_path}")
    if manifest.get("asof_date") != asof:
        raise ValueError(f"Source manifest as-of mismatch: {manifest_path}")
    if manifest.get("sidecar_sha256") != sidecar_sha:
        raise ValueError(f"Source sidecar hash mismatch: {sidecar}")
    if int(manifest.get("row_count") or -1) != len(rows):
        raise ValueError(f"Source sidecar row-count mismatch: {sidecar}")
    tickers = [row.get("ticker", "") for row in rows]
    if len(tickers) != len(set(tickers)) or any(not ticker for ticker in tickers):
        raise ValueError(f"Invalid ticker keys in {sidecar}")
    if {row.get("asof_date", "") for row in rows} != {asof}:
        raise ValueError(f"Source sidecar as-of mismatch: {sidecar}")
    return SnapshotArtifact(
        asof_date=asof,
        sidecar_path=sidecar,
        manifest_path=manifest_path,
        sidecar_sha256=sidecar_sha,
        manifest_sha256=file_sha256(manifest_path),
        rows=rows,
    )


def _price_sources(config: dict[str, Any]) -> list[str]:
    raw = [
        str(
            cfg_get(
                config,
                "market_data_policy.scoring_primary_source",
                "yahoo_finance_adjusted",
            )
        ),
        *[
            str(item)
            for item in cfg_get(
                config,
                "market_data_policy.scoring_fallback_sources",
                [],
            )
        ],
    ]
    return list(dict.fromkeys(item for item in raw if item))


def _load_prices(
    conn: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    sources: Sequence[str],
    end_date: str,
) -> dict[str, dict[str, list[PricePoint]]]:
    clean_tickers = sorted(set(tickers))
    ticker_ph = ",".join("?" for _ in clean_tickers)
    source_ph = ",".join("?" for _ in sources)
    rows = conn.execute(
        f"""
        SELECT ticker, source_id, bar_date, open, adj_close, close
        FROM fact_price_ohlcv
        WHERE ticker IN ({ticker_ph})
          AND source_id IN ({source_ph})
          AND bar_date <= ?
          AND (adj_close IS NOT NULL OR close IS NOT NULL)
        ORDER BY ticker, source_id, bar_date
        """,
        (*clean_tickers, *sources, end_date),
    )
    output: dict[str, dict[str, list[PricePoint]]] = {}
    for row in rows:
        adjusted = as_float(row["adj_close"])
        close = as_float(row["close"])
        open_price = as_float(row["open"])
        value = adjusted if adjusted is not None else close
        # Preserve an explicit zero terminal quote so a reviewed bankruptcy
        # can realize a -100% outcome. Entry and ordinary exit prices still
        # require strictly positive values below.
        if value is None or value < 0:
            continue
        adjusted_open = open_price
        if (
            adjusted is not None
            and close is not None
            and close > 0
            and open_price is not None
            and open_price > 0
        ):
            adjusted_open = open_price * adjusted / close
        ticker = str(row["ticker"]).upper()
        source = str(row["source_id"])
        output.setdefault(ticker, {}).setdefault(source, []).append(
            PricePoint(
                bar_date=parse_date(row["bar_date"], field="bar_date"),
                value=value,
                source_id=source,
                price_basis="adj_close" if adjusted is not None else "close",
                open_value=adjusted_open,
            )
        )
    return output


def _return_window(
    series_by_source: Mapping[str, Sequence[PricePoint]],
    *,
    asof: str,
    horizon: int,
    source_order: Sequence[str],
) -> tuple[PricePoint | None, PricePoint | None, str]:
    asof_date = parse_date(asof, field="asof_date")
    partial: PricePoint | None = None
    for source in source_order:
        series = list(series_by_source.get(source, ()))
        if not series:
            continue
        dates = [point.bar_date for point in series]
        anchor_index = bisect_right(dates, asof_date) - 1
        if anchor_index < 0:
            continue
        anchor = series[anchor_index]
        if anchor.value <= 0:
            continue
        if (asof_date - anchor.bar_date).days > 10:
            continue
        forward_index = anchor_index + horizon
        if forward_index < len(series):
            return anchor, series[forward_index], ""
        partial = partial or anchor
    if partial is not None:
        return partial, None, "label_crosses_development_end"
    return None, None, "missing_asof_price"


def _execution_window(
    series_by_source: Mapping[str, Sequence[PricePoint]],
    *,
    asof: str,
    horizon: int,
    source_order: Sequence[str],
    terminal_date: date | None = None,
    horizon_end: date | None = None,
    max_terminal_staleness_days: int = 10,
) -> tuple[PricePoint | None, PricePoint | None, str, str]:
    asof_date = parse_date(asof, field="asof_date")
    partial = False
    terminal_expected = bool(
        terminal_date is not None
        and horizon_end is not None
        and asof_date < terminal_date <= horizon_end
    )
    terminal_candidate: tuple[PricePoint, PricePoint] | None = None
    for source in source_order:
        series = list(series_by_source.get(source, ()))
        if not series:
            continue
        dates = [point.bar_date for point in series]
        signal_index = bisect_right(dates, asof_date) - 1
        if signal_index < 0:
            continue
        signal_point = series[signal_index]
        if (asof_date - signal_point.bar_date).days > 10:
            continue
        entry_index = signal_index + 1
        exit_index = entry_index + horizon
        if entry_index >= len(series):
            partial = True
            continue
        entry = series[entry_index]
        if entry.open_value is None or entry.open_value <= 0:
            continue
        if exit_index < len(series) and not terminal_expected:
            exit_point = series[exit_index]
            if exit_point.open_value is not None and exit_point.open_value > 0:
                return entry, exit_point, "", "scheduled_horizon"
        partial = True
        if terminal_expected and terminal_date is not None:
            eligible_terminal_points = [
                point
                for point in series[entry_index:]
                if point.bar_date <= terminal_date
            ]
            if eligible_terminal_points:
                terminal_exit = eligible_terminal_points[-1]
                if (
                    0 <= (terminal_date - terminal_exit.bar_date).days
                    <= max_terminal_staleness_days
                    and terminal_exit.value >= 0
                ):
                    terminal_candidate = terminal_candidate or (
                        entry,
                        terminal_exit,
                    )
    if terminal_candidate is not None:
        return (
            terminal_candidate[0],
            terminal_candidate[1],
            "",
            "terminal_membership_exit",
        )
    if terminal_expected:
        return None, None, "missing_terminal_outcome", ""
    if partial:
        return None, None, "execution_window_crosses_development_end", ""
    return None, None, "missing_d1_open_execution_price", ""


def _split_map(
    snapshot_dates: Sequence[str],
    *,
    train_fraction: float,
    validation_fraction: float,
    purge_calendar_days: int,
) -> dict[str, str]:
    dates = sorted(set(snapshot_dates))
    if len(dates) < 10:
        return {item: "insufficient_history" for item in dates}
    train_cut = max(1, int(len(dates) * train_fraction))
    validation_cut = min(
        len(dates) - 1,
        max(train_cut + 1, int(len(dates) * (train_fraction + validation_fraction))),
    )
    base: dict[str, str] = {}
    for index, asof in enumerate(dates):
        if index < train_cut:
            base[asof] = "train"
        elif index < validation_cut:
            base[asof] = "validation"
        else:
            base[asof] = "holdout"
    first_validation = parse_date(dates[train_cut])
    first_holdout = parse_date(dates[validation_cut])
    for asof, split_name in list(base.items()):
        parsed = parse_date(asof)
        boundary = (
            first_validation
            if split_name == "train"
            else first_holdout
            if split_name == "validation"
            else None
        )
        if boundary is not None and (boundary - parsed).days <= purge_calendar_days:
            base[asof] = "embargo"
    return base


def _split_rows(split_map: Mapping[str, str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for split in (
        "train",
        "validation",
        "holdout",
        "embargo",
        "insufficient_history",
    ):
        members = sorted(
            asof for asof, role in split_map.items() if role == split
        )
        if not members:
            continue
        rows.append(
            {
                "split_name": split,
                "start_date": members[0],
                "end_date": members[-1],
                "snapshot_count": str(len(members)),
                "role": (
                    "purged_boundary_overlap"
                    if split == "embargo"
                    else "not_calibratable"
                    if split == "insufficient_history"
                    else "research_only"
                ),
            }
        )
    return rows


def _source_date_violation(row: Mapping[str, str], asof: str) -> str:
    asof_date = parse_date(asof)
    for field in (
        "market_feature_asof_date",
        "financial_feature_asof_date",
        "positioning_feature_asof_date",
        "financial_metric_availability_asof_date",
        "latest_bar_date",
    ):
        value = str(row.get(field) or "")
        if value and parse_date(value, field=field) > asof_date:
            return f"{field}_after_asof"
    return ""


def _horizon_fields(horizons: Sequence[int]) -> tuple[str, ...]:
    fields: list[str] = []
    for horizon in horizons:
        prefix = f"{horizon}d"
        fields.extend(
            (
                f"price_source_id_{prefix}",
                f"price_basis_{prefix}",
                f"price_asof_date_{prefix}",
                f"price_forward_date_{prefix}",
                f"forward_return_{prefix}",
                f"benchmark_ticker_{prefix}",
                f"benchmark_price_source_id_{prefix}",
                f"benchmark_asof_date_{prefix}",
                f"benchmark_forward_date_{prefix}",
                f"benchmark_return_{prefix}",
                f"forward_excess_return_{prefix}",
                f"return_available_flag_{prefix}",
                f"return_unavailable_reason_{prefix}",
                f"panel_row_eligible_flag_{prefix}",
                f"execution_price_source_id_{prefix}",
                f"execution_price_basis_{prefix}",
                f"execution_entry_date_{prefix}",
                f"execution_exit_date_{prefix}",
                f"execution_entry_price_{prefix}",
                f"execution_exit_price_{prefix}",
                f"execution_exit_price_basis_{prefix}",
                f"execution_return_{prefix}",
                f"execution_outcome_type_{prefix}",
                f"benchmark_execution_price_source_id_{prefix}",
                f"benchmark_execution_entry_date_{prefix}",
                f"benchmark_execution_exit_date_{prefix}",
                f"benchmark_execution_return_{prefix}",
                f"execution_excess_return_{prefix}",
                f"execution_available_flag_{prefix}",
                f"execution_unavailable_reason_{prefix}",
            )
        )
    return tuple(fields)


def build_panel(
    config: dict[str, Any],
    *,
    config_path: Path,
    db_path: Path,
    paths: Stage8Paths,
) -> tuple[list[dict[str, str]], list[int], dict[str, Any]]:
    start = parse_date(
        cfg_get(config, f"{CONFIG_KEY}.development_start_date")
    )
    end = parse_date(cfg_get(config, f"{CONFIG_KEY}.development_end_date"))
    sealed_start = parse_date(
        cfg_get(config, f"{CONFIG_KEY}.sealed_start_date")
    )
    if end >= sealed_start:
        raise ValueError("Stage 8 development window overlaps the sealed window")
    core_universe_policy = configured_universe_policy(
        config,
        config_key=CONFIG_KEY,
    )
    snapshot_root = resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.snapshot_root"),
        base_dir=config_path.parent,
    )
    weekly_anchor = parse_date(
        cfg_get(config, f"{CONFIG_KEY}.weekly_anchor_date")
    )
    selection = str(
        cfg_get(config, f"{CONFIG_KEY}.weekly_selection", "last")
    )
    snapshot_paths = _weekly_paths(
        snapshot_root,
        start=start,
        end=end,
        anchor=weekly_anchor,
        selection=selection,
    )
    snapshots = [_load_snapshot(path) for path in snapshot_paths]
    if not snapshots:
        raise ValueError("No machinery Stage 8 source snapshots selected")
    horizons = [
        int(item)
        for item in cfg_get(
            config,
            f"{CONFIG_KEY}.horizons_trading_days",
            [21, 63],
        )
    ]
    if not horizons or any(item <= 0 for item in horizons):
        raise ValueError("Stage 8 horizons must be positive")
    calibration_return_basis = str(
        cfg_get(config, f"{CONFIG_KEY}.calibration_return_basis", "")
    )
    if calibration_return_basis != "next_session_open_execution_excess":
        raise ValueError(
            "Stage 8 calibration_return_basis must be "
            "next_session_open_execution_excess"
        )
    terminal_staleness_days = int(
        cfg_get(
            config,
            f"{CONFIG_KEY}.terminal_price_max_staleness_days",
            10,
        )
    )
    if terminal_staleness_days < 0:
        raise ValueError("terminal_price_max_staleness_days cannot be negative")
    embargo_days = int(
        cfg_get(config, f"{CONFIG_KEY}.embargo_trading_days", 21)
    )
    purge_calendar_days = math.ceil(
        (max(horizons) + embargo_days) * 7 / 5
    )
    splits = _split_map(
        [snapshot.asof_date for snapshot in snapshots],
        train_fraction=float(
            cfg_get(config, f"{CONFIG_KEY}.train_fraction", 0.60)
        ),
        validation_fraction=float(
            cfg_get(config, f"{CONFIG_KEY}.validation_fraction", 0.20)
        ),
        purge_calendar_days=purge_calendar_days,
    )
    benchmark = str(
        cfg_get(config, f"{CONFIG_KEY}.benchmark_ticker", "XLI")
    ).upper()
    sources = _price_sources(config)
    tickers = sorted(
        {
            benchmark,
            *(
                str(row.get("ticker") or "").upper()
                for snapshot in snapshots
                for row in snapshot.rows
            ),
        }
    )
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        prices = _load_prices(
            conn,
            tickers=tickers,
            sources=sources,
            end_date=end.isoformat(),
        )
    finally:
        conn.close()
    panel_rows: list[dict[str, str]] = []
    source_rows: list[dict[str, str]] = []
    for snapshot in snapshots:
        source_rows.append(
            {
                "asof_date": snapshot.asof_date,
                "sidecar_path": str(snapshot.sidecar_path),
                "sidecar_sha256": snapshot.sidecar_sha256,
                "manifest_path": str(snapshot.manifest_path),
                "manifest_sha256": snapshot.manifest_sha256,
                "row_count": str(len(snapshot.rows)),
            }
        )
        benchmark_windows = {
            horizon: _return_window(
                prices.get(benchmark, {}),
                asof=snapshot.asof_date,
                horizon=horizon,
                source_order=sources,
            )
            for horizon in horizons
        }
        benchmark_execution_windows = {
            horizon: _execution_window(
                prices.get(benchmark, {}),
                asof=snapshot.asof_date,
                horizon=horizon,
                source_order=sources,
            )
            for horizon in horizons
        }
        for source_row in snapshot.rows:
            ticker = str(source_row.get("ticker") or "").upper()
            membership_end_raw = str(
                source_row.get("membership_end_date") or ""
            ).strip()
            membership_end = (
                parse_date(
                    membership_end_raw,
                    field="membership_end_date",
                )
                if membership_end_raw
                else None
            )
            source_violation = _source_date_violation(
                source_row,
                snapshot.asof_date,
            )
            components_valid = all(
                as_float(source_row.get(field)) is not None
                for field in COMPONENT_FIELDS
            )
            reasons: list[str] = []
            if (
                str(
                    source_row.get(
                        "stage11_calibration_input_eligible_flag"
                    )
                    or ""
                )
                != "1"
            ):
                reasons.append("stage11_input_ineligible")
            if (
                str(
                    source_row.get("survivorship_corrected_panel_flag") or ""
                )
                != "1"
            ):
                reasons.append("not_survivorship_corrected")
            if (
                str(
                    source_row.get("stage11_calibration_panel_source") or ""
                )
                != PANEL_SOURCE
            ):
                reasons.append("invalid_panel_source")
            if not components_valid:
                reasons.append("missing_component_score")
            if source_violation:
                reasons.append(source_violation)
            base_eligible = not reasons
            core_eligible = base_eligible and production_universe_eligible(
                source_row,
                policy=core_universe_policy,
            )
            next_open_eligible = core_eligible and (
                membership_end is None
                or membership_end > parse_date(snapshot.asof_date)
            )
            record = {
                field: str(source_row.get(field) or "")
                for field in PANEL_BASE_FIELDS
                if field
                not in {
                    "source_sidecar_sha256",
                    "source_manifest_sha256",
                    "source_sidecar_path",
                    "base_panel_eligible_flag",
                    "base_panel_eligible_reason",
                    "core_model_eligible_flag",
                    "core_model_eligible_reason",
                    "execution_universe_eligible_flag",
                    "execution_universe_eligible_reason",
                    "split_name",
                }
            }
            record.update(
                {
                    "model_family": MODEL_FAMILY,
                    "source_sidecar_sha256": snapshot.sidecar_sha256,
                    "source_manifest_sha256": snapshot.manifest_sha256,
                    "source_sidecar_path": str(snapshot.sidecar_path),
                    "base_panel_eligible_flag": "1" if base_eligible else "0",
                    "base_panel_eligible_reason": (
                        "eligible"
                        if base_eligible
                        else ";".join(dict.fromkeys(reasons))
                    ),
                    "core_model_eligible_flag": (
                        "1" if core_eligible else "0"
                    ),
                    "core_model_eligible_reason": (
                        "eligible"
                        if core_eligible
                        else "base_panel_ineligible"
                        if not base_eligible
                        else "development_stage_core_sleeve_excluded"
                    ),
                    "execution_universe_eligible_flag": (
                        "1" if next_open_eligible else "0"
                    ),
                    "execution_universe_eligible_reason": (
                        "eligible"
                        if next_open_eligible
                        else "base_panel_ineligible"
                        if not base_eligible
                        else "core_model_ineligible"
                        if not core_eligible
                        else "membership_ends_before_next_open"
                    ),
                    "split_name": splits[snapshot.asof_date],
                }
            )
            for horizon in horizons:
                prefix = f"{horizon}d"
                price_anchor, price_forward, reason = _return_window(
                    prices.get(ticker, {}),
                    asof=snapshot.asof_date,
                    horizon=horizon,
                    source_order=sources,
                )
                bench_anchor, bench_forward, bench_reason = (
                    benchmark_windows[horizon]
                )
                benchmark_horizon_exit = benchmark_execution_windows[
                    horizon
                ][1]
                (
                    execution_entry,
                    execution_exit,
                    execution_reason,
                    execution_outcome_type,
                ) = _execution_window(
                    prices.get(ticker, {}),
                    asof=snapshot.asof_date,
                    horizon=horizon,
                    source_order=sources,
                    terminal_date=membership_end,
                    horizon_end=(
                        benchmark_horizon_exit.bar_date
                        if benchmark_horizon_exit is not None
                        else None
                    ),
                    max_terminal_staleness_days=terminal_staleness_days,
                )
                (
                    benchmark_execution_entry,
                    benchmark_execution_exit,
                    benchmark_execution_reason,
                    _,
                ) = benchmark_execution_windows[horizon]
                security_return = (
                    price_forward.value / price_anchor.value - 1.0
                    if price_anchor is not None and price_forward is not None
                    else None
                )
                benchmark_return = (
                    bench_forward.value / bench_anchor.value - 1.0
                    if bench_anchor is not None and bench_forward is not None
                    else None
                )
                excess = (
                    security_return - benchmark_return
                    if security_return is not None
                    and benchmark_return is not None
                    else None
                )
                execution_exit_value = (
                    execution_exit.value
                    if execution_outcome_type == "terminal_membership_exit"
                    and execution_exit is not None
                    else execution_exit.open_value
                    if execution_exit is not None
                    else None
                )
                execution_return = (
                    execution_exit_value / execution_entry.open_value - 1.0
                    if execution_entry is not None
                    and execution_exit is not None
                    and execution_entry.open_value is not None
                    and execution_exit_value is not None
                    else None
                )
                benchmark_execution_return = (
                    benchmark_execution_exit.open_value
                    / benchmark_execution_entry.open_value
                    - 1.0
                    if benchmark_execution_entry is not None
                    and benchmark_execution_exit is not None
                    and benchmark_execution_entry.open_value is not None
                    and benchmark_execution_exit.open_value is not None
                    else None
                )
                execution_excess = (
                    execution_return - benchmark_execution_return
                    if execution_return is not None
                    and benchmark_execution_return is not None
                    else None
                )
                return_reason = reason or bench_reason
                execution_unavailable_reason = (
                    execution_reason or benchmark_execution_reason
                )
                eligible = (
                    next_open_eligible and execution_excess is not None
                )
                record.update(
                    {
                        f"price_source_id_{prefix}": (
                            price_anchor.source_id if price_anchor else ""
                        ),
                        f"price_basis_{prefix}": (
                            price_anchor.price_basis if price_anchor else ""
                        ),
                        f"price_asof_date_{prefix}": (
                            price_anchor.bar_date.isoformat()
                            if price_anchor
                            else ""
                        ),
                        f"price_forward_date_{prefix}": (
                            price_forward.bar_date.isoformat()
                            if price_forward
                            else ""
                        ),
                        f"forward_return_{prefix}": fmt(
                            security_return,
                            12,
                        ),
                        f"benchmark_ticker_{prefix}": benchmark,
                        f"benchmark_price_source_id_{prefix}": (
                            bench_anchor.source_id if bench_anchor else ""
                        ),
                        f"benchmark_asof_date_{prefix}": (
                            bench_anchor.bar_date.isoformat()
                            if bench_anchor
                            else ""
                        ),
                        f"benchmark_forward_date_{prefix}": (
                            bench_forward.bar_date.isoformat()
                            if bench_forward
                            else ""
                        ),
                        f"benchmark_return_{prefix}": fmt(
                            benchmark_return,
                            12,
                        ),
                        f"forward_excess_return_{prefix}": fmt(excess, 12),
                        f"return_available_flag_{prefix}": (
                            "1" if excess is not None else "0"
                        ),
                        f"return_unavailable_reason_{prefix}": (
                            "" if excess is not None else return_reason
                        ),
                        f"panel_row_eligible_flag_{prefix}": (
                            "1" if eligible else "0"
                        ),
                        f"execution_price_source_id_{prefix}": (
                            execution_entry.source_id
                            if execution_entry
                            else ""
                        ),
                        f"execution_price_basis_{prefix}": (
                            "adjusted_open"
                            if execution_entry
                            and execution_entry.price_basis == "adj_close"
                            else "open"
                            if execution_entry
                            else ""
                        ),
                        f"execution_entry_date_{prefix}": (
                            execution_entry.bar_date.isoformat()
                            if execution_entry
                            else ""
                        ),
                        f"execution_exit_date_{prefix}": (
                            execution_exit.bar_date.isoformat()
                            if execution_exit
                            else ""
                        ),
                        f"execution_entry_price_{prefix}": fmt(
                            execution_entry.open_value
                            if execution_entry
                            else None,
                            12,
                        ),
                        f"execution_exit_price_{prefix}": fmt(
                            execution_exit_value,
                            12,
                        ),
                        f"execution_exit_price_basis_{prefix}": (
                            "adjusted_close"
                            if execution_outcome_type
                            == "terminal_membership_exit"
                            else "adjusted_open"
                            if execution_exit
                            and execution_exit.price_basis == "adj_close"
                            else "open"
                            if execution_exit
                            else ""
                        ),
                        f"execution_return_{prefix}": fmt(
                            execution_return,
                            12,
                        ),
                        f"execution_outcome_type_{prefix}": (
                            execution_outcome_type
                        ),
                        f"benchmark_execution_price_source_id_{prefix}": (
                            benchmark_execution_entry.source_id
                            if benchmark_execution_entry
                            else ""
                        ),
                        f"benchmark_execution_entry_date_{prefix}": (
                            benchmark_execution_entry.bar_date.isoformat()
                            if benchmark_execution_entry
                            else ""
                        ),
                        f"benchmark_execution_exit_date_{prefix}": (
                            benchmark_execution_exit.bar_date.isoformat()
                            if benchmark_execution_exit
                            else ""
                        ),
                        f"benchmark_execution_return_{prefix}": fmt(
                            benchmark_execution_return,
                            12,
                        ),
                        f"execution_excess_return_{prefix}": fmt(
                            execution_excess,
                            12,
                        ),
                        f"execution_available_flag_{prefix}": (
                            "1" if execution_excess is not None else "0"
                        ),
                        f"execution_unavailable_reason_{prefix}": (
                            ""
                            if execution_excess is not None
                            else execution_unavailable_reason
                        ),
                    }
                )
            panel_rows.append(record)
    panel_fields = (*PANEL_BASE_FIELDS, *_horizon_fields(horizons))
    if len(panel_fields) != len(set(panel_fields)):
        raise ValueError("Stage 8 panel fields contain duplicate columns")
    write_csv_atomic(paths.panel_csv, panel_fields, panel_rows)
    write_csv_atomic(paths.source_index_csv, SOURCE_INDEX_FIELDS, source_rows)
    write_csv_atomic(paths.splits_csv, SPLIT_FIELDS, _split_rows(splits))
    manifest = {
        "artifact_family": "machinery_stage8_panel",
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "development_start_date": start.isoformat(),
        "development_end_date": end.isoformat(),
        "sealed_start_date": sealed_start.isoformat(),
        "lockbox_outcomes_accessed": False,
        "snapshot_cadence": "weekly",
        "weekly_anchor_date": weekly_anchor.isoformat(),
        "weekly_selection": selection,
        "snapshot_count": len(snapshots),
        "snapshot_start_date": snapshots[0].asof_date,
        "snapshot_end_date": snapshots[-1].asof_date,
        "panel_rows": len(panel_rows),
        "horizons_trading_days": horizons,
        "calibration_return_basis": calibration_return_basis,
        "terminal_price_max_staleness_days": terminal_staleness_days,
        "terminal_outcomes_by_horizon": {
            str(horizon): sum(
                row.get(f"execution_outcome_type_{horizon}d")
                == "terminal_membership_exit"
                for row in panel_rows
            )
            for horizon in horizons
        },
        "embargo_trading_days": embargo_days,
        "purge_calendar_days": purge_calendar_days,
        "benchmark_ticker": benchmark,
        "price_source_order": sources,
        "source_mode": PANEL_SOURCE,
        "survivorship_corrected": True,
        "production_universe_policy": core_universe_policy,
        "core_model_eligible_rows": sum(
            row["core_model_eligible_flag"] == "1"
            for row in panel_rows
        ),
        "execution_universe_eligible_rows": sum(
            row["execution_universe_eligible_flag"] == "1"
            for row in panel_rows
        ),
        "report_only": True,
        "files": {
            paths.panel_csv.name: {
                "sha256": file_sha256(paths.panel_csv),
                "rows": len(panel_rows),
            },
            paths.source_index_csv.name: {
                "sha256": file_sha256(paths.source_index_csv),
                "rows": len(source_rows),
            },
            paths.splits_csv.name: {
                "sha256": file_sha256(paths.splits_csv),
                "rows": len(_split_rows(splits)),
            },
        },
    }
    write_json_atomic(paths.panel_manifest_json, manifest)
    return panel_rows, horizons, manifest


def _date_pairs(
    rows: Sequence[Mapping[str, str]],
    *,
    signal: str,
    direction: int,
    horizon: int,
    minimum_cross_section: int,
) -> tuple[list[float], list[float], list[int], int]:
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["asof_date"])].append(row)
    ics: list[float] = []
    spreads: list[float] = []
    coverages: list[int] = []
    observation_count = 0
    for date_rows in grouped.values():
        pairs = [
            (
                float(value) * direction,
                float(outcome),
            )
            for row in date_rows
            if (
                value := as_float(row.get(signal))
            )
            is not None
            and (
                outcome := as_float(
                    row.get(f"execution_excess_return_{horizon}d")
                )
            )
            is not None
            and str(row.get(f"panel_row_eligible_flag_{horizon}d") or "")
            == "1"
        ]
        observation_count += len(pairs)
        if len(pairs) < minimum_cross_section:
            continue
        ic = spearman(
            [pair[0] for pair in pairs],
            [pair[1] for pair in pairs],
        )
        spread = quintile_spread(
            [pair[0] for pair in pairs],
            [pair[1] for pair in pairs],
        )
        if ic is not None:
            ics.append(ic)
            coverages.append(len(pairs))
        if spread is not None:
            spreads.append(spread)
    return ics, spreads, coverages, observation_count


def build_diagnostics(
    config: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, str]],
    horizons: Sequence[int],
    paths: Stage8Paths,
) -> list[dict[str, object]]:
    minimum = int(
        cfg_get(config, f"{CONFIG_KEY}.minimum_cross_section", 30)
    )
    step = int(cfg_get(config, f"{CONFIG_KEY}.cadence_trading_days", 5))
    output: list[dict[str, object]] = []
    split_sets = {
        "train": {"train"},
        "validation": {"validation"},
        "holdout": {"holdout"},
        "all_development": {
            "train",
            "validation",
            "holdout",
            "embargo",
        },
    }
    for signal in SIGNAL_FIELDS:
        direction = 1 if signal in COMPONENT_FIELDS else METRIC_DIRECTIONS[signal]
        role = (
            "calibration_component"
            if signal in COMPONENT_FIELDS
            else "diagnostic_only_insufficient_historical_depth"
            if signal == "book_to_bill"
            else "metric_diagnostic"
        )
        for split_name, allowed in split_sets.items():
            selected = [
                row for row in rows if str(row.get("split_name")) in allowed
            ]
            for horizon in horizons:
                ics, spreads, coverage, observations = _date_pairs(
                    selected,
                    signal=signal,
                    direction=direction,
                    horizon=horizon,
                    minimum_cross_section=minimum,
                )
                output.append(
                    {
                        "signal": signal,
                        "signal_role": role,
                        "split_name": split_name,
                        "horizon_days": horizon,
                        "direction": direction,
                        "observation_count": observations,
                        "date_count": len(ics),
                        "mean_cross_section": fmt(mean(coverage), 6),
                        "mean_ic": fmt(mean(ics), 10),
                        "ic_std": fmt(stdev(ics), 10),
                        "ic_hit_rate": fmt(
                            mean(1.0 if value > 0 else 0.0 for value in ics),
                            8,
                        ),
                        "newey_west_t": fmt(
                            newey_west_t(
                                ics,
                                max(0, math.ceil(horizon / step) - 1),
                            ),
                            8,
                        ),
                        "mean_quintile_spread": fmt(mean(spreads), 10),
                    }
                )
    write_csv_atomic(paths.diagnostics_csv, DIAGNOSTIC_FIELDS, output)
    return output


def build_return_reconciliation(
    *,
    rows: Sequence[Mapping[str, str]],
    horizons: Sequence[int],
    paths: Stage8Paths,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    split_sets = {
        "train": {"train"},
        "validation": {"validation"},
        "holdout": {"holdout"},
        "all_development": {"train", "validation", "holdout"},
    }
    for split_name, allowed in split_sets.items():
        split_rows = [
            row
            for row in rows
            if str(row.get("split_name") or "") in allowed
            and str(
                row.get("execution_universe_eligible_flag") or ""
            )
            == "1"
        ]
        for horizon in horizons:
            evaluable = [
                row
                for row in split_rows
                if as_float(
                    row.get(
                        f"benchmark_execution_return_{horizon}d"
                    )
                )
                is not None
            ]
            close_values = [
                value
                for row in evaluable
                if (
                    value := as_float(
                        row.get(f"forward_excess_return_{horizon}d")
                    )
                )
                is not None
            ]
            execution_values = [
                value
                for row in evaluable
                if (
                    value := as_float(
                        row.get(f"execution_excess_return_{horizon}d")
                    )
                )
                is not None
            ]
            paired = [
                (close_value, execution_value)
                for row in evaluable
                if (
                    close_value := as_float(
                        row.get(f"forward_excess_return_{horizon}d")
                    )
                )
                is not None
                and (
                    execution_value := as_float(
                        row.get(f"execution_excess_return_{horizon}d")
                    )
                )
                is not None
            ]
            output.append(
                {
                    "split_name": split_name,
                    "horizon_days": horizon,
                    "core_observation_count": len(evaluable),
                    "close_available_count": len(close_values),
                    "execution_available_count": len(execution_values),
                    "both_available_count": len(paired),
                    "execution_coverage": fmt(
                        len(execution_values) / len(evaluable)
                        if evaluable
                        else None,
                        10,
                    ),
                    "terminal_outcome_count": sum(
                        str(
                            row.get(
                                f"execution_outcome_type_{horizon}d"
                            )
                            or ""
                        )
                        == "terminal_membership_exit"
                        for row in evaluable
                    ),
                    "unresolved_terminal_count": sum(
                        str(
                            row.get(
                                f"execution_unavailable_reason_{horizon}d"
                            )
                            or ""
                        )
                        == "missing_terminal_outcome"
                        for row in evaluable
                    ),
                    "mean_close_excess_return": fmt(mean(close_values), 12),
                    "mean_execution_excess_return": fmt(
                        mean(execution_values),
                        12,
                    ),
                    "mean_execution_minus_close": fmt(
                        mean(
                            execution - close
                            for close, execution in paired
                        ),
                        12,
                    ),
                    "close_execution_correlation": fmt(
                        pearson(
                            [close for close, _ in paired],
                            [execution for _, execution in paired],
                        ),
                        10,
                    ),
                }
            )
    write_csv_atomic(
        paths.return_reconciliation_csv,
        RETURN_RECONCILIATION_FIELDS,
        output,
    )
    return output


def _normalized_weights(
    raw: Mapping[str, float],
    bounds: Mapping[str, tuple[float, float]],
) -> dict[str, float]:
    lower_total = sum(bounds[field][0] for field in COMPONENT_FIELDS)
    upper_total = sum(bounds[field][1] for field in COMPONENT_FIELDS)
    if lower_total > 1.0 or upper_total < 1.0:
        raise ValueError("Stage 8 component bounds do not contain a simplex")
    targets = {
        field: float(raw.get(field, 0.0))
        for field in COMPONENT_FIELDS
    }
    if any(not math.isfinite(value) for value in targets.values()):
        raise ValueError("Stage 8 component weights must be finite")

    # Euclidean projection onto the bounded simplex. This leaves an already
    # valid configured baseline unchanged and treats search samples uniformly.
    lambda_low = min(
        targets[field] - bounds[field][1] for field in COMPONENT_FIELDS
    )
    lambda_high = max(
        targets[field] - bounds[field][0] for field in COMPONENT_FIELDS
    )
    for _ in range(100):
        midpoint = (lambda_low + lambda_high) / 2.0
        total = sum(
            min(
                bounds[field][1],
                max(bounds[field][0], targets[field] - midpoint),
            )
            for field in COMPONENT_FIELDS
        )
        if total > 1.0:
            lambda_low = midpoint
        else:
            lambda_high = midpoint
    projection_lambda = (lambda_low + lambda_high) / 2.0
    weights = {
        field: min(
            bounds[field][1],
            max(bounds[field][0], targets[field] - projection_lambda),
        )
        for field in COMPONENT_FIELDS
    }
    residual = 1.0 - sum(weights.values())
    if abs(residual) > 1e-10:
        candidates = [
            field
            for field in COMPONENT_FIELDS
            if (
                weights[field] < bounds[field][1] - 1e-12
                if residual > 0
                else weights[field] > bounds[field][0] + 1e-12
            )
        ]
        if not candidates:
            raise ValueError("Unable to project Stage 8 weights into bounds")
        field = candidates[0]
        weights[field] += residual
    if abs(sum(weights.values()) - 1.0) > 1e-8:
        raise ValueError("Unable to project Stage 8 weights into bounds")
    return weights


def _component_bounds(
    config: dict[str, Any],
) -> dict[str, tuple[float, float]]:
    raw = cfg_get(config, f"{CONFIG_KEY}.component_bounds", {})
    if not isinstance(raw, Mapping):
        raise ValueError("machinery_stage8.component_bounds must be a mapping")
    output: dict[str, tuple[float, float]] = {}
    for field in COMPONENT_FIELDS:
        value = raw.get(field)
        if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2:
            raise ValueError(f"Missing Stage 8 bounds for {field}")
        lower = float(value[0])
        upper = float(value[1])
        if lower < 0 or upper < lower or upper > 1:
            raise ValueError(f"Invalid Stage 8 bounds for {field}")
        output[field] = (lower, upper)
    _normalized_weights({field: 1.0 for field in COMPONENT_FIELDS}, output)
    return output


def _baseline_weights(
    config: dict[str, Any],
    bounds: Mapping[str, tuple[float, float]],
) -> dict[str, float]:
    raw = cfg_get(config, "machinery_scoring.component_weights", {})
    if not isinstance(raw, Mapping):
        raise ValueError("machinery scoring weights must be a mapping")
    return _normalized_weights(
        {field: float(raw[field]) for field in COMPONENT_FIELDS},
        bounds,
    )


def _score_row(
    row: Mapping[str, str],
    weights: Mapping[str, float],
) -> float | None:
    weighted = 0.0
    available = 0.0
    for field, weight in weights.items():
        value = as_float(row.get(field))
        if value is None:
            continue
        weighted += value * weight
        available += weight
    return weighted / available if available > 0 else None


def _ranked_sleeves(
    scored: Sequence[tuple[Mapping[str, str], float]],
    *,
    quantile: float,
    minimum_positions: int,
) -> tuple[
    list[tuple[Mapping[str, str], float]],
    list[tuple[Mapping[str, str], float]],
]:
    ordered = sorted(
        scored,
        key=lambda item: (
            -float(item[1]),
            str(item[0].get("ticker") or ""),
        ),
    )
    count = min(
        len(ordered),
        max(minimum_positions, math.ceil(len(ordered) * quantile)),
    )
    return ordered[:count], ordered[-count:]


def _ranked_evaluation_population(
    config: dict[str, Any],
    *,
    date_rows: Sequence[Mapping[str, str]],
    weights: Mapping[str, float],
) -> RankedEvaluationPopulation | None:
    minimum = int(
        cfg_get(config, f"{CONFIG_KEY}.minimum_cross_section", 30)
    )
    scored = [
        (row, score)
        for row in date_rows
        if str(row.get("execution_universe_eligible_flag") or "") == "1"
        and (score := _score_row(row, weights)) is not None
    ]
    if len(scored) < minimum:
        return None
    top, bottom = _ranked_sleeves(
        scored,
        quantile=float(
            cfg_get(config, f"{CONFIG_KEY}.top_quantile", 0.20)
        ),
        minimum_positions=int(
            cfg_get(config, f"{CONFIG_KEY}.minimum_positions", 10)
        ),
    )
    ordered = sorted(
        scored,
        key=lambda item: (
            -float(item[1]),
            str(item[0].get("ticker") or ""),
        ),
    )
    return RankedEvaluationPopulation(
        ordered=tuple(ordered),
        top=tuple(top),
        bottom=tuple(bottom),
    )


def _non_overlapping_values(
    observations: Sequence[tuple[str, str, float]],
) -> list[float]:
    selected: list[float] = []
    last_exit: date | None = None
    for asof_raw, exit_raw, value in sorted(observations):
        asof = parse_date(asof_raw, field="non-overlapping asof")
        if last_exit is not None and asof <= last_exit:
            continue
        selected.append(value)
        last_exit = parse_date(exit_raw, field="non-overlapping exit")
    return selected


def _equal_weights(
    sleeve: Sequence[tuple[Mapping[str, str], float]],
) -> dict[str, float]:
    if not sleeve:
        return {}
    weight = 1.0 / len(sleeve)
    return {
        str(row.get("ticker") or ""): weight
        for row, _ in sleeve
    }


def _turnover_and_cost(
    current: Mapping[str, float],
    previous: Mapping[str, float] | None,
    *,
    transaction_cost_rate: float,
) -> tuple[float, float, float]:
    previous_weights = previous or {}
    traded_notional = sum(
        abs(current.get(ticker, 0.0) - previous_weights.get(ticker, 0.0))
        for ticker in set(current) | set(previous_weights)
    )
    one_way_turnover = (
        traded_notional if previous is None else traded_notional / 2.0
    )
    return (
        one_way_turnover,
        traded_notional,
        traded_notional * transaction_cost_rate,
    )


def _stats(values: Sequence[float]) -> dict[str, Any]:
    average = mean(values)
    deviation = stdev(values)
    t_stat = (
        average / deviation * math.sqrt(len(values))
        if average is not None
        and deviation is not None
        and deviation > 0
        else None
    )
    return {
        "count": len(values),
        "mean": average or 0.0,
        "std": deviation or 0.0,
        "hit_rate": (
            sum(value > 0 for value in values) / len(values)
            if values
            else 0.0
        ),
        "t_stat": t_stat,
    }


def _horizon_objective_setting(
    config: dict[str, Any],
    *,
    name: str,
    horizon: int,
    default: float,
) -> float:
    raw = cfg_get(config, f"{CONFIG_KEY}.objective.{name}", {})
    if isinstance(raw, Mapping):
        value = raw.get(str(horizon), raw.get(horizon, default))
        return float(value)
    return default


def _product_aligned_objective(
    config: dict[str, Any],
    metrics: Mapping[str, Any],
    horizons: Sequence[int],
) -> float:
    objective = 0.0
    uncertainty = 0.0
    for horizon in horizons:
        return_scale = _horizon_objective_setting(
            config,
            name="return_scales",
            horizon=horizon,
            default=0.02 if horizon <= 21 else 0.05,
        )
        spread_scale = _horizon_objective_setting(
            config,
            name="spread_scales",
            horizon=horizon,
            default=return_scale,
        )
        if return_scale <= 0 or spread_scale <= 0:
            raise ValueError("Stage 8 objective scales must be positive")
        objective += _horizon_objective_setting(
            config,
            name="top_excess_net_weights",
            horizon=horizon,
            default=0.30,
        ) * float(metrics.get(f"mean_top_excess_net_{horizon}d") or 0.0) / return_scale
        objective += _horizon_objective_setting(
            config,
            name="non_overlapping_top_excess_net_weights",
            horizon=horizon,
            default=0.10,
        ) * float(
            metrics.get(
                f"mean_non_overlapping_top_excess_net_{horizon}d"
            )
            or 0.0
        ) / return_scale
        objective += _horizon_objective_setting(
            config,
            name="mean_ic_weights",
            horizon=horizon,
            default=0.075,
        ) * float(metrics.get(f"mean_ic_{horizon}d") or 0.0) / float(
            cfg_get(config, f"{CONFIG_KEY}.objective.ic_scale", 0.05)
        )
        objective += _horizon_objective_setting(
            config,
            name="spread_diagnostic_weights",
            horizon=horizon,
            default=0.025,
        ) * float(metrics.get(f"mean_spread_net_{horizon}d") or 0.0) / spread_scale
        uncertainty += float(
            metrics.get(f"top_excess_net_newey_west_se_{horizon}d") or 0.0
        ) / return_scale
    objective -= float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.objective.uncertainty_penalty_weight",
            0.10,
        )
    ) * uncertainty
    return objective


def evaluate_weights(
    config: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, str]],
    dates: Sequence[str],
    horizons: Sequence[int],
    weights: Mapping[str, float],
) -> dict[str, Any]:
    minimum = int(
        cfg_get(config, f"{CONFIG_KEY}.minimum_cross_section", 30)
    )
    transaction_cost_rate = float(
        cfg_get(config, f"{CONFIG_KEY}.turnover_cost_bps", 20.0)
    ) / 10000.0
    minimum_outcome_coverage = float(
        cfg_get(config, f"{CONFIG_KEY}.minimum_outcome_coverage", 0.95)
    )
    date_set = set(dates)
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        if str(row.get("asof_date")) in date_set:
            grouped[str(row["asof_date"])].append(row)
    ic_values: dict[int, list[float]] = {
        horizon: [] for horizon in horizons
    }
    spread_values: dict[int, list[float]] = {
        horizon: [] for horizon in horizons
    }
    spread_net_values: dict[int, list[float]] = {
        horizon: [] for horizon in horizons
    }
    top_excess_values: dict[int, list[float]] = {
        horizon: [] for horizon in horizons
    }
    top_excess_net_values: dict[int, list[float]] = {
        horizon: [] for horizon in horizons
    }
    top_excess_net_observations: dict[
        int, list[tuple[str, str, float]]
    ] = {horizon: [] for horizon in horizons}
    coverage_values: dict[int, list[float]] = {
        horizon: [] for horizon in horizons
    }
    outcome_coverage_values: dict[int, list[float]] = {
        horizon: [] for horizon in horizons
    }
    top_missing_dates: dict[int, int] = {
        horizon: 0 for horizon in horizons
    }
    bottom_missing_dates: dict[int, int] = {
        horizon: 0 for horizon in horizons
    }
    turnovers: list[float] = []
    transaction_costs: list[float] = []
    cohort_shares: list[float] = []
    previous_top: dict[str, float] | None = None
    previous_bottom: dict[str, float] | None = None
    date_rows: list[dict[str, object]] = []
    for asof in sorted(grouped):
        population = _ranked_evaluation_population(
            config,
            date_rows=grouped[asof],
            weights=weights,
        )
        if population is None:
            continue
        scored = population.ordered
        top_rows = population.top
        bottom_rows = population.bottom
        top_weights = _equal_weights(top_rows)
        bottom_weights = _equal_weights(bottom_rows)
        top_turnover, _, top_cost = _turnover_and_cost(
            top_weights,
            previous_top,
            transaction_cost_rate=transaction_cost_rate,
        )
        _, _, bottom_cost = _turnover_and_cost(
            bottom_weights,
            previous_bottom,
            transaction_cost_rate=transaction_cost_rate,
        )
        previous_top = top_weights
        previous_bottom = bottom_weights
        turnovers.append(top_turnover)
        transaction_costs.append(top_cost)
        cohorts: dict[str, int] = defaultdict(int)
        for row, _ in top_rows:
            cohorts[str(row.get("calibration_cohort") or "")] += 1
        if cohorts:
            cohort_shares.append(max(cohorts.values()) / len(top_rows))
        per_date: dict[str, object] = {
            "asof_date": asof,
            "ranked_cross_section": len(scored),
            "top_turnover": top_turnover,
            "top_transaction_cost": top_cost,
            "bottom_transaction_cost": bottom_cost,
        }
        for horizon in horizons:
            benchmark_available = any(
                as_float(
                    row.get(
                        f"benchmark_execution_return_{horizon}d"
                    )
                )
                is not None
                for row, _ in scored
            )
            if not benchmark_available:
                continue
            pairs = [
                (
                    row,
                    float(score),
                    float(outcome),
                )
                for row, score in scored
                if (
                    outcome := as_float(
                        row.get(f"execution_excess_return_{horizon}d")
                    )
                )
                is not None
            ]
            outcome_by_ticker = {
                str(row.get("ticker") or ""): outcome
                for row, _, outcome in pairs
            }
            outcome_coverage = len(pairs) / len(scored)
            outcome_coverage_values[horizon].append(outcome_coverage)
            per_date[f"outcome_coverage_{horizon}d"] = outcome_coverage
            top_outcomes = [
                outcome_by_ticker.get(ticker) for ticker in top_weights
            ]
            bottom_outcomes = [
                outcome_by_ticker.get(ticker) for ticker in bottom_weights
            ]
            top_outcome_missing = any(
                value is None for value in top_outcomes
            )
            bottom_outcome_missing = any(
                value is None for value in bottom_outcomes
            )
            if top_outcome_missing:
                top_missing_dates[horizon] += 1
            if bottom_outcome_missing:
                bottom_missing_dates[horizon] += 1
            if (
                len(pairs) < minimum
                or outcome_coverage < minimum_outcome_coverage
            ):
                continue
            ic = spearman(
                [pair[1] for pair in pairs],
                [pair[2] for pair in pairs],
            )
            if ic is not None:
                ic_values[horizon].append(ic)
                coverage_values[horizon].append(float(len(pairs)))
                per_date[f"ic_{horizon}d"] = ic
            if top_outcome_missing:
                continue
            top_mean = mean(
                float(value) for value in top_outcomes if value is not None
            )
            if top_mean is None:
                continue
            top_net = top_mean - top_cost
            top_excess_values[horizon].append(top_mean)
            top_excess_net_values[horizon].append(top_net)
            benchmark_exit = next(
                (
                    str(
                        row.get(
                            f"benchmark_execution_exit_date_{horizon}d"
                        )
                        or ""
                    )
                    for row, _ in scored
                    if str(
                        row.get(
                            f"benchmark_execution_exit_date_{horizon}d"
                        )
                        or ""
                    )
                ),
                "",
            )
            if benchmark_exit:
                top_excess_net_observations[horizon].append(
                    (asof, benchmark_exit, top_net)
                )
                per_date[f"benchmark_execution_exit_date_{horizon}d"] = (
                    benchmark_exit
                )
            per_date[f"top_excess_{horizon}d"] = top_mean
            per_date[f"top_excess_net_{horizon}d"] = top_net
            if bottom_outcome_missing:
                continue
            bottom_mean = mean(
                float(value)
                for value in bottom_outcomes
                if value is not None
            )
            if bottom_mean is None:
                continue
            spread = top_mean - bottom_mean
            spread_net = spread - top_cost - bottom_cost
            spread_values[horizon].append(spread)
            spread_net_values[horizon].append(spread_net)
            per_date[f"spread_{horizon}d"] = spread
            per_date[f"spread_net_{horizon}d"] = spread_net
        if len(per_date) > 1:
            date_rows.append(per_date)
    average_turnover = mean(turnovers) or 0.0
    cost_drag = mean(transaction_costs) or 0.0
    result: dict[str, Any] = {
        "avg_top_turnover": average_turnover,
        "avg_top_cohort_share": mean(cohort_shares) or 0.0,
        "cost_drag_per_period": cost_drag,
        "date_rows": date_rows,
    }
    for horizon in horizons:
        ic_stats = _stats(ic_values[horizon])
        spread_stats = _stats(spread_values[horizon])
        spread_net_stats = _stats(spread_net_values[horizon])
        top_stats = _stats(top_excess_values[horizon])
        top_net_stats = _stats(top_excess_net_values[horizon])
        overlap_lags = max(
            0,
            math.ceil(
                horizon
                / int(
                    cfg_get(
                        config,
                        f"{CONFIG_KEY}.cadence_trading_days",
                        5,
                    )
                )
            )
            - 1,
        )
        top_nw_se = newey_west_mean_standard_error(
            top_excess_net_values[horizon],
            overlap_lags,
        )
        top_nw_t = newey_west_t(
            top_excess_net_values[horizon],
            overlap_lags,
        )
        confidence_z = float(
            cfg_get(
                config,
                f"{CONFIG_KEY}.gates.top_excess_one_sided_confidence_z",
                1.281552,
            )
        )
        top_lcb = (
            float(top_net_stats["mean"]) - confidence_z * top_nw_se
            if top_nw_se is not None
            else None
        )
        non_overlapping = _non_overlapping_values(
            top_excess_net_observations[horizon]
        )
        non_overlapping_stats = _stats(non_overlapping)
        result[f"n_dates_{horizon}d"] = ic_stats["count"]
        result[f"n_spread_dates_{horizon}d"] = spread_net_stats["count"]
        result[f"n_top_dates_{horizon}d"] = top_net_stats["count"]
        result[f"mean_ic_{horizon}d"] = ic_stats["mean"]
        result[f"std_ic_{horizon}d"] = ic_stats["std"]
        result[f"ic_hit_rate_{horizon}d"] = ic_stats["hit_rate"]
        result[f"ic_t_stat_{horizon}d"] = ic_stats["t_stat"]
        result[f"mean_spread_{horizon}d"] = spread_stats["mean"]
        result[f"mean_spread_net_{horizon}d"] = spread_net_stats["mean"]
        result[f"mean_top_excess_{horizon}d"] = top_stats["mean"]
        result[f"mean_top_excess_net_{horizon}d"] = top_net_stats["mean"]
        result[f"median_top_excess_net_{horizon}d"] = (
            median(top_excess_net_values[horizon]) or 0.0
        )
        result[f"top_excess_net_newey_west_lags_{horizon}d"] = overlap_lags
        result[f"top_excess_net_newey_west_se_{horizon}d"] = top_nw_se
        result[f"top_excess_net_newey_west_t_{horizon}d"] = top_nw_t
        result[f"top_excess_net_lower_confidence_bound_{horizon}d"] = top_lcb
        result[f"n_non_overlapping_top_dates_{horizon}d"] = (
            non_overlapping_stats["count"]
        )
        result[f"mean_non_overlapping_top_excess_net_{horizon}d"] = (
            non_overlapping_stats["mean"]
        )
        result[f"non_overlapping_top_excess_hit_rate_{horizon}d"] = (
            non_overlapping_stats["hit_rate"]
        )
        result[f"top_excess_hit_rate_{horizon}d"] = top_net_stats[
            "hit_rate"
        ]
        result[f"mean_outcome_coverage_{horizon}d"] = (
            mean(outcome_coverage_values[horizon]) or 0.0
        )
        result[f"top_missing_outcome_dates_{horizon}d"] = (
            top_missing_dates[horizon]
        )
        result[f"bottom_missing_outcome_dates_{horizon}d"] = (
            bottom_missing_dates[horizon]
        )
        result[f"mean_cross_section_{horizon}d"] = (
            mean(coverage_values[horizon]) or 0.0
        )
    result["objective"] = _product_aligned_objective(
        config,
        result,
        horizons,
    )
    max_turnover = float(
        cfg_get(config, f"{CONFIG_KEY}.maximum_turnover", 0.75)
    )
    max_cohort = float(
        cfg_get(config, f"{CONFIG_KEY}.maximum_cohort_share", 0.50)
    )
    result["constraint_penalty"] = (
        max(0.0, average_turnover - max_turnover) * 0.08
        + max(
            0.0,
            float(result["avg_top_cohort_share"]) - max_cohort,
        )
        * 0.10
    )
    result["objective"] -= result["constraint_penalty"]
    return result


def _write_model_date_diagnostics(
    *,
    model_metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    horizons: Sequence[int],
    paths: Stage8Paths,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for model, split_metrics in model_metrics.items():
        for split_name, metrics in split_metrics.items():
            for date_row in metrics.get("date_rows", []):
                asof = str(date_row.get("asof_date") or "")
                for horizon in horizons:
                    if not any(
                        key in date_row
                        for key in (
                            f"ic_{horizon}d",
                            f"top_excess_{horizon}d",
                            f"spread_{horizon}d",
                        )
                    ):
                        continue
                    output.append(
                        {
                            "model": model,
                            "split_name": split_name,
                            "asof_date": asof,
                            "calendar_year": asof[:4],
                            "horizon_days": horizon,
                            "ranked_cross_section": date_row.get(
                                "ranked_cross_section",
                                "",
                            ),
                            "outcome_coverage": fmt(
                                date_row.get(
                                    f"outcome_coverage_{horizon}d"
                                ),
                                10,
                            ),
                            "ic": fmt(date_row.get(f"ic_{horizon}d"), 10),
                            "top_excess": fmt(
                                date_row.get(f"top_excess_{horizon}d"),
                                12,
                            ),
                            "top_excess_net": fmt(
                                date_row.get(
                                    f"top_excess_net_{horizon}d"
                                ),
                                12,
                            ),
                            "spread": fmt(
                                date_row.get(f"spread_{horizon}d"),
                                12,
                            ),
                            "spread_net": fmt(
                                date_row.get(f"spread_net_{horizon}d"),
                                12,
                            ),
                            "top_turnover": fmt(
                                date_row.get("top_turnover"),
                                10,
                            ),
                            "top_transaction_cost": fmt(
                                date_row.get("top_transaction_cost"),
                                12,
                            ),
                            "bottom_transaction_cost": fmt(
                                date_row.get("bottom_transaction_cost"),
                                12,
                            ),
                        }
                    )
    write_csv_atomic(
        paths.model_date_diagnostics_csv,
        MODEL_DATE_DIAGNOSTIC_FIELDS,
        output,
    )
    return output


def _write_sleeve_membership(
    config: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, str]],
    models: Mapping[str, Mapping[str, float]],
    horizons: Sequence[int],
    paths: Stage8Paths,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    split_sets = {
        "train": {"train"},
        "validation": {"validation"},
        "holdout": {"holdout"},
        "all_development": {"train", "validation", "holdout"},
    }
    universe_policy = configured_universe_policy(
        config,
        config_key=CONFIG_KEY,
    )
    bucket_count = int(
        cfg_get(config, f"{CONFIG_KEY}.diagnostics.quantile_buckets", 10)
    )
    if bucket_count < 2:
        raise ValueError("Stage 8 quantile bucket count must be at least two")
    for model, weights in models.items():
        for split_name, allowed in split_sets.items():
            grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
            for row in rows:
                if str(row.get("split_name") or "") in allowed:
                    grouped[str(row.get("asof_date") or "")].append(row)
            for asof, date_rows in sorted(grouped.items()):
                population = _ranked_evaluation_population(
                    config,
                    date_rows=date_rows,
                    weights=weights,
                )
                if population is None:
                    continue
                top_tickers = {
                    str(row.get("ticker") or "")
                    for row, _ in population.top
                }
                bottom_tickers = {
                    str(row.get("ticker") or "")
                    for row, _ in population.bottom
                }
                top_weight = 1.0 / len(population.top)
                bottom_weight = 1.0 / len(population.bottom)
                cross_section = len(population.ordered)
                for rank, (row, score) in enumerate(
                    population.ordered,
                    start=1,
                ):
                    ticker = str(row.get("ticker") or "")
                    bucket = min(
                        bucket_count,
                        math.floor((rank - 1) * bucket_count / cross_section)
                        + 1,
                    )
                    sleeve = (
                        "top"
                        if ticker in top_tickers
                        else "bottom"
                        if ticker in bottom_tickers
                        else "middle"
                    )
                    sleeve_weight = (
                        top_weight
                        if sleeve == "top"
                        else bottom_weight
                        if sleeve == "bottom"
                        else 0.0
                    )
                    for horizon in horizons:
                        outcome = as_float(
                            row.get(
                                f"execution_excess_return_{horizon}d"
                            )
                        )
                        output.append(
                            {
                                "model": model,
                                "split_name": split_name,
                                "asof_date": asof,
                                "calendar_year": asof[:4],
                                "horizon_days": horizon,
                                "universe_policy": universe_policy,
                                "rank_direction": "1_is_highest_score",
                                "ticker": ticker,
                                "calibration_cohort": str(
                                    row.get("calibration_cohort") or ""
                                ),
                                "score": fmt(score, 12),
                                "rank": rank,
                                "ranked_cross_section": cross_section,
                                "quantile": bucket,
                                "configured_sleeve": sleeve,
                                "sleeve_weight": fmt(sleeve_weight, 12),
                                "outcome_available_flag": int(
                                    outcome is not None
                                ),
                                "execution_excess_return": fmt(outcome, 12),
                                "gross_return_contribution": fmt(
                                    (
                                        sleeve_weight * outcome
                                        if outcome is not None
                                        and sleeve != "middle"
                                        else None
                                    ),
                                    12,
                                ),
                            }
                        )
    write_csv_atomic(
        paths.sleeve_membership_csv,
        SLEEVE_MEMBERSHIP_FIELDS,
        output,
    )
    return output


def _write_quantile_diagnostics(
    config: dict[str, Any],
    *,
    membership_rows: Sequence[Mapping[str, object]],
    paths: Stage8Paths,
) -> list[dict[str, object]]:
    bucket_count = int(
        cfg_get(config, f"{CONFIG_KEY}.diagnostics.quantile_buckets", 10)
    )
    by_bucket_date: dict[
        tuple[str, str, int, int, str], list[float]
    ] = defaultdict(list)
    observations: dict[tuple[str, str, int, int], int] = defaultdict(int)
    missing: dict[tuple[str, str, int, int], int] = defaultdict(int)
    universe_policy = configured_universe_policy(
        config,
        config_key=CONFIG_KEY,
    )
    for row in membership_rows:
        key = (
            str(row["model"]),
            str(row["split_name"]),
            int(str(row["horizon_days"])),
            int(str(row["quantile"])),
        )
        outcome = as_float(row.get("execution_excess_return"))
        if outcome is None:
            missing[key] += 1
            continue
        observations[key] += 1
        by_bucket_date[(*key, str(row["asof_date"]))].append(outcome)
    bucket_means: dict[tuple[str, str, int, int], list[float]] = defaultdict(list)
    for compound_key, values in by_bucket_date.items():
        bucket_mean = mean(values)
        if bucket_mean is not None:
            bucket_means[compound_key[:4]].append(bucket_mean)
    output: list[dict[str, object]] = []
    model_splits = sorted(
        {
            (str(row["model"]), str(row["split_name"]))
            for row in membership_rows
        }
    )
    horizons = sorted(
        {int(str(row["horizon_days"])) for row in membership_rows}
    )
    for model, split_name in model_splits:
        for horizon in horizons:
            for bucket in range(1, bucket_count + 1):
                key = (model, split_name, horizon, bucket)
                values = bucket_means[key]
                output.append(
                    {
                        "model": model,
                        "split_name": split_name,
                        "horizon_days": horizon,
                        "universe_policy": universe_policy,
                        "rank_direction": "1_is_highest_score",
                        "bucket_count": bucket_count,
                        "quantile": bucket,
                        "date_count": len(values),
                        "observation_count": observations[key],
                        "missing_outcome_count": missing[key],
                        "mean_execution_excess_return": fmt(
                            mean(values),
                            12,
                        ),
                        "hit_rate": fmt(
                            mean(
                                1.0 if value > 0 else 0.0
                                for value in values
                            ),
                            10,
                        ),
                    }
                )
    write_csv_atomic(
        paths.quantile_diagnostics_csv,
        QUANTILE_DIAGNOSTIC_FIELDS,
        output,
    )
    return output


def _write_regime_diagnostics(
    config: dict[str, Any],
    *,
    model_metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    horizons: Sequence[int],
    paths: Stage8Paths,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    confidence_z = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.top_excess_one_sided_confidence_z",
            1.281552,
        )
    )
    cadence = int(
        cfg_get(config, f"{CONFIG_KEY}.cadence_trading_days", 5)
    )
    for model, splits in model_metrics.items():
        for split_name, metrics in splits.items():
            by_year: dict[str, list[Mapping[str, object]]] = defaultdict(list)
            for row in metrics.get("date_rows", []):
                if isinstance(row, Mapping):
                    by_year[str(row.get("asof_date") or "")[:4]].append(row)
            for year, date_rows in sorted(by_year.items()):
                for horizon in horizons:
                    top_values = [
                        value
                        for row in date_rows
                        if (
                            value := as_float(
                                row.get(f"top_excess_net_{horizon}d")
                            )
                        )
                        is not None
                    ]
                    ic_values = [
                        value
                        for row in date_rows
                        if (
                            value := as_float(row.get(f"ic_{horizon}d"))
                        )
                        is not None
                    ]
                    spread_values = [
                        value
                        for row in date_rows
                        if (
                            value := as_float(
                                row.get(f"spread_net_{horizon}d")
                            )
                        )
                        is not None
                    ]
                    lags = max(0, math.ceil(horizon / cadence) - 1)
                    standard_error = newey_west_mean_standard_error(
                        top_values,
                        lags,
                    )
                    top_mean = mean(top_values)
                    lower_bound = (
                        top_mean - confidence_z * standard_error
                        if top_mean is not None and standard_error is not None
                        else None
                    )
                    output.append(
                        {
                            "model": model,
                            "split_name": split_name,
                            "regime_type": "calendar_year_asof",
                            "regime_value": year,
                            "horizon_days": horizon,
                            "date_count": len(top_values),
                            "mean_top_excess_net": fmt(top_mean, 12),
                            "median_top_excess_net": fmt(
                                median(top_values),
                                12,
                            ),
                            "top_excess_net_hit_rate": fmt(
                                mean(
                                    1.0 if value > 0 else 0.0
                                    for value in top_values
                                ),
                                10,
                            ),
                            "top_excess_net_newey_west_t": fmt(
                                newey_west_t(top_values, lags),
                                10,
                            ),
                            "top_excess_net_lower_confidence_bound": fmt(
                                lower_bound,
                                12,
                            ),
                            "mean_ic": fmt(mean(ic_values), 12),
                            "mean_spread_net": fmt(
                                mean(spread_values),
                                12,
                            ),
                        }
                    )
    write_csv_atomic(
        paths.regime_diagnostics_csv,
        REGIME_DIAGNOSTIC_FIELDS,
        output,
    )
    return output


def _write_ticker_attribution(
    *,
    membership_rows: Sequence[Mapping[str, object]],
    paths: Stage8Paths,
) -> list[dict[str, object]]:
    values: dict[
        tuple[str, str, int, str, str], list[tuple[float, float]]
    ] = defaultdict(list)
    sleeve_by_date: dict[
        tuple[str, str, int, str, str], list[float]
    ] = defaultdict(list)
    for row in membership_rows:
        sleeve = str(row.get("configured_sleeve") or "")
        if sleeve not in {"top", "bottom"}:
            continue
        outcome = as_float(row.get("execution_excess_return"))
        contribution = as_float(row.get("gross_return_contribution"))
        if outcome is None or contribution is None:
            continue
        model = str(row["model"])
        split_name = str(row["split_name"])
        horizon = int(str(row["horizon_days"]))
        ticker = str(row["ticker"])
        asof = str(row["asof_date"])
        values[(model, split_name, horizon, sleeve, ticker)].append(
            (outcome, contribution)
        )
        sleeve_by_date[(model, split_name, horizon, sleeve, asof)].append(
            contribution
        )
    sleeve_means: dict[tuple[str, str, int, str], float] = {}
    date_totals: dict[tuple[str, str, int, str], list[float]] = defaultdict(list)
    for key, contributions in sleeve_by_date.items():
        date_totals[key[:4]].append(sum(contributions))
    for key, totals in date_totals.items():
        sleeve_means[key] = mean(totals) or 0.0
    output: list[dict[str, object]] = []
    for key, observations in sorted(values.items()):
        model, split_name, horizon, sleeve, ticker = key
        output.append(
            {
                "model": model,
                "split_name": split_name,
                "horizon_days": horizon,
                "configured_sleeve": sleeve,
                "ticker": ticker,
                "membership_dates": len(observations),
                "mean_execution_excess_return": fmt(
                    mean(value for value, _ in observations),
                    12,
                ),
                "mean_gross_return_contribution": fmt(
                    mean(value for _, value in observations),
                    12,
                ),
                "total_mean_sleeve_contribution": fmt(
                    sleeve_means[(model, split_name, horizon, sleeve)],
                    12,
                ),
            }
        )
    write_csv_atomic(
        paths.ticker_attribution_csv,
        TICKER_ATTRIBUTION_FIELDS,
        output,
    )
    return output


def _write_component_ablations(
    config: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, str]],
    split_dates: Mapping[str, Sequence[str]],
    horizons: Sequence[int],
    models: Mapping[str, Mapping[str, float]],
    model_metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    bounds: Mapping[str, tuple[float, float]],
    paths: Stage8Paths,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for model, weights in models.items():
        for component in COMPONENT_FIELDS:
            ablated_raw = dict(weights)
            ablated_raw[component] = bounds[component][0]
            ablated = _normalized_weights(ablated_raw, bounds)
            standalone = {component: 1.0}
            for split_name, dates in split_dates.items():
                baseline_objective = float(
                    model_metrics[model][split_name]["objective"]
                )
                ablated_metrics = evaluate_weights(
                    config,
                    rows=rows,
                    dates=dates,
                    horizons=horizons,
                    weights=ablated,
                )
                standalone_metrics = evaluate_weights(
                    config,
                    rows=rows,
                    dates=dates,
                    horizons=horizons,
                    weights=standalone,
                )
                ablated_objective = float(ablated_metrics["objective"])
                output.append(
                    {
                        "model": model,
                        "split_name": split_name,
                        "component": component,
                        "ablation_method": (
                            "reduce_to_configured_lower_bound_and_reproject"
                        ),
                        "baseline_objective": fmt(
                            baseline_objective,
                            12,
                        ),
                        "ablated_objective": fmt(
                            ablated_objective,
                            12,
                        ),
                        "objective_contribution": fmt(
                            baseline_objective - ablated_objective,
                            12,
                        ),
                        "standalone_objective": fmt(
                            standalone_metrics["objective"],
                            12,
                        ),
                    }
                )
    write_csv_atomic(
        paths.component_ablation_csv,
        COMPONENT_ABLATION_FIELDS,
        output,
    )
    return output


def _candidate_registry(
    config: dict[str, Any],
    bounds: Mapping[str, tuple[float, float]],
) -> tuple[dict[str, dict[str, float]], dict[str, Any], str]:
    raw_candidates = cfg_get(
        config,
        f"{CONFIG_KEY}.candidate_registry.candidates",
        {},
    )
    if not isinstance(raw_candidates, Mapping) or not raw_candidates:
        raise ValueError("Stage 8 candidate registry is empty")
    maximum = int(
        cfg_get(
            config,
            f"{CONFIG_KEY}.candidate_registry.maximum_specifications",
            6,
        )
    )
    if len(raw_candidates) + 1 > maximum:
        raise ValueError(
            "Stage 8 candidate registry exceeds maximum_specifications"
        )
    candidates = {
        "configured_baseline": _baseline_weights(config, bounds)
    }
    for candidate_id, raw_weights in raw_candidates.items():
        identifier = str(candidate_id).strip()
        if not identifier or identifier == "configured_baseline":
            raise ValueError(f"Invalid Stage 8 candidate id: {candidate_id!r}")
        if not isinstance(raw_weights, Mapping):
            raise ValueError(
                f"Stage 8 candidate {identifier} weights must be a mapping"
            )
        missing = set(COMPONENT_FIELDS) - {
            str(field) for field in raw_weights
        }
        extra = {str(field) for field in raw_weights} - set(COMPONENT_FIELDS)
        if missing or extra:
            raise ValueError(
                f"Stage 8 candidate {identifier} field mismatch: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        candidates[identifier] = _normalized_weights(
            {
                field: float(raw_weights[field])
                for field in COMPONENT_FIELDS
            },
            bounds,
        )
    payload = {
        "artifact_family": "machinery_stage8_candidate_registry",
        "research_protocol_version": str(
            cfg_get(
                config,
                f"{CONFIG_KEY}.research_protocol_version",
                "",
            )
        ),
        "evaluation_policy_version": str(
            cfg_get(
                config,
                f"{CONFIG_KEY}.evaluation_policy_version",
                "",
            )
        ),
        "search_policy": str(
            cfg_get(
                config,
                f"{CONFIG_KEY}.candidate_registry.search_policy",
                "preregistered_only",
            )
        ),
        "maximum_specifications": maximum,
        "prior_same_panel_static_trials": int(
            cfg_get(
                config,
                f"{CONFIG_KEY}.candidate_registry.prior_same_panel_static_trials",
                0,
            )
        ),
        "prior_same_panel_walk_forward_trials": int(
            cfg_get(
                config,
                f"{CONFIG_KEY}.candidate_registry.prior_same_panel_walk_forward_trials",
                0,
            )
        ),
        "candidates": candidates,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    registry_hash = hashlib.sha256(encoded).hexdigest()
    return candidates, payload, registry_hash


def _optimize(
    config: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, str]],
    train_dates: Sequence[str],
    horizons: Sequence[int],
    bounds: Mapping[str, tuple[float, float]],
    trials: int,
    seed: int,
) -> tuple[dict[str, float], list[dict[str, Any]], str, str]:
    baseline = _baseline_weights(config, bounds)
    records: list[dict[str, Any]] = []
    registry, _, registry_hash = _candidate_registry(config, bounds)
    search_policy = str(
        cfg_get(
            config,
            f"{CONFIG_KEY}.candidate_registry.search_policy",
            "preregistered_only",
        )
    )

    def evaluate(
        trial_number: int,
        candidate_id: str,
        method: str,
        raw: Mapping[str, float],
        *,
        pre_registered: bool,
    ) -> float:
        weights = _normalized_weights(raw, bounds)
        metrics = evaluate_weights(
            config,
            rows=rows,
            dates=train_dates,
            horizons=horizons,
            weights=weights,
        )
        records.append(
            {
                "trial_number": trial_number,
                "candidate_id": candidate_id,
                "pre_registered_flag": int(pre_registered),
                "candidate_registry_sha256": registry_hash,
                "search_method": method,
                "train_objective": metrics["objective"],
                "train_avg_top_turnover": metrics["avg_top_turnover"],
                "train_avg_top_cohort_share": (
                    metrics["avg_top_cohort_share"]
                ),
                "weights_json": json.dumps(weights, sort_keys=True),
            }
        )
        return float(metrics["objective"])

    if search_policy == "preregistered_only":
        maximum = int(
            cfg_get(
                config,
                f"{CONFIG_KEY}.candidate_registry.maximum_specifications",
                len(registry),
            )
        )
        if trials > maximum or trials > len(registry):
            raise ValueError(
                "Requested Stage 8 trials exceed the pre-registered "
                "candidate budget"
            )
        for trial_number, (candidate_id, weights) in enumerate(
            list(registry.items())[:trials]
        ):
            evaluate(
                trial_number,
                candidate_id,
                "preregistered_grid",
                weights,
                pre_registered=True,
            )
        method = "preregistered_grid"
    else:
        evaluate(
            0,
            "configured_baseline",
            "configured_baseline",
            baseline,
            pre_registered=True,
        )
    try:
        if search_policy == "preregistered_only":
            raise ImportError
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        sampler = optuna.samplers.TPESampler(seed=seed)
        study = optuna.create_study(direction="maximize", sampler=sampler)

        def objective(trial: Any) -> float:
            raw = {
                field: float(
                    trial.suggest_float(
                        field,
                        bounds[field][0],
                        bounds[field][1],
                    )
                )
                for field in COMPONENT_FIELDS
            }
            return evaluate(
                trial.number + 1,
                f"optuna_trial_{trial.number + 1}",
                "optuna_tpe",
                raw,
                pre_registered=False,
            )

        study.optimize(
            objective,
            n_trials=max(0, trials - 1),
            show_progress_bar=False,
        )
        method = "optuna_tpe"
    except ImportError:
        if search_policy == "preregistered_only":
            pass
        elif search_policy != "deterministic_random_search":
            raise ValueError(
                f"Unsupported Stage 8 search policy: {search_policy!r}"
            )
        else:
            rng = random.Random(seed)
            for trial_number in range(1, trials):
                evaluate(
                    trial_number,
                    f"random_trial_{trial_number}",
                    "deterministic_random_search",
                    {field: rng.random() for field in COMPONENT_FIELDS},
                    pre_registered=False,
                )
            method = "deterministic_random_search"
    best = max(
        records,
        key=lambda row: (
            float(row["train_objective"]),
            -int(row["trial_number"]),
        ),
    )
    return (
        {
            str(key): float(value)
            for key, value in json.loads(
                str(best["weights_json"])
            ).items()
        },
        records,
        method,
        str(best["candidate_id"]),
    )


def _metric_gate(
    config: dict[str, Any],
    metrics: Mapping[str, Any],
    horizons: Sequence[int],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else primary
    minimum_dates = int(
        cfg_get(config, f"{CONFIG_KEY}.gates.minimum_evaluation_dates", 20)
    )
    minimum_ic = float(
        cfg_get(config, f"{CONFIG_KEY}.gates.minimum_mean_ic", 0.0)
    )
    minimum_hit = float(
        cfg_get(config, f"{CONFIG_KEY}.gates.minimum_ic_hit_rate", 0.50)
    )
    spread_gate_mode = str(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.spread_gate_mode",
            "diagnostic_only",
        )
    )
    if spread_gate_mode != "diagnostic_only":
        raise ValueError(
            "Stage 8 v1.3 requires spread_gate_mode=diagnostic_only"
        )
    minimum_top_excess = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.minimum_mean_top_excess_net",
            0.0,
        )
    )
    minimum_outcome_coverage = float(
        cfg_get(config, f"{CONFIG_KEY}.minimum_outcome_coverage", 0.95)
    )
    minimum_lcb = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.minimum_top_excess_net_lcb",
            0.0,
        )
    )
    minimum_non_overlapping_dates = int(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.minimum_non_overlapping_top_dates",
            4,
        )
    )
    minimum_non_overlapping_top = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.minimum_non_overlapping_top_excess_net",
            0.0,
        )
    )
    for horizon in (primary, secondary):
        if int(metrics.get(f"n_dates_{horizon}d") or 0) < minimum_dates:
            reasons.append(f"{horizon}d_insufficient_dates")
        if int(metrics.get(f"n_top_dates_{horizon}d") or 0) < minimum_dates:
            reasons.append(f"{horizon}d_insufficient_top_dates")
        if float(metrics.get(f"mean_ic_{horizon}d") or 0.0) < minimum_ic:
            reasons.append(f"{horizon}d_mean_ic_below_gate")
        if (
            float(metrics.get(f"mean_top_excess_net_{horizon}d") or 0.0)
            < minimum_top_excess
        ):
            reasons.append(f"{horizon}d_top_excess_below_gate")
        lcb = as_float(
            metrics.get(
                f"top_excess_net_lower_confidence_bound_{horizon}d"
            )
        )
        if lcb is None or lcb < minimum_lcb:
            reasons.append(f"{horizon}d_top_excess_lcb_below_gate")
        if (
            int(
                metrics.get(
                    f"n_non_overlapping_top_dates_{horizon}d"
                )
                or 0
            )
            < minimum_non_overlapping_dates
        ):
            reasons.append(
                f"{horizon}d_insufficient_non_overlapping_top_dates"
            )
        if (
            float(
                metrics.get(
                    f"mean_non_overlapping_top_excess_net_{horizon}d"
                )
                or 0.0
            )
            < minimum_non_overlapping_top
        ):
            reasons.append(
                f"{horizon}d_non_overlapping_top_excess_below_gate"
            )
        if (
            float(metrics.get(f"mean_outcome_coverage_{horizon}d") or 0.0)
            < minimum_outcome_coverage
        ):
            reasons.append(f"{horizon}d_outcome_coverage_below_gate")
    if float(metrics.get(f"ic_hit_rate_{primary}d") or 0.0) < minimum_hit:
        reasons.append(f"{primary}d_hit_rate_below_gate")
    average_turnover = as_float(metrics.get("avg_top_turnover"))
    if average_turnover is None or average_turnover > float(
        cfg_get(config, f"{CONFIG_KEY}.maximum_turnover", 0.75)
    ):
        reasons.append("turnover_above_gate")
    average_cohort_share = as_float(metrics.get("avg_top_cohort_share"))
    if average_cohort_share is None or average_cohort_share > float(
        cfg_get(config, f"{CONFIG_KEY}.maximum_cohort_share", 0.50)
    ):
        reasons.append("cohort_concentration_above_gate")
    return not reasons, reasons


def _fold_product_gate(
    config: dict[str, Any],
    metrics: Mapping[str, Any],
    horizons: Sequence[int],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    minimum_outcome_coverage = float(
        cfg_get(config, f"{CONFIG_KEY}.minimum_outcome_coverage", 0.95)
    )
    for horizon in horizons:
        if float(
            metrics.get(f"mean_top_excess_net_{horizon}d") or 0.0
        ) <= 0:
            reasons.append(f"{horizon}d_top_excess_not_positive")
        if float(
            metrics.get(f"mean_outcome_coverage_{horizon}d") or 0.0
        ) < minimum_outcome_coverage:
            reasons.append(f"{horizon}d_outcome_coverage_below_gate")
    average_turnover = as_float(metrics.get("avg_top_turnover"))
    if average_turnover is None or average_turnover > float(
        cfg_get(config, f"{CONFIG_KEY}.maximum_turnover", 0.75)
    ):
        reasons.append("turnover_above_gate")
    cohort_share = as_float(metrics.get("avg_top_cohort_share"))
    if cohort_share is None or cohort_share > float(
        cfg_get(config, f"{CONFIG_KEY}.maximum_cohort_share", 0.50)
    ):
        reasons.append("cohort_concentration_above_gate")
    return not reasons, reasons


def _fold_protocol_summary(
    config: dict[str, Any],
    metrics_by_block: Sequence[Mapping[str, Any]],
    horizons: Sequence[int],
) -> dict[str, Any]:
    minimum_blocks = int(
        cfg_get(config, f"{CONFIG_KEY}.walk_forward.minimum_blocks", 4)
    )
    minimum_pass_rate = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.walk_forward.minimum_fold_product_pass_rate",
            0.70,
        )
    )
    minimum_positive_rate = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.walk_forward.minimum_positive_top_block_rate",
            0.70,
        )
    )
    passes = [
        _fold_product_gate(config, metrics, horizons)[0]
        for metrics in metrics_by_block
    ]
    summary: dict[str, Any] = {
        "block_count": len(metrics_by_block),
        "objective_mean": mean(
            float(metrics.get("objective") or 0.0)
            for metrics in metrics_by_block
        )
        or 0.0,
        "fold_product_pass_rate": (
            sum(passes) / len(passes) if passes else 0.0
        ),
    }
    protocol_pass = (
        len(metrics_by_block) >= minimum_blocks
        and float(summary["fold_product_pass_rate"]) >= minimum_pass_rate
    )
    for horizon in horizons:
        values = [
            float(metrics.get(f"mean_top_excess_net_{horizon}d") or 0.0)
            for metrics in metrics_by_block
        ]
        positive_rate = (
            sum(value > 0 for value in values) / len(values)
            if values
            else 0.0
        )
        middle = median(values) or 0.0
        summary[f"median_block_top_excess_net_{horizon}d"] = middle
        summary[f"positive_top_block_rate_{horizon}d"] = positive_rate
        protocol_pass = (
            protocol_pass
            and middle > 0
            and positive_rate >= minimum_positive_rate
        )
    summary["protocol_pass"] = protocol_pass
    return summary


def run_calibration(
    config: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, str]],
    horizons: Sequence[int],
    paths: Stage8Paths,
    trials: int,
    walk_forward_trials: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    bounds = _component_bounds(config)
    registry, registry_payload, registry_hash = _candidate_registry(
        config,
        bounds,
    )
    if (
        str(registry_payload["search_policy"]) == "preregistered_only"
        and trials != walk_forward_trials
    ):
        raise ValueError(
            "Pre-registered Stage 8 static and walk-forward candidate "
            "budgets must match"
        )
    minimum_cross_section = int(
        cfg_get(config, f"{CONFIG_KEY}.minimum_cross_section", 30)
    )
    eligible_by_date: dict[str, int] = defaultdict(int)
    benchmark_available_by_date: dict[str, bool] = defaultdict(bool)
    for row in rows:
        asof = str(row["asof_date"])
        if str(row.get("execution_universe_eligible_flag") or "") == "1":
            eligible_by_date[asof] += 1
        if as_float(
            row.get(f"benchmark_execution_return_{horizons[0]}d")
        ) is not None:
            benchmark_available_by_date[asof] = True
    coverage_dates = sorted(
        asof
        for asof, count in eligible_by_date.items()
        if count >= minimum_cross_section
        and benchmark_available_by_date[asof]
    )
    split_dates = {
        split: sorted(
            {
                str(row["asof_date"])
                for row in rows
                if str(row.get("split_name")) == split
            }
        )
        for split in ("train", "validation", "holdout")
    }
    evaluation_dates = {
        **split_dates,
        "all_development": coverage_dates,
    }
    baseline = _baseline_weights(config, bounds)
    candidate, trial_rows, search_method, candidate_id = _optimize(
        config,
        rows=rows,
        train_dates=coverage_dates,
        horizons=horizons,
        bounds=bounds,
        trials=trials,
        seed=int(cfg_get(config, f"{CONFIG_KEY}.random_seed", 357)),
    )
    evaluated_candidate_ids = [
        str(row["candidate_id"]) for row in trial_rows
    ]
    registry_artifact = {
        **registry_payload,
        "created_at_utc": utc_now(),
        "candidate_registry_sha256": registry_hash,
        "requested_static_evaluations": trials,
        "requested_evaluations_per_outer_fold": walk_forward_trials,
        "evaluated_candidate_ids": evaluated_candidate_ids,
        "prior_same_panel_total_trials": int(
            registry_payload["prior_same_panel_static_trials"]
        )
        + int(registry_payload["prior_same_panel_walk_forward_trials"]),
        "lockbox_outcomes_accessed": False,
    }
    write_json_atomic(paths.candidate_registry_json, registry_artifact)
    write_csv_atomic(paths.trials_csv, TRIAL_FIELDS, trial_rows)
    models = {
        "configured_baseline": baseline,
        "stage8_candidate": candidate,
    }
    model_metrics: dict[str, dict[str, Any]] = {}
    for model, weights in models.items():
        model_metrics[model] = {
            split: evaluate_weights(
                config,
                rows=rows,
                dates=dates,
                horizons=horizons,
                weights=weights,
            )
            for split, dates in evaluation_dates.items()
        }
    model_date_diagnostics = _write_model_date_diagnostics(
        model_metrics=model_metrics,
        horizons=horizons,
        paths=paths,
    )
    membership_rows = _write_sleeve_membership(
        config,
        rows=rows,
        models=models,
        horizons=horizons,
        paths=paths,
    )
    quantile_diagnostics = _write_quantile_diagnostics(
        config,
        membership_rows=membership_rows,
        paths=paths,
    )
    regime_diagnostics = _write_regime_diagnostics(
        config,
        model_metrics=model_metrics,
        horizons=horizons,
        paths=paths,
    )
    ticker_attribution = _write_ticker_attribution(
        membership_rows=membership_rows,
        paths=paths,
    )
    component_ablations = _write_component_ablations(
        config,
        rows=rows,
        split_dates=evaluation_dates,
        horizons=horizons,
        models=models,
        model_metrics=model_metrics,
        bounds=bounds,
        paths=paths,
    )
    baseline_validation_gate, baseline_validation_reasons = _metric_gate(
        config,
        model_metrics["configured_baseline"]["validation"],
        horizons,
    )
    baseline_holdout_gate, baseline_holdout_reasons = _metric_gate(
        config,
        model_metrics["configured_baseline"]["holdout"],
        horizons,
    )
    candidate_validation_gate, candidate_validation_reasons = _metric_gate(
        config,
        model_metrics["stage8_candidate"]["validation"],
        horizons,
    )
    candidate_holdout_gate, candidate_holdout_reasons = _metric_gate(
        config,
        model_metrics["stage8_candidate"]["holdout"],
        horizons,
    )
    baseline_development_gate, baseline_development_reasons = _metric_gate(
        config,
        model_metrics["configured_baseline"]["all_development"],
        horizons,
    )
    candidate_development_gate, candidate_development_reasons = _metric_gate(
        config,
        model_metrics["stage8_candidate"]["all_development"],
        horizons,
    )
    improvement = (
        float(
            model_metrics["stage8_candidate"]["validation"]["objective"]
        )
        - float(
            model_metrics["configured_baseline"]["validation"]["objective"]
        )
    )
    minimum_improvement = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.minimum_validation_objective_improvement",
            0.001,
        )
    )
    static = {
        "artifact_family": "machinery_stage8_static_calibration",
        "created_at_utc": utc_now(),
        "report_only": True,
        "search_method": search_method,
        "research_protocol_version": registry_payload[
            "research_protocol_version"
        ],
        "evaluation_policy_version": registry_payload[
            "evaluation_policy_version"
        ],
        "candidate_registry_sha256": registry_hash,
        "selected_candidate_id": candidate_id,
        "static_split_gates_diagnostic_only": True,
        "trial_count": len(trial_rows),
        "horizons_trading_days": list(horizons),
        "component_bounds": {
            field: list(value) for field, value in bounds.items()
        },
        "baseline_weights": baseline,
        "candidate_weights": candidate,
        "validation_objective_improvement": improvement,
        "minimum_validation_objective_improvement": minimum_improvement,
        "baseline_validation_gate": baseline_validation_gate,
        "baseline_validation_gate_reasons": baseline_validation_reasons,
        "baseline_holdout_gate": baseline_holdout_gate,
        "baseline_holdout_gate_reasons": baseline_holdout_reasons,
        "candidate_validation_gate": candidate_validation_gate,
        "candidate_validation_gate_reasons": candidate_validation_reasons,
        "candidate_holdout_gate": candidate_holdout_gate,
        "candidate_holdout_gate_reasons": candidate_holdout_reasons,
        "candidate_improves_validation": improvement >= minimum_improvement,
        "baseline_development_gate": baseline_development_gate,
        "baseline_development_gate_reasons": baseline_development_reasons,
        "candidate_development_gate": candidate_development_gate,
        "candidate_development_gate_reasons": candidate_development_reasons,
        "metrics": {
            model: {
                split: {
                    key: value
                    for key, value in metrics.items()
                    if key != "date_rows"
                }
                for split, metrics in splits.items()
            }
            for model, splits in model_metrics.items()
        },
        "model_date_diagnostic_rows": len(model_date_diagnostics),
        "quantile_diagnostic_rows": len(quantile_diagnostics),
        "component_ablation_rows": len(component_ablations),
        "sleeve_membership_rows": len(membership_rows),
        "regime_diagnostic_rows": len(regime_diagnostics),
        "ticker_attribution_rows": len(ticker_attribution),
    }
    write_json_atomic(paths.static_summary_json, static)

    initial_train = int(
        cfg_get(
            config,
            f"{CONFIG_KEY}.walk_forward.initial_train_dates",
            156,
        )
    )
    block_size = int(
        cfg_get(
            config,
            f"{CONFIG_KEY}.walk_forward.test_block_dates",
            26,
        )
    )
    step_days = int(
        cfg_get(config, f"{CONFIG_KEY}.cadence_trading_days", 5)
    )
    embargo_periods = math.ceil(
        (
            max(horizons)
            + int(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.embargo_trading_days",
                    21,
                )
            )
        )
        / step_days
    )
    blocks: list[dict[str, Any]] = []
    candidate_fold_rows: list[dict[str, Any]] = []
    fixed_candidate_metrics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    selected_procedure_metrics: list[dict[str, Any]] = []
    improvements: list[float] = []
    wins = 0
    gate_passes = 0
    test_start = initial_train + embargo_periods
    block = 0
    seed = int(cfg_get(config, f"{CONFIG_KEY}.random_seed", 357))
    while test_start < len(coverage_dates):
        train_dates = coverage_dates[: test_start - embargo_periods]
        test_dates = coverage_dates[test_start : test_start + block_size]
        if len(train_dates) < initial_train or len(test_dates) < 4:
            break
        block_candidate, _, _, block_candidate_id = _optimize(
            config,
            rows=rows,
            train_dates=train_dates,
            horizons=horizons,
            bounds=bounds,
            trials=walk_forward_trials,
            seed=seed + block,
        )
        candidate_metrics = evaluate_weights(
            config,
            rows=rows,
            dates=test_dates,
            horizons=horizons,
            weights=block_candidate,
        )
        baseline_metrics = evaluate_weights(
            config,
            rows=rows,
            dates=test_dates,
            horizons=horizons,
            weights=baseline,
        )
        block_improvement = float(candidate_metrics["objective"]) - float(
            baseline_metrics["objective"]
        )
        gate_pass, _ = _metric_gate(config, candidate_metrics, horizons)
        fold_product_pass, _ = _fold_product_gate(
            config,
            candidate_metrics,
            horizons,
        )
        win = block_improvement > 0
        wins += int(win)
        gate_passes += int(fold_product_pass)
        improvements.append(block_improvement)
        selected_procedure_metrics.append(candidate_metrics)
        record: dict[str, object] = {
            "block": block,
            "candidate_id": block_candidate_id,
            "train_start": train_dates[0],
            "train_end": train_dates[-1],
            "test_start": test_dates[0],
            "test_end": test_dates[-1],
            "train_date_count": len(train_dates),
            "test_date_count": len(test_dates),
            "candidate_objective": candidate_metrics["objective"],
            "baseline_objective": baseline_metrics["objective"],
            "objective_improvement": block_improvement,
            "candidate_wins": int(win),
            "candidate_gate_pass": int(gate_pass),
            "candidate_fold_product_pass": int(fold_product_pass),
            "candidate_avg_top_turnover": candidate_metrics[
                "avg_top_turnover"
            ],
            "candidate_avg_top_cohort_share": candidate_metrics[
                "avg_top_cohort_share"
            ],
            "weights_json": json.dumps(block_candidate, sort_keys=True),
        }
        for horizon in horizons:
            record[f"candidate_mean_ic_{horizon}d"] = candidate_metrics[
                f"mean_ic_{horizon}d"
            ]
            record[f"baseline_mean_ic_{horizon}d"] = baseline_metrics[
                f"mean_ic_{horizon}d"
            ]
            record[f"candidate_mean_spread_net_{horizon}d"] = (
                candidate_metrics[f"mean_spread_net_{horizon}d"]
            )
            record[f"candidate_mean_top_excess_net_{horizon}d"] = (
                candidate_metrics[f"mean_top_excess_net_{horizon}d"]
            )
            record[
                f"candidate_mean_non_overlapping_top_excess_net_{horizon}d"
            ] = candidate_metrics[
                f"mean_non_overlapping_top_excess_net_{horizon}d"
            ]
        blocks.append(record)
        for fixed_candidate_id in evaluated_candidate_ids:
            fixed_weights = registry[fixed_candidate_id]
            fixed_metrics = evaluate_weights(
                config,
                rows=rows,
                dates=test_dates,
                horizons=horizons,
                weights=fixed_weights,
            )
            fixed_pass, fixed_reasons = _fold_product_gate(
                config,
                fixed_metrics,
                horizons,
            )
            fixed_candidate_metrics[fixed_candidate_id].append(fixed_metrics)
            fixed_record: dict[str, Any] = {
                "candidate_id": fixed_candidate_id,
                "candidate_role": (
                    "configured_baseline"
                    if fixed_candidate_id == "configured_baseline"
                    else "pre_registered_simple_baseline"
                ),
                "block": block,
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "test_date_count": len(test_dates),
                "objective": fixed_metrics["objective"],
                "product_gate_pass": int(fixed_pass),
                "gate_reasons": ";".join(fixed_reasons),
                "weights_json": json.dumps(
                    fixed_weights,
                    sort_keys=True,
                ),
            }
            for horizon in horizons:
                fixed_record[f"mean_ic_{horizon}d"] = fixed_metrics[
                    f"mean_ic_{horizon}d"
                ]
                fixed_record[f"mean_top_excess_net_{horizon}d"] = (
                    fixed_metrics[f"mean_top_excess_net_{horizon}d"]
                )
                fixed_record[
                    f"mean_non_overlapping_top_excess_net_{horizon}d"
                ] = fixed_metrics[
                    f"mean_non_overlapping_top_excess_net_{horizon}d"
                ]
                fixed_record[f"mean_spread_net_{horizon}d"] = (
                    fixed_metrics[f"mean_spread_net_{horizon}d"]
                )
            candidate_fold_rows.append(fixed_record)
        block += 1
        test_start += block_size
    block_fields: list[str] = list(WALK_FORWARD_FIELDS)
    for horizon in horizons:
        block_fields.extend(
            (
                f"candidate_mean_ic_{horizon}d",
                f"baseline_mean_ic_{horizon}d",
                f"candidate_mean_spread_net_{horizon}d",
                f"candidate_mean_top_excess_net_{horizon}d",
                f"candidate_mean_non_overlapping_top_excess_net_{horizon}d",
            )
        )
    write_csv_atomic(paths.walk_forward_csv, block_fields, blocks)
    candidate_fold_fields: list[str] = list(CANDIDATE_FOLD_FIELDS)
    for horizon in horizons:
        candidate_fold_fields.extend(
            (
                f"mean_ic_{horizon}d",
                f"mean_top_excess_net_{horizon}d",
                f"mean_non_overlapping_top_excess_net_{horizon}d",
                f"mean_spread_net_{horizon}d",
            )
        )
    write_csv_atomic(
        paths.candidate_fold_comparison_csv,
        candidate_fold_fields,
        candidate_fold_rows,
    )
    minimum_blocks = int(
        cfg_get(config, f"{CONFIG_KEY}.walk_forward.minimum_blocks", 4)
    )
    minimum_win_rate = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.walk_forward.minimum_win_rate",
            0.50,
        )
    )
    win_rate = wins / len(blocks) if blocks else 0.0
    gate_rate = gate_passes / len(blocks) if blocks else 0.0
    average_improvement = mean(improvements) or 0.0
    bootstrap_confidence = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.walk_forward.improvement_bootstrap_confidence",
            0.90,
        )
    )
    bootstrap_simulations = int(
        cfg_get(
            config,
            f"{CONFIG_KEY}.walk_forward.improvement_bootstrap_simulations",
            2000,
        )
    )
    minimum_noise_band = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.walk_forward.minimum_objective_improvement_noise_band",
            0.0,
        )
    )
    nested_improvement_lcb = _bootstrap_mean_lower_bound(
        improvements,
        confidence=bootstrap_confidence,
        simulations=bootstrap_simulations,
        seed=seed + 10000,
    )
    nested_summary = _fold_protocol_summary(
        config,
        selected_procedure_metrics,
        horizons,
    )
    fixed_summaries = {
        identifier: _fold_protocol_summary(config, metrics, horizons)
        for identifier, metrics in fixed_candidate_metrics.items()
    }
    selected_fixed_metrics = fixed_candidate_metrics.get(candidate_id, [])
    baseline_fixed_metrics = fixed_candidate_metrics.get(
        "configured_baseline",
        [],
    )
    selected_fixed_improvements = [
        float(candidate_metrics.get("objective") or 0.0)
        - float(baseline_metrics.get("objective") or 0.0)
        for candidate_metrics, baseline_metrics in zip(
            selected_fixed_metrics,
            baseline_fixed_metrics,
        )
    ]
    selected_fixed_improvement_lcb = _bootstrap_mean_lower_bound(
        selected_fixed_improvements,
        confidence=bootstrap_confidence,
        simulations=bootstrap_simulations,
        seed=seed + 20000,
    )
    selected_fixed_summary = fixed_summaries.get(candidate_id, {})
    baseline_fixed_summary = fixed_summaries.get(
        "configured_baseline",
        {},
    )
    selected_candidate_protocol_pass = bool(
        candidate_id != "configured_baseline"
        and selected_fixed_summary.get("protocol_pass") is True
        and selected_fixed_improvement_lcb is not None
        and selected_fixed_improvement_lcb > minimum_noise_band
    )
    nested_selection_protocol_pass = bool(
        nested_summary.get("protocol_pass") is True
        and len(blocks) >= minimum_blocks
        and win_rate >= minimum_win_rate
        and nested_improvement_lcb is not None
        and nested_improvement_lcb > minimum_noise_band
    )
    procedure_adds_value = bool(
        selected_candidate_protocol_pass
        and nested_selection_protocol_pass
    )
    baseline_protocol_pass = bool(
        baseline_fixed_summary.get("protocol_pass") is True
    )
    simple_candidate_passes = sorted(
        identifier
        for identifier, summary in fixed_summaries.items()
        if identifier != "configured_baseline"
        and summary.get("protocol_pass") is True
    )
    walk_forward = {
        "artifact_family": "machinery_stage8_walk_forward",
        "created_at_utc": utc_now(),
        "report_only": True,
        "block_count": len(blocks),
        "minimum_blocks": minimum_blocks,
        "initial_train_dates": initial_train,
        "embargo_periods": embargo_periods,
        "test_block_dates": block_size,
        "trials_per_refit": walk_forward_trials,
        "candidate_registry_sha256": registry_hash,
        "selected_candidate_id": candidate_id,
        "candidate_win_rate": win_rate,
        "candidate_gate_pass_rate": gate_rate,
        "mean_objective_improvement": average_improvement,
        "improvement_t_stat": _stats(improvements)["t_stat"],
        "improvement_bootstrap_confidence": bootstrap_confidence,
        "improvement_lower_confidence_bound": nested_improvement_lcb,
        "minimum_objective_improvement_noise_band": minimum_noise_band,
        "nested_selection_summary": nested_summary,
        "nested_selection_protocol_pass": nested_selection_protocol_pass,
        "fixed_candidate_summaries": fixed_summaries,
        "selected_fixed_candidate_summary": selected_fixed_summary,
        "selected_fixed_improvement_lower_confidence_bound": (
            selected_fixed_improvement_lcb
        ),
        "selected_candidate_protocol_pass": selected_candidate_protocol_pass,
        "baseline_protocol_pass": baseline_protocol_pass,
        "simple_candidate_passes": simple_candidate_passes,
        "strategic_exit_recommended": not simple_candidate_passes,
        "procedure_adds_value": procedure_adds_value,
    }
    write_json_atomic(paths.walk_forward_summary_json, walk_forward)
    return static, walk_forward, blocks


def _git_commit(project_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except OSError:
        return ""


def run_stage8(
    config: dict[str, Any],
    *,
    config_path: Path,
    db_path: Path,
    output_root: Path,
    trials: int,
    walk_forward_trials: int,
) -> dict[str, Any]:
    paths = stage8_paths(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    panel_rows, horizons, panel_manifest = build_panel(
        config,
        config_path=config_path,
        db_path=db_path,
        paths=paths,
    )
    diagnostics = build_diagnostics(
        config,
        rows=panel_rows,
        horizons=horizons,
        paths=paths,
    )
    return_reconciliation = build_return_reconciliation(
        rows=panel_rows,
        horizons=horizons,
        paths=paths,
    )
    static, walk_forward, blocks = run_calibration(
        config,
        rows=panel_rows,
        horizons=horizons,
        paths=paths,
        trials=trials,
        walk_forward_trials=walk_forward_trials,
    )
    baseline_ready = bool(
        static["baseline_development_gate"]
        and walk_forward["baseline_protocol_pass"]
    )
    candidate_ready = bool(
        static["candidate_development_gate"]
        and walk_forward["selected_candidate_protocol_pass"]
        and walk_forward["procedure_adds_value"]
    )
    recommendation = (
        "stage8_candidate"
        if candidate_ready
        else "configured_baseline"
        if baseline_ready
        else "none"
    )
    blockers: list[str] = []
    if recommendation == "none":
        blockers.append("no_model_passed_v13_product_aligned_oos_gates")
    if not blocks:
        blockers.append("walk_forward_blocks_missing")
    acceptance = {
        "acceptance": "PASS",
        "stage8_implementation_status": "COMPLETE",
        "research_protocol_version": str(
            cfg_get(
                config,
                f"{CONFIG_KEY}.research_protocol_version",
                "",
            )
        ),
        "evaluation_policy_version": str(
            cfg_get(
                config,
                f"{CONFIG_KEY}.evaluation_policy_version",
                "",
            )
        ),
        "stage9_readiness": (
            "READY" if recommendation != "none" else "BLOCKED"
        ),
        "recommended_model_for_stage9": recommendation,
        "recommended_weights": (
            static["candidate_weights"]
            if recommendation == "stage8_candidate"
            else static["baseline_weights"]
            if recommendation == "configured_baseline"
            else {}
        ),
        "production_promotion_performed": False,
        "live_dashboard_modified": False,
        "lockbox_outcomes_accessed": False,
        "development_end_date": panel_manifest["development_end_date"],
        "sealed_start_date": panel_manifest["sealed_start_date"],
        "panel_snapshot_count": panel_manifest["snapshot_count"],
        "panel_row_count": panel_manifest["panel_rows"],
        "diagnostic_row_count": len(diagnostics),
        "return_reconciliation_row_count": len(return_reconciliation),
        "walk_forward_block_count": len(blocks),
        "strategic_exit_recommended": walk_forward[
            "strategic_exit_recommended"
        ],
        "baseline_ready": baseline_ready,
        "candidate_ready": candidate_ready,
        "blockers": blockers,
    }
    write_json_atomic(paths.acceptance_json, acceptance)
    project_root = config_path.parents[2]
    artifact_paths = [
        paths.panel_csv,
        paths.source_index_csv,
        paths.splits_csv,
        paths.panel_manifest_json,
        paths.diagnostics_csv,
        paths.return_reconciliation_csv,
        paths.model_date_diagnostics_csv,
        paths.quantile_diagnostics_csv,
        paths.component_ablation_csv,
        paths.sleeve_membership_csv,
        paths.candidate_registry_json,
        paths.candidate_fold_comparison_csv,
        paths.regime_diagnostics_csv,
        paths.ticker_attribution_csv,
        paths.trials_csv,
        paths.static_summary_json,
        paths.walk_forward_csv,
        paths.walk_forward_summary_json,
        paths.acceptance_json,
    ]
    manifest = {
        "artifact_family": "machinery_stage8_run",
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "config_path": str(config_path),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "effective_stage8_config_sha256": stage8_config_sha256(config),
        "git_commit": _git_commit(project_root),
        "calibration_source_sha256": file_sha256(Path(__file__)),
        "source_db_path": str(db_path),
        "source_db_modified": False,
        "report_only": True,
        "production_promotion_performed": False,
        "files": {
            path.name: {
                "path": str(path),
                "sha256": file_sha256(path),
            }
            for path in artifact_paths
        },
    }
    write_json_atomic(paths.run_manifest_json, manifest)
    return acceptance


def _manifest_hash_issues(
    paths: Stage8Paths,
    manifest: Mapping[str, Any],
) -> list[str]:
    issues: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return ["run manifest missing files mapping"]
    for name, metadata in files.items():
        if not isinstance(metadata, Mapping):
            issues.append(f"invalid manifest metadata for {name}")
            continue
        path = Path(str(metadata.get("path") or ""))
        if not path.exists():
            issues.append(f"missing Stage 8 artifact {path}")
            continue
        if file_sha256(path) != str(metadata.get("sha256") or ""):
            issues.append(f"Stage 8 artifact hash mismatch {path}")
    if paths.run_manifest_json.name in files:
        issues.append("run manifest must not hash itself")
    return issues


def validate_stage8(
    config: dict[str, Any],
    *,
    output_root: Path,
    require_stage9_ready: bool,
) -> dict[str, Any]:
    paths = stage8_paths(output_root)
    required = [
        paths.panel_csv,
        paths.source_index_csv,
        paths.splits_csv,
        paths.panel_manifest_json,
        paths.diagnostics_csv,
        paths.return_reconciliation_csv,
        paths.model_date_diagnostics_csv,
        paths.quantile_diagnostics_csv,
        paths.component_ablation_csv,
        paths.sleeve_membership_csv,
        paths.candidate_registry_json,
        paths.candidate_fold_comparison_csv,
        paths.regime_diagnostics_csv,
        paths.ticker_attribution_csv,
        paths.trials_csv,
        paths.static_summary_json,
        paths.walk_forward_csv,
        paths.walk_forward_summary_json,
        paths.acceptance_json,
        paths.run_manifest_json,
    ]
    issues = [
        f"missing Stage 8 artifact {path}"
        for path in required
        if not path.exists() or path.stat().st_size == 0
    ]
    if issues:
        result = {
            "acceptance": "FAIL",
            "stage9_readiness": "UNKNOWN",
            "issues": issues,
        }
        write_json_atomic(paths.validation_json, result)
        return result
    manifest = json.loads(paths.run_manifest_json.read_text(encoding="utf-8"))
    issues.extend(_manifest_hash_issues(paths, manifest))
    if manifest.get("calibration_source_sha256") != file_sha256(
        Path(__file__)
    ):
        issues.append("Stage 8 calibration source changed after run")
    if manifest.get("effective_stage8_config_sha256") != stage8_config_sha256(
        config
    ):
        issues.append("effective Stage 8 configuration changed after run")
    panel_manifest = json.loads(
        paths.panel_manifest_json.read_text(encoding="utf-8")
    )
    acceptance = json.loads(paths.acceptance_json.read_text(encoding="utf-8"))
    static = json.loads(paths.static_summary_json.read_text(encoding="utf-8"))
    walk = json.loads(
        paths.walk_forward_summary_json.read_text(encoding="utf-8")
    )
    registry_artifact = json.loads(
        paths.candidate_registry_json.read_text(encoding="utf-8")
    )
    bounds = _component_bounds(config)
    _, _, expected_registry_hash = _candidate_registry(config, bounds)
    if (
        registry_artifact.get("candidate_registry_sha256")
        != expected_registry_hash
    ):
        issues.append("Stage 8 candidate registry changed after run")
    if registry_artifact.get("lockbox_outcomes_accessed") is not False:
        issues.append("Stage 8 candidate registry does not seal the lockbox")
    expected_protocol = str(
        cfg_get(config, f"{CONFIG_KEY}.research_protocol_version", "")
    )
    expected_policy = str(
        cfg_get(config, f"{CONFIG_KEY}.evaluation_policy_version", "")
    )
    if acceptance.get("research_protocol_version") != expected_protocol:
        issues.append("Stage 8 research protocol version mismatch")
    if acceptance.get("evaluation_policy_version") != expected_policy:
        issues.append("Stage 8 evaluation policy version mismatch")
    if static.get("static_split_gates_diagnostic_only") is not True:
        issues.append("Stage 8 static splits are not diagnostic-only")
    sealed_start = parse_date(
        cfg_get(config, f"{CONFIG_KEY}.sealed_start_date")
    )
    development_end = parse_date(
        cfg_get(config, f"{CONFIG_KEY}.development_end_date")
    )
    if panel_manifest.get("lockbox_outcomes_accessed") is not False:
        issues.append("panel manifest does not prove lockbox exclusion")
    if panel_manifest.get("survivorship_corrected") is not True:
        issues.append("panel is not survivorship-corrected")
    configured_core_policy = configured_universe_policy(
        config,
        config_key=CONFIG_KEY,
    )
    if (
        panel_manifest.get("production_universe_policy")
        != configured_core_policy
    ):
        issues.append("Stage 8 production universe policy mismatch")
    if panel_manifest.get("source_mode") != PANEL_SOURCE:
        issues.append("panel source mode mismatch")
    if panel_manifest.get("calibration_return_basis") != str(
        cfg_get(config, f"{CONFIG_KEY}.calibration_return_basis", "")
    ):
        issues.append("Stage 8 calibration return basis mismatch")
    panel_rows = read_csv_rows(paths.panel_csv)
    if len(panel_rows) != int(panel_manifest.get("panel_rows") or -1):
        issues.append("panel row count does not match manifest")
    horizons = [
        int(item)
        for item in panel_manifest.get("horizons_trading_days", [])
    ]
    for row in panel_rows:
        expected_core_eligible = (
            str(row.get("base_panel_eligible_flag") or "") == "1"
            and production_universe_eligible(
                row,
                policy=configured_core_policy,
            )
        )
        if (
            str(row.get("core_model_eligible_flag") or "") == "1"
        ) != expected_core_eligible:
            issues.append("Stage 8 core-model eligibility policy mismatch")
            break
        asof = parse_date(row.get("asof_date"), field="panel asof")
        membership_start_raw = str(
            row.get("membership_start_date") or ""
        )
        if not membership_start_raw:
            issues.append("panel row missing membership_start_date")
            break
        membership_end_raw = str(row.get("membership_end_date") or "")
        membership_end = (
            parse_date(membership_end_raw, field="membership_end_date")
            if membership_end_raw
            else None
        )
        expected_execution_eligible = expected_core_eligible and (
            membership_end is None or membership_end > asof
        )
        if (
            str(
                row.get("execution_universe_eligible_flag") or ""
            )
            == "1"
        ) != expected_execution_eligible:
            issues.append("Stage 8 execution-universe eligibility mismatch")
            break
        if asof >= sealed_start:
            issues.append(f"sealed-window panel row detected: {asof}")
            break
        if str(row.get("survivorship_corrected_panel_flag") or "") != "1":
            issues.append("panel contains non-survivorship-corrected row")
            break
        for horizon in horizons:
            forward = str(row.get(f"price_forward_date_{horizon}d") or "")
            benchmark_forward = str(
                row.get(f"benchmark_forward_date_{horizon}d") or ""
            )
            execution_entry = str(
                row.get(f"execution_entry_date_{horizon}d") or ""
            )
            execution_exit = str(
                row.get(f"execution_exit_date_{horizon}d") or ""
            )
            benchmark_execution_exit = str(
                row.get(f"benchmark_execution_exit_date_{horizon}d") or ""
            )
            if forward and parse_date(forward) > development_end:
                issues.append(f"{horizon}d security label crosses lockbox")
                break
            if (
                benchmark_forward
                and parse_date(benchmark_forward) > development_end
            ):
                issues.append(f"{horizon}d benchmark label crosses lockbox")
                break
            if execution_entry and parse_date(execution_entry) <= asof:
                issues.append(f"{horizon}d execution is not D+1 or later")
                break
            if execution_exit and parse_date(execution_exit) > development_end:
                issues.append(f"{horizon}d execution label crosses lockbox")
                break
            if (
                benchmark_execution_exit
                and parse_date(benchmark_execution_exit) > development_end
            ):
                issues.append(
                    f"{horizon}d benchmark execution label crosses lockbox"
                )
                break
            execution_available = str(
                row.get(f"execution_available_flag_{horizon}d") or ""
            )
            execution_excess = as_float(
                row.get(f"execution_excess_return_{horizon}d")
            )
            expected_panel_eligible = (
                expected_execution_eligible and execution_excess is not None
            )
            if (
                str(
                    row.get(f"panel_row_eligible_flag_{horizon}d")
                    or ""
                )
                == "1"
            ) != expected_panel_eligible:
                issues.append(
                    f"{horizon}d executable panel eligibility mismatch"
                )
                break
            if execution_available == "1" and (
                not execution_entry
                or not execution_exit
                or as_float(row.get(f"execution_return_{horizon}d")) is None
                or as_float(
                    row.get(f"benchmark_execution_return_{horizon}d")
                )
                is None
                or execution_excess is None
            ):
                issues.append(f"{horizon}d execution availability mismatch")
                break
            outcome_type = str(
                row.get(f"execution_outcome_type_{horizon}d") or ""
            )
            if execution_available == "1" and outcome_type not in {
                "scheduled_horizon",
                "terminal_membership_exit",
            }:
                issues.append(f"{horizon}d execution outcome type missing")
                break
            unavailable_reason = str(
                row.get(f"execution_unavailable_reason_{horizon}d") or ""
            )
            if expected_execution_eligible and unavailable_reason == (
                "missing_terminal_outcome"
            ):
                issues.append(f"{horizon}d unresolved terminal outcome")
                break
            if outcome_type == "terminal_membership_exit":
                if (
                    membership_end is None
                    or not execution_exit
                    or parse_date(execution_exit) > membership_end
                    or str(
                        row.get(
                            f"execution_exit_price_basis_{horizon}d"
                        )
                        or ""
                    )
                    != "adjusted_close"
                ):
                    issues.append(
                        f"{horizon}d invalid terminal execution provenance"
                    )
                    break
        if issues:
            break
    for key in ("baseline_weights", "candidate_weights"):
        weights = static.get(key)
        if not isinstance(weights, Mapping):
            issues.append(f"static summary missing {key}")
            continue
        total = sum(float(value) for value in weights.values())
        if abs(total - 1.0) > 1e-8:
            issues.append(f"{key} does not sum to 1")
        for field, (lower, upper) in bounds.items():
            value = float(weights.get(field, -1.0))
            if value < lower - 1e-8 or value > upper + 1e-8:
                issues.append(f"{key}.{field} outside bounds")
    if int(walk.get("block_count") or 0) < int(
        cfg_get(config, f"{CONFIG_KEY}.walk_forward.minimum_blocks", 4)
    ):
        issues.append("walk-forward block count below configured minimum")
    membership_rows = read_csv_rows(paths.sleeve_membership_csv)
    if not membership_rows:
        issues.append("Stage 8 sleeve membership is empty")
    else:
        if any(
            row.get("rank_direction") != "1_is_highest_score"
            for row in membership_rows
        ):
            issues.append("Stage 8 sleeve rank direction mismatch")
        if any(
            row.get("universe_policy") != configured_core_policy
            for row in membership_rows
        ):
            issues.append("Stage 8 sleeve universe policy mismatch")
    quantile_rows = read_csv_rows(paths.quantile_diagnostics_csv)
    if any(
        row.get("rank_direction") != "1_is_highest_score"
        for row in quantile_rows
    ):
        issues.append("Stage 8 quantile rank direction mismatch")
    candidate_fold_rows = read_csv_rows(paths.candidate_fold_comparison_csv)
    expected_fold_rows = int(walk.get("block_count") or 0) * len(
        registry_artifact.get("evaluated_candidate_ids") or []
    )
    if len(candidate_fold_rows) != expected_fold_rows:
        issues.append("Stage 8 candidate-fold comparison is incomplete")
    if acceptance.get("production_promotion_performed") is not False:
        issues.append("Stage 8 unexpectedly performed production promotion")
    if acceptance.get("live_dashboard_modified") is not False:
        issues.append("Stage 8 unexpectedly modified the live dashboard")
    stage9_readiness = str(acceptance.get("stage9_readiness") or "UNKNOWN")
    if require_stage9_ready and stage9_readiness != "READY":
        issues.append("Stage 8 did not clear the Stage 9 readiness gate")
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "stage9_readiness": stage9_readiness,
        "recommended_model_for_stage9": acceptance.get(
            "recommended_model_for_stage9",
            "none",
        ),
        "panel_rows": len(panel_rows),
        "snapshot_count": panel_manifest.get("snapshot_count"),
        "diagnostic_rows": len(read_csv_rows(paths.diagnostics_csv)),
        "trial_rows": len(read_csv_rows(paths.trials_csv)),
        "walk_forward_blocks": len(read_csv_rows(paths.walk_forward_csv)),
        "sleeve_membership_rows": len(membership_rows),
        "candidate_fold_rows": len(candidate_fold_rows),
        "lockbox_outcomes_accessed": False,
        "issues": issues,
    }
    write_csv_atomic(
        paths.validation_csv,
        (
            "acceptance",
            "stage9_readiness",
            "recommended_model_for_stage9",
            "panel_rows",
            "snapshot_count",
            "diagnostic_rows",
            "trial_rows",
            "walk_forward_blocks",
            "sleeve_membership_rows",
            "candidate_fold_rows",
            "lockbox_outcomes_accessed",
            "issues",
        ),
        [
            {
                **result,
                "issues": ";".join(issues),
            }
        ],
    )
    write_json_atomic(paths.validation_json, result)
    return result
