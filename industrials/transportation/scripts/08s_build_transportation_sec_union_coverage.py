#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.parser_coverage import (  # noqa: E402
    build_cohort_summary,
    build_final_coverage,
    build_metric_summary,
    build_support_coverage,
    load_evidence_stats,
    load_financial_values,
    load_review_evidence_stats,
    load_run,
    load_work_stats,
    read_csv,
    read_only_connection,
    write_coverage_artifacts,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.sec_union_coverage import (  # noqa: E402
    coverage_counts,
    coverage_rates_from_counts,
    merge_evidence_stats,
    merge_work_stats,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build coverage-only transportation artifacts from the union of "
            "the reviewed run-58 evidence and the sealed SEC delta run."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--base-evaluation-id", type=int, default=1)
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "SEC union coverage requires general parser execution disabled"
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    execution_gate_path = (
        output_dir / "transportation_sec_delta_execution_gate.json"
    )
    baseline_path = (
        output_dir / "transportation_post_review_coverage_manifest.json"
    )
    source_path = (
        output_dir / "transportation_delta_parser_source_manifest.csv"
    )
    execution_gate = _read_json(execution_gate_path)
    baseline = _read_json(baseline_path)
    if (
        execution_gate.get("acceptance") != "PASS"
        or baseline.get("acceptance") != "PASS"
        or int(baseline.get("evaluation_id") or 0)
        != args.base_evaluation_id
        or str(execution_gate.get("source_manifest_sha256") or "")
        != file_sha256(source_path)
    ):
        raise ValueError(
            "SEC union coverage requires passing sealed base and delta gates"
        )
    delta_run_id = int(execution_gate.get("run_id") or 0)
    base_run_id = int(baseline.get("base_run_id") or 0)
    scope_path = resolve_path(
        parser_cfg["scope_manifest_csv"],
        base_dir=base_dir,
    )
    support_scope_path = resolve_path(
        parser_cfg["supporting_scope_manifest_csv"],
        base_dir=base_dir,
    )
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        base_evidence = load_review_evidence_stats(
            connection,
            evaluation_id=args.base_evaluation_id,
        )
        delta_evidence = load_evidence_stats(
            connection,
            run_id=delta_run_id,
        )
        base_work = load_work_stats(connection, run_id=base_run_id)
        delta_work = load_work_stats(
            connection,
            run_id=delta_run_id,
        )
        delta_run = load_run(connection, run_id=delta_run_id)
        base_run = load_run(connection, run_id=base_run_id)
        overlap_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT ticker, accession_number
                    FROM sec_parser_run_work
                    WHERE run_id=?
                    INTERSECT
                    SELECT ticker, accession_number
                    FROM sec_parser_run_work
                    WHERE run_id=?
                )
                """,
                (base_run_id, delta_run_id),
            ).fetchone()[0]
        )
        base_evidence_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM sec_parser_review_evidence
                WHERE evaluation_id=?
                """,
                (args.base_evaluation_id,),
            ).fetchone()[0]
        )
        delta_evidence_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM sec_parser_run_metric_evidence
                WHERE run_id=?
                """,
                (delta_run_id,),
            ).fetchone()[0]
        )
        financial = load_financial_values(
            connection,
            asof_date=str(delta_run["asof_date"]),
        )
    if (
        overlap_count != 0
        or str(base_run["status"]) != "COMPLETED"
        or str(delta_run["status"]) != "COMPLETED"
        or int(str(base_run["failed_work_count"])) != 0
        or int(str(delta_run["failed_work_count"])) != 0
    ):
        raise ValueError(
            "Base/delta run union is overlapping, failed, or incomplete"
        )
    evidence = merge_evidence_stats(base_evidence, delta_evidence)
    work = merge_work_stats(base_work, delta_work)
    final_rows = build_final_coverage(
        run_id=delta_run_id,
        scope_rows=read_csv(scope_path),
        evidence=evidence,
        work=work,
        financial_values=financial,
    )
    support_rows = build_support_coverage(
        run_id=delta_run_id,
        scope_rows=read_csv(support_scope_path),
        evidence=evidence,
        work=work,
    )
    metric_rows = build_metric_summary(final_rows)
    cohort_rows = build_cohort_summary(final_rows)
    delta_linked = int(
        str(delta_run.get("linked_completed_work_count") or 0)
    )
    base_work_count = sum(
        int(stats["completed"]) for stats in base_work.values()
    )
    composite_run = {
        **delta_run,
        "linked_completed_work_count": base_work_count + delta_linked,
    }
    prefix = "transportation_sec_union"
    manifest_path = output_dir / f"{prefix}_coverage_manifest.json"
    payload = write_coverage_artifacts(
        final_rows=final_rows,
        metric_rows=metric_rows,
        cohort_rows=cohort_rows,
        support_rows=support_rows,
        final_path=output_dir / f"{prefix}_ticker_metric_coverage.csv",
        metric_path=output_dir / f"{prefix}_metric_summary.csv",
        cohort_path=output_dir / f"{prefix}_cohort_metric_coverage.csv",
        support_path=output_dir / f"{prefix}_support_coverage.csv",
        manifest_path=manifest_path,
        run=composite_run,
        scope_path=scope_path,
        support_scope_path=support_scope_path,
    )
    after_counts = coverage_counts(final_rows)
    raw_before_counts = baseline.get("coverage_status_counts") or {}
    if not isinstance(raw_before_counts, dict):
        raise ValueError(
            "Baseline coverage status counts are not a JSON object"
        )
    before_counts = {
        str(key): int(value)
        for key, value in raw_before_counts.items()
    }
    before_rates = coverage_rates_from_counts(before_counts)
    after_rates = coverage_rates_from_counts(after_counts)
    payload.update(
        {
            "gate": "DP6F_SEC_UNION_COVERAGE_ONLY",
            "base_run_id": base_run_id,
            "base_review_evaluation_id": args.base_evaluation_id,
            "delta_run_id": delta_run_id,
            "base_work_count": base_work_count,
            "delta_effective_work_count": int(
                execution_gate.get(
                    "effective_completed_work_count"
                )
                or 0
            ),
            "run_accession_overlap_count": overlap_count,
            "base_review_evidence_count": base_evidence_count,
            "delta_evidence_count": delta_evidence_count,
            "source_manifest_path": str(source_path.resolve()),
            "source_manifest_sha256": file_sha256(source_path),
            "before_coverage_status_counts": before_counts,
            "coverage_status_counts": after_counts,
            "before_accepted_coverage_rate": before_rates["accepted"],
            "before_usable_coverage_rate": before_rates["usable"],
            "before_discovery_coverage_rate": before_rates["discovery"],
            "accepted_coverage_rate": after_rates["accepted"],
            "usable_coverage_rate": after_rates["usable"],
            "discovery_coverage_rate": after_rates["discovery"],
            "accepted_coverage_rate_change": (
                after_rates["accepted"] - before_rates["accepted"]
            ),
            "usable_coverage_rate_change": (
                after_rates["usable"] - before_rates["usable"]
            ),
            "discovery_coverage_rate_change": (
                after_rates["discovery"] - before_rates["discovery"]
            ),
            "parser_invocations": 0,
            "feature_build_invocations": 0,
            "historical_materialization_invocations": 0,
            "calibration_invocations": 0,
            "portfolio_invocations": 0,
            "next_gate": "BUILD_NON_SEC_RESIDUAL_SOURCE_AUDIT",
        }
    )
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
