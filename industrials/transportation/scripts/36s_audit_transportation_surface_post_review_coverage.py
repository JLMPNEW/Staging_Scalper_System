#!/usr/bin/env python3
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
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.investable_universe import load_investable_universe_policy  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402
from industrials.transportation.surface_semantic_review import (  # noqa: E402
    REVIEW_POLICY_VERSION,
    candidate_key,
)


DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
POLICY = DATA_ROOT / "transportation_investable_universe_v5.yaml"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3" / "surface_delta"
)
DETAIL_FIELDS = (
    "run_id", "metric_id", "comparison_domain_id", "ticker", "calibration_eligibility",
    "required_accepted_breadth", "canonical_accepted_period_count", "shadow_accepted_period_count",
    "semantic_replay_accepted_candidate_count", "semantic_replay_accepted_period_count",
    "unresolved_review_period_count", "union_accepted_period_count", "potential_period_count",
    "coverage_disposition",
)
DOMAIN_FIELDS = (
    "run_id", "metric_id", "comparison_domain_id", "domain_applicable_ticker_count",
    "minimum_accepted_fraction", "required_accepted_breadth", "calibration_eligibility",
    "normalization_scope", "canonical_accepted_ticker_count", "shadow_accepted_ticker_count",
    "semantic_replay_accepted_ticker_count", "union_accepted_ticker_count",
    "accepted_applicable_fraction", "unresolved_review_ticker_count", "potential_ticker_count",
    "potential_fraction", "median_union_accepted_periods", "median_union_history_years",
    "median_potential_periods", "median_potential_history_years", "breadth_gate", "history_gate",
    "potential_breadth_gate", "potential_history_gate", "post_review_calibration_disposition",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the surface domain coverage audit once after semantic replay.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--replay-manifest", type=Path, default=None)
    parser.add_argument("--fact-store-candidates", type=Path, default=None)
    return parser.parse_args()


def _csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _span(periods: set[str]) -> float:
    values = sorted(value for value in periods if value and value != "UNDATED")
    if len(values) < 2:
        return 0.0
    return (date.fromisoformat(values[-1]) - date.fromisoformat(values[0])).days / 365.25


def _sets(rows: list[Mapping[str, object]], metric_field: str, status_field: str) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str, str], set[str]]]:
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    periods: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        status = str(row.get(status_field) or "").upper()
        key = (str(row.get("ticker") or ""), str(row.get(metric_field) or ""), status)
        counts[key] += 1
        periods[key].add(str(row.get("period_end") or "")[:10] or "UNDATED")
    return counts, periods


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else DEFAULT_OUTPUT_ROOT / args.asof
    replay_manifest_path = args.replay_manifest.expanduser().resolve() if args.replay_manifest else output_dir / "transportation_surface_semantic_replay.json"
    replay_manifest = json.loads(replay_manifest_path.read_text(encoding="utf-8"))
    if (
        replay_manifest.get("acceptance") != "PASS"
        or replay_manifest.get("asof_date") != args.asof
        or int(replay_manifest.get("run_id") or 0) != args.run_id
        or replay_manifest.get("review_policy_version") != REVIEW_POLICY_VERSION
    ):
        raise ValueError("semantic replay manifest does not match this audit")
    replay_path = Path(str(replay_manifest["replay_csv"]))
    if file_sha256(replay_path) != replay_manifest["replay_csv_sha256"]:
        raise ValueError("semantic replay file changed after sealing")
    replay_rows = _csv(replay_path)
    reviewed_keys = {row["candidate_key"] for row in replay_rows}
    replay_counts, replay_periods = _sets(replay_rows, "metric_id", "replay_status")

    policy = load_investable_universe_policy(POLICY)
    surface = next(group for group in policy.groups if group.group_id == "surface_freight_core")
    rules = tuple(rule for rule in policy.surface_metric_domain_rules if rule.metric_id != "surface_volume_growth")
    tickers = tuple(surface.tickers)
    metrics = tuple(sorted({rule.metric_id for rule in rules}))
    valid_pairs = {
        (ticker, rule.metric_id)
        for rule in rules
        for ticker in rule.applicable_tickers
    }
    ticker_sql = ",".join("?" for _ in tickers)
    metric_sql = ",".join("?" for _ in metrics)
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    run = connection.execute("SELECT * FROM sec_parser_run WHERE run_id=?", (args.run_id,)).fetchone()
    if run is None or str(run["asof_date"]) != args.asof or str(run["status"]) != "COMPLETED":
        raise ValueError("parser run is not eligible for the post-review audit")
    canonical = [dict(row) for row in connection.execute(
        "SELECT ticker, metric_name, candidate_status, period_end FROM fact_sec_metric_disclosure_candidate "
        "WHERE model_family='transportation' "
        f"AND ticker IN ({ticker_sql}) AND metric_name IN ({metric_sql}) AND filing_date<=?",
        (*tickers, *metrics, args.asof),
    ).fetchall()]
    shadow = [dict(row) for row in connection.execute(
        "SELECT evidence.evidence_key, evidence.ticker, evidence.metric_name, evidence.candidate_status, "
        "evidence.candidate_value, evidence.period_end FROM sec_parser_run_metric_evidence AS relation "
        "JOIN sec_parser_metric_evidence_shadow AS evidence ON evidence.evidence_key=relation.evidence_key "
        "WHERE relation.run_id=? "
        f"AND evidence.ticker IN ({ticker_sql}) AND evidence.metric_name IN ({metric_sql})",
        (args.run_id, *tickers, *metrics),
    ).fetchall()]
    canonical_counts, canonical_periods = _sets(canonical, "metric_name", "candidate_status")
    shadow_counts, shadow_periods = _sets(shadow, "metric_name", "candidate_status")

    fact_path = args.fact_store_candidates.expanduser().resolve() if args.fact_store_candidates else output_dir / "transportation_surface_fact_store_ratio_candidates.csv"
    unresolved: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    unresolved_keys: set[str] = set()
    for row in shadow:
        if row.get("candidate_value") is None:
            continue
        if str(row["candidate_status"]).upper() not in {"REVIEW", "REVIEW_REQUIRED", "PENDING_REVIEW"}:
            continue
        pair = (str(row["ticker"]), str(row["metric_name"]))
        if pair not in valid_pairs:
            continue
        key = str(row["evidence_key"])
        if key not in reviewed_keys:
            unresolved_keys.add(key)
            unresolved[(str(row["ticker"]), str(row["metric_name"]))].add(str(row["period_end"] or "")[:10] or "UNDATED")
    for row in _csv(fact_path):
        if (row["ticker"], row["metric_id"]) not in valid_pairs:
            continue
        mapped: dict[str, object] = {
            "source_lane": "fact_store_ratio", "ticker": row["ticker"], "metric_id": row["metric_id"],
            "accession_number": row["accession_number"], "period_start": row["period_start"],
            "period_end": row["period_end"], "candidate_value": row["value"],
            "numerator_concept": row["numerator_concept"], "denominator_concept": row["denominator_concept"],
            "source_document": row["source_id"],
        }
        key = candidate_key(mapped)
        if key not in reviewed_keys:
            unresolved_keys.add(key)
            unresolved[(row["ticker"], row["metric_id"])].add(row["period_end"][:10] or "UNDATED")
    for row in replay_rows:
        if row["replay_status"] == "REVIEW_REQUIRED":
            unresolved[(row["ticker"], row["metric_id"])].add(row["period_end"][:10] or "UNDATED")

    detail: list[dict[str, object]] = []
    domains: list[dict[str, object]] = []
    for rule in rules:
        union_by_ticker: dict[str, set[str]] = {}
        potential_by_ticker: dict[str, set[str]] = {}
        canonical_tickers: set[str] = set()
        shadow_tickers: set[str] = set()
        replay_tickers: set[str] = set()
        unresolved_tickers: set[str] = set()
        for ticker in rule.applicable_tickers:
            canonical_set = canonical_periods.get((ticker, rule.metric_id, "ACCEPTED"), set())
            shadow_set = shadow_periods.get((ticker, rule.metric_id, "ACCEPTED"), set())
            replay_set = replay_periods.get((ticker, rule.metric_id, "ACCEPTED"), set())
            unresolved_set = unresolved.get((ticker, rule.metric_id), set())
            union = set(canonical_set) | set(shadow_set) | set(replay_set)
            potential = union | set(unresolved_set)
            if canonical_set: canonical_tickers.add(ticker)
            if shadow_set: shadow_tickers.add(ticker)
            if replay_set: replay_tickers.add(ticker)
            if unresolved_set: unresolved_tickers.add(ticker)
            if union: union_by_ticker[ticker] = union
            if potential: potential_by_ticker[ticker] = potential
            detail.append({
                "run_id": args.run_id, "metric_id": rule.metric_id,
                "comparison_domain_id": rule.comparison_domain_id, "ticker": ticker,
                "calibration_eligibility": rule.calibration_eligibility,
                "required_accepted_breadth": rule.minimum_accepted_breadth,
                "canonical_accepted_period_count": len(canonical_set),
                "shadow_accepted_period_count": len(shadow_set),
                "semantic_replay_accepted_candidate_count": replay_counts.get((ticker, rule.metric_id, "ACCEPTED"), 0),
                "semantic_replay_accepted_period_count": len(replay_set),
                "unresolved_review_period_count": len(unresolved_set),
                "union_accepted_period_count": len(union), "potential_period_count": len(potential),
                "coverage_disposition": "ACCEPTED" if union else "UNRESOLVED_REVIEW" if unresolved_set else "NO_DISCOVERED_EVIDENCE",
            })
        accepted_depth = [len(value) for value in union_by_ticker.values()]
        potential_depth = [len(value) for value in potential_by_ticker.values()]
        median_periods = statistics.median(accepted_depth) if accepted_depth else 0.0
        median_years = statistics.median([_span(value) for value in union_by_ticker.values()]) if union_by_ticker else 0.0
        median_potential = statistics.median(potential_depth) if potential_depth else 0.0
        median_potential_years = statistics.median([_span(value) for value in potential_by_ticker.values()]) if potential_by_ticker else 0.0
        candidate = rule.is_calibration_candidate
        breadth = candidate and len(union_by_ticker) >= rule.minimum_accepted_breadth
        history = candidate and median_periods >= policy.minimum_median_periods and median_years >= policy.minimum_median_history_years
        p_breadth = candidate and len(potential_by_ticker) >= rule.minimum_accepted_breadth
        p_history = candidate and median_potential >= policy.minimum_median_periods and median_potential_years >= policy.minimum_median_history_years
        disposition = (
            "DIAGNOSTIC_ONLY_BY_POLICY" if not candidate else
            "QUALIFIES_AFTER_SEMANTIC_REPLAY" if breadth and history else
            "UNRESOLVED_REVIEW_COULD_MEET_GATES" if p_breadth and p_history else
            "INSUFFICIENT_VALIDATED_DOMAIN_COVERAGE"
        )
        domains.append({
            "run_id": args.run_id, "metric_id": rule.metric_id,
            "comparison_domain_id": rule.comparison_domain_id,
            "domain_applicable_ticker_count": len(rule.applicable_tickers),
            "minimum_accepted_fraction": rule.minimum_accepted_fraction,
            "required_accepted_breadth": rule.minimum_accepted_breadth,
            "calibration_eligibility": rule.calibration_eligibility,
            "normalization_scope": rule.normalization_scope,
            "canonical_accepted_ticker_count": len(canonical_tickers),
            "shadow_accepted_ticker_count": len(shadow_tickers),
            "semantic_replay_accepted_ticker_count": len(replay_tickers),
            "union_accepted_ticker_count": len(union_by_ticker),
            "accepted_applicable_fraction": round(len(union_by_ticker) / len(rule.applicable_tickers), 6),
            "unresolved_review_ticker_count": len(unresolved_tickers),
            "potential_ticker_count": len(potential_by_ticker),
            "potential_fraction": round(len(potential_by_ticker) / len(rule.applicable_tickers), 6),
            "median_union_accepted_periods": median_periods,
            "median_union_history_years": round(median_years, 6),
            "median_potential_periods": median_potential,
            "median_potential_history_years": round(median_potential_years, 6),
            "breadth_gate": "PASS" if breadth else "FAIL", "history_gate": "PASS" if history else "FAIL",
            "potential_breadth_gate": "PASS" if p_breadth else "FAIL",
            "potential_history_gate": "PASS" if p_history else "FAIL",
            "post_review_calibration_disposition": disposition,
        })

    qualifying = [f"{row['metric_id']}::{row['comparison_domain_id']}" for row in domains if row["post_review_calibration_disposition"] == "QUALIFIES_AFTER_SEMANTIC_REPLAY"]
    summary: dict[str, Any] = {
        "acceptance": "PASS", "audit_phase": "POST_SEMANTIC_REPLAY", "asof_date": args.asof,
        "run_id": args.run_id, "review_policy_version": REVIEW_POLICY_VERSION,
        "surface_domain_policy_version": policy.surface_domain_policy_version,
        "investable_universe_policy_sha256": file_sha256(POLICY),
        "semantic_replay_manifest_sha256": file_sha256(replay_manifest_path),
        "reviewed_high_definition_count": int(replay_manifest["high_definition_count"]),
        "semantic_replay_accepted_candidate_count": int(replay_manifest["accepted_candidate_count"]),
        "unresolved_candidate_count": len(unresolved_keys),
        "metric_domain_rule_count": len(rules),
        "metric_domains_meeting_gates_after_semantic_replay": qualifying,
        "metric_domain_count_meeting_gates_after_semantic_replay": len(qualifying),
        "disposition_counts": dict(sorted(Counter(str(row["post_review_calibration_disposition"]) for row in domains).items())),
        "canonical_candidate_mutation": False, "historical_reconstruction_authorized": False,
        "calibration_authorized": False, "production_promotion_authorized": False,
        "next_gate": "FREEZE_QUALIFYING_DOMAIN_METRICS_OR_RESOLVE_REMAINING_REVIEW_QUEUE",
    }
    write_csv_atomic(output_dir / "transportation_surface_delta_parser_coverage_by_ticker_domain.csv", DETAIL_FIELDS, detail)
    write_csv_atomic(output_dir / "transportation_surface_delta_parser_coverage_by_metric_domain.csv", DOMAIN_FIELDS, domains)
    write_text_atomic(
        output_dir / "transportation_surface_delta_parser_domain_coverage.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
