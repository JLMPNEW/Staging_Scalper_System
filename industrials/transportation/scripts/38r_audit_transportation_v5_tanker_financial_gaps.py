#!/usr/bin/env python3
"""Classify v5 tanker financial-history gaps without fetching or mutating data."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_VALIDATION = (
    ROOT
    / "investable_v5"
    / "historical_rebuild"
    / "2026-08-15"
    / "transportation_v5_historical_rebuild_validation.json"
)
DEFAULT_OUTPUT = (
    ROOT
    / "investable_v5"
    / "historical_rebuild"
    / "2026-08-15"
    / "transportation_v5_tanker_financial_gap_audit.json"
)
RATIO_DEPENDENCIES = {
    "operating_margin": ("revenue", "operating_income"),
    "fcf_margin": ("revenue", "operating_cash_flow", "capex"),
    "capex_to_revenue": ("revenue", "capex"),
}
CONCEPT_TERMS = {
    "revenue": ("revenue", "turnover", "charterhire"),
    "operating_income": ("operatingincome", "operatingprofit", "profitfromoperations"),
    "operating_cash_flow": ("operatingactivities", "operatingcashflow"),
    "capex": (
        "propertyplantandequipment",
        "capitalexpenditure",
        "capitalasset",
        "vessel",
        "newbuild",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def normalize_concept(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def candidate_for(metric: str, concept_name: str) -> bool:
    normalized = normalize_concept(concept_name)
    if not any(term in normalized for term in CONCEPT_TERMS[metric]):
        return False
    if metric != "capex":
        return True
    spend_terms = ("payment", "purchase", "acquire", "acquisition", "addition", "expenditure")
    return any(term in normalized for term in spend_terms)


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    validation_path = args.validation.expanduser().resolve()
    validation = read_json(validation_path)
    cohort_results = dict(validation["cohort_results"])
    tanker_ids = [cohort_id for cohort_id in cohort_results if "tanker" in cohort_id]
    if len(tanker_ids) != 1:
        raise ValueError(f"expected one tanker cohort, found {tanker_ids}")
    tanker = dict(cohort_results[tanker_ids[0]])
    missing = {
        str(ticker): {
            str(metric): int(count)
            for metric, count in dict(metrics).items()
            if metric in RATIO_DEPENDENCIES and int(count) > 0
        }
        for ticker, metrics in dict(tanker["missing_required_dates_by_ticker"]).items()
    }
    missing = {ticker: metrics for ticker, metrics in missing.items() if metrics}
    ticker_dependencies = {
        ticker: sorted(
            {
                dependency
                for metric in metrics
                for dependency in RATIO_DEPENDENCIES[metric]
            }
        )
        for ticker, metrics in missing.items()
    }
    findings: dict[str, Any] = {}
    unmapped_candidates: list[dict[str, Any]] = []
    with read_only(db_path) as connection:
        for ticker, dependencies in sorted(ticker_dependencies.items()):
            canonical_rows = connection.execute(
                """
                SELECT canonical_metric, COUNT(*) AS row_count,
                       COUNT(DISTINCT period_end) AS period_count,
                       MIN(period_end) AS first_period_end,
                       MAX(period_end) AS last_period_end
                FROM fact_financial_statement_canonical
                WHERE model_family=? AND ticker=?
                  AND canonical_metric IN ({})
                GROUP BY canonical_metric
                ORDER BY canonical_metric
                """.format(",".join("?" for _ in dependencies)),
                (MODEL_FAMILY, ticker, *dependencies),
            ).fetchall()
            canonical = {str(row["canonical_metric"]): dict(row) for row in canonical_rows}
            backfill_rows = connection.execute(
                """
                SELECT m.canonical_metric,r.taxonomy,r.concept_name,
                       COUNT(*) AS row_count,
                       COUNT(DISTINCT r.period_end) AS period_count,
                       MIN(r.period_end) AS first_period_end,
                       MAX(r.period_end) AS last_period_end
                FROM fact_sec_xbrl_fact_raw AS r
                JOIN dim_xbrl_concept_map AS m
                  ON m.taxonomy=r.taxonomy
                 AND m.concept_name=r.concept_name
                 AND m.active_flag=1
                LEFT JOIN fact_sec_xbrl_fact AS f
                  ON f.raw_fact_id=r.raw_fact_id
                 AND f.canonical_metric=m.canonical_metric
                WHERE r.ticker=? AND r.period_end IS NOT NULL
                  AND m.canonical_metric IN ({})
                  AND f.fact_id IS NULL
                GROUP BY m.canonical_metric,r.taxonomy,r.concept_name
                ORDER BY m.canonical_metric,r.taxonomy,r.concept_name
                """.format(",".join("?" for _ in dependencies)),
                (ticker, *dependencies),
            ).fetchall()
            backfill_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in backfill_rows:
                backfill_candidates[str(row["canonical_metric"])].append(dict(row))
            raw_rows = connection.execute(
                """
                SELECT r.taxonomy,r.concept_name,COUNT(*) AS row_count,
                       COUNT(DISTINCT r.period_end) AS period_count,
                       MIN(r.period_end) AS first_period_end,
                       MAX(r.period_end) AS last_period_end,
                       GROUP_CONCAT(DISTINCT r.form_type) AS form_types,
                       GROUP_CONCAT(DISTINCT r.unit) AS units,
                       GROUP_CONCAT(DISTINCT r.source_detail) AS source_details,
                       MIN(r.raw_value) AS minimum_raw_value,
                       MAX(r.raw_value) AS maximum_raw_value,
                       GROUP_CONCAT(DISTINCT f.canonical_metric) AS mapped_metrics
                FROM fact_sec_xbrl_fact_raw AS r
                LEFT JOIN fact_sec_xbrl_fact AS f ON f.raw_fact_id=r.raw_fact_id
                WHERE r.ticker=? AND r.period_end IS NOT NULL
                GROUP BY r.taxonomy,r.concept_name
                ORDER BY r.taxonomy,r.concept_name
                """,
                (ticker,),
            ).fetchall()
            candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for raw in raw_rows:
                concept_name = str(raw["concept_name"])
                mapped = {
                    value
                    for value in str(raw["mapped_metrics"] or "").split(",")
                    if value
                }
                for dependency in dependencies:
                    if not candidate_for(dependency, concept_name):
                        continue
                    item = dict(raw)
                    item["target_dependency"] = dependency
                    item["target_already_mapped"] = dependency in mapped
                    candidates[dependency].append(item)
                    if dependency not in mapped:
                        unmapped_candidates.append({"ticker": ticker, **item})
            dependency_classification: dict[str, str] = {}
            for dependency in dependencies:
                mapped_periods = int(canonical.get(dependency, {}).get("period_count") or 0)
                reviewed_backfill_periods = sum(
                    int(row["period_count"])
                    for row in backfill_candidates.get(dependency, [])
                )
                raw_candidate_periods = sum(
                    int(row["period_count"])
                    for row in candidates.get(dependency, [])
                    if not row["target_already_mapped"]
                )
                if reviewed_backfill_periods:
                    classification = "LOADED_REVIEWED_MAP_BACKFILL_AVAILABLE"
                elif mapped_periods:
                    classification = "MAPPED_FACT_SELECTION_OR_PERIOD_ALIGNMENT"
                elif raw_candidate_periods:
                    classification = "SEMANTIC_REVIEW_REQUIRED"
                else:
                    classification = "NO_LOCAL_STRUCTURED_CANDIDATE"
                dependency_classification[dependency] = classification
            findings[ticker] = {
                "missing_ratio_dates": missing[ticker],
                "dependencies": dependencies,
                "dependency_classification": dependency_classification,
                "canonical_coverage": canonical,
                "reviewed_map_backfill_candidates": dict(backfill_candidates),
                "raw_concept_candidates": dict(candidates),
            }
    result = {
        "acceptance": "PASS",
        "contract_version": "transportation_v5_tanker_financial_gap_audit_v1",
        "purpose": "Classify already-loaded evidence before any delta repair; no fetch or parse.",
        "affected_ticker_count": len(findings),
        "unmapped_candidate_count": len(unmapped_candidates),
        "findings": findings,
        "unmapped_candidates": unmapped_candidates,
        "network_requests": 0,
        "parser_invocations": 0,
        "database_mutations": 0,
        "source_validation": str(validation_path),
    }
    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(output_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "acceptance": result["acceptance"],
        "affected_ticker_count": len(findings),
        "unmapped_candidate_count": len(unmapped_candidates),
        "output": str(output_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
