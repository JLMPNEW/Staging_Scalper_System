#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402
from industrials.defense.research_artifacts import (  # noqa: E402
    DEFAULT_EMBARGO_DAYS,
    DEFAULT_FORWARD_DAYS,
    MODEL_FAMILY,
    PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY,
    PANEL_SOURCE_SURVIVORSHIP_CORRECTED,
    PILLAR_SCORE_FIELDS,
    PricePoint,
    as_float,
    as_int,
    command_line,
    fmt,
    latest_valid_manifest,
    parse_date,
    parse_required_date,
    purged_split_snapshot_dates,
    select_weekly_snapshot_dates,
    sha256_file,
    split_rows,
    utc_now,
    write_json_atomic,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PANEL_FIELDS = [
    "ticker",
    "asof_date",
    "model_family",
    "score_model_version",
    "model_version",
    "scoring_contract_version",
    "company_name",
    "universe_status",
    "historical_universe_source",
    "historical_price_ticker",
    "calibration_cohort_id",
    "calibration_cohort",
    "rank_ready_flag",
    "model_status",
    "review_reason",
    "final_rank",
    "final_percentile",
    "final_score",
    "native_score_value",
    "score_confidence",
    "data_quality_confidence",
    "market_cap",
    "avg_dollar_volume_60d",
    *PILLAR_SCORE_FIELDS,
    "defense_budget_backlog_quality",
    "defense_budget_backlog_status",
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "market_feature_asof_date",
    "financial_feature_asof_date",
    "positioning_feature_asof_date",
    "feature_data_asof_date",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "source_rank_table_sha256",
    "source_rank_manifest_sha256",
    "source_rank_table_path",
    "price_ticker",
    "price_source_id",
    "price_basis",
    "price_adjustment",
    "price_asof_date",
    "price_forward_date",
    "forward_days",
    "forward_return",
    "benchmark_ticker",
    "benchmark_price_source_id",
    "benchmark_price_basis",
    "benchmark_asof_date",
    "benchmark_forward_date",
    "benchmark_forward_return",
    "forward_excess_return_vs_sector",
    "return_available_flag",
    "return_unavailable_reason",
    "panel_row_eligible_flag",
    "panel_row_eligible_reason",
    "split_name",
]
SPLIT_FIELDS = [
    "split_name",
    "start_date",
    "end_date",
    "snapshot_count",
    "embargo_days",
    "role",
]


@dataclass(frozen=True)
class SnapshotArtifact:
    asof_date: str
    csv_path: Path
    manifest_path: Path
    csv_sha256: str
    manifest_sha256: str
    rows: list[dict[str, str]]
    manifest: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a research-only defense OOS calibration panel from sealed rank snapshots.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default="", help="Build from one snapshot date.")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--snapshot-root", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--forward-days", type=int, default=DEFAULT_FORWARD_DAYS)
    parser.add_argument("--embargo-days", type=int, default=DEFAULT_EMBARGO_DAYS)
    parser.add_argument("--benchmark-ticker", default="")
    parser.add_argument("--cadence", choices=["available", "weekly"], default="available")
    parser.add_argument("--weekly-start-date", default="", help="Weekly bucket anchor date when --cadence weekly.")
    parser.add_argument("--weekly-selection", choices=["first", "last"], default="last")
    parser.add_argument(
        "--evaluation-calendar",
        type=Path,
        default=None,
        help="Frozen one-column asof_date CSV. Panel dates must match it exactly.",
    )
    parser.add_argument("--allow-overwrite", action="store_true")
    parser.add_argument("--include-review-rows", action="store_true")
    return parser.parse_args()


def try_parse_snapshot_date(name: str):
    """Tolerant date probe for directory names: a stray non-date folder under
    the snapshot root (e.g. 'latest') must be skipped, not crash the build."""
    try:
        return parse_date(name, field="snapshot_dir")
    except ValueError:
        return None


def snapshot_date(path: Path) -> str | None:
    parsed = try_parse_snapshot_date(path.name)
    return parsed.isoformat() if parsed else None


