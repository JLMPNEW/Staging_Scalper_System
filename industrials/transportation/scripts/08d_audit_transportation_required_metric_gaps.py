#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


STALE_FACT_MAX_LAG_DAYS = 400
METRIC_DEPENDENCIES = {
    "operating_margin": ("revenue", "operating_income"),
    "fcf_margin": ("revenue", "operating_cash_flow", "capex"),
    "capex_to_revenue": ("revenue", "capex"),
    "cash_runway_years": (
        "cash_and_equivalents",
        "operating_cash_flow",
        "capex",
    ),
    "capital_raise_dependence": (
        "operating_cash_flow",
        "capex",
        "equity_issuance_proceeds",
        "debt_issuance_proceeds",
    ),
}
FIELDS = [
    "ticker",
    "metric_name",
    "gap_classification",
    "missing_or_stale_dependencies",
    "latest_dependency_periods_json",
    "candidate_taxonomy",
    "candidate_concept_name",
    "candidate_unit",
    "candidate_fact_count",
    "candidate_latest_period_end",
    "recommended_dependency",
    "alias_review_status",
    "recommended_action",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of missing required transportation financial metrics "
            "before freezing parser rules for historical backfill."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def read_reviewed_aliases(path: Path) -> set[tuple[str, str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            (
                str(row.get("taxonomy") or "").strip(),
                str(row.get("concept_name") or "").strip(),
                str(row.get("canonical_metric") or "").strip(),
            )
            for row in csv.DictReader(handle)
            if str(row.get("review_status") or "").strip().lower() == "reviewed"
        }


def candidate_dependency(concept_name: str) -> str:
    lower = concept_name.lower()
    if (
        ("paymentstoacquire" in lower or "purchaseofpropertyplant" in lower)
        and any(
            token in lower
            for token in (
                "propertyplant",
                "equipment",
                "productiveasset",
                "aircraft",
                "flight",
                "vessel",
            )
        )
    ) or lower == "paymentsforflightequipment":
        return "capex"
    if lower in {
        "operatingincomeloss",
        "profitlossfromoperatingactivities",
        "profitfromoperations",
    }:
        return "operating_income"
    if lower in {
        "netcashprovidedbyusedinoperatingactivities",
        "cashflowsfromusedinoperatingactivities",
        "netcashgeneratedfromoperatingactivities",
    }:
        return "operating_cash_flow"
    if lower.startswith("proceedsfromissuanceof") and any(
        token in lower
        for token in ("commonstock", "preferredstock", "ordinaryshares", "equity")
    ):
        return "equity_issuance_proceeds"
    if lower.startswith("proceedsfrom") and any(
        token in lower
        for token in (
            "borrowings",
            "issuanceofdebt",
            "issuanceoflongtermdebt",
            "issuanceofsecureddebt",
            "linesofcredit",
            "shorttermdebt",
            "longtermdebt",
        )
    ):
        return "debt_issuance_proceeds"
    return ""


def canonical_coverage(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    dependencies: tuple[str, ...],
    asof: str,
) -> dict[str, dict[str, Any]]:
    placeholders = ",".join("?" for _ in dependencies)
    return {
        str(row["canonical_metric"]): dict(row)
        for row in connection.execute(
            f"""
            SELECT canonical_metric, COUNT(*) AS fact_count,
                   MAX(period_end) AS latest_period_end,
                   GROUP_CONCAT(DISTINCT concept_name) AS concepts
            FROM fact_financial_statement_canonical
            WHERE ticker=? AND model_family=? AND filing_date<=?
              AND canonical_metric IN ({placeholders})
            GROUP BY canonical_metric
            ORDER BY canonical_metric
            """,
            (ticker, MODEL_FAMILY, asof, *dependencies),
        ).fetchall()
    }


def raw_candidates(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    missing_or_stale: set[str],
    asof: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT r.taxonomy, r.concept_name, COALESCE(r.unit, '') AS unit,
               COUNT(*) AS fact_count, MAX(r.period_end) AS latest_period_end,
               GROUP_CONCAT(DISTINCT m.canonical_metric) AS mapped_metrics
        FROM fact_sec_xbrl_fact_raw AS r
        LEFT JOIN dim_xbrl_concept_map AS m
          ON m.taxonomy=r.taxonomy AND m.concept_name=r.concept_name
         AND m.active_flag=1
        WHERE r.ticker=? AND r.filing_date<=?
          AND r.taxonomy IN ('us-gaap', 'ifrs-full')
        GROUP BY r.taxonomy, r.concept_name, COALESCE(r.unit, '')
        ORDER BY MAX(r.period_end) DESC, r.taxonomy, r.concept_name
        """,
        (ticker, asof),
    ).fetchall():
        dependency = candidate_dependency(str(row["concept_name"]))
        if dependency not in missing_or_stale:
            continue
        if str(row["mapped_metrics"] or ""):
            continue
        output.append({**dict(row), "recommended_dependency": dependency})
    return output


def main() -> int:
    args = parse_args()
    asof_date = date.fromisoformat(str(args.asof)[:10])
    asof = asof_date.isoformat()
    stale_cutoff = (asof_date - timedelta(days=STALE_FACT_MAX_LAG_DAYS)).isoformat()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    financial = family["financial"]
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    stage4_dir = resolve_path(
        financial["metric_validation_output_json"], base_dir=base_dir
    ).parent
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else stage4_dir / "transportation_required_metric_gap_audit.csv"
    )
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else stage4_dir / "transportation_required_metric_gap_audit.json"
    )
    aliases_path = resolve_path(
        financial["concept_aliases_csv"], base_dir=base_dir
    )
    reviewed_aliases = read_reviewed_aliases(aliases_path)
    rows: list[dict[str, Any]] = []
    gap_classifications: list[str] = []
    with read_only_connection(db_path) as connection:
        gaps = connection.execute(
            """
            SELECT ticker, metric_name
            FROM feature_financial_metric_availability
            WHERE model_family=? AND asof_date=?
              AND availability_status='NOT_DISCLOSED'
              AND metric_name IN (
                  'operating_margin', 'fcf_margin', 'capex_to_revenue',
                  'cash_runway_years', 'capital_raise_dependence'
              )
            ORDER BY ticker, metric_name
            """,
            (MODEL_FAMILY, asof),
        ).fetchall()
        for gap in gaps:
            ticker = str(gap["ticker"])
            metric_name = str(gap["metric_name"])
            dependencies = METRIC_DEPENDENCIES[metric_name]
            canonical = canonical_coverage(
                connection,
                ticker=ticker,
                dependencies=dependencies,
                asof=asof,
            )
            latest_periods = {
                dependency: str(
                    canonical.get(dependency, {}).get("latest_period_end") or ""
                )
                for dependency in dependencies
            }
            missing_or_stale = {
                dependency
                for dependency, latest_period in latest_periods.items()
                if not latest_period or latest_period < stale_cutoff
            }
            candidates = raw_candidates(
                connection,
                ticker=ticker,
                missing_or_stale=missing_or_stale,
                asof=asof,
            )
            for candidate in candidates:
                dependency = str(candidate["recommended_dependency"])
                mapped_concepts = {
                    value
                    for value in str(
                        canonical.get(dependency, {}).get("concepts") or ""
                    ).split(",")
                    if value
                }
                candidate["canonical_remap_present"] = (
                    str(candidate["concept_name"]) in mapped_concepts
                )
            reviewed = [
                candidate
                for candidate in candidates
                if (
                    str(candidate["taxonomy"]),
                    str(candidate["concept_name"]),
                    str(candidate["recommended_dependency"]),
                )
                in reviewed_aliases
            ]
            unreviewed = [candidate for candidate in candidates if candidate not in reviewed]
            reviewed_unmapped = [
                candidate
                for candidate in reviewed
                if not bool(candidate["canonical_remap_present"])
            ]
            if unreviewed:
                classification = "REUSABLE_MAPPING_REVIEW"
            elif reviewed_unmapped:
                classification = "APPROVED_ALIAS_REMAP_REQUIRED"
            elif missing_or_stale:
                classification = "SOURCE_OR_PERIOD_GAP"
            else:
                classification = "TTM_ALIGNMENT_GAP"
            gap_classifications.append(classification)
            candidate_rows = candidates or [None]
            for candidate in candidate_rows:
                alias_status = ""
                action = "retain_missing_source_or_period"
                if candidate is not None:
                    alias_key = (
                        str(candidate["taxonomy"]),
                        str(candidate["concept_name"]),
                        str(candidate["recommended_dependency"]),
                    )
                    alias_status = (
                        "reviewed" if alias_key in reviewed_aliases else "unreviewed"
                    )
                    if alias_status == "unreviewed":
                        action = "analyst_review_before_alias"
                    elif bool(candidate["canonical_remap_present"]):
                        action = "retain_reviewed_mapping_current_source_gap"
                    else:
                        action = "remap_reviewed_alias"
                elif classification == "TTM_ALIGNMENT_GAP":
                    action = "investigate_ttm_window_alignment"
                rows.append(
                    {
                        "ticker": ticker,
                        "metric_name": metric_name,
                        "gap_classification": classification,
                        "missing_or_stale_dependencies": "|".join(
                            sorted(missing_or_stale)
                        ),
                        "latest_dependency_periods_json": json.dumps(
                            latest_periods,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "candidate_taxonomy": (
                            str(candidate["taxonomy"]) if candidate else ""
                        ),
                        "candidate_concept_name": (
                            str(candidate["concept_name"]) if candidate else ""
                        ),
                        "candidate_unit": str(candidate["unit"]) if candidate else "",
                        "candidate_fact_count": (
                            int(candidate["fact_count"]) if candidate else ""
                        ),
                        "candidate_latest_period_end": (
                            str(candidate["latest_period_end"] or "")
                            if candidate
                            else ""
                        ),
                        "recommended_dependency": (
                            str(candidate["recommended_dependency"])
                            if candidate
                            else ""
                        ),
                        "alias_review_status": alias_status,
                        "recommended_action": action,
                    }
                )
    counts = Counter(gap_classifications)
    freeze_ready = not counts.get("REUSABLE_MAPPING_REVIEW", 0) and not counts.get(
        "APPROVED_ALIAS_REMAP_REQUIRED", 0
    )
    write_csv_atomic(output_csv, FIELDS, rows)
    errors = (
        []
        if freeze_ready
        else [
            "financial parser rules cannot be frozen while reusable mappings "
            "or approved-but-unmapped aliases remain"
        ]
    )
    result = {
        "acceptance": "PASS" if freeze_ready else "FAIL",
        "asof_date": asof,
        "database_path": str(db_path),
        "metric_gap_count": len(gap_classifications),
        "audit_row_count": len(rows),
        "gap_classification_counts": dict(sorted(counts.items())),
        "stale_fact_max_lag_days": STALE_FACT_MAX_LAG_DAYS,
        "stale_period_cutoff": stale_cutoff,
        "reviewed_alias_count": len(reviewed_aliases),
        "financial_parser_rule_freeze_status": (
            "READY" if freeze_ready else "MAPPING_REVIEW_REQUIRED"
        ),
        "output_csv": str(output_csv),
        "errors": errors,
    }
    write_manifest(output_json, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if freeze_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
