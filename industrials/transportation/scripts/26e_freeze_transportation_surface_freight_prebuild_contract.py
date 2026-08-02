#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.oos_research import artifact_sha256, finite_float  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.surface_freight_score_engine import (  # noqa: E402
    build_surface_component_scores,
    candidate_registry_from_policy,
    load_surface_freight_score_policy,
    metric_score_field,
    score_surface_metric_percentiles,
    surface_freight_score_eligible,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_DIAGNOSTIC_PANEL = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "research_redesign"
    / "surface_freight_v1"
    / "transportation_generic_oos_panel.csv"
)
DEFAULT_DISPOSITIONS = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "dedicated_parser"
    / "2026-07-22"
    / "transportation_final_metric_dispositions.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the outcome-blind 24-name transportation score contract "
            "before the single historical rebuild."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="2026-07-30")
    parser.add_argument("--diagnostic-panel", type=Path, default=DEFAULT_DIAGNOSTIC_PANEL)
    parser.add_argument("--dispositions", type=Path, default=DEFAULT_DISPOSITIONS)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def read_dispositions(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("metric_id") or ""): str(row.get("metric_disposition") or "")
            for row in csv.DictReader(handle)
        }


def parse_iso(value: str) -> date:
    return date.fromisoformat(value[:10])


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_dates_by_ticker(
    connection: sqlite3.Connection,
    *,
    table: str,
    date_field: str,
    tickers: list[str],
) -> dict[str, list[date]]:
    marks = ",".join("?" for _ in tickers)
    rows = connection.execute(
        f"SELECT ticker,{date_field} FROM {table} "
        f"WHERE ticker IN ({marks}) ORDER BY ticker,{date_field}",
        tickers,
    ).fetchall()
    output: dict[str, list[date]] = defaultdict(list)
    for row in rows:
        raw = str(row[date_field] or "")[:10]
        if raw:
            output[str(row["ticker"])].append(parse_iso(raw))
    return output


