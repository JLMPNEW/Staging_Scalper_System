#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    ADAPTER_VERSION,
    _surface_xbrl_rules,
)
from industrials.transportation.surface_metric_parser import surface_fact_rule  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
FILING_PROFILES = DATA_ROOT / "transportation_surface_filing_profiles_v1.csv"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
)
TARGET_METRICS = ("operating_ratio", "purchased_transportation_ratio")
FIELDS = (
    "fact_store_recovery_version",
    "adapter_version",
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "metric_id",
    "value",
    "unit",
    "period_start",
    "period_end",
    "status",
    "reason",
    "confidence",
    "formula",
    "numerator_concept",
    "numerator_value",
    "denominator_concept",
    "denominator_value",
    "currency",
    "source_id",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Derive surface operating and purchased-transportation ratios from "
            "the already-loaded SEC fact store; no filing document is reparsed."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--parser-run-id",
        type=int,
        default=0,
        help="Completed parser run supplying issuer-extension numerator facts",
    )
    return parser.parse_args()


def _tickers() -> tuple[str, ...]:
    import csv

    with FILING_PROFILES.open("r", encoding="utf-8-sig", newline="") as handle:
        return tuple(str(row["ticker"]).upper() for row in csv.DictReader(handle))


def _select(
    rows: Sequence[tuple[Mapping[str, Any], Mapping[str, str]]],
) -> tuple[Mapping[str, Any], Mapping[str, str]] | None:
    if not rows:
        return None
    ranked = sorted(
        rows,
        key=lambda item: (
            int(str(item[1].get("priority") or 99)),
            str(item[0]["concept_name"]),
            int(item[0]["raw_fact_id"]),
        ),
    )
    priority = int(str(ranked[0][1].get("priority") or 99))
    best = [item for item in ranked if int(str(item[1].get("priority") or 99)) == priority]
    values = {round(float(item[0]["raw_value"]), 8) for item in best if item[0]["raw_value"] is not None}
    if len(values) != 1:
        return None
    return best[0]


