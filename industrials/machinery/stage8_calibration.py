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
    "rank_ready_flag",
    "model_status",
    "score_confidence",
    "final_score",
    *SIGNAL_FIELDS,
    "latest_adj_close",
    "avg_dollar_volume_60d",
    "market_cap",
    "latest_borrow_fee_rate",
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
    "search_method",
    "train_objective",
    "train_avg_top_turnover",
    "train_avg_top_cohort_share",
    "weights_json",
)
WALK_FORWARD_FIELDS = (
    "block",
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
    "candidate_avg_top_turnover",
    "candidate_avg_top_cohort_share",
    "weights_json",
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
class Stage8Paths:
    root: Path
    panel_csv: Path
    source_index_csv: Path
    splits_csv: Path
    panel_manifest_json: Path
    diagnostics_csv: Path
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
        return [
            {str(key): str(value or "") for key, value in row.items()}
            for row in csv.DictReader(handle)
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


def newey_west_t(values: Sequence[float], lags: int) -> float | None:
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
    standard_error = math.sqrt(long_run_variance / len(values))
    return average / standard_error if standard_error > 0 else None


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
        if value is None or value <= 0:
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
) -> tuple[PricePoint | None, PricePoint | None, str]:
    asof_date = parse_date(asof, field="asof_date")
    partial = False
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
        if entry_index >= len(series) or exit_index >= len(series):
            partial = True
            continue
        entry = series[entry_index]
        exit_point = series[exit_index]
        if (
            entry.open_value is None
            or entry.open_value <= 0
            or exit_point.open_value is None
            or exit_point.open_value <= 0
        ):
            continue
        return entry, exit_point, ""
    if partial:
        return None, None, "execution_window_crosses_development_end"
    return None, None, "missing_d1_open_execution_price"


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
                f"execution_return_{prefix}",
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
                execution_entry, execution_exit, execution_reason = (
                    _execution_window(
                        prices.get(ticker, {}),
                        asof=snapshot.asof_date,
                        horizon=horizon,
                        source_order=sources,
                    )
                )
                (
                    benchmark_execution_entry,
                    benchmark_execution_exit,
                    benchmark_execution_reason,
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
                execution_return = (
                    execution_exit.open_value
                    / execution_entry.open_value
                    - 1.0
                    if execution_entry is not None
                    and execution_exit is not None
                    and execution_entry.open_value is not None
                    and execution_exit.open_value is not None
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
                eligible = base_eligible and excess is not None
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
                            execution_exit.open_value
                            if execution_exit
                            else None,
                            12,
                        ),
                        f"execution_return_{prefix}": fmt(
                            execution_return,
                            12,
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
        "embargo_trading_days": embargo_days,
        "purge_calendar_days": purge_calendar_days,
        "benchmark_ticker": benchmark,
        "price_source_order": sources,
        "source_mode": PANEL_SOURCE,
        "survivorship_corrected": True,
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
                    row.get(f"forward_excess_return_{horizon}d")
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
        "all_development": {"train", "validation", "holdout"},
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
    top_quantile = float(
        cfg_get(config, f"{CONFIG_KEY}.top_quantile", 0.20)
    )
    min_positions = int(
        cfg_get(config, f"{CONFIG_KEY}.minimum_positions", 10)
    )
    turnover_cost_bps = float(
        cfg_get(config, f"{CONFIG_KEY}.turnover_cost_bps", 20.0)
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
    coverage_values: dict[int, list[float]] = {
        horizon: [] for horizon in horizons
    }
    turnovers: list[float] = []
    cohort_shares: list[float] = []
    previous_top: set[str] | None = None
    primary = horizons[0]
    date_rows: list[dict[str, object]] = []
    for asof in sorted(grouped):
        scored = [
            (row, score)
            for row in grouped[asof]
            if str(row.get("base_panel_eligible_flag") or "") == "1"
            and (score := _score_row(row, weights)) is not None
        ]
        primary_rows = [
            (row, score)
            for row, score in scored
            if str(
                row.get(f"panel_row_eligible_flag_{primary}d") or ""
            )
            == "1"
        ]
        if len(primary_rows) >= minimum:
            primary_rows.sort(
                key=lambda item: (
                    -float(item[1]),
                    str(item[0].get("ticker") or ""),
                )
            )
            top_count = min(
                len(primary_rows),
                max(
                    min_positions,
                    math.ceil(len(primary_rows) * top_quantile),
                ),
            )
            top_rows = primary_rows[:top_count]
            top = {str(row.get("ticker")) for row, _ in top_rows}
            if previous_top is not None and top:
                turnovers.append(1.0 - len(top & previous_top) / len(top))
            previous_top = top
            cohorts: dict[str, int] = defaultdict(int)
            for row, _ in top_rows:
                cohorts[str(row.get("calibration_cohort") or "")] += 1
            if cohorts:
                cohort_shares.append(max(cohorts.values()) / len(top_rows))
        per_date: dict[str, object] = {"asof_date": asof}
        for horizon in horizons:
            pairs = [
                (
                    float(score),
                    float(outcome),
                )
                for row, score in scored
                if str(
                    row.get(f"panel_row_eligible_flag_{horizon}d") or ""
                )
                == "1"
                and (
                    outcome := as_float(
                        row.get(f"forward_excess_return_{horizon}d")
                    )
                )
                is not None
            ]
            if len(pairs) < minimum:
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
                ic_values[horizon].append(ic)
                coverage_values[horizon].append(float(len(pairs)))
                per_date[f"ic_{horizon}d"] = ic
            if spread is not None:
                spread_values[horizon].append(spread)
                per_date[f"spread_{horizon}d"] = spread
        if len(per_date) > 1:
            date_rows.append(per_date)
    average_turnover = mean(turnovers) or 0.0
    cost_drag = average_turnover * 2.0 * turnover_cost_bps / 10000.0
    result: dict[str, Any] = {
        "avg_top_turnover": average_turnover,
        "avg_top_cohort_share": mean(cohort_shares) or 0.0,
        "cost_drag_per_period": cost_drag,
        "date_rows": date_rows,
    }
    for horizon in horizons:
        ic_stats = _stats(ic_values[horizon])
        spread_stats = _stats(spread_values[horizon])
        result[f"n_dates_{horizon}d"] = ic_stats["count"]
        result[f"mean_ic_{horizon}d"] = ic_stats["mean"]
        result[f"std_ic_{horizon}d"] = ic_stats["std"]
        result[f"ic_hit_rate_{horizon}d"] = ic_stats["hit_rate"]
        result[f"ic_t_stat_{horizon}d"] = ic_stats["t_stat"]
        result[f"mean_spread_{horizon}d"] = spread_stats["mean"]
        result[f"mean_spread_net_{horizon}d"] = (
            float(spread_stats["mean"]) - cost_drag
        )
        result[f"mean_cross_section_{horizon}d"] = (
            mean(coverage_values[horizon]) or 0.0
        )
    secondary = horizons[1] if len(horizons) > 1 else primary
    stability = float(
        cfg_get(config, f"{CONFIG_KEY}.stability_penalty", 0.10)
    )
    result["objective"] = (
        0.48 * float(result[f"mean_ic_{primary}d"])
        + 0.34 * float(result[f"mean_ic_{secondary}d"])
        + 0.06
        * (float(result[f"ic_hit_rate_{primary}d"]) - 0.50)
        + 0.04
        * (float(result[f"ic_hit_rate_{secondary}d"]) - 0.50)
        + 0.05 * float(result[f"mean_spread_net_{primary}d"])
        + 0.03 * float(result[f"mean_spread_net_{secondary}d"])
        - stability * float(result[f"std_ic_{primary}d"])
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


def _optimize(
    config: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, str]],
    train_dates: Sequence[str],
    horizons: Sequence[int],
    bounds: Mapping[str, tuple[float, float]],
    trials: int,
    seed: int,
) -> tuple[dict[str, float], list[dict[str, Any]], str]:
    baseline = _baseline_weights(config, bounds)
    records: list[dict[str, Any]] = []

    def evaluate(
        trial_number: int,
        method: str,
        raw: Mapping[str, float],
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

    evaluate(0, "configured_baseline", baseline)
    try:
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
            return evaluate(trial.number + 1, "optuna_tpe", raw)

        study.optimize(
            objective,
            n_trials=max(0, trials - 1),
            show_progress_bar=False,
        )
        method = "optuna_tpe"
    except ImportError:
        rng = random.Random(seed)
        for trial_number in range(1, trials):
            evaluate(
                trial_number,
                "deterministic_random_search",
                {field: rng.random() for field in COMPONENT_FIELDS},
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
    minimum_spread = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.minimum_mean_spread_net",
            0.0,
        )
    )
    for horizon in (primary, secondary):
        if int(metrics.get(f"n_dates_{horizon}d") or 0) < minimum_dates:
            reasons.append(f"{horizon}d_insufficient_dates")
        if float(metrics.get(f"mean_ic_{horizon}d") or 0.0) < minimum_ic:
            reasons.append(f"{horizon}d_mean_ic_below_gate")
        if (
            float(metrics.get(f"mean_spread_net_{horizon}d") or 0.0)
            < minimum_spread
        ):
            reasons.append(f"{horizon}d_spread_below_gate")
    if float(metrics.get(f"ic_hit_rate_{primary}d") or 0.0) < minimum_hit:
        reasons.append(f"{primary}d_hit_rate_below_gate")
    if float(metrics.get("avg_top_turnover") or 1.0) > float(
        cfg_get(config, f"{CONFIG_KEY}.maximum_turnover", 0.75)
    ):
        reasons.append("turnover_above_gate")
    if float(metrics.get("avg_top_cohort_share") or 1.0) > float(
        cfg_get(config, f"{CONFIG_KEY}.maximum_cohort_share", 0.50)
    ):
        reasons.append("cohort_concentration_above_gate")
    return not reasons, reasons


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
    baseline = _baseline_weights(config, bounds)
    candidate, trial_rows, search_method = _optimize(
        config,
        rows=rows,
        train_dates=split_dates["train"],
        horizons=horizons,
        bounds=bounds,
        trials=trials,
        seed=int(cfg_get(config, f"{CONFIG_KEY}.random_seed", 357)),
    )
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
            for split, dates in split_dates.items()
        }
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
    }
    write_json_atomic(paths.static_summary_json, static)

    minimum_cross_section = int(
        cfg_get(config, f"{CONFIG_KEY}.minimum_cross_section", 30)
    )
    eligible_by_date: dict[str, int] = defaultdict(int)
    for row in rows:
        if (
            str(
                row.get(
                    f"panel_row_eligible_flag_{horizons[0]}d"
                )
                or ""
            )
            == "1"
        ):
            eligible_by_date[str(row["asof_date"])] += 1
    coverage_dates = sorted(
        asof
        for asof, count in eligible_by_date.items()
        if count >= minimum_cross_section
    )
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
        block_candidate, _, _ = _optimize(
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
        win = block_improvement > 0
        wins += int(win)
        gate_passes += int(gate_pass)
        improvements.append(block_improvement)
        record: dict[str, object] = {
            "block": block,
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
        blocks.append(record)
        block += 1
        test_start += block_size
    block_fields: list[str] = list(WALK_FORWARD_FIELDS)
    for horizon in horizons:
        block_fields.extend(
            (
                f"candidate_mean_ic_{horizon}d",
                f"baseline_mean_ic_{horizon}d",
                f"candidate_mean_spread_net_{horizon}d",
            )
        )
    write_csv_atomic(paths.walk_forward_csv, block_fields, blocks)
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
    procedure_adds_value = (
        len(blocks) >= minimum_blocks
        and win_rate >= minimum_win_rate
        and average_improvement > 0
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
        "candidate_win_rate": win_rate,
        "candidate_gate_pass_rate": gate_rate,
        "mean_objective_improvement": average_improvement,
        "improvement_t_stat": _stats(improvements)["t_stat"],
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
    static, walk_forward, blocks = run_calibration(
        config,
        rows=panel_rows,
        horizons=horizons,
        paths=paths,
        trials=trials,
        walk_forward_trials=walk_forward_trials,
    )
    baseline_ready = bool(
        static["baseline_validation_gate"]
        and static["baseline_holdout_gate"]
    )
    candidate_ready = bool(
        static["candidate_validation_gate"]
        and static["candidate_holdout_gate"]
        and static["candidate_improves_validation"]
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
        blockers.append("no_model_passed_validation_and_holdout_gates")
    if not blocks:
        blockers.append("walk_forward_blocks_missing")
    acceptance = {
        "acceptance": "PASS",
        "stage8_implementation_status": "COMPLETE",
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
        "walk_forward_block_count": len(blocks),
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
    if panel_manifest.get("source_mode") != PANEL_SOURCE:
        issues.append("panel source mode mismatch")
    panel_rows = read_csv_rows(paths.panel_csv)
    if len(panel_rows) != int(panel_manifest.get("panel_rows") or -1):
        issues.append("panel row count does not match manifest")
    horizons = [
        int(item)
        for item in panel_manifest.get("horizons_trading_days", [])
    ]
    for row in panel_rows:
        asof = parse_date(row.get("asof_date"), field="panel asof")
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
            if execution_available == "1" and (
                not execution_entry
                or not execution_exit
                or as_float(row.get(f"execution_return_{horizon}d")) is None
                or as_float(
                    row.get(f"benchmark_execution_return_{horizon}d")
                )
                is None
            ):
                issues.append(f"{horizon}d execution availability mismatch")
                break
        if issues:
            break
    bounds = _component_bounds(config)
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
