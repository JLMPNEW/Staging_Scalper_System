#!/usr/bin/env python3
"""Audit current 35-name v5 inputs before any historical reconstruction."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    load_investable_universe_policy,
    validate_investable_universe_policy,
)
from industrials.transportation.semantic_candidate_materialization import (  # noqa: E402
    EXTRACTION_METHOD,
    SOURCE_ID,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


DEFAULT_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_investable_universe_v5.yaml"
DEFAULT_AVAILABILITY = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "preflight" / "2026-08-13" / "transportation_metric_availability_post_semantic.csv"
DEFAULT_MATERIALIZATION = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "semantic_candidates" / "2026-08-13" / "transportation_semantic_candidate_materialization.json"
DEFAULT_SURFACE = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v4" / "surface_reentry" / "2026-08-13" / "v5_domain_coverage_strict" / "transportation_surface_v5_domain_coverage.json"
DEFAULT_TANKER = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "tanker_coverage_strict" / "2026-08-13" / "transportation_tanker_v5_coverage.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "current_readiness"
OBSERVED = {"REPORTED", "DERIVED", "PROXY"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--availability", type=Path, default=DEFAULT_AVAILABILITY)
    parser.add_argument("--materialization", type=Path, default=DEFAULT_MATERIALIZATION)
    parser.add_argument("--surface-coverage", type=Path, default=DEFAULT_SURFACE)
    parser.add_argument("--tanker-coverage", type=Path, default=DEFAULT_TANKER)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    config_path = args.config.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    availability_path = args.availability.expanduser().resolve()
    materialization_path = args.materialization.expanduser().resolve()
    surface_path = args.surface_coverage.expanduser().resolve()
    tanker_path = args.tanker_coverage.expanduser().resolve()
    errors: list[str] = []

    policy = load_investable_universe_policy(policy_path)
    policy_errors, policy_summary = validate_investable_universe_policy(policy)
    errors.extend(policy_errors)
    if policy.policy_version != "transportation_investable_universe_v5":
        errors.append("candidate policy is not v5")
    selected = set(policy.selected_tickers)
    group_by_ticker = {
        ticker: group.group_id for group in policy.groups for ticker in group.tickers
    }
    if len(selected) != 35:
        errors.append(f"v5 selected count={len(selected)} expected=35")

    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    registry_path = resolve_path(
        config["model_families"][MODEL_FAMILY]["financial"]["metric_registry"],
        base_dir=config_path.parent,
    )
    registry_version, definitions = load_metric_registry(registry_path)
    availability = _csv(availability_path)
    expected_total = policy.expected_catalog_count * len(definitions)
    if len(availability) != expected_total:
        errors.append(
            f"availability row count={len(availability)} expected={expected_total}"
        )
    duplicate_keys = Counter(
        (row.get("ticker", ""), row.get("metric_name", "")) for row in availability
    )
    duplicates = sorted(key for key, count in duplicate_keys.items() if count != 1)
    if duplicates:
        errors.append(f"availability duplicate/missing identities={duplicates[:20]}")
    availability_by_pair = {
        (str(row.get("ticker") or ""), str(row.get("metric_name") or "")): row
        for row in availability
    }
    cohort_industry = {
        str(row.get("ticker") or ""): (
            str(row.get("calibration_cohort") or ""),
            str(row.get("industry") or ""),
        )
        for row in availability
        if str(row.get("ticker") or "") in selected
    }
    required_counts: Counter[str] = Counter()
    missing_required: list[str] = []
    required_statuses: Counter[str] = Counter()
    for ticker in sorted(selected):
        cohort, industry = cohort_industry.get(ticker, ("", ""))
        for definition in definitions:
            if not definition.required_for_rank or not definition.applies_to(
                cohort=cohort, industry=industry
            ):
                continue
            required_counts[ticker] += 1
            row = availability_by_pair.get((ticker, definition.metric_id), {})
            status = str(row.get("availability_status") or "MISSING_ROW")
            required_statuses[status] += 1
            if status not in OBSERVED:
                missing_required.append(f"{ticker}:{definition.metric_id}:{status}")
    if set(required_counts.values()) != {9}:
        errors.append(
            f"required metric applicability is not exactly 9 per v5 ticker: {dict(required_counts)}"
        )
    if missing_required:
        errors.append(f"v5 required metrics missing={missing_required[:30]}")

    materialization = _manifest(materialization_path)
    surface = _manifest(surface_path)
    tanker = _manifest(tanker_path)
    if materialization.get("acceptance") != "PASS" or materialization.get("mode") != "execute":
        errors.append("semantic candidate materialization is not an executed PASS")
    if surface.get("acceptance") != "PASS" or tanker.get("acceptance") != "PASS":
        errors.append("strict specialized coverage manifest is not PASS")
    if str(tanker.get("policy_sha256") or "") != file_sha256(policy_path):
        errors.append("tanker coverage does not pin current v5 policy")

    latest_feature_counts: dict[str, int] = {}
    active: set[str] = set()
    candidate_count = 0
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as connection:
        init_db(connection)
        active = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT ticker FROM dim_universe_membership
                WHERE model_family=? AND membership_source_id=?
                  AND membership_status='active' AND start_date<=?
                  AND COALESCE(end_date,'9999-12-31')>=?
                """,
                (
                    MODEL_FAMILY,
                    config["model_families"][MODEL_FAMILY]["universe"]["seed_source_id"],
                    asof,
                    asof,
                ),
            ).fetchall()
        }
        for table in ("feature_market_technical", "feature_financial_statement"):
            count = connection.execute(
                f"""
                SELECT COUNT(DISTINCT ticker) FROM {table}
                WHERE model_family=? AND asof_date<=?
                  AND ticker IN ({','.join('?' for _ in selected)})
                """,
                (MODEL_FAMILY, asof, *sorted(selected)),
            ).fetchone()[0]
            latest_feature_counts[table] = int(count)
            if int(count) != len(selected):
                errors.append(f"{table} v5 coverage={count}/{len(selected)}")
        candidate_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM fact_sec_metric_disclosure_candidate
                WHERE model_family=? AND source_id=? AND extraction_method=?
                """,
                (MODEL_FAMILY, SOURCE_ID, EXTRACTION_METHOD),
            ).fetchone()[0]
        )
    if not selected <= active:
        errors.append(f"v5 tickers absent from active research catalog={sorted(selected-active)}")
    expected_candidates = int(materialization.get("candidate_count") or 0)
    if candidate_count != expected_candidates:
        errors.append(
            f"materialized candidate count={candidate_count} expected={expected_candidates}"
        )

    specialized_metrics = {
        "operating_ratio",
        "purchased_transportation_ratio",
        "freight_weight_per_shipment",
        "shipment_or_load_growth",
        "pricing_or_yield_growth",
        "fleet_age",
    }
    specialized_current: defaultdict[str, Counter[str]] = defaultdict(Counter)
    specialized_tickers: defaultdict[str, set[str]] = defaultdict(set)
    for row in availability:
        ticker = str(row.get("ticker") or "")
        metric = str(row.get("metric_name") or "")
        if ticker not in selected or metric not in specialized_metrics:
            continue
        if str(row.get("availability_status") or "") in OBSERVED:
            group = group_by_ticker[ticker]
            specialized_current[group][metric] += 1
            specialized_tickers[f"{group}:{metric}"].add(ticker)
    tanker_fleet_age = specialized_current["oil_tanker_operators"]["fleet_age"]
    tanker_group = next(group for group in policy.groups if group.group_id == "oil_tanker_operators")
    if tanker_fleet_age < tanker_group.minimum_specialized_breadth:
        errors.append(
            f"current tanker fleet_age breadth={tanker_fleet_age} required={tanker_group.minimum_specialized_breadth}"
        )

    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "asof_date": asof,
        "contract_version": "transportation_v5_current_readiness_v1",
        "policy_version": policy.policy_version,
        "policy_effective_from": policy.effective_from,
        "selected_ticker_count": len(selected),
        "selected_count_by_group": dict(sorted(Counter(group_by_ticker.values()).items())),
        "active_research_ticker_count": len(active),
        "required_metric_expected_count": sum(required_counts.values()),
        "required_metric_observed_count": sum(required_statuses[status] for status in OBSERVED),
        "required_metric_status_counts": dict(sorted(required_statuses.items())),
        "required_metrics_per_ticker": dict(sorted(required_counts.items())),
        "current_specialized_observed_counts": {
            group: dict(sorted(counts.items()))
            for group, counts in sorted(specialized_current.items())
        },
        "current_specialized_observed_tickers": {
            key: sorted(values) for key, values in sorted(specialized_tickers.items())
        },
        "strict_surface_qualifying_metric_domains": surface.get("qualifying_metric_domains", []),
        "strict_tanker_qualifying_metrics": tanker.get("metrics_meeting_strict_gates", []),
        "materialized_candidate_count": candidate_count,
        "latest_feature_ticker_counts": latest_feature_counts,
        "metric_registry_version": registry_version,
        "artifacts": {
            "policy": {"path": str(policy_path), "sha256": file_sha256(policy_path)},
            "availability": {"path": str(availability_path), "sha256": file_sha256(availability_path)},
            "materialization": {"path": str(materialization_path), "sha256": file_sha256(materialization_path)},
            "surface_coverage": {"path": str(surface_path), "sha256": file_sha256(surface_path)},
            "tanker_coverage": {"path": str(tanker_path), "sha256": file_sha256(tanker_path)},
            "metric_registry": {"path": str(registry_path), "sha256": file_sha256(registry_path)},
            "positioning_universe": {"path": str(policy.positioning_universe_path), "sha256": file_sha256(policy.positioning_universe_path)},
        },
        "policy_validation": policy_summary,
        "errors": errors,
        "network_requests": 0,
        "parser_invocations": 0,
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_activation_authorized": False,
        "next_gate": "BUILD_AND_VALIDATE_COHORT_ISOLATED_CURRENT_SCORES" if not errors else "REPAIR_CURRENT_INPUTS",
    }
    output_dir = args.output_root.expanduser().resolve() / asof
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "transportation_v5_current_readiness.json"
    write_text_atomic(output_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
