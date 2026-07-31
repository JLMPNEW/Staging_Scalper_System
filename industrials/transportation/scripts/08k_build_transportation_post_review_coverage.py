#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.golden import (  # noqa: E402
    load_corpus,
    validate_corpus,
)
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.coverage_lift import (  # noqa: E402
    build_metric_gate_rows,
)
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    get_registry,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    COHORT_SUMMARY_FIELDS,
    FINAL_COVERAGE_FIELDS,
    METRIC_SUMMARY_FIELDS,
    SUPPORT_COVERAGE_FIELDS,
    accepted_periods_for_final_metric,
    build_cohort_summary,
    build_final_coverage,
    build_metric_summary,
    build_support_coverage,
    load_financial_values,
    load_review_evidence_stats,
    load_run,
    load_work_stats,
    read_csv,
    read_only_connection,
)
from industrials.transportation.post_review import (  # noqa: E402
    POST_REVIEW_METRIC_FIELDS,
    build_post_review_metric_rows,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build formal post-adjudication transportation coverage from a "
            "completed policy-only review evaluation."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--evaluation-id", type=int, required=True)
    return parser.parse_args(argv)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def _artifact(path: Path, rows: int) -> dict[str, object]:
    return {
        "path": str(path),
        "row_count": rows,
        "sha256": file_sha256(path),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Post-review coverage requires parser execution disabled"
        )
    foundation = resolve_foundation(config_path, args.db)
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    scope_path = resolve_path(
        parser_cfg["scope_manifest_csv"],
        base_dir=base_dir,
    )
    support_scope_path = resolve_path(
        parser_cfg["supporting_scope_manifest_csv"],
        base_dir=base_dir,
    )
    pre_gate_path = (
        output_dir / "transportation_coverage_lift_metric_gate.csv"
    )
    adjudication_path = (
        output_dir / "transportation_evidence_adjudication.csv"
    )
    adjudication_manifest_path = (
        output_dir / "transportation_evidence_adjudication_manifest.json"
    )
    replay_manifest_path = (
        output_dir / "transportation_policy_replay_manifest.json"
    )
    adapter_registry = get_registry()
    registry_path = Path(
        adapter_registry.review_policy_path
    ).expanduser().resolve()
    golden_path = Path(
        adapter_registry.review_policy_golden_path
    ).expanduser().resolve()
    for path in (
        scope_path,
        support_scope_path,
        pre_gate_path,
        adjudication_path,
        adjudication_manifest_path,
        replay_manifest_path,
        registry_path,
        golden_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    adjudication_manifest = json.loads(
        adjudication_manifest_path.read_text(encoding="utf-8")
    )
    replay_manifest = json.loads(
        replay_manifest_path.read_text(encoding="utf-8")
    )
    if (
        adjudication_manifest.get("acceptance") != "PASS"
        or adjudication_manifest.get("applied") is not True
        or replay_manifest.get("status") != "COMPLETED"
        or int(replay_manifest.get("evaluation_id") or 0)
        != args.evaluation_id
        or str(replay_manifest.get("policy_sha256") or "")
        != file_sha256(registry_path)
    ):
        raise ValueError(
            "Post-review coverage inputs are not a sealed applied replay"
        )
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        evaluation = connection.execute(
            """
            SELECT *
            FROM sec_parser_review_evaluation
            WHERE evaluation_id=?
            """,
            (args.evaluation_id,),
        ).fetchone()
        if (
            evaluation is None
            or str(evaluation["status"]) != "COMPLETED"
            or str(evaluation["model_family"]) != MODEL_FAMILY
            or str(evaluation["policy_sha256"])
            != file_sha256(registry_path)
        ):
            raise ValueError("Review evaluation contract mismatch")
        run_id = int(evaluation["base_run_id"])
        run = load_run(connection, run_id=run_id)
        evidence = load_review_evidence_stats(
            connection,
            evaluation_id=args.evaluation_id,
        )
        golden_errors = validate_corpus(
            connection,
            corpus_path=golden_path,
            table="sec_parser_review_evidence",
            evaluation_id=args.evaluation_id,
        )
        if golden_errors:
            raise ValueError(
                "Policy-generated golden validation failed: "
                + "; ".join(golden_errors[:10])
            )
        work = load_work_stats(connection, run_id=run_id)
        financial = load_financial_values(
            connection,
            asof_date=str(run["asof_date"]),
        )
    final_rows = build_final_coverage(
        run_id=run_id,
        scope_rows=read_csv(scope_path),
        evidence=evidence,
        work=work,
        financial_values=financial,
    )
    support_rows = build_support_coverage(
        run_id=run_id,
        scope_rows=read_csv(support_scope_path),
        evidence=evidence,
        work=work,
    )
    metric_rows = build_metric_summary(final_rows)
    cohort_rows = build_cohort_summary(final_rows)
    post_gate_rows = build_metric_gate_rows(final_rows)
    accepted_periods = {
        (str(row["ticker"]), str(row["metric_id"])): (
            accepted_periods_for_final_metric(
                ticker=str(row["ticker"]),
                metric_id=str(row["metric_id"]),
                source_lane=str(row["source_lane"]),
                evidence=evidence,
            )
        )
        for row in final_rows
        if str(row["applicability_status"]) == "APPLICABLE"
    }
    formal_rows = build_post_review_metric_rows(
        run_id=run_id,
        evaluation_id=args.evaluation_id,
        pre_gate_rows=_read_csv(pre_gate_path),
        post_gate_rows=post_gate_rows,
        post_coverage_rows=final_rows,
        adjudication_rows=_read_csv(adjudication_path),
        accepted_periods=accepted_periods,
    )
    final_path = (
        output_dir
        / "transportation_post_review_ticker_metric_coverage.csv"
    )
    metric_path = (
        output_dir / "transportation_post_review_metric_summary.csv"
    )
    cohort_path = (
        output_dir
        / "transportation_post_review_cohort_metric_coverage.csv"
    )
    support_path = (
        output_dir / "transportation_post_review_support_coverage.csv"
    )
    formal_path = (
        output_dir / "transportation_post_review_metric_acceptance.csv"
    )
    manifest_path = (
        output_dir / "transportation_post_review_coverage_manifest.json"
    )
    write_csv_atomic(final_path, FINAL_COVERAGE_FIELDS, final_rows)
    write_csv_atomic(metric_path, METRIC_SUMMARY_FIELDS, metric_rows)
    write_csv_atomic(cohort_path, COHORT_SUMMARY_FIELDS, cohort_rows)
    write_csv_atomic(
        support_path,
        SUPPORT_COVERAGE_FIELDS,
        support_rows,
    )
    write_csv_atomic(
        formal_path,
        POST_REVIEW_METRIC_FIELDS,
        formal_rows,
    )
    applicable = [
        row
        for row in final_rows
        if str(row["applicability_status"]) == "APPLICABLE"
    ]
    status_counts = Counter(
        str(row["coverage_status"]) for row in applicable
    )
    disposition_counts = Counter(
        str(row["metric_disposition"]) for row in formal_rows
    )
    before_manifest = json.loads(
        (
            output_dir / "transportation_parser_coverage_manifest.json"
        ).read_text(encoding="utf-8")
    )
    after_accepted = (
        status_counts["COVERED_ACCEPTED"]
        + status_counts["COVERED_FINANCIAL_DERIVED"]
    )
    payload = {
        "acceptance": (
            "PASS"
            if len(final_rows) == 14_400
            and len(metric_rows) == 90
            and len(support_rows) == 1_120
            else "FAIL"
        ),
        "gate": "DP6D_POST_REVIEW_FORMAL_COVERAGE",
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "base_run_id": run_id,
        "evaluation_id": args.evaluation_id,
        "policy_sha256": str(evaluation["policy_sha256"]),
        "golden_corpus_sha256": file_sha256(golden_path),
        "golden_expectation_count": len(
            load_corpus(golden_path)["expectations"]
        ),
        "golden_validation_error_count": len(golden_errors),
        "base_scope_hash_before": str(
            evaluation["base_scope_hash_before"]
        ),
        "base_scope_hash_after": str(
            evaluation["base_scope_hash_after"]
        ),
        "source_document_open_count": int(
            evaluation["source_document_open_count"]
        ),
        "arelle_invocation_count": int(
            evaluation["arelle_invocation_count"]
        ),
        "edgartools_invocation_count": int(
            evaluation["edgartools_invocation_count"]
        ),
        "ocr_invocation_count": int(evaluation["ocr_invocation_count"]),
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "before_accepted_pair_count": int(
            before_manifest["coverage_status_counts"].get(
                "COVERED_ACCEPTED",
                0,
            )
        )
        + int(
            before_manifest["coverage_status_counts"].get(
                "COVERED_FINANCIAL_DERIVED",
                0,
            )
        ),
        "after_accepted_pair_count": after_accepted,
        "accepted_pair_increase": (
            after_accepted
            - int(
                before_manifest["coverage_status_counts"].get(
                    "COVERED_ACCEPTED",
                    0,
                )
            )
            - int(
                before_manifest["coverage_status_counts"].get(
                    "COVERED_FINANCIAL_DERIVED",
                    0,
                )
            )
        ),
        "coverage_status_counts": dict(sorted(status_counts.items())),
        "metric_disposition_counts": dict(
            sorted(disposition_counts.items())
        ),
        "formal_calibration_candidate_count": sum(
            int(str(row["formal_calibration_gate_pass"]))
            for row in formal_rows
        ),
        "artifacts": {
            "ticker_metric_coverage": _artifact(
                final_path,
                len(final_rows),
            ),
            "metric_summary": _artifact(metric_path, len(metric_rows)),
            "cohort_summary": _artifact(cohort_path, len(cohort_rows)),
            "support_coverage": _artifact(
                support_path,
                len(support_rows),
            ),
            "metric_acceptance": _artifact(
                formal_path,
                len(formal_rows),
            ),
        },
        "next_gate": (
            "REVIEW_DEFERRED_PAIRS_THEN_TARGET_MINIMAL_DELTA_SOURCES"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