def snapshot_dirs(root: Path, *, asof: str, start_date: str, end_date: str) -> list[Path]:
    if asof:
        return [root / asof]
    start = parse_date(start_date, field="start_date")
    end = parse_date(end_date, field="end_date")
    if start and end and start > end:
        raise ValueError("--start-date cannot be after --end-date")
    out: list[Path] = []
    if not root.exists():
        return out
    for path in root.iterdir():
        if not path.is_dir():
            continue
        parsed = try_parse_snapshot_date(path.name)
        if parsed is None:
            continue
        if start and parsed < start:
            continue
        if end and parsed > end:
            continue
        out.append(path)
    return sorted(out, key=lambda item: item.name)


def filter_weekly_snapshot_paths(
    paths: list[Path],
    *,
    weekly_start_date: str,
    weekly_selection: str,
) -> list[Path]:
    if not weekly_start_date:
        raise ValueError("--weekly-start-date is required when --cadence weekly")
    by_date = {str(snapshot_date(path) or ""): path for path in paths if snapshot_date(path)}
    selected = select_weekly_snapshot_dates(
        list(by_date),
        weekly_start_date=weekly_start_date,
        selection=weekly_selection,
    )
    return [by_date[asof] for asof in selected]


def read_evaluation_calendar(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Evaluation calendar does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    dates = [str(row.get("asof_date") or "").strip() for row in rows]
    if not dates or any(not value for value in dates):
        raise ValueError(f"Evaluation calendar must contain nonblank asof_date rows: {path}")
    normalized = [
        parse_required_date(value, field="evaluation_calendar.asof_date").isoformat()
        for value in dates
    ]
    if normalized != sorted(set(normalized)):
        raise ValueError(f"Evaluation calendar dates must be unique and ascending: {path}")
    return normalized


def load_snapshot(path: Path) -> SnapshotArtifact:
    asof = snapshot_date(path)
    if not asof:
        raise ValueError(f"Snapshot directory is not a market date: {path}")
    csv_path = path / "defense_final_rank_table.csv"
    manifest_path = path / "defense_final_rank_table_manifest.json"
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    manifest = latest_valid_manifest(manifest_path)
    if manifest is None:
        raise ValueError(f"Invalid rank-table manifest JSON: {manifest_path}")
    csv_sha = sha256_file(csv_path)
    manifest_sha = sha256_file(manifest_path)
    if manifest.get("sha256") != csv_sha:
        raise ValueError(f"Manifest hash mismatch for {csv_path}")
    if manifest.get("asof_date") != asof:
        raise ValueError(f"Manifest asof mismatch for {csv_path}")
    if manifest.get("model_family") != MODEL_FAMILY:
        raise ValueError(f"Manifest model_family mismatch for {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [{str(k): str(v or "") for k, v in row.items()} for row in csv.DictReader(handle)]
    return SnapshotArtifact(
        asof_date=asof,
        csv_path=csv_path,
        manifest_path=manifest_path,
        csv_sha256=csv_sha,
        manifest_sha256=manifest_sha,
        rows=rows,
        manifest=manifest,
    )


def price_sources(config: dict[str, Any]) -> list[str]:
    primary = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "").strip()
    fallback_raw = cfg_get(config, "market_data_policy.scoring_fallback_sources", []) or []
    fallback = [str(item).strip() for item in fallback_raw if str(item).strip()]
    out = [source for source in [primary, *fallback] if source]
    if not out:
        raise ValueError("No market price source configured")
    return list(dict.fromkeys(out))


def load_price_series(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    sources: list[str],
) -> dict[str, dict[str, list[PricePoint]]]:
    clean_tickers = sorted({normalize_ticker(ticker) for ticker in tickers if normalize_ticker(ticker)})
    if not clean_tickers:
        return {}
    ticker_ph = ",".join("?" for _ in clean_tickers)
    source_ph = ",".join("?" for _ in sources)
    rows = conn.execute(
        f"""
        SELECT ticker, source_id, bar_date, adj_close, close, price_adjustment
        FROM fact_price_ohlcv
        WHERE ticker IN ({ticker_ph})
          AND source_id IN ({source_ph})
          AND (adj_close IS NOT NULL OR close IS NOT NULL)
        ORDER BY ticker, source_id, bar_date
        """,
        (*clean_tickers, *sources),
    ).fetchall()
    out: dict[str, dict[str, list[PricePoint]]] = {}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        source_id = str(row["source_id"] or "")
        adj_close = as_float(row["adj_close"])
        close = as_float(row["close"])
        value = adj_close if adj_close is not None else close
        if value is None or value <= 0:
            continue
        point = PricePoint(
            bar_date=parse_required_date(row["bar_date"], field="bar_date"),
            value=value,
            source_id=source_id,
            price_basis="adj_close" if adj_close is not None else "close",
            price_adjustment=str(row["price_adjustment"] or ""),
        )
        out.setdefault(ticker, {}).setdefault(source_id, []).append(point)
    return out


def return_window(
    series_by_source: dict[str, list[PricePoint]],
    *,
    asof: str,
    forward_days: int,
    source_order: list[str],
) -> tuple[PricePoint | None, PricePoint | None, str]:
    """Anchor + forward price from the first source that can supply BOTH legs.

    Both legs always come from the same source (no cross-source basis mixing).
    A source whose series ends inside the forward window (e.g. Yahoo history
    for a name that delists mid-window) no longer blocks a later source such
    as the Norgate delisted feed from providing the complete window; the
    partial anchor is only reported when no source has both legs.
    """
    asof_date = parse_required_date(asof, field="asof_date")
    partial_anchor: PricePoint | None = None
    for source_id in source_order:
        series = series_by_source.get(source_id) or []
        if not series:
            continue
        anchor_idx = -1
        for idx, point in enumerate(series):
            if point.bar_date <= asof_date:
                anchor_idx = idx
            else:
                break
        if anchor_idx < 0:
            continue
        forward_idx = anchor_idx + forward_days
        if forward_idx >= len(series):
            if partial_anchor is None:
                partial_anchor = series[anchor_idx]
            continue
        return series[anchor_idx], series[forward_idx], ""
    if partial_anchor is not None:
        return partial_anchor, None, "missing_forward_price"
    return None, None, "missing_asof_price"


def source_date_violation(row: dict[str, str], asof: str) -> str:
    asof_date = parse_required_date(asof, field="asof_date")
    fields = [
        "source_snapshot_asof_date",
        "price_data_asof_date",
        "market_feature_asof_date",
        "financial_feature_asof_date",
        "positioning_feature_asof_date",
        "feature_data_asof_date",
        "latest_sec_filing_date",
        "short_interest_asof_date",
        "institutional_data_asof_date",
        "insider_data_asof_date",
        "borrow_data_asof_date",
    ]
    for field in fields:
        value = str(row.get(field) or "").strip()
        if not value:
            continue
        parsed = parse_date(value, field=field)
        if parsed and parsed > asof_date:
            return f"{field}_after_asof"
    return ""


def row_source(row: dict[str, str]) -> str:
    source = str(row.get("stage11_calibration_panel_source") or "").strip()
    return source or PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY


def compose_panel_rows(
    snapshots: list[SnapshotArtifact],
    *,
    price_data: dict[str, dict[str, list[PricePoint]]],
    benchmark_ticker: str,
    forward_days: int,
    source_order: list[str],
    include_review_rows: bool,
    split_map: dict[str, str],
) -> tuple[list[dict[str, str]], list[str]]:
    benchmark_series = price_data.get(benchmark_ticker, {})
    out: list[dict[str, str]] = []
    source_modes: list[str] = []
    for snapshot in snapshots:
        bench_anchor, bench_forward, bench_reason = return_window(
            benchmark_series,
            asof=snapshot.asof_date,
            forward_days=forward_days,
            source_order=source_order,
        )
        benchmark_return = (
            bench_forward.value / bench_anchor.value - 1.0
            if bench_anchor is not None and bench_forward is not None
            else None
        )
        for source_row in snapshot.rows:
            ticker = normalize_ticker(source_row.get("ticker"))
            if not ticker:
                continue
            source_mode = row_source(source_row)
            source_modes.append(source_mode)
            price_ticker = normalize_ticker(source_row.get("historical_price_ticker")) or ticker
            anchor, forward, reason = return_window(
                price_data.get(price_ticker, {}),
                asof=snapshot.asof_date,
                forward_days=forward_days,
                source_order=source_order,
            )
            forward_return = (
                forward.value / anchor.value - 1.0
                if anchor is not None and forward is not None
                else None
            )
            excess_return = (
                forward_return - benchmark_return
                if forward_return is not None and benchmark_return is not None
                else None
            )
            return_reason = reason or bench_reason
            source_violation = source_date_violation(source_row, snapshot.asof_date)
            score = as_float(source_row.get("final_score"))
            rank_ready = str(source_row.get("rank_ready_flag") or "") == "1"
            model_complete = str(source_row.get("model_status") or "") == "complete"
            return_available = excess_return is not None
            eligible = (
                score is not None
                and 0.0 <= score <= 100.0
                and return_available
                and not source_violation
                and (include_review_rows or (rank_ready and model_complete))
            )
            reasons: list[str] = []
            if score is None or not (0.0 <= (score or 0.0) <= 100.0):
                reasons.append("invalid_final_score")
            if not rank_ready:
                reasons.append("rank_ready_flag_zero")
            if not model_complete:
                reasons.append("model_status_not_complete")
            if not return_available:
                reasons.append(return_reason or "missing_forward_return")
            if source_violation:
                reasons.append(source_violation)
            record = {
                "ticker": ticker,
                "asof_date": snapshot.asof_date,
                "model_family": str(source_row.get("model_family") or MODEL_FAMILY),
                "score_model_version": str(source_row.get("score_model_version") or ""),
                "model_version": str(source_row.get("model_version") or ""),
                "scoring_contract_version": str(source_row.get("scoring_contract_version") or ""),
                "company_name": str(source_row.get("company_name") or ""),
                "universe_status": str(source_row.get("universe_status") or ""),
                "historical_universe_source": str(source_row.get("historical_universe_source") or ""),
                "historical_price_ticker": price_ticker,
                "calibration_cohort_id": str(source_row.get("calibration_cohort_id") or ""),
                "calibration_cohort": str(source_row.get("calibration_cohort") or ""),
                "rank_ready_flag": str(source_row.get("rank_ready_flag") or "0"),
                "model_status": str(source_row.get("model_status") or ""),
                "review_reason": str(source_row.get("review_reason") or ""),
                "final_rank": str(source_row.get("final_rank") or ""),
                "final_percentile": str(source_row.get("final_percentile") or ""),
                "final_score": fmt(score),
                "native_score_value": str(source_row.get("native_score_value") or source_row.get("final_score") or ""),
                "score_confidence": str(source_row.get("score_confidence") or ""),
                "data_quality_confidence": str(source_row.get("data_quality_confidence") or ""),
                "market_cap": str(source_row.get("market_cap") or ""),
                "avg_dollar_volume_60d": str(source_row.get("avg_dollar_volume_60d") or ""),
                "source_snapshot_asof_date": str(source_row.get("source_snapshot_asof_date") or ""),
                "price_data_asof_date": str(source_row.get("price_data_asof_date") or ""),
                "market_feature_asof_date": str(source_row.get("market_feature_asof_date") or ""),
                "financial_feature_asof_date": str(source_row.get("financial_feature_asof_date") or ""),
                "positioning_feature_asof_date": str(source_row.get("positioning_feature_asof_date") or ""),
                "feature_data_asof_date": str(source_row.get("feature_data_asof_date") or ""),
                "stage11_calibration_panel_source": source_mode,
                "survivorship_corrected_panel_flag": str(source_row.get("survivorship_corrected_panel_flag") or "0"),
                "source_rank_table_sha256": snapshot.csv_sha256,
                "source_rank_manifest_sha256": snapshot.manifest_sha256,
                "source_rank_table_path": str(snapshot.csv_path),
                "price_ticker": price_ticker,
                "price_source_id": anchor.source_id if anchor else "",
                "price_basis": anchor.price_basis if anchor else "",
                "price_adjustment": anchor.price_adjustment if anchor else "",
                "price_asof_date": anchor.bar_date.isoformat() if anchor else "",
                "price_forward_date": forward.bar_date.isoformat() if forward else "",
                "forward_days": str(forward_days),
                "forward_return": fmt(forward_return, 10),
                "benchmark_ticker": benchmark_ticker,
                "benchmark_price_source_id": bench_anchor.source_id if bench_anchor else "",
                "benchmark_price_basis": bench_anchor.price_basis if bench_anchor else "",
                "benchmark_asof_date": bench_anchor.bar_date.isoformat() if bench_anchor else "",
                "benchmark_forward_date": bench_forward.bar_date.isoformat() if bench_forward else "",
                "benchmark_forward_return": fmt(benchmark_return, 10),
                "forward_excess_return_vs_sector": fmt(excess_return, 10),
                "return_available_flag": "1" if return_available else "0",
                "return_unavailable_reason": "" if return_available else return_reason or "missing_return",
                "panel_row_eligible_flag": "1" if eligible else "0",
                "panel_row_eligible_reason": "eligible" if eligible else ";".join(dict.fromkeys(reasons)),
                "split_name": split_map.get(snapshot.asof_date, "insufficient_history"),
            }
            for field in PILLAR_SCORE_FIELDS:
                record[field] = str(source_row.get(field) or "")
            record["defense_budget_backlog_quality"] = str(
                source_row.get("defense_budget_backlog_quality") or ""
            )
            record["defense_budget_backlog_status"] = str(
                source_row.get("defense_budget_backlog_status") or ""
            )
            out.append(record)
    return out, sorted(set(source_modes))


def output_paths(output_dir: Path) -> tuple[Path, Path, Path]:
    return (
        output_dir / "defense_oos_calibration_panel.csv",
        output_dir / "defense_oos_calibration_splits.csv",
        output_dir / "defense_oos_calibration_panel_manifest.json",
    )


def existing_artifact_valid(panel_path: Path, split_path: Path, manifest_path: Path) -> bool:
    manifest = latest_valid_manifest(manifest_path)
    if manifest is None or not panel_path.exists() or not split_path.exists():
        return False
    files = manifest.get("files")
    if not isinstance(files, dict):
        return False
    panel_meta = files.get(panel_path.name)
    split_meta = files.get(split_path.name)
    if not isinstance(panel_meta, dict) or not isinstance(split_meta, dict):
        return False
    return panel_meta.get("sha256") == sha256_file(panel_path) and split_meta.get("sha256") == sha256_file(split_path)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if args.forward_days <= 0:
        raise ValueError("--forward-days must be positive")
    if args.embargo_days < 0:
        raise ValueError("--embargo-days cannot be negative")
    if args.asof and (args.start_date or args.end_date):
        raise ValueError("--asof cannot be combined with --start-date/--end-date")
    if args.asof and args.cadence == "weekly":
        raise ValueError("--asof cannot be combined with --cadence weekly")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family_cfg = cfg_get(config, "oos_calibration_standards.families.defense", {}) or {}
    snapshot_root = (
        args.snapshot_root.expanduser().resolve()
        if args.snapshot_root
        else resolve_path(str(cfg_get(family_cfg, "snapshot_history_root", "../output/industrials/defense/dashboard")), base_dir=base_dir)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8" / "oos_calibration_panel"
    )
    panel_path, split_path, manifest_path = output_paths(output_dir)
    if (panel_path.exists() or split_path.exists() or manifest_path.exists()) and not args.allow_overwrite:
        if existing_artifact_valid(panel_path, split_path, manifest_path):
            print(f"Existing sealed OOS calibration panel is valid; keeping {panel_path}")
            print(f"Existing sealed split file is valid; keeping {split_path}")
            print(f"Existing sealed manifest is valid; keeping {manifest_path}")
            return 0
        raise FileExistsError(f"Refusing to overwrite existing calibration artifact under {output_dir}; use --allow-overwrite")

    benchmark_ticker = normalize_ticker(args.benchmark_ticker) or normalize_ticker(
        cfg_get(family_cfg, "primary_benchmark_ticker", "")
    )
    if benchmark_ticker != "XAR":
        raise ValueError(f"Defense calibration benchmark must be XAR unless the contract is updated, got {benchmark_ticker!r}")
    snapshot_paths = snapshot_dirs(snapshot_root, asof=args.asof, start_date=args.start_date, end_date=args.end_date)
    evaluation_calendar = (
        args.evaluation_calendar.expanduser().resolve()
        if args.evaluation_calendar
        else None
    )
    evaluation_dates: list[str] = []
    if evaluation_calendar is not None:
        evaluation_dates = read_evaluation_calendar(evaluation_calendar)
        by_date = {str(snapshot_date(path) or ""): path for path in snapshot_paths}
        missing_dates = [asof for asof in evaluation_dates if asof not in by_date]
        if missing_dates:
            raise ValueError(
                f"Snapshot root is missing frozen evaluation-calendar dates: {missing_dates[:20]}"
            )
        snapshot_paths = [by_date[asof] for asof in evaluation_dates]
    elif args.cadence == "weekly":
        snapshot_paths = filter_weekly_snapshot_paths(
            snapshot_paths,
            weekly_start_date=args.weekly_start_date,
            weekly_selection=args.weekly_selection,
        )
    snapshots = [load_snapshot(path) for path in snapshot_paths]
    if not snapshots:
        raise ValueError(f"No sealed defense rank snapshots found under {snapshot_root}")
    if evaluation_dates and [snapshot.asof_date for snapshot in snapshots] != evaluation_dates:
        raise ValueError("Loaded snapshot dates do not exactly match the frozen evaluation calendar")
    score_model_versions = {
        str(snapshot.manifest.get("score_model_version") or "")
        for snapshot in snapshots
    }
    scoring_modes = {
        str(snapshot.manifest.get("scoring_mode") or "baseline")
        for snapshot in snapshots
    }
    research_candidate_values = {
        bool(snapshot.manifest.get("research_candidate", False))
        for snapshot in snapshots
    }
    if len(score_model_versions) != 1 or "" in score_model_versions:
        raise ValueError(f"Source snapshots mix score_model_version values: {sorted(score_model_versions)}")
    if len(scoring_modes) != 1:
        raise ValueError(f"Source snapshots mix scoring modes: {sorted(scoring_modes)}")
    if len(research_candidate_values) != 1:
        raise ValueError("Source snapshots mix research-candidate and production/baseline artifacts")

    tickers = [benchmark_ticker]
    for snapshot in snapshots:
        for row in snapshot.rows:
            tickers.append(normalize_ticker(row.get("historical_price_ticker")) or normalize_ticker(row.get("ticker")))
    sources = price_sources(config)
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"Industrials DB does not exist: {db_path}")
    # Research reader: open the production DB strictly read-only. Never run
    # init_db/migrations from this script — panel builds must not mutate state.
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        price_data = load_price_series(conn, tickers=tickers, sources=sources)

    snapshot_dates = sorted({snapshot.asof_date for snapshot in snapshots})
    # Purged split: train/validation snapshots whose forward window (+embargo)
    # crosses the next split's start are relabelled 'embargo' so selection can
    # never train on outcomes that overlap its evaluation window.
    split_map = purged_split_snapshot_dates(
        snapshot_dates,
        forward_days=args.forward_days,
        embargo_days=args.embargo_days,
    )
    panel_rows, source_modes = compose_panel_rows(
        snapshots,
        price_data=price_data,
        benchmark_ticker=benchmark_ticker,
        forward_days=args.forward_days,
        source_order=sources,
        include_review_rows=bool(args.include_review_rows),
        split_map=split_map,
    )
    split_report_rows = split_rows(snapshot_dates, split_map, embargo_days=args.embargo_days)
    split_counts: dict[str, int] = {}
    for row in panel_rows:
        split_counts[row["split_name"]] = split_counts.get(row["split_name"], 0) + 1
    split_report_rows = [{**row, "row_count": str(split_counts.get(row["split_name"], 0))} for row in split_report_rows]
    split_fields = [*SPLIT_FIELDS, "row_count"]

    write_csv_atomic(panel_path, PANEL_FIELDS, [{field: row.get(field, "") for field in PANEL_FIELDS} for row in panel_rows])
    write_csv_atomic(split_path, split_fields, split_report_rows)
    panel_sha = sha256_file(panel_path)
    split_sha = sha256_file(split_path)
    all_source_modes = sorted(set(source_modes))
    current_universe_replay = all_source_modes == [PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY]
    survivorship_corrected = all_source_modes == [PANEL_SOURCE_SURVIVORSHIP_CORRECTED]
    eligible_rows = sum(1 for row in panel_rows if row["panel_row_eligible_flag"] == "1")
    return_available_rows = sum(1 for row in panel_rows if row["return_available_flag"] == "1")
    promotion_blockers: list[str] = []
    if current_universe_replay:
        promotion_blockers.append("current_universe_replay_not_survivorship_corrected")
    if not survivorship_corrected:
        promotion_blockers.append("not_survivorship_corrected_pit_membership_recompute")
    if len(snapshot_dates) < as_int(cfg_get(family_cfg, "min_shadow_snapshots_for_promotion", 60), 60):
        promotion_blockers.append("insufficient_snapshot_history")
    if eligible_rows == 0:
        promotion_blockers.append("no_panel_rows_with_forward_returns")
    manifest = {
        "artifact_family": "defense_oos_calibration_panel",
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "generator": "22_build_defense_oos_calibration_panel.py",
        "command": command_line(),
        "config_path": str(config_path),
        "source_db_path": str(db_path),
        "snapshot_root": str(snapshot_root),
        "snapshot_cadence": args.cadence,
        "weekly_start_date": args.weekly_start_date if args.cadence == "weekly" else "",
        "weekly_selection": args.weekly_selection if args.cadence == "weekly" else "",
        "evaluation_calendar_path": str(evaluation_calendar or ""),
        "evaluation_calendar_sha256": sha256_file(evaluation_calendar) if evaluation_calendar else "",
        "snapshot_count": len(snapshot_dates),
        "snapshot_start_date": snapshot_dates[0],
        "snapshot_end_date": snapshot_dates[-1],
        "forward_days": args.forward_days,
        "embargo_days": args.embargo_days,
        "embargoed_snapshots": sum(1 for value in split_map.values() if value == "embargo"),
        "benchmark_ticker": benchmark_ticker,
        "price_source_order": sources,
        "score_model_version": next(iter(score_model_versions)),
        "scoring_mode": next(iter(scoring_modes)),
        "research_candidate": next(iter(research_candidate_values)),
        "panel_rows": len(panel_rows),
        "eligible_rows": eligible_rows,
        "return_available_rows": return_available_rows,
        "panel_source_modes": all_source_modes,
        "shadow_only": True,
        "promotable": not promotion_blockers,
        "promotion_blockers": promotion_blockers,
        "files": {
            panel_path.name: {"path": str(panel_path), "sha256": panel_sha, "rows": len(panel_rows)},
            split_path.name: {"path": str(split_path), "sha256": split_sha, "rows": len(split_report_rows)},
        },
        "source_snapshots": [
            {
                "asof_date": snapshot.asof_date,
                "rank_table_path": str(snapshot.csv_path),
                "rank_table_sha256": snapshot.csv_sha256,
                "rank_manifest_path": str(snapshot.manifest_path),
                "rank_manifest_sha256": snapshot.manifest_sha256,
                "rank_rows": len(snapshot.rows),
                "score_model_version": str(snapshot.manifest.get("score_model_version") or ""),
                "scoring_mode": str(snapshot.manifest.get("scoring_mode") or "baseline"),
                "research_candidate": bool(snapshot.manifest.get("research_candidate", False)),
                "shadow_only": bool(snapshot.manifest.get("shadow_only", False)),
                "production_promoted": bool(snapshot.manifest.get("production_promoted", False)),
            }
            for snapshot in snapshots
        ],
    }
    write_json_atomic(manifest_path, manifest)
    print(
        f"Wrote {panel_path} rows={len(panel_rows)} eligible={eligible_rows} "
        f"return_available={return_available_rows} snapshots={len(snapshot_dates)}"
    )
    print(f"Wrote {split_path}")
    print(f"Wrote {manifest_path}")
    if promotion_blockers:
        print(f"Research-only panel; promotion blockers: {', '.join(promotion_blockers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
