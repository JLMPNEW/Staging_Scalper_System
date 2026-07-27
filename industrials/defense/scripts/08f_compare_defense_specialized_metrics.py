#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT.parent / "config.yaml"
ADAPTER = "industrials.defense.dedicated_parser_adapter:extract_metric_evidence"
OUTPUT_FIELDS = [
    "ticker",
    "company_name",
    "cik",
    "calibration_cohort",
    "membership_status",
    "membership_start_date",
    "membership_end_date",
    "asof_date",
    "run_id",
    "metric_name",
    "baseline_status",
    "baseline_value",
    "baseline_covered_flag",
    "shadow_predicted_status",
    "shadow_value",
    "shadow_period_end",
    "shadow_covered_flag",
    "coverage_delta",
    "recovery_class",
    "current_match_mode",
    "current_evidence_period_end",
    "current_evidence_age_days",
    "accepted_current_count",
    "accepted_historical_count",
    "review_required_count",
    "rejected_count",
    "parser_failure_count",
    "searched_filing_count",
    "searched_document_count",
    "failed_filing_count",
    "missing_cache_filing_count",
    "status_reason",
]
_COVERED_BASELINE = frozenset({"REPORTED", "PROXY"})
_COVERED_SHADOW = frozenset({"REPORTED", "PROXY", "REPORTED_SHADOW"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a complete before/after specialized-metric comparison for "
            "every current and historical defense ticker."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument(
        "--expected-adapter-version",
        default="",
        help=(
            "Explicit adapter version for a sealed historical run. Defaults to the currently loaded defense adapter."
        ),
    )
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-json", type=Path, default=None)
    parser.add_argument(
        "--rank-csv",
        type=Path,
        default=None,
        help="Optional production rank file whose hash is recorded as a shadow-isolation control.",
    )
    return parser.parse_args(argv)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _latest_run(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    adapter_version: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT *
        FROM sec_parser_run
        WHERE model_family = 'defense'
          AND asof_date = ?
          AND adapter_version = ?
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (asof_date, adapter_version),
    ).fetchone()
    if row is None:
        raise ValueError(f"No defense dedicated-parser run exists for asof={asof_date} adapter={adapter_version}")
    return row


def _load_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    asof_date: str,
    adapter_version: str,
) -> sqlite3.Row:
    row = (
        conn.execute(
            "SELECT * FROM sec_parser_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run_id
        else _latest_run(
            conn,
            asof_date=asof_date,
            adapter_version=adapter_version,
        )
    )
    if row is None:
        raise ValueError(f"Unknown dedicated-parser run_id={run_id}")
    if (
        str(row["model_family"]) != "defense"
        or str(row["asof_date"]) != asof_date
        or str(row["adapter_version"]) != adapter_version
    ):
        raise ValueError("Parser run does not match the requested defense/asof/adapter contract")
    if str(row["status"]) != "COMPLETED" or int(row["failed_work_count"] or 0):
        raise ValueError(f"Parser run {row['run_id']} is not a fully completed run")
    return row


def _universe_rows(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.ticker,
               COALESCE(c.company_name, '') AS company_name,
               COALESCE(c.cik, '') AS cik,
               COALESCE(t.calibration_cohort, '') AS calibration_cohort,
               MIN(m.start_date) AS membership_start_date,
               MAX(m.end_date) AS membership_end_date,
               MAX(
                   CASE
                       WHEN m.start_date <= ?
                        AND COALESCE(m.end_date, '9999-12-31') >= ?
                       THEN 1 ELSE 0
                   END
               ) AS current_member_flag
        FROM dim_universe_membership AS m
        LEFT JOIN dim_company AS c
          ON c.ticker = m.ticker
        LEFT JOIN dim_industrials_taxonomy AS t
          ON t.ticker = m.ticker
         AND t.model_family = m.model_family
        WHERE m.model_family = 'defense'
          AND m.start_date <= ?
        GROUP BY m.ticker, c.company_name, c.cik, t.calibration_cohort
        ORDER BY m.ticker
        """,
        (asof_date, asof_date, asof_date),
    ).fetchall()
    return [
        {
            **dict(row),
            "membership_status": ("active" if int(row["current_member_flag"] or 0) else "historical"),
        }
        for row in rows
    ]


def _latest_shadow_values(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    asof_date: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    rows = conn.execute(
        """
        SELECT e.ticker, e.metric_name, e.candidate_value, e.period_end,
               e.confidence, e.evidence_key
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS e
          ON e.evidence_key = relation.evidence_key
        WHERE relation.run_id = ?
          AND e.model_family = 'defense'
          AND e.candidate_status = 'ACCEPTED'
          AND SUBSTR(
                COALESCE(NULLIF(e.accepted_at, ''), e.filing_date),
                1,
                10
              ) <= ?
          AND e.period_end <= ?
        ORDER BY e.ticker, e.metric_name, e.period_end DESC,
                 e.confidence DESC, e.evidence_key
        """,
        (run_id, asof_date, asof_date),
    )
    for row in rows:
        key = (str(row["ticker"]), str(row["metric_name"]))
        output.setdefault(key, dict(row))
    return output


def build_comparison(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    asof_date: str,
    metric_names: tuple[str, ...],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    universe = _universe_rows(conn, asof_date=asof_date)
    assessment_rows = {
        (str(row["ticker"]), str(row["metric_name"])): dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM sec_parser_recovery_assessment
            WHERE run_id = ? AND model_family = 'defense'
            """,
            (run_id,),
        )
    }
    shadow_values = _latest_shadow_values(
        conn,
        run_id=run_id,
        asof_date=asof_date,
    )
    output: list[dict[str, Any]] = []
    missing_pairs: list[str] = []
    for company in universe:
        ticker = str(company["ticker"])
        for metric_name in metric_names:
            assessment = assessment_rows.get((ticker, metric_name))
            if assessment is None:
                missing_pairs.append(f"{ticker}:{metric_name}")
                assessment = {
                    "baseline_status": "NOT_EVALUATED",
                    "baseline_value": None,
                    "predicted_status": "NOT_EVALUATED",
                    "recovery_class": "NOT_EVALUATED",
                    "current_match_mode": "none",
                    "current_evidence_period_end": "",
                    "current_evidence_age_days": None,
                    "accepted_current_count": 0,
                    "accepted_historical_count": 0,
                    "review_required_count": 0,
                    "rejected_count": 0,
                    "parser_failure_count": 0,
                    "searched_filing_count": 0,
                    "searched_document_count": 0,
                    "failed_filing_count": 0,
                    "missing_cache_filing_count": 0,
                    "status_reason": "assessment_row_missing",
                }
            baseline_status = str(assessment["baseline_status"])
            predicted_status = str(assessment["predicted_status"])
            baseline_covered = int(baseline_status in _COVERED_BASELINE)
            shadow_covered = int(predicted_status in _COVERED_SHADOW)
            shadow = shadow_values.get((ticker, metric_name), {})
            output.append(
                {
                    "ticker": ticker,
                    "company_name": company["company_name"],
                    "cik": company["cik"],
                    "calibration_cohort": company["calibration_cohort"],
                    "membership_status": company["membership_status"],
                    "membership_start_date": company["membership_start_date"],
                    "membership_end_date": company["membership_end_date"],
                    "asof_date": asof_date,
                    "run_id": run_id,
                    "metric_name": metric_name,
                    "baseline_status": baseline_status,
                    "baseline_value": assessment["baseline_value"],
                    "baseline_covered_flag": baseline_covered,
                    "shadow_predicted_status": predicted_status,
                    "shadow_value": shadow.get("candidate_value"),
                    "shadow_period_end": shadow.get("period_end"),
                    "shadow_covered_flag": shadow_covered,
                    "coverage_delta": shadow_covered - baseline_covered,
                    "recovery_class": assessment["recovery_class"],
                    "current_match_mode": assessment["current_match_mode"],
                    "current_evidence_period_end": assessment["current_evidence_period_end"],
                    "current_evidence_age_days": assessment["current_evidence_age_days"],
                    "accepted_current_count": assessment["accepted_current_count"],
                    "accepted_historical_count": assessment["accepted_historical_count"],
                    "review_required_count": assessment["review_required_count"],
                    "rejected_count": assessment["rejected_count"],
                    "parser_failure_count": assessment["parser_failure_count"],
                    "searched_filing_count": assessment["searched_filing_count"],
                    "searched_document_count": assessment["searched_document_count"],
                    "failed_filing_count": assessment["failed_filing_count"],
                    "missing_cache_filing_count": assessment["missing_cache_filing_count"],
                    "status_reason": assessment["status_reason"],
                }
            )
    expected_rows = len(universe) * len(metric_names)
    active_count = sum(row["membership_status"] == "active" for row in universe)
    metric_coverage: dict[str, dict[str, Any]] = {}
    for metric_name in metric_names:
        metric_rows = [row for row in output if row["metric_name"] == metric_name]
        baseline_count = sum(int(row["baseline_covered_flag"]) for row in metric_rows)
        shadow_count = sum(int(row["shadow_covered_flag"]) for row in metric_rows)
        metric_coverage[metric_name] = {
            "denominator": len(metric_rows),
            "denominator_type": "full_universe_raw",
            "baseline_covered": baseline_count,
            "shadow_covered": shadow_count,
            "net_coverage_delta": shadow_count - baseline_count,
            "active_shadow_covered": sum(
                int(row["shadow_covered_flag"]) for row in metric_rows if row["membership_status"] == "active"
            ),
            "historical_shadow_covered": sum(
                int(row["shadow_covered_flag"]) for row in metric_rows if row["membership_status"] == "historical"
            ),
            "shadow_covered_match_mode_counts": dict(
                sorted(
                    Counter(
                        str(row["current_match_mode"]) for row in metric_rows if int(row["shadow_covered_flag"])
                    ).items()
                )
            ),
        }
    summary = {
        "run_id": run_id,
        "asof_date": asof_date,
        "model_family": "defense",
        "ticker_count": len(universe),
        "active_ticker_count": active_count,
        "historical_ticker_count": len(universe) - active_count,
        "metric_count": len(metric_names),
        "expected_comparison_rows": expected_rows,
        "comparison_rows": len(output),
        "missing_assessment_pair_count": len(missing_pairs),
        "missing_assessment_pairs": missing_pairs[:100],
        "baseline_covered_count": sum(int(row["baseline_covered_flag"]) for row in output),
        "shadow_covered_count": sum(int(row["shadow_covered_flag"]) for row in output),
        "net_coverage_delta": sum(int(row["coverage_delta"]) for row in output),
        "metric_coverage": metric_coverage,
        "recovery_class_counts": dict(sorted(Counter(row["recovery_class"] for row in output).items())),
        "acceptance": ("PASS" if len(output) == expected_rows and not missing_pairs else "FAIL"),
    }
    return output, summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    output_root = resolve_path(
        cfg_get(
            config,
            "dedicated_parser.output_root",
            "../output/industrials/defense/dedicated_parser",
        ),
        base_dir=config_path.parent,
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv is not None
        else output_root / args.asof / "defense_specialized_metrics_before_after.csv"
    )
    summary_json = (
        args.summary_json.expanduser().resolve()
        if args.summary_json is not None
        else output_root / args.asof / "defense_specialized_metrics_before_after_summary.json"
    )
    registry = load_registry(ADAPTER)
    expected_adapter_version = args.expected_adapter_version.strip() or registry.adapter_version
    with connect_database(db_path) as conn:
        run = _load_run(
            conn,
            run_id=args.run_id,
            asof_date=args.asof,
            adapter_version=expected_adapter_version,
        )
        rows, summary = build_comparison(
            conn,
            run_id=int(run["run_id"]),
            asof_date=args.asof,
            metric_names=tuple(request.metric_name for request in registry.source_metrics),
        )
        metadata = json.loads(str(run["metadata_json"] or "{}"))
        plan = metadata.get("plan") if isinstance(metadata, dict) else {}
        if not isinstance(plan, dict):
            plan = {}
        total_filing_work_items = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM sec_parser_run_work
                WHERE run_id = ?
                """,
                (int(run["run_id"]),),
            ).fetchone()[0]
        )
    summary["work_units"] = {
        "parser_run_id": int(run["run_id"]),
        "newly_executed_filing_work_items": int(run["completed_work_count"] or 0),
        "reused_completed_filing_work_items": int(plan.get("linked_completed_work_count") or 0),
        "total_filing_work_items": total_filing_work_items,
        "documents_for_new_work_items": int(plan.get("scheduled_documents") or 0),
    }
    summary["adapter_version"] = str(run["adapter_version"])
    rank_path = (
        args.rank_csv.expanduser().resolve()
        if args.rank_csv is not None
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "dashboard"
        / args.asof
        / "defense_final_rank_table.csv"
    )
    summary["production_rank_csv"] = str(rank_path)
    summary["production_rank_sha256"] = _sha256(rank_path)
    summary["shadow_only"] = True
    write_csv_atomic(output_csv, OUTPUT_FIELDS, rows)
    summary["comparison_csv"] = str(output_csv)
    summary["comparison_csv_sha256"] = _sha256(output_csv)
    _write_json(summary_json, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