def positioning_feasibility(
    connection: sqlite3.Connection,
    *,
    panel_rows: list[dict[str, object]],
    policy: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    tickers = sorted({str(item).upper() for item in policy["eligible_tickers"]})
    form4_not_applicable = {"CNI", "CP", "TFII"}
    form4_dates = load_dates_by_ticker(
        connection,
        table="fact_sec_form4_transaction",
        date_field="filing_date",
        tickers=tickers,
    )
    institutional_dates = load_dates_by_ticker(
        connection,
        table="fact_13f_positioning",
        date_field="asof_date",
        tickers=tickers,
    )
    marks = ",".join("?" for _ in tickers)
    short_rows: dict[str, list[tuple[date, date]]] = defaultdict(list)
    for row in connection.execute(
        f"SELECT ticker,settlement_date,publication_date FROM fact_short_interest "
        f"WHERE ticker IN ({marks}) ORDER BY ticker,settlement_date",
        tickers,
    ).fetchall():
        settlement = parse_iso(str(row["settlement_date"]))
        publication_raw = str(row["publication_date"] or "")[:10]
        known = parse_iso(publication_raw) if publication_raw else settlement + timedelta(days=14)
        short_rows[str(row["ticker"])].append((settlement, known))

    unique_pairs = sorted(
        {
            (str(row.get("asof_date") or "")[:10], str(row.get("ticker") or "").upper())
            for row in panel_rows
        }
    )
    per_ticker: dict[str, dict[str, int]] = {
        ticker: {"rows": 0, "rebuildable": 0, "institutional": 0, "short": 0}
        for ticker in tickers
    }
    by_date: dict[str, list[bool]] = defaultdict(list)
    for asof_text, ticker in unique_pairs:
        asof = parse_iso(asof_text)
        inst_dates = institutional_dates.get(ticker, [])
        inst_index = bisect.bisect_right(inst_dates, asof) - 1
        has_inst = (
            inst_index >= 0 and (asof - inst_dates[inst_index]).days <= 120
        )
        latest_short = any(
            settlement <= asof and known <= asof
            for settlement, known in short_rows.get(ticker, [])
        )
        prior_cutoff = asof - timedelta(days=92)
        prior_short = any(
            settlement <= prior_cutoff and known <= asof
            for settlement, known in short_rows.get(ticker, [])
        )
        has_short_change = latest_short and prior_short
        form4_applicable = ticker not in form4_not_applicable
        form4_covered = (not form4_applicable) or bool(form4_dates.get(ticker))
        applicable = 4 if form4_applicable else 2
        observed = int(has_inst) + int(has_short_change) + (
            2 if form4_applicable and form4_covered else 0
        )
        rebuildable = form4_covered and observed / applicable >= 0.50
        stats = per_ticker[ticker]
        stats["rows"] += 1
        stats["rebuildable"] += int(rebuildable)
        stats["institutional"] += int(has_inst)
        stats["short"] += int(has_short_change)
        by_date[asof_text].append(rebuildable)

    gate = policy["positioning_history_gate"]
    minimum_cross_section = int(gate["minimum_date_cross_section"])
    eligible_dates = sum(sum(flags) >= minimum_cross_section for flags in by_date.values())
    row_count = len(unique_pairs)
    rebuildable_rows = sum(item["rebuildable"] for item in per_ticker.values())
    row_coverage = rebuildable_rows / row_count if row_count else 0.0
    date_fraction = eligible_dates / len(by_date) if by_date else 0.0
    passed = (
        row_coverage >= float(gate["minimum_rebuildable_row_coverage"])
        and date_fraction >= float(gate["minimum_eligible_date_fraction"])
    )
    report = [
        {
            "ticker": ticker,
            "historical_row_count": str(stats["rows"]),
            "rebuildable_row_count": str(stats["rebuildable"]),
            "rebuildable_coverage": (
                f"{stats['rebuildable'] / stats['rows']:.8f}" if stats["rows"] else "0"
            ),
            "institutional_coverage": (
                f"{stats['institutional'] / stats['rows']:.8f}" if stats["rows"] else "0"
            ),
            "short_change_coverage": (
                f"{stats['short'] / stats['rows']:.8f}" if stats["rows"] else "0"
            ),
            "form4_policy": (
                "not_applicable" if ticker in form4_not_applicable else "covered_route"
            ),
        }
        for ticker, stats in sorted(per_ticker.items())
    ]
    return {
        "acceptance": "PASS" if passed else "FAIL",
        "historical_row_count": row_count,
        "rebuildable_row_count": rebuildable_rows,
        "rebuildable_row_coverage": row_coverage,
        "historical_date_count": len(by_date),
        "eligible_date_count": eligible_dates,
        "eligible_date_fraction": date_fraction,
        "minimum_date_cross_section": minimum_cross_section,
        "source_tables": [
            "fact_sec_form4_transaction",
            "fact_13f_positioning",
            "fact_short_interest",
        ],
        "point_in_time_rule": "filing_or_publication_known_by_score_date",
    }, report


def metric_coverage_rows(
    rows: list[dict[str, object]],
    definitions: list[Any],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for definition in definitions:
        applicable = 0
        observed = 0
        for row in rows:
            if definition.birthdate and str(row.get("asof_date") or "") < definition.birthdate:
                continue
            if not definition.applies_to(
                cohort=str(row.get("calibration_cohort") or ""),
                industry=str(row.get("industry") or ""),
            ):
                continue
            statuses = json.loads(str(row.get("metric_status_json") or "{}"))
            if statuses.get(definition.metric_id) == "NOT_APPLICABLE":
                continue
            applicable += 1
            if finite_float(row.get(metric_score_field(definition.metric_id))) is not None:
                observed += 1
        output.append(
            {
                "metric_id": definition.metric_id,
                "component": definition.component,
                "specialized": "1" if definition.specialized else "0",
                "required_for_rank": "1" if definition.required_for_rank else "0",
                "applicable_row_count": str(applicable),
                "observed_row_count": str(observed),
                "observed_coverage": f"{observed / applicable:.8f}" if applicable else "",
            }
        )
    return output


def artifact(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": artifact_sha256(path)}


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, "transportation")
    scoring = family["scoring"]
    base_dir = config_path.parent
    policy_path = resolve_path(scoring["surface_freight_score_policy"], base_dir=base_dir)
    policy = load_surface_freight_score_policy(policy_path)
    registry_path = resolve_path(family["financial"]["metric_registry"], base_dir=base_dir)
    _, definitions = load_metric_registry(registry_path)
    retained = set(policy["score_construction"]["retained_specialized_metrics"])
    score_definitions = [
        definition
        for definition in definitions
        if not definition.specialized or definition.metric_id in retained
    ]
    panel_path = args.diagnostic_panel.expanduser().resolve()
    dispositions_path = args.dispositions.expanduser().resolve()
    current_path = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "transportation"
        / "dashboard"
        / args.asof
        / "transportation_stage11_survivorship_calibration_panel.csv"
    )
    for path in (panel_path, dispositions_path, current_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else resolve_path(scoring["prebuild_contract_output_dir"], base_dir=base_dir)
    )
    manifest_path = output_dir / "transportation_surface_freight_prebuild_manifest.json"
    coverage_path = output_dir / "transportation_surface_freight_metric_coverage.csv"
    positioning_path = output_dir / "transportation_surface_freight_positioning_feasibility.csv"
    candidates_path = output_dir / "transportation_surface_freight_candidate_registry.json"
    parity_path = output_dir / "transportation_surface_freight_current_score_parity.csv"
    if not args.allow_overwrite and any(
        path.exists() for path in (manifest_path, coverage_path, positioning_path, candidates_path, parity_path)
    ):
        raise FileExistsError("prebuild contract already exists; use --allow-overwrite")

    raw_panel = [
        row
        for row in read_rows(panel_path)
        if row.get("horizon_sessions") == "63"
    ]
    historical_scored = score_surface_metric_percentiles(
        raw_panel,
        definitions=score_definitions,
        policy=policy,
    )
    for row in historical_scored:
        components, _ = build_surface_component_scores(row, policy=policy)
        row.update(components)
    current_raw = read_rows(current_path)
    current_scored = score_surface_metric_percentiles(
        current_raw,
        definitions=score_definitions,
        policy=policy,
    )
    parity_rows: list[dict[str, str]] = []
    for row in current_scored:
        components, coverage = build_surface_component_scores(row, policy=policy)
        parity_rows.append(
            {
                "ticker": str(row.get("ticker") or ""),
                "rank_ready_flag": str(row.get("rank_ready_flag") or ""),
                **{field: f"{value:.8f}" for field, value in components.items()},
                "fixed_metric_slot_count": str(sum(item["applicable"] for item in coverage.values())),
                "observed_metric_slot_count": str(sum(item["observed"] for item in coverage.values())),
                "engine_entrypoint": "shared_production_research",
            }
        )
    write_csv_atomic(parity_path, list(parity_rows[0]), parity_rows)
    coverage_scope = [
        row
        for row in historical_scored
        if str(row.get("rank_ready_flag") or "") == "1"
    ]
    coverage_rows = metric_coverage_rows(coverage_scope, score_definitions)
    write_csv_atomic(coverage_path, list(coverage_rows[0]), coverage_rows)

    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    connection = read_only(db_path)
    try:
        positioning, positioning_rows = positioning_feasibility(
            connection,
            panel_rows=historical_scored,
            policy=policy,
        )
    finally:
        connection.close()
    write_csv_atomic(positioning_path, list(positioning_rows[0]), positioning_rows)
    positioning_enabled = positioning["acceptance"] == "PASS"
    candidates = candidate_registry_from_policy(
        policy,
        positioning_enabled=positioning_enabled,
    )
    candidate_payload = {
        "artifact_family": "transportation_surface_freight_candidate_registry_v2",
        "policy_version": policy["policy_version"],
        "candidate_count": len(candidates),
        "positioning_candidate_enabled": positioning_enabled,
        "candidates": candidates,
        "candidate_design_uses_outcomes": False,
        "selection_evidence": "future_untouched_validation_only",
    }
    write_text_atomic(candidates_path, json.dumps(candidate_payload, indent=2, sort_keys=True) + "\n")

    frozen_dispositions = read_dispositions(dispositions_path)
    configured_dispositions = {
        str(key): str(value)
        for key, value in policy["specialized_metric_dispositions"].items()
    }
    disposition_sources = {
        str(metric_id): [str(item) for item in source_ids]
        for metric_id, source_ids in (
            policy.get("specialized_disposition_sources") or {}
        ).items()
    }
    disposition_issues: list[str] = []
    for metric_id, expected in configured_dispositions.items():
        source_ids = disposition_sources.get(metric_id, [metric_id])
        observed = {
            source_id: frozen_dispositions.get(source_id) for source_id in source_ids
        }
        if any(value != expected for value in observed.values()):
            disposition_issues.append(f"{metric_id}:{observed}!={expected}")
    eligible = sorted(str(item).upper() for item in policy["eligible_tickers"])
    current_by_ticker = {str(row.get("ticker") or "").upper(): row for row in current_scored}
    current_missing = sorted(set(eligible) - set(current_by_ticker))
    current_not_ready = sorted(
        ticker
        for ticker in eligible
        if str(current_by_ticker.get(ticker, {}).get("rank_ready_flag") or "") != "1"
    )
    required_coverage_failures = [
        row["metric_id"]
        for row in coverage_rows
        if row["required_for_rank"] == "1"
        and (finite_float(row["observed_coverage"]) or 0.0) < 0.90
    ]
    issues = [
        *(["current_eligible_tickers_missing=" + ",".join(current_missing)] if current_missing else []),
        *(["current_eligible_tickers_not_rank_ready=" + ",".join(current_not_ready)] if current_not_ready else []),
        *(["specialized_disposition_mismatch=" + ",".join(disposition_issues)] if disposition_issues else []),
        *(["required_metric_history_below_90pct=" + ",".join(required_coverage_failures)] if required_coverage_failures else []),
        *(["fewer_than_two_enabled_candidates"] if len(candidates) < 2 else []),
    ]
    acceptance = "PASS" if not issues else "FAIL"
    source_files = {
        "score_engine": PROJECT_ROOT / "industrials/transportation/surface_freight_score_engine.py",
        "production_scoring": PROJECT_ROOT / "industrials/transportation/scoring.py",
        "research_adapter": PROJECT_ROOT / "industrials/transportation/surface_freight_research.py",
        "scoring_builder": PROJECT_ROOT / "industrials/transportation/scripts/06a_build_transportation_scoring_features.py",
        "history_builder": PROJECT_ROOT / "industrials/transportation/scripts/25_build_transportation_daily_score_history.py",
        "panel_builder": PROJECT_ROOT / "industrials/transportation/scripts/26_build_transportation_generic_oos_panel.py",
        "calibration_preflight": PROJECT_ROOT / "industrials/transportation/scripts/26aa_audit_transportation_calibration_inputs.py",
        "calibration_runner": PROJECT_ROOT / "industrials/transportation/scripts/26b_run_transportation_generic_oos_calibration.py",
        "prebuild_freezer": Path(__file__).resolve(),
        "prebuild_validator": PROJECT_ROOT / "industrials/transportation/prebuild_contract.py",
    }
    result = {
        "artifact_family": "transportation_surface_freight_prebuild_contract_v2",
        "acceptance": acceptance,
        "issues": issues,
        "asof_date": args.asof,
        "policy_version": policy["policy_version"],
        "score_engine_version": policy["score_engine_version"],
        "eligible_ticker_count": len(eligible),
        "eligible_tickers": eligible,
        "current_rank_ready_count": len(eligible) - len(current_not_ready),
        "historical_diagnostic_row_count": len(historical_scored),
        "historical_rank_ready_coverage_row_count": len(coverage_scope),
        "historical_diagnostic_posture": "coverage_and_feasibility_only_no_outcomes_used",
        "required_metric_history_failures": required_coverage_failures,
        "retained_specialized_metric_ids": sorted(retained),
        "non_scoring_specialized_metric_ids": sorted(set(configured_dispositions) - retained),
        "specialized_dispositions_match_frozen_dp6x": not disposition_issues,
        "positioning_history": positioning,
        "positioning_candidate_enabled": positioning_enabled,
        "enabled_candidate_registry": candidates,
        "broad_parser_rerun_authorized": False,
        "full_historical_rebuild_authorized": acceptance == "PASS",
        "calibration_or_promotion_authorized": False,
        "evidence_governance": policy["governance"],
        "next_sequence": [
            "one_point_in_time_financial_and_positioning_history_rebuild",
            "one_shared_engine_score_history_rebuild",
            "freeze_and_independently_validate_new_price_linked_panel",
            "run_only_the_enabled_pre_registered_candidates",
            "promote_only_if_untouched_validation_holdout_and_walk_forward_gates_pass",
        ],
        "source_artifacts": {
            key: artifact(path) for key, path in source_files.items()
        },
        "input_artifacts": {
            "config": artifact(config_path),
            "score_policy": artifact(policy_path),
            "metric_registry": artifact(registry_path),
            "frozen_metric_dispositions": artifact(dispositions_path),
            "diagnostic_panel": artifact(panel_path),
            "current_sidecar": artifact(current_path),
        },
        "output_artifacts": {
            "metric_coverage": artifact(coverage_path),
            "positioning_feasibility": artifact(positioning_path),
            "candidate_registry": artifact(candidates_path),
            "current_score_parity": artifact(parity_path),
        },
    }
    write_text_atomic(manifest_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
