#!/usr/bin/env python3
"""Audit the proposed 24-name surface domains from already-reviewed evidence."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
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
)
from industrials.transportation.semantic_replay_contract import (  # noqa: E402
    resolve_semantic_replay_rows,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


MODEL_FAMILY = "transportation"
POLICY_VERSION = "transportation_surface_metric_domains_v2"
NEW_TICKERS = ("CVLG", "FWRD", "HTLD", "MRTN", "WERN")
DOMAIN_TICKERS = {
    "rail_networks": ("CNI", "CP", "CSX", "NSC", "UNP"),
    "ltl_carriers": ("ARCB", "ODFL", "SAIA", "TFII", "XPO"),
    "truckload_intermodal": (
        "HUBG", "JBHT", "KNX", "SNDR", "CVLG", "HTLD", "MRTN", "WERN"
    ),
    "asset_light_logistics": ("CHRW", "EXPD", "HUBG", "LSTR", "FWRD"),
    "integrated_parcel": ("FDX", "UPS"),
}
SURFACE_TICKERS = tuple(
    dict.fromkeys(ticker for tickers in DOMAIN_TICKERS.values() for ticker in tickers)
)
DEFAULT_MAPPING = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_surface_metric_comparison_domains_v2.csv"
)
DEFAULT_SOURCE_MAP = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_surface_metric_source_map_v2.csv"
)
DEFAULT_V4_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v4.yaml"
)
DEFAULT_V1_MAPPING = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_surface_metric_comparison_domains_v1.csv"
)
DEFAULT_PRIOR_COVERAGE = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
    / "2026-07-30"
    / "transportation_surface_delta_parser_domain_coverage.json"
)
DEFAULT_REPLAY_MANIFEST = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
    / "2026-07-30"
    / "transportation_surface_semantic_replay.json"
)
DEFAULT_REENTRY_MANIFEST = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v4"
    / "surface_reentry"
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
DETAIL_FIELDS = (
    "metric_id",
    "comparison_domain_id",
    "ticker",
    "calibration_eligibility",
    "accepted_period_count",
    "accepted_history_years",
    "coverage_status",
)
DOMAIN_FIELDS = (
    "metric_id",
    "comparison_domain_id",
    "calibration_eligibility",
    "applicable_ticker_count",
    "accepted_ticker_count",
    "accepted_fraction",
    "minimum_accepted_fraction",
    "required_accepted_breadth",
    "median_accepted_periods",
    "median_accepted_history_years",
    "breadth_gate",
    "history_gate",
    "disposition",
)


@dataclass(frozen=True)
class Rule:
    metric_id: str
    domain_id: str
    tickers: tuple[str, ...]
    minimum_fraction: float
    minimum_breadth: int
    eligibility: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the proposed 24-name surface comparison domains using only "
            "already-reviewed disclosure candidates."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--source-map", type=Path, default=DEFAULT_SOURCE_MAP)
    parser.add_argument(
        "--semantic-materialization-manifest",
        type=Path,
        default=DEFAULT_MATERIALIZATION_MANIFEST,
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _tickers(value: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in value.split("|") if item.strip())


def validate_maps(mapping_path: Path, source_map_path: Path) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    mapped_by_metric: defaultdict[str, set[str]] = defaultdict(set)
    seen: set[tuple[str, str]] = set()
    for row in _rows(mapping_path):
        if row.get("policy_version") != POLICY_VERSION:
            raise ValueError("surface v2 mapping contains a wrong policy version")
        metric = str(row.get("metric_id") or "")
        domain = str(row.get("comparison_domain_id") or "")
        key = (metric, domain)
        tickers = _tickers(str(row.get("applicable_tickers") or ""))
        if not metric or domain not in DOMAIN_TICKERS or key in seen:
            raise ValueError(f"invalid or duplicate metric-domain rule={key}")
        if not tickers or len(set(tickers)) != len(tickers):
            raise ValueError(f"{key}: blank or duplicate tickers")
        if not set(tickers) <= set(DOMAIN_TICKERS[domain]):
            raise ValueError(f"{key}: ticker outside proposed domain")
        minimum_fraction = float(row["minimum_accepted_fraction"])
        minimum_breadth = int(row["minimum_accepted_breadth"])
        expected = max(3, math.ceil(minimum_fraction * len(tickers)))
        if not math.isclose(minimum_fraction, 0.75) or minimum_breadth != expected:
            raise ValueError(f"{key}: breadth is not derived from the 75% rule")
        eligibility = str(row.get("calibration_eligibility") or "")
        if eligibility not in {"CANDIDATE", "DIAGNOSTIC_ONLY"}:
            raise ValueError(f"{key}: invalid eligibility")
        seen.add(key)
        mapped_by_metric[metric].update(tickers)
        rules.append(
            Rule(metric, domain, tickers, minimum_fraction, minimum_breadth, eligibility)
        )
    source_by_metric = {
        str(row["metric_id"]): set(_tickers(str(row["applicable_tickers"])))
        for row in _rows(source_map_path)
    }
    if dict(mapped_by_metric) != source_by_metric:
        raise ValueError("surface v2 domain/source applicability mismatch")
    covered = set().union(*(set(tickers) for tickers in DOMAIN_TICKERS.values()))
    if covered != set(SURFACE_TICKERS) or len(SURFACE_TICKERS) != 24:
        raise ValueError("proposed surface domains do not define exactly 24 unique tickers")
    return tuple(rules)


def _span(periods: set[str]) -> float:
    dated = sorted(value for value in periods if value and value != "UNDATED")
    if len(dated) < 2:
        return 0.0
    return (date.fromisoformat(dated[-1]) - date.fromisoformat(dated[0])).days / 365.25


def main() -> int:
    args = parse_args()
    asof = date.fromisoformat(str(args.asof)[:10]).isoformat()
    mapping_path = args.mapping.expanduser().resolve()
    source_map_path = args.source_map.expanduser().resolve()
    rules = validate_maps(mapping_path, source_map_path)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    v4 = load_investable_universe_policy(DEFAULT_V4_POLICY)
    reentry_manifest_path = DEFAULT_REENTRY_MANIFEST / asof / "transportation_surface_reentry_audit.json"
    reentry = json.loads(reentry_manifest_path.read_text(encoding="utf-8"))
    if not reentry.get("all_candidates_passed") or tuple(reentry.get("passed_tickers") or ()) != NEW_TICKERS:
        raise ValueError("five-name re-entry gate is not a complete PASS")
    materialization_manifest_path = (
        args.semantic_materialization_manifest.expanduser().resolve()
    )
    materialization = json.loads(
        materialization_manifest_path.read_text(encoding="utf-8")
    )
    if (
        materialization.get("acceptance") != "PASS"
        or materialization.get("contract_version")
        != "transportation_semantic_materialization_v1"
    ):
        raise ValueError("semantic materialization audit is not accepted")
    surface_lane = (materialization.get("lanes") or {}).get("surface") or {}
    replay_path = Path(str(surface_lane.get("conflict_free_csv") or "")).resolve()
    if (
        not replay_path.is_file()
        or file_sha256(replay_path)
        != str(surface_lane.get("conflict_free_csv_sha256") or "")
    ):
        raise ValueError("conflict-free surface semantic replay changed")
    prior_coverage = json.loads(DEFAULT_PRIOR_COVERAGE.read_text(encoding="utf-8"))
    if prior_coverage.get("acceptance") != "PASS":
        raise ValueError("prior surface domain coverage is not accepted")

    applicable_pairs = {
        (ticker, rule.metric_id)
        for rule in rules
        for ticker in rule.tickers
    }
    combined_rows = list(_rows(replay_path))
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in SURFACE_TICKERS)
    for row in connection.execute(
        f"""
        SELECT candidate_key, ticker, metric_name, candidate_value, unit,
               period_end, filing_date, accession_number, candidate_status
        FROM fact_sec_metric_disclosure_candidate
        WHERE model_family=? AND ticker IN ({placeholders})
          AND candidate_status='ACCEPTED' AND filing_date<=?
        """,
        (MODEL_FAMILY, *SURFACE_TICKERS, asof),
    ):
        pair = (str(row["ticker"]), str(row["metric_name"]))
        if pair in applicable_pairs:
            combined_rows.append(
                {
                    "candidate_key": str(row["candidate_key"]),
                    "ticker": str(row["ticker"]),
                    "metric_id": str(row["metric_name"]),
                    "value": (
                        ""
                        if row["candidate_value"] is None
                        else str(row["candidate_value"])
                    ),
                    "unit": str(row["unit"] or ""),
                    "period_end": str(row["period_end"] or "")[:10],
                    "filing_date": str(row["filing_date"] or "")[:10],
                    "accession_number": str(row["accession_number"] or ""),
                    "replay_status": str(row["candidate_status"] or ""),
                }
            )
    connection.close()
    strict_resolution = resolve_semantic_replay_rows(combined_rows)
    periods: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for row in strict_resolution.conflict_free_rows:
        pair = (str(row.get("ticker") or ""), str(row.get("metric_id") or ""))
        if pair in applicable_pairs:
            periods[pair].add(str(row.get("period_end") or "")[:10])

    detail: list[dict[str, object]] = []
    domains: list[dict[str, object]] = []
    qualifying: list[str] = []
    for rule in rules:
        accepted_depth: list[int] = []
        accepted_spans: list[float] = []
        accepted_count = 0
        for ticker in rule.tickers:
            accepted = periods.get((ticker, rule.metric_id), set())
            if accepted:
                accepted_count += 1
                accepted_depth.append(len(accepted))
                accepted_spans.append(_span(set(accepted)))
            detail.append(
                {
                    "metric_id": rule.metric_id,
                    "comparison_domain_id": rule.domain_id,
                    "ticker": ticker,
                    "calibration_eligibility": rule.eligibility,
                    "accepted_period_count": len(accepted),
                    "accepted_history_years": round(_span(set(accepted)), 6),
                    "coverage_status": "ACCEPTED" if accepted else "NO_ACCEPTED_EVIDENCE",
                }
            )
        fraction = accepted_count / len(rule.tickers)
        median_periods = statistics.median(accepted_depth) if accepted_depth else 0.0
        median_years = statistics.median(accepted_spans) if accepted_spans else 0.0
        breadth = (
            accepted_count >= rule.minimum_breadth
            and fraction >= rule.minimum_fraction
        )
        history = (
            median_periods >= v4.minimum_median_periods
            and median_years >= v4.minimum_median_history_years
        )
        qualifies = rule.eligibility == "CANDIDATE" and breadth and history
        disposition = (
            "DIAGNOSTIC_ONLY_BY_POLICY"
            if rule.eligibility != "CANDIDATE"
            else "QUALIFIES"
            if qualifies
            else "EXCLUDE_FROM_SPECIALIZED_CALIBRATION"
        )
        key = f"{rule.metric_id}::{rule.domain_id}"
        if qualifies:
            qualifying.append(key)
        domains.append(
            {
                "metric_id": rule.metric_id,
                "comparison_domain_id": rule.domain_id,
                "calibration_eligibility": rule.eligibility,
                "applicable_ticker_count": len(rule.tickers),
                "accepted_ticker_count": accepted_count,
                "accepted_fraction": round(fraction, 6),
                "minimum_accepted_fraction": rule.minimum_fraction,
                "required_accepted_breadth": rule.minimum_breadth,
                "median_accepted_periods": median_periods,
                "median_accepted_history_years": round(median_years, 6),
                "breadth_gate": "PASS" if breadth else "FAIL",
                "history_gate": "PASS" if history else "FAIL",
                "disposition": disposition,
            }
        )

    new_ticker_accepted = {
        ticker: sorted(metric for candidate, metric in periods if candidate == ticker)
        for ticker in NEW_TICKERS
    }
    v1_rule_tickers = {
        f"{row['metric_id']}::{row['comparison_domain_id']}": set(
            _tickers(str(row["applicable_tickers"]))
        )
        for row in _rows(DEFAULT_V1_MAPPING)
    }
    v2_rule_tickers = {
        f"{rule.metric_id}::{rule.domain_id}": set(rule.tickers) for rule in rules
    }
    baseline_qualifying = set(
        prior_coverage.get("metric_domains_meeting_gates_after_semantic_replay") or ()
    )
    unchanged_baseline = {
        key
        for key in baseline_qualifying
        if v1_rule_tickers.get(key) == v2_rule_tickers.get(key)
    }
    expanded_baseline = baseline_qualifying - unchanged_baseline
    qualifying_set = set(qualifying)
    unchanged_regressions = sorted(unchanged_baseline - qualifying_set)
    expanded_survivors = sorted(expanded_baseline & qualifying_set)
    # The prior replay counted definition-approved segment/table variants as
    # interchangeable observations. It is retained for lineage, but cannot be
    # a production gate. V5 requires a surviving expanded truckload metric
    # under the stricter conflict-free contract.
    required_expanded_survivor = "operating_ratio::truckload_intermodal"
    acceptance = "PASS" if required_expanded_survivor in qualifying_set else "FAIL"
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_REENTRY_MANIFEST / asof / "v5_domain_coverage_strict"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    detail_path = output_dir / "transportation_surface_v5_coverage_by_ticker_domain.csv"
    domain_path = output_dir / "transportation_surface_v5_coverage_by_metric_domain.csv"
    write_csv_atomic(detail_path, DETAIL_FIELDS, detail)
    write_csv_atomic(domain_path, DOMAIN_FIELDS, domains)
    summary: dict[str, Any] = {
        "acceptance": acceptance,
        "asof_date": asof,
        "policy_version": POLICY_VERSION,
        "surface_ticker_count": len(SURFACE_TICKERS),
        "new_tickers": list(NEW_TICKERS),
        "new_ticker_accepted_specialized_metrics": new_ticker_accepted,
        "all_new_tickers_have_accepted_specialized_evidence": all(new_ticker_accepted.values()),
        "metric_domain_rule_count": len(rules),
        "qualifying_metric_domains": qualifying,
        "qualifying_metric_domain_count": len(qualifying),
        "baseline_qualifying_metric_domains": sorted(baseline_qualifying),
        "unchanged_baseline_regressions": unchanged_regressions,
        "expanded_baseline_survivors": expanded_survivors,
        "required_expanded_survivor": required_expanded_survivor,
        "strict_combined_observation_group_count": strict_resolution.observation_group_count,
        "strict_combined_conflict_group_count": strict_resolution.conflict_group_count,
        "strict_combined_conflict_free_observation_count": len(
            strict_resolution.conflict_free_rows
        ),
        "mapping_path": str(mapping_path),
        "mapping_sha256": file_sha256(mapping_path),
        "source_map_path": str(source_map_path),
        "source_map_sha256": file_sha256(source_map_path),
        "reentry_manifest_path": str(reentry_manifest_path),
        "reentry_manifest_sha256": file_sha256(reentry_manifest_path),
        "semantic_materialization_manifest_path": str(materialization_manifest_path),
        "semantic_materialization_manifest_sha256": file_sha256(
            materialization_manifest_path
        ),
        "semantic_replay_conflict_free_csv": str(replay_path),
        "semantic_replay_conflict_free_csv_sha256": file_sha256(replay_path),
        "prior_domain_coverage_path": str(DEFAULT_PRIOR_COVERAGE),
        "prior_domain_coverage_sha256": file_sha256(DEFAULT_PRIOR_COVERAGE),
        "detail_csv": str(detail_path),
        "detail_csv_sha256": file_sha256(detail_path),
        "domain_csv": str(domain_path),
        "domain_csv_sha256": file_sha256(domain_path),
        "parser_invocations": 0,
        "network_requests": 0,
        "historical_reconstruction_performed": False,
        "calibration_performed": False,
        "v5_policy_creation_authorized": acceptance == "PASS",
        "v5_activation_authorized": False,
        "next_gate": (
            "AUDIT_TANKER_STRICT_COVERAGE_THEN_VALIDATE_V5_CANDIDATE_POLICY"
            if acceptance == "PASS"
            else "RETAIN_V4"
        ),
    }
    summary_path = output_dir / "transportation_surface_v5_domain_coverage.json"
    write_text_atomic(summary_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
