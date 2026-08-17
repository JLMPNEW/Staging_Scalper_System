#!/usr/bin/env python3
"""Audit tanker metric breadth from conflict-free reviewed evidence."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    load_investable_universe_policy,
    validate_investable_universe_policy,
)
from industrials.transportation.semantic_replay_contract import (  # noqa: E402
    resolve_semantic_replay_rows,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v5.yaml"
)
DEFAULT_MATERIALIZATION_MANIFEST = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v5"
    / "semantic_materialization"
    / "2026-08-13"
    / "transportation_semantic_materialization_audit.json"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v5"
    / "tanker_coverage_strict"
)
DETAIL_FIELDS = (
    "ticker",
    "metric_id",
    "accepted_period_count",
    "accepted_history_years",
    "coverage_status",
)
METRIC_FIELDS = (
    "metric_id",
    "applicable_ticker_count",
    "accepted_ticker_count",
    "accepted_fraction",
    "minimum_accepted_breadth",
    "median_accepted_periods",
    "median_accepted_history_years",
    "breadth_gate",
    "history_gate",
    "disposition",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit v5 tanker coverage after conflict-free semantic replay."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--asof", required=True)
    parser.add_argument(
        "--semantic-materialization-manifest",
        type=Path,
        default=DEFAULT_MATERIALIZATION_MANIFEST,
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _span(periods: set[str]) -> float:
    values = sorted(value for value in periods if value)
    if len(values) < 2:
        return 0.0
    return (date.fromisoformat(values[-1]) - date.fromisoformat(values[0])).days / 365.25


def main() -> int:
    args = parse_args()
    asof = date.fromisoformat(str(args.asof)[:10]).isoformat()
    policy_path = args.policy.expanduser().resolve()
    policy = load_investable_universe_policy(policy_path)
    errors, _ = validate_investable_universe_policy(policy)
    if errors or policy.policy_version != "transportation_investable_universe_v5":
        raise ValueError(f"v5 investable policy is invalid: {errors}")
    tanker = next(
        group for group in policy.groups if group.group_id == "oil_tanker_operators"
    )
    tickers = tuple(tanker.tickers)
    metrics = tuple(policy.direct_tanker_metrics)

    materialization_path = args.semantic_materialization_manifest.expanduser().resolve()
    materialization = json.loads(materialization_path.read_text(encoding="utf-8"))
    if (
        materialization.get("acceptance") != "PASS"
        or materialization.get("contract_version")
        != "transportation_semantic_materialization_v1"
    ):
        raise ValueError("semantic materialization audit is not accepted")
    lane = (materialization.get("lanes") or {}).get("tanker") or {}
    replay_path = Path(str(lane.get("conflict_free_csv") or "")).resolve()
    if (
        not replay_path.is_file()
        or file_sha256(replay_path)
        != str(lane.get("conflict_free_csv_sha256") or "")
    ):
        raise ValueError("conflict-free tanker replay changed")
    combined = list(_csv(replay_path))

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    ticker_sql = ",".join("?" for _ in tickers)
    metric_sql = ",".join("?" for _ in metrics)
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT candidate_key,ticker,metric_name,candidate_value,unit,period_end,"
        "filing_date,accession_number,candidate_status "
        "FROM fact_sec_metric_disclosure_candidate "
        "WHERE model_family='transportation' AND candidate_status='ACCEPTED' "
        f"AND ticker IN ({ticker_sql}) AND metric_name IN ({metric_sql}) "
        "AND filing_date<=?",
        (*tickers, *metrics, asof),
    ).fetchall()
    connection.close()
    for row in rows:
        combined.append(
            {
                "candidate_key": str(row["candidate_key"]),
                "ticker": str(row["ticker"]),
                "metric_id": str(row["metric_name"]),
                "value": "" if row["candidate_value"] is None else str(row["candidate_value"]),
                "unit": str(row["unit"] or ""),
                "period_end": str(row["period_end"] or "")[:10],
                "filing_date": str(row["filing_date"] or "")[:10],
                "accession_number": str(row["accession_number"] or ""),
                "replay_status": str(row["candidate_status"] or ""),
            }
        )
    resolution = resolve_semantic_replay_rows(combined)
    periods: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for row in resolution.conflict_free_rows:
        ticker = str(row.get("ticker") or "")
        metric = str(row.get("metric_id") or "")
        if ticker in tickers and metric in metrics:
            periods[(ticker, metric)].add(str(row.get("period_end") or "")[:10])

    detail: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    qualifying: list[str] = []
    for metric in metrics:
        depths: list[int] = []
        spans: list[float] = []
        accepted_tickers = 0
        for ticker in tickers:
            values = periods.get((ticker, metric), set())
            if values:
                accepted_tickers += 1
                depths.append(len(values))
                spans.append(_span(set(values)))
            detail.append(
                {
                    "ticker": ticker,
                    "metric_id": metric,
                    "accepted_period_count": len(values),
                    "accepted_history_years": round(_span(set(values)), 6),
                    "coverage_status": "ACCEPTED" if values else "NO_ACCEPTED_EVIDENCE",
                }
            )
        median_periods = statistics.median(depths) if depths else 0.0
        median_years = statistics.median(spans) if spans else 0.0
        breadth = accepted_tickers >= tanker.minimum_specialized_breadth
        history = (
            median_periods >= policy.minimum_median_periods
            and median_years >= policy.minimum_median_history_years
        )
        qualifies = breadth and history
        if qualifies:
            qualifying.append(metric)
        metric_rows.append(
            {
                "metric_id": metric,
                "applicable_ticker_count": len(tickers),
                "accepted_ticker_count": accepted_tickers,
                "accepted_fraction": round(accepted_tickers / len(tickers), 6),
                "minimum_accepted_breadth": tanker.minimum_specialized_breadth,
                "median_accepted_periods": median_periods,
                "median_accepted_history_years": round(median_years, 6),
                "breadth_gate": "PASS" if breadth else "FAIL",
                "history_gate": "PASS" if history else "FAIL",
                "disposition": "QUALIFIES" if qualifies else "EXCLUDE_FROM_SPECIALIZED_CALIBRATION",
            }
        )

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    detail_path = output_dir / "transportation_tanker_v5_coverage_by_ticker.csv"
    metric_path = output_dir / "transportation_tanker_v5_coverage_by_metric.csv"
    write_csv_atomic(detail_path, DETAIL_FIELDS, detail)
    write_csv_atomic(metric_path, METRIC_FIELDS, metric_rows)
    result: dict[str, Any] = {
        "acceptance": "PASS",
        "asof_date": asof,
        "policy_version": policy.policy_version,
        "tanker_ticker_count": len(tickers),
        "direct_metric_count": len(metrics),
        "metrics_meeting_strict_gates": qualifying,
        "metric_count_meeting_strict_gates": len(qualifying),
        "disposition_counts": dict(
            sorted(Counter(str(row["disposition"]) for row in metric_rows).items())
        ),
        "strict_combined_observation_group_count": resolution.observation_group_count,
        "strict_combined_conflict_group_count": resolution.conflict_group_count,
        "strict_combined_conflict_free_observation_count": len(
            resolution.conflict_free_rows
        ),
        "semantic_materialization_manifest_path": str(materialization_path),
        "semantic_materialization_manifest_sha256": file_sha256(materialization_path),
        "policy_path": str(policy_path),
        "policy_sha256": file_sha256(policy_path),
        "detail_csv": str(detail_path),
        "detail_csv_sha256": file_sha256(detail_path),
        "metric_csv": str(metric_path),
        "metric_csv_sha256": file_sha256(metric_path),
        "network_requests": 0,
        "parser_invocations": 0,
        "canonical_candidate_mutation": False,
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "VALIDATE_V5_CURRENT_REQUIRED_METRICS",
    }
    summary_path = output_dir / "transportation_tanker_v5_coverage.json"
    write_text_atomic(summary_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