def _span(periods: set[str]) -> float:
    ordered = sorted(periods)
    if len(ordered) < 2:
        return 0.0
    return (date.fromisoformat(ordered[-1]) - date.fromisoformat(ordered[0])).days / 365.25


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    tickers = _tickers()
    placeholders = ",".join("?" for _ in tickers)
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    facts = connection.execute(
        "SELECT * FROM fact_sec_xbrl_fact_raw "
        f"WHERE ticker IN ({placeholders}) AND filing_date<=? "
        "AND raw_value IS NOT NULL AND period_end IS NOT NULL "
        "ORDER BY ticker, accession_number, period_start, period_end, raw_fact_id",
        (*tickers, args.asof),
    ).fetchall()

    rules = _surface_xbrl_rules()
    grouped: defaultdict[
        tuple[str, str, str, str, str, str, str, str, str, str],
        defaultdict[str, list[tuple[Mapping[str, Any], Mapping[str, str]]]],
    ] = defaultdict(lambda: defaultdict(list))
    for fact in facts:
        unit = str(fact["unit"] or "").upper()
        if not unit or unit in {"PURE", "SHARES"}:
            continue
        for metric in TARGET_METRICS:
            rule = surface_fact_rule(metric, str(fact["concept_name"]), rules)
            if rule is None or rule.get("operand_role") == "direct_value":
                continue
            key = (
                str(fact["ticker"]),
                str(fact["cik"] or ""),
                str(fact["accession_number"] or ""),
                str(fact["form_type"] or ""),
                str(fact["filing_date"] or ""),
                str(fact["accepted_at"] or ""),
                str(fact["period_start"] or ""),
                str(fact["period_end"] or ""),
                unit,
                metric,
            )
            grouped[key][str(rule["operand_role"])].append((fact, rule))

    parser_run_id = args.parser_run_id
    if not parser_run_id:
        run_row = connection.execute(
            "SELECT run_id FROM sec_parser_run WHERE model_family='transportation' "
            "AND asof_date=? AND status='COMPLETED' AND failed_work_count=0 "
            "ORDER BY run_id DESC LIMIT 1",
            (args.asof,),
        ).fetchone()
        if run_row is None:
            raise ValueError("no completed parser run is available for extension facts")
        parser_run_id = int(run_row[0])
    normalized_operands = connection.execute(
        "SELECT fact.* FROM sec_parser_run_normalized_fact AS relation "
        "JOIN sec_parser_normalized_fact_shadow AS fact "
        "ON fact.fact_fingerprint=relation.fact_fingerprint "
        "WHERE relation.run_id=? AND fact.scope='consolidated' "
        "AND fact.numeric_value IS NOT NULL",
        (parser_run_id,),
    ).fetchall()
    for index, fact in enumerate(normalized_operands, start=1):
        rule = surface_fact_rule(
            "purchased_transportation_ratio",
            str(fact["concept_name"]),
            rules,
        )
        if rule is None or rule.get("operand_role") not in {
            "purchased_transportation",
            "purchased_transportation_broad",
        }:
            continue
        unit = str(fact["unit"] or "").upper()
        key = (
            str(fact["ticker"]),
            str(fact["cik"] or ""),
            str(fact["accession_number"] or ""),
            str(fact["form_type"] or ""),
            str(fact["filing_date"] or ""),
            "",  # CompanyFacts has no accepted_at; accession+filing date remain exact.
            str(fact["period_start"] or ""),
            str(fact["period_end"] or ""),
            unit,
            "purchased_transportation_ratio",
        )
        normalized = {
            "raw_fact_id": -index,
            "concept_name": str(fact["concept_name"]),
            "raw_value": float(fact["numeric_value"]),
            "source_id": f"dedicated_parser_run_{parser_run_id}_normalized_fact",
        }
        grouped[key][str(rule["operand_role"])].append((normalized, rule))

    output: list[dict[str, object]] = []
    suppressed_conflict_groups = 0
    for key in sorted(grouped):
        (
            ticker,
            cik,
            accession,
            form_type,
            filing_date,
            accepted_at,
            period_start,
            period_end,
            currency,
            metric,
        ) = key
        roles = grouped[key]
        revenue = _select(roles.get("revenue", ()))
        if revenue is None or float(revenue[0]["raw_value"]) <= 0:
            continue
        denominator = float(revenue[0]["raw_value"])
        numerator = None
        broad = False
        if metric == "operating_ratio":
            numerator = _select(roles.get("operating_expense", ()))
            if numerator is not None:
                value = float(numerator[0]["raw_value"]) / denominator
                formula = "operating_expense/revenue"
            else:
                numerator = _select(roles.get("operating_income", ()))
                if numerator is None:
                    continue
                value = 1.0 - float(numerator[0]["raw_value"]) / denominator
                formula = "1-operating_income/revenue"
            upper_bound = 3.0
        else:
            numerator = _select(roles.get("purchased_transportation", ()))
            if numerator is None:
                numerator = _select(roles.get("purchased_transportation_broad", ()))
                broad = numerator is not None
            if numerator is None:
                continue
            value = float(numerator[0]["raw_value"]) / denominator
            formula = "purchased_transportation/revenue"
            upper_bound = 1.0
        if not math.isfinite(value) or value < 0 or value > upper_bound:
            suppressed_conflict_groups += 1
            continue
        numerator_fact, _ = numerator
        output.append(
            {
                "fact_store_recovery_version": "transportation_surface_fact_store_ratios_v1",
                "adapter_version": ADAPTER_VERSION,
                "ticker": ticker,
                "cik": cik,
                "accession_number": accession,
                "form_type": form_type,
                "filing_date": filing_date,
                "accepted_at": accepted_at,
                "metric_id": metric,
                "value": value,
                "unit": "ratio",
                "period_start": period_start,
                "period_end": period_end,
                "status": "REVIEW_REQUIRED",
                "reason": (
                    "broad_fact_store_operand_requires_note_confirmation"
                    if broad
                    else "derived_from_loaded_sec_fact_store_requires_definition_review"
                ),
                "confidence": 0.78 if broad else 0.92,
                "formula": formula,
                "numerator_concept": str(numerator_fact["concept_name"]),
                "numerator_value": float(numerator_fact["raw_value"]),
                "denominator_concept": str(revenue[0]["concept_name"]),
                "denominator_value": denominator,
                "currency": currency,
                "source_id": str(numerator_fact["source_id"]),
            }
        )

    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for row in output:
        dedupe_key = (
            row["ticker"],
            row["metric_id"],
            row["accession_number"],
            row["period_start"],
            row["period_end"],
            round(float(row["value"]), 12),
            row["numerator_concept"],
            row["denominator_concept"],
        )
        unique[dedupe_key] = row
    output = sorted(
        unique.values(),
        key=lambda row: (
            str(row["metric_id"]),
            str(row["ticker"]),
            str(row["period_end"]),
            str(row["accession_number"]),
        ),
    )
    csv_path = output_dir / "transportation_surface_fact_store_ratio_candidates.csv"
    write_csv_atomic(csv_path, FIELDS, output)

    counts = Counter(str(row["metric_id"]) for row in output)
    issuer_counts = {
        metric: len({str(row["ticker"]) for row in output if row["metric_id"] == metric})
        for metric in TARGET_METRICS
    }
    periods_by_metric_ticker: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for row in output:
        periods_by_metric_ticker[(str(row["metric_id"]), str(row["ticker"]))].add(
            str(row["period_end"])
        )
    depth = {
        metric: {
            "median_periods": statistics.median(
                [len(periods) for (row_metric, _), periods in periods_by_metric_ticker.items() if row_metric == metric]
            )
            if any(row_metric == metric for row_metric, _ in periods_by_metric_ticker)
            else 0,
            "median_history_years": statistics.median(
                [_span(periods) for (row_metric, _), periods in periods_by_metric_ticker.items() if row_metric == metric]
            )
            if any(row_metric == metric for row_metric, _ in periods_by_metric_ticker)
            else 0.0,
        }
        for metric in TARGET_METRICS
    }
    summary = {
        "acceptance": "PASS",
        "asof_date": args.asof,
        "adapter_version": ADAPTER_VERSION,
        "input_raw_fact_count": len(facts),
        "parser_run_id_for_extension_facts": parser_run_id,
        "input_normalized_extension_fact_count": len(normalized_operands),
        "source_document_reparse_count": 0,
        "derived_candidate_count": len(output),
        "candidate_counts_by_metric": dict(sorted(counts.items())),
        "issuer_counts_by_metric": issuer_counts,
        "history_depth_by_metric": depth,
        "suppressed_invalid_or_conflicting_group_count": suppressed_conflict_groups,
        "output_csv": str(csv_path),
        "canonical_candidate_mutation": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "SEMANTICALLY_VALIDATE_FACT_STORE_RATIO_DEFINITIONS",
    }
    write_text_atomic(
        output_dir / "transportation_surface_fact_store_ratio_candidates.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
