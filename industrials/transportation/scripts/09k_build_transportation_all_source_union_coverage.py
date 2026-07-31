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
from industrials.transportation.repair_coverage import (  # noqa: E402
    repaired_document_keys,
    suppress_repaired_failure_counts,
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
            "Build parse-free transportation coverage from the reviewed SEC "
            "union plus the one-shot non-SEC direct-document evidence run."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--base-evaluation-id", type=int, default=1)
    parser.add_argument("--delta-evaluation-id", type=int, default=None)
    parser.add_argument("--repair-evaluation-id", type=int, default=None)
    parser.add_argument("--direct-evaluation-id", type=int, default=None)
    parser.add_argument(
        "--comparison-coverage-manifest",
        default="transportation_all_source_union_coverage_manifest.json",
        help=(
            "Passing pre-fixture coverage manifest used to report the "
            "policy-only coverage change."
        ),
    )
    parser.add_argument(
        "--artifact-prefix",
        default="transportation_all_source_union",
        help=("Output filename prefix. Use a versioned prefix to preserve the immutable pre-policy coverage."),
    )
    parser.add_argument(
        "--additional-execution-gate",
        action="append",
        type=Path,
        default=[],
        help=(
            "Optional sealed supplemental parser execution gate. May be "
            "repeated. Relative paths resolve in the configured output "
            "directory; runs are merged in the supplied order."
        ),
    )
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
    artifact_prefix = str(args.artifact_prefix).strip()
    if not artifact_prefix or artifact_prefix in {".", ".."} or "/" in artifact_prefix or "\\" in artifact_prefix:
        raise ValueError("--artifact-prefix must be a filename prefix")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError("All-source coverage requires the general parser switch off")
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    output_dir = resolve_path(parser_cfg["output_root"], base_dir=base_dir) / str(parser_cfg["source_census_asof_date"])
    prior_union_path = output_dir / ("transportation_repaired_sec_union_coverage_manifest.json")
    delta_gate_path = output_dir / "transportation_sec_delta_execution_gate.json"
    repair_manifest_path = output_dir / "transportation_parser_repair_manifest.json"
    repair_gate_path = output_dir / "transportation_parser_repair_execution_gate.json"
    repair_source_path = output_dir / "transportation_parser_repair_source_manifest.csv"
    direct_gate_path = output_dir / ("transportation_non_sec_direct_delta_execution_gate.json")
    direct_source_path = output_dir / ("transportation_non_sec_direct_delta_source_manifest.csv")
    comparison_manifest_path = (
        output_dir / str(args.comparison_coverage_manifest)
    )
    additional_gate_paths = [
        (
            path
            if path.is_absolute()
            else output_dir / path
        ).expanduser().resolve()
        for path in args.additional_execution_gate
    ]
    for path in (
        prior_union_path,
        delta_gate_path,
        repair_manifest_path,
        repair_gate_path,
        repair_source_path,
        direct_gate_path,
        direct_source_path,
        comparison_manifest_path,
        *additional_gate_paths,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    prior_union = _read_json(prior_union_path)
    delta_gate = _read_json(delta_gate_path)
    repair_manifest = _read_json(repair_manifest_path)
    repair_gate = _read_json(repair_gate_path)
    direct_gate = _read_json(direct_gate_path)
    comparison_manifest = _read_json(comparison_manifest_path)
    additional_gates = [
        _read_json(path) for path in additional_gate_paths
    ]
    if (
        prior_union.get("acceptance") != "PASS"
        or delta_gate.get("acceptance") != "PASS"
        or repair_manifest.get("acceptance") != "PASS"
        or repair_gate.get("acceptance") != "PASS"
        or str(repair_gate.get("source_manifest_sha256") or "") != file_sha256(repair_source_path)
        or str((repair_manifest.get("artifact") or {}).get("sha256") or "") != file_sha256(repair_source_path)
        or direct_gate.get("acceptance") != "PASS"
        or str(direct_gate.get("source_manifest_sha256") or "") != file_sha256(direct_source_path)
        or comparison_manifest.get("acceptance") != "PASS"
    ):
        raise ValueError("All-source union inputs are not sealed and passing")
    for gate, gate_path in zip(
        additional_gates,
        additional_gate_paths,
        strict=True,
    ):
        source_path = Path(
            str(gate.get("source_manifest_path") or "")
        )
        if (
            gate.get("acceptance") != "PASS"
            or int(gate.get("parser_invocations") or 0) != 1
            or int(
                gate.get("physical_document_reextraction_count") or 0
            )
            != 0
            or not source_path.is_file()
            or str(gate.get("source_manifest_sha256") or "")
            != file_sha256(source_path)
        ):
            raise ValueError(
                f"Supplemental execution gate is not sealed: {gate_path}"
            )
    base_run_id = int(prior_union.get("base_run_id") or 0)
    delta_run_id = int(delta_gate.get("run_id") or 0)
    repair_run_id = int(repair_gate.get("run_id") or 0)
    direct_run_id = int(direct_gate.get("run_id") or 0)
    supplemental_run_ids = [
        int(gate.get("run_id") or 0) for gate in additional_gates
    ]
    scope_path = resolve_path(
        parser_cfg["scope_manifest_csv"],
        base_dir=base_dir,
    )
    support_scope_path = resolve_path(
        parser_cfg["supporting_scope_manifest_csv"],
        base_dir=base_dir,
    )
    repair_rows = read_csv(repair_source_path)
    repaired_keys = repaired_document_keys(repair_rows)
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        base_evidence = load_review_evidence_stats(
            connection,
            evaluation_id=args.base_evaluation_id,
        )
        evaluation_ids = {
            "base": args.base_evaluation_id,
            "delta": args.delta_evaluation_id,
            "repair": args.repair_evaluation_id,
            "direct": args.direct_evaluation_id,
        }
        for label, evaluation_id, expected_run_id in (
            ("base", args.base_evaluation_id, base_run_id),
            ("delta", args.delta_evaluation_id, delta_run_id),
            ("repair", args.repair_evaluation_id, repair_run_id),
            ("direct", args.direct_evaluation_id, direct_run_id),
        ):
            if evaluation_id is None:
                continue
            evaluation = connection.execute(
                """
                SELECT base_run_id, status, model_family
                FROM sec_parser_review_evaluation
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
            if (
                evaluation is None
                or int(evaluation["base_run_id"]) != expected_run_id
                or str(evaluation["status"]) != "COMPLETED"
                or str(evaluation["model_family"]) != MODEL_FAMILY
            ):
                raise ValueError(
                    f"{label} review evaluation does not match run "
                    f"{expected_run_id}"
                )
        raw_delta_evidence = (
            load_review_evidence_stats(
                connection,
                evaluation_id=args.delta_evaluation_id,
            )
            if args.delta_evaluation_id is not None
            else load_evidence_stats(connection, run_id=delta_run_id)
        )
        repair_evidence = (
            load_review_evidence_stats(
                connection,
                evaluation_id=args.repair_evaluation_id,
            )
            if args.repair_evaluation_id is not None
            else load_evidence_stats(connection, run_id=repair_run_id)
        )
        direct_evidence = (
            load_review_evidence_stats(
                connection,
                evaluation_id=args.direct_evaluation_id,
            )
            if args.direct_evaluation_id is not None
            else load_evidence_stats(connection, run_id=direct_run_id)
        )
        supplemental_evidence = [
            load_evidence_stats(connection, run_id=run_id)
            for run_id in supplemental_run_ids
        ]
        direct_evidence_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM sec_parser_run_metric_evidence
                WHERE run_id=?
                """,
                (direct_run_id,),
            ).fetchone()[0]
        )
        failure_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT evidence.ticker, evidence.accession_number,
                       evidence.metric_name, evidence.source_document,
                       evidence.candidate_status
                FROM sec_parser_run_metric_evidence AS relation
                JOIN sec_parser_metric_evidence_shadow AS evidence
                  ON evidence.evidence_key=relation.evidence_key
                WHERE relation.run_id=?
                  AND evidence.model_family=?
                  AND evidence.candidate_status='PARSER_FAILURE'
                """,
                (delta_run_id, MODEL_FAMILY),
            )
        ]
        repair_failure_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM sec_parser_run_metric_evidence AS relation
                JOIN sec_parser_metric_evidence_shadow AS evidence
                  ON evidence.evidence_key=relation.evidence_key
                WHERE relation.run_id=?
                  AND evidence.model_family=?
                  AND evidence.candidate_status='PARSER_FAILURE'
                """,
                (repair_run_id, MODEL_FAMILY),
            ).fetchone()[0]
        )
        repair_evidence_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM sec_parser_run_metric_evidence
                WHERE run_id=?
                """,
                (repair_run_id,),
            ).fetchone()[0]
        )
        base_work = load_work_stats(connection, run_id=base_run_id)
        delta_work = load_work_stats(connection, run_id=delta_run_id)
        repair_work = load_work_stats(
            connection,
            run_id=repair_run_id,
        )
        direct_work = load_work_stats(
            connection,
            run_id=direct_run_id,
        )
        supplemental_work = [
            load_work_stats(connection, run_id=run_id)
            for run_id in supplemental_run_ids
        ]
        base_run = load_run(connection, run_id=base_run_id)
        delta_run = load_run(connection, run_id=delta_run_id)
        repair_run = load_run(connection, run_id=repair_run_id)
        direct_run = load_run(connection, run_id=direct_run_id)
        supplemental_runs = [
            load_run(connection, run_id=run_id)
            for run_id in supplemental_run_ids
        ]
        delta_repair_overlap = int(
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
                (delta_run_id, repair_run_id),
            ).fetchone()[0]
        )
        base_repair_overlap = int(
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
                (base_run_id, repair_run_id),
            ).fetchone()[0]
        )
        prior_direct_overlap = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT ticker, accession_number
                    FROM sec_parser_run_work
                    WHERE run_id IN (?, ?, ?)
                    INTERSECT
                    SELECT ticker, accession_number
                    FROM sec_parser_run_work
                    WHERE run_id=?
                )
                """,
                (
                    base_run_id,
                    delta_run_id,
                    repair_run_id,
                    direct_run_id,
                ),
            ).fetchone()[0]
        )
        financial = load_financial_values(
            connection,
            asof_date=str(direct_run["asof_date"]),
        )
    delta_evidence, suppressed_count, suppression_errors = suppress_repaired_failure_counts(
        evidence=raw_delta_evidence,
        failure_rows=failure_rows,
        repaired_keys=repaired_keys,
    )
    expected_accessions = int(repair_manifest.get("repair_accession_count") or 0)
    expected_failures = int(repair_manifest.get("failure_evidence_count") or 0)
    errors = list(suppression_errors)
    if suppressed_count != expected_failures:
        errors.append("not all sealed PDF failure rows were superseded")
    if repair_failure_count:
        errors.append("repair run still contains parser failures")
    if delta_repair_overlap != expected_accessions:
        errors.append("repair run does not exactly overlap its delta scope")
    if base_repair_overlap:
        errors.append("repair scope unexpectedly overlaps base run")
    if prior_direct_overlap:
        errors.append("direct-document scope overlaps a prior parser run")
    for run in (
        base_run,
        delta_run,
        repair_run,
        direct_run,
        *supplemental_runs,
    ):
        if str(run["status"]) != "COMPLETED" or int(str(run["failed_work_count"])) != 0:
            errors.append(f"run {run['run_id']} is failed or incomplete")
    if errors:
        raise ValueError("Repaired SEC union validation failed: " + "; ".join(errors))
    scope_rows = read_csv(scope_path)
    support_scope_rows = read_csv(support_scope_path)
    baseline_evidence = merge_evidence_stats(
        base_evidence,
        delta_evidence,
        repair_evidence,
    )
    baseline_work = merge_work_stats(
        base_work,
        delta_work,
        repair_work,
    )
    baseline_rows = build_final_coverage(
        run_id=repair_run_id,
        scope_rows=scope_rows,
        evidence=baseline_evidence,
        work=baseline_work,
        financial_values=financial,
    )
    evidence = merge_evidence_stats(
        baseline_evidence,
        direct_evidence,
        *supplemental_evidence,
    )
    work = merge_work_stats(
        baseline_work,
        direct_work,
        *supplemental_work,
    )
    terminal_run = (
        supplemental_runs[-1]
        if supplemental_runs
        else direct_run
    )
    terminal_run_id = int(str(terminal_run["run_id"]))
    final_rows = build_final_coverage(
        run_id=terminal_run_id,
        scope_rows=scope_rows,
        evidence=evidence,
        work=work,
        financial_values=financial,
    )
    support_rows = build_support_coverage(
        run_id=terminal_run_id,
        scope_rows=support_scope_rows,
        evidence=evidence,
        work=work,
    )
    metric_rows = build_metric_summary(final_rows)
    cohort_rows = build_cohort_summary(final_rows)
    base_completed = sum(int(stats["completed"]) for stats in base_work.values())
    delta_completed = sum(int(stats["completed"]) for stats in delta_work.values())
    repair_completed = sum(int(stats["completed"]) for stats in repair_work.values())
    direct_linked = int(str(direct_run.get("linked_completed_work_count") or 0))
    supplemental_prior_completed = (
        int(str(direct_run.get("completed_work_count") or 0))
        + direct_linked
        + sum(
            int(str(run.get("completed_work_count") or 0))
            + int(str(run.get("linked_completed_work_count") or 0))
            for run in supplemental_runs[:-1]
        )
    )
    composite_run = {
        **terminal_run,
        "linked_completed_work_count": (
            base_completed
            + delta_completed
            + repair_completed
            + (
                supplemental_prior_completed
                if supplemental_runs
                else direct_linked
            )
        ),
    }
    prefix = artifact_prefix
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
    declared_prior_counts = {
        str(key): int(value) for key, value in (prior_union.get("coverage_status_counts") or {}).items()
    }
    before_counts = coverage_counts(baseline_rows)
    after_counts = coverage_counts(final_rows)
    before_accepted_count = int(before_counts.get("COVERED_ACCEPTED") or 0) + int(
        before_counts.get("COVERED_FINANCIAL_DERIVED") or 0
    )
    after_accepted_count = int(after_counts.get("COVERED_ACCEPTED") or 0) + int(
        after_counts.get("COVERED_FINANCIAL_DERIVED") or 0
    )
    if after_accepted_count < before_accepted_count:
        raise ValueError(
            "All-source union would demote accepted coverage: "
            f"before={before_accepted_count} after={after_accepted_count}"
        )
    before_rates = coverage_rates_from_counts(before_counts)
    after_rates = coverage_rates_from_counts(after_counts)
    comparison_counts = {
        str(key): int(value)
        for key, value in (
            comparison_manifest.get("coverage_status_counts") or {}
        ).items()
    }
    comparison_rates = coverage_rates_from_counts(comparison_counts)
    payload.update(
        {
            "gate": (
                "DP6ZC_FIXTURE_REVIEWED_ALL_SOURCE_COVERAGE"
                if any(
                    value is not None
                    for value in (
                        args.delta_evaluation_id,
                        args.repair_evaluation_id,
                        args.direct_evaluation_id,
                    )
                )
                else "DP6W_ALL_SOURCE_UNION_COVERAGE_ONLY"
            ),
            "artifact_prefix": artifact_prefix,
            "base_run_id": base_run_id,
            "base_review_evaluation_id": args.base_evaluation_id,
            "review_evaluation_ids": evaluation_ids,
            "delta_run_id": delta_run_id,
            "repair_run_id": repair_run_id,
            "direct_document_run_id": direct_run_id,
            "additional_run_ids": [
                direct_run_id,
                *supplemental_run_ids,
            ],
            "supplemental_run_ids": supplemental_run_ids,
            "supplemental_execution_gate_paths": [
                str(path) for path in additional_gate_paths
            ],
            "delta_repair_accession_overlap_count": (delta_repair_overlap),
            "base_repair_accession_overlap_count": (base_repair_overlap),
            "prior_direct_accession_overlap_count": (prior_direct_overlap),
            "superseded_failure_evidence_count": suppressed_count,
            "repair_evidence_count": repair_evidence_count,
            "repair_parser_failure_count": repair_failure_count,
            "direct_document_evidence_count": direct_evidence_count,
            "source_manifest_path": str(direct_source_path.resolve()),
            "source_manifest_sha256": file_sha256(direct_source_path),
            "before_coverage_status_counts": before_counts,
            "declared_prior_artifact_coverage_status_counts": (declared_prior_counts),
            "baseline_recomputed_from_current_database": True,
            "coverage_status_counts": after_counts,
            "pre_fixture_coverage_status_counts": comparison_counts,
            "before_accepted_coverage_rate": before_rates["accepted"],
            "before_usable_coverage_rate": before_rates["usable"],
            "before_discovery_coverage_rate": before_rates["discovery"],
            "accepted_coverage_rate": after_rates["accepted"],
            "usable_coverage_rate": after_rates["usable"],
            "discovery_coverage_rate": after_rates["discovery"],
            "pre_fixture_accepted_coverage_rate": comparison_rates[
                "accepted"
            ],
            "pre_fixture_usable_coverage_rate": comparison_rates["usable"],
            "pre_fixture_discovery_coverage_rate": comparison_rates[
                "discovery"
            ],
            "fixture_accepted_coverage_rate_change": (
                after_rates["accepted"] - comparison_rates["accepted"]
            ),
            "fixture_usable_coverage_rate_change": (
                after_rates["usable"] - comparison_rates["usable"]
            ),
            "fixture_discovery_coverage_rate_change": (
                after_rates["discovery"] - comparison_rates["discovery"]
            ),
            "accepted_coverage_rate_change": (after_rates["accepted"] - before_rates["accepted"]),
            "usable_coverage_rate_change": (after_rates["usable"] - before_rates["usable"]),
            "discovery_coverage_rate_change": (after_rates["discovery"] - before_rates["discovery"]),
            "parser_invocations": 0,
            "feature_build_invocations": 0,
            "historical_materialization_invocations": 0,
            "calibration_invocations": 0,
            "portfolio_invocations": 0,
            "next_gate": "SYSTEMATIC_ALL_SOURCE_EVIDENCE_ADJUDICATION",
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
