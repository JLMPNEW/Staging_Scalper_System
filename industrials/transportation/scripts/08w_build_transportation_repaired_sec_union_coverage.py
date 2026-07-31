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
            "Build coverage-only transportation artifacts after replacing "
            "run-59 PDF failure markers with sealed run-60 repair evidence."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--base-evaluation-id", type=int, default=1)
    parser.add_argument(
        "--artifact-prefix",
        default="transportation_repaired_sec_union",
        help=(
            "Output filename prefix. Use a versioned prefix to preserve "
            "the immutable pre-policy coverage."
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
    if (
        not artifact_prefix
        or artifact_prefix in {".", ".."}
        or "/" in artifact_prefix
        or "\\" in artifact_prefix
    ):
        raise ValueError("--artifact-prefix must be a filename prefix")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Repaired coverage requires the general parser switch off"
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    prior_union_path = (
        output_dir / "transportation_sec_union_coverage_manifest.json"
    )
    delta_gate_path = (
        output_dir / "transportation_sec_delta_execution_gate.json"
    )
    repair_manifest_path = (
        output_dir / "transportation_parser_repair_manifest.json"
    )
    repair_gate_path = (
        output_dir
        / "transportation_parser_repair_execution_gate.json"
    )
    repair_source_path = (
        output_dir
        / "transportation_parser_repair_source_manifest.csv"
    )
    for path in (
        prior_union_path,
        delta_gate_path,
        repair_manifest_path,
        repair_gate_path,
        repair_source_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    prior_union = _read_json(prior_union_path)
    delta_gate = _read_json(delta_gate_path)
    repair_manifest = _read_json(repair_manifest_path)
    repair_gate = _read_json(repair_gate_path)
    if (
        prior_union.get("acceptance") != "PASS"
        or delta_gate.get("acceptance") != "PASS"
        or repair_manifest.get("acceptance") != "PASS"
        or repair_gate.get("acceptance") != "PASS"
        or str(repair_gate.get("source_manifest_sha256") or "")
        != file_sha256(repair_source_path)
        or str((repair_manifest.get("artifact") or {}).get("sha256") or "")
        != file_sha256(repair_source_path)
    ):
        raise ValueError("Repaired-union inputs are not sealed and passing")
    base_run_id = int(prior_union.get("base_run_id") or 0)
    delta_run_id = int(delta_gate.get("run_id") or 0)
    repair_run_id = int(repair_gate.get("run_id") or 0)
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
        raw_delta_evidence = load_evidence_stats(
            connection,
            run_id=delta_run_id,
        )
        repair_evidence = load_evidence_stats(
            connection,
            run_id=repair_run_id,
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
        base_run = load_run(connection, run_id=base_run_id)
        delta_run = load_run(connection, run_id=delta_run_id)
        repair_run = load_run(connection, run_id=repair_run_id)
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
        financial = load_financial_values(
            connection,
            asof_date=str(repair_run["asof_date"]),
        )
    delta_evidence, suppressed_count, suppression_errors = (
        suppress_repaired_failure_counts(
            evidence=raw_delta_evidence,
            failure_rows=failure_rows,
            repaired_keys=repaired_keys,
        )
    )
    expected_accessions = int(
        repair_manifest.get("repair_accession_count") or 0
    )
    expected_failures = int(
        repair_manifest.get("failure_evidence_count") or 0
    )
    errors = list(suppression_errors)
    if suppressed_count != expected_failures:
        errors.append("not all sealed PDF failure rows were superseded")
    if repair_failure_count:
        errors.append("repair run still contains parser failures")
    if delta_repair_overlap != expected_accessions:
        errors.append("repair run does not exactly overlap its delta scope")
    if base_repair_overlap:
        errors.append("repair scope unexpectedly overlaps base run")
    for run in (base_run, delta_run, repair_run):
        if (
            str(run["status"]) != "COMPLETED"
            or int(str(run["failed_work_count"])) != 0
        ):
            errors.append(
                f"run {run['run_id']} is failed or incomplete"
            )
    if errors:
        raise ValueError(
            "Repaired SEC union validation failed: "
            + "; ".join(errors)
        )
    evidence = merge_evidence_stats(
        base_evidence,
        delta_evidence,
        repair_evidence,
    )
    work = merge_work_stats(base_work, delta_work, repair_work)
    final_rows = build_final_coverage(
        run_id=repair_run_id,
        scope_rows=read_csv(scope_path),
        evidence=evidence,
        work=work,
        financial_values=financial,
    )
    support_rows = build_support_coverage(
        run_id=repair_run_id,
        scope_rows=read_csv(support_scope_path),
        evidence=evidence,
        work=work,
    )
    metric_rows = build_metric_summary(final_rows)
    cohort_rows = build_cohort_summary(final_rows)
    base_completed = sum(
        int(stats["completed"]) for stats in base_work.values()
    )
    delta_completed = sum(
        int(stats["completed"]) for stats in delta_work.values()
    )
    repair_linked = int(
        str(repair_run.get("linked_completed_work_count") or 0)
    )
    composite_run = {
        **repair_run,
        "linked_completed_work_count": (
            base_completed + delta_completed + repair_linked
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
    before_counts = {
        str(key): int(value)
        for key, value in (
            prior_union.get("coverage_status_counts") or {}
        ).items()
    }
    after_counts = coverage_counts(final_rows)
    before_rates = coverage_rates_from_counts(before_counts)
    after_rates = coverage_rates_from_counts(after_counts)
    payload.update(
        {
            "gate": "DP6H_REPAIRED_SEC_UNION_COVERAGE_ONLY",
            "artifact_prefix": artifact_prefix,
            "base_run_id": base_run_id,
            "base_review_evaluation_id": args.base_evaluation_id,
            "delta_run_id": delta_run_id,
            "repair_run_id": repair_run_id,
            "delta_repair_accession_overlap_count": (
                delta_repair_overlap
            ),
            "base_repair_accession_overlap_count": (
                base_repair_overlap
            ),
            "superseded_failure_evidence_count": suppressed_count,
            "repair_evidence_count": repair_evidence_count,
            "repair_parser_failure_count": repair_failure_count,
            "before_coverage_status_counts": before_counts,
            "coverage_status_counts": after_counts,
            "before_accepted_coverage_rate": before_rates["accepted"],
            "before_usable_coverage_rate": before_rates["usable"],
            "before_discovery_coverage_rate": before_rates[
                "discovery"
            ],
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
            "next_gate": "SYSTEMATIC_UNION_EVIDENCE_ADJUDICATION",
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
