#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contemporaneous_metric_coverage import (  # noqa: E402
    POLICY_VERSION,
    DomainRule,
    audit_contemporaneous_coverage,
)
from industrials.transportation.contracts import file_sha256  # noqa: E402


DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
SURFACE_DOMAINS = DATA_ROOT / "transportation_surface_metric_comparison_domains_v2.csv"
TANKER_DOMAINS = DATA_ROOT / "transportation_tanker_metric_comparison_domains_v1.csv"
REGISTRY = DATA_ROOT / "transportation_specialized_metric_discovery_registry.csv"
DEFAULT_SCORE_HISTORY = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v5"
    / "pit_scores_v6"
    / "transportation_v5_pit_score_history_build.csv"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v5"
    / "specialized_contemporaneous_coverage"
)
DETAIL_FIELDS = (
    "policy_version", "cohort", "metric_id", "comparison_domain_id", "score_date",
    "applicable_ticker_count", "minimum_breadth", "accepted_compatible_breadth",
    "accepted_tickers", "missing_or_unusable_tickers", "stale_tickers",
    "future_only_tickers", "incompatible_definition_tickers",
    "selected_comparison_key", "date_gate", "calibration_eligibility",
)
SUMMARY_FIELDS = (
    "policy_version", "cohort", "metric_id", "comparison_domain_id",
    "score_date_count", "passing_score_date_count", "passing_score_date_fraction",
    "minimum_date_pass_fraction", "latest_score_date", "latest_date_gate",
    "calibration_eligibility", "calibration_gate",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit specialized issuer breadth on every frozen PIT score date; "
            "static all-history coverage cannot authorize calibration."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--score-history", type=Path, default=DEFAULT_SCORE_HISTORY)
    parser.add_argument("--surface-replay", type=Path, required=True)
    parser.add_argument("--tanker-replay", type=Path, required=True)
    parser.add_argument("--surface-domains", type=Path, default=SURFACE_DOMAINS)
    parser.add_argument("--tanker-domains", type=Path, default=TANKER_DOMAINS)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--minimum-date-pass-fraction", type=float, default=0.75)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _rules(path: Path, *, cohort: str) -> list[DomainRule]:
    output: list[DomainRule] = []
    for row in _rows(path):
        minimum = row.get("minimum_accepted_breadth") or row.get("minimum_breadth")
        output.append(
            DomainRule(
                cohort=cohort,
                metric_id=row["metric_id"],
                domain_id=row["comparison_domain_id"],
                tickers=tuple(
                    ticker.strip().upper()
                    for ticker in row["applicable_tickers"].split("|")
                    if ticker.strip()
                ),
                minimum_breadth=int(minimum),
                calibration_eligibility=row.get("calibration_eligibility") or "CANDIDATE",
            )
        )
    return output


def main() -> int:
    args = parse_args()
    paths = {
        "score_history": args.score_history.expanduser().resolve(),
        "surface_replay": args.surface_replay.expanduser().resolve(),
        "tanker_replay": args.tanker_replay.expanduser().resolve(),
        "surface_domains": args.surface_domains.expanduser().resolve(),
        "tanker_domains": args.tanker_domains.expanduser().resolve(),
        "registry": args.registry.expanduser().resolve(),
    }
    for label, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label} missing: {path}")
    score_rows = _rows(paths["score_history"])
    score_dates = [
        row["asof_date"] for row in score_rows if row.get("status") == "PASS"
    ]
    accepted_rows = _rows(paths["surface_replay"]) + _rows(paths["tanker_replay"])
    rules = _rules(paths["surface_domains"], cohort="surface") + _rules(
        paths["tanker_domains"], cohort="tanker"
    )
    freshness = {
        row["metric_id"]: int(row["max_staleness_days"])
        for row in _rows(paths["registry"])
        if row.get("metric_id") and row.get("max_staleness_days")
    }
    detail, summary_rows, manifest = audit_contemporaneous_coverage(
        score_dates=score_dates,
        rules=rules,
        accepted_rows=accepted_rows,
        max_staleness_days=freshness,
        minimum_date_pass_fraction=args.minimum_date_pass_fraction,
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    detail_path = output_dir / "transportation_specialized_contemporaneous_coverage_by_date.csv"
    summary_path = output_dir / "transportation_specialized_contemporaneous_coverage_summary.csv"
    manifest_path = output_dir / "transportation_specialized_contemporaneous_coverage.json"
    write_csv_atomic(detail_path, DETAIL_FIELDS, detail)
    write_csv_atomic(summary_path, SUMMARY_FIELDS, summary_rows)
    manifest.update(
        asof_date=args.asof,
        input_hashes={label: file_sha256(path) for label, path in paths.items()},
        detail_csv=str(detail_path),
        detail_csv_sha256=file_sha256(detail_path),
        summary_csv=str(summary_path),
        summary_csv_sha256=file_sha256(summary_path),
        historical_reconstruction_authorized=False,
        calibration_authorized=bool(manifest["calibration_accepted_metric_count"]),
        production_promotion_authorized=False,
        next_gate=(
            "FREEZE_ACCEPTED_METRIC_SET_AND_REBUILD_ONCE"
            if manifest["calibration_accepted_metric_count"]
            else "NO_SPECIALIZED_METRIC_HAS_CONTEMPORANEOUS_BREADTH"
        ),
    )
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

