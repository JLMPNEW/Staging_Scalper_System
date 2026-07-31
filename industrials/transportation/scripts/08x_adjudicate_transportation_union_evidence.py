#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.adjudication import (  # noqa: E402
    build_legacy_index,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
    read_only_connection,
)
from industrials.transportation.repair_coverage import (  # noqa: E402
    repaired_document_keys,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.union_adjudication import (  # noqa: E402
    FIXTURE_EVIDENCE_FIELDS,
    UNION_ADJUDICATION_FIELDS,
    UNION_ADJUDICATION_VERSION,
    build_union_adjudication,
    summarize_union_adjudication,
)


REVIEWED_BY = "codex_transportation_systematic_union_review_v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Systematically adjudicate every repaired-union review pair. "
            "Only exact prior accepted-source confirmation may auto-accept."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--base-evaluation-id", type=int, default=1)
    parser.add_argument(
        "--coverage-prefix",
        default="transportation_repaired_sec_union",
        help="Filename prefix of the sealed repaired-union coverage.",
    )
    parser.add_argument("--reviewed-at", default="2026-07-27")
    parser.add_argument(
        "--artifact-prefix",
        default="transportation_union",
        help=("Output filename prefix. Use a versioned prefix to preserve the immutable pre-policy adjudication."),
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _normalized_evidence(
    row: Any,
    *,
    source_stage: str,
    evidence_key_field: str,
) -> dict[str, object]:
    output = dict(row)
    output["source_stage"] = source_stage
    output["evidence_key"] = str(output.get(evidence_key_field) or "")
    return output


def _stage_evidence_rows(
    connection: Any,
    *,
    run_id: int,
    evaluation_id: int | None,
    source_stage: str,
    repaired_keys: set[tuple[str, str, str]] | None = None,
) -> list[dict[str, object]]:
    if evaluation_id is not None:
        evaluation = connection.execute(
            """
            SELECT base_run_id, model_family, status
            FROM sec_parser_review_evaluation
            WHERE evaluation_id=?
            """,
            (evaluation_id,),
        ).fetchone()
        if (
            evaluation is None
            or int(evaluation["base_run_id"]) != run_id
            or str(evaluation["model_family"]) != MODEL_FAMILY
            or str(evaluation["status"]) != "COMPLETED"
        ):
            raise ValueError(
                f"review evaluation {evaluation_id} does not match "
                f"run {run_id}"
            )
        rows = connection.execute(
            """
            SELECT *
            FROM sec_parser_review_evidence
            WHERE evaluation_id=?
            ORDER BY ticker, metric_name, evaluated_evidence_key
            """,
            (evaluation_id,),
        )
        evidence_key_field = "evaluated_evidence_key"
    else:
        rows = connection.execute(
            """
            SELECT evidence.*
            FROM sec_parser_run_metric_evidence AS relation
            JOIN sec_parser_metric_evidence_shadow AS evidence
              ON evidence.evidence_key=relation.evidence_key
            WHERE relation.run_id=?
              AND evidence.model_family=?
            ORDER BY evidence.ticker, evidence.metric_name,
                     evidence.evidence_key
            """,
            (run_id, MODEL_FAMILY),
        )
        evidence_key_field = "evidence_key"
    output: list[dict[str, object]] = []
    for row in rows:
        if (
            repaired_keys
            and str(row["candidate_status"]) == "PARSER_FAILURE"
            and (
                str(row["ticker"]).upper(),
                str(row["accession_number"]),
                str(row["source_document"]),
            )
            in repaired_keys
        ):
            continue
        output.append(
            _normalized_evidence(
                row,
                source_stage=source_stage,
                evidence_key_field=evidence_key_field,
            )
        )
    return output


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    coverage_prefix = str(args.coverage_prefix).strip()
    if not coverage_prefix or coverage_prefix in {".", ".."} or "/" in coverage_prefix or "\\" in coverage_prefix:
        raise ValueError("--coverage-prefix must be a filename prefix")
    artifact_prefix = str(args.artifact_prefix).strip()
    if not artifact_prefix or artifact_prefix in {".", ".."} or "/" in artifact_prefix or "\\" in artifact_prefix:
        raise ValueError("--artifact-prefix must be a filename prefix")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError("Union adjudication requires the general parser switch off")
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    output_dir = resolve_path(parser_cfg["output_root"], base_dir=base_dir) / str(parser_cfg["source_census_asof_date"])
    coverage_path = output_dir / f"{coverage_prefix}_ticker_metric_coverage.csv"
    coverage_manifest_path = output_dir / f"{coverage_prefix}_coverage_manifest.json"
    repair_source_path = output_dir / "transportation_parser_repair_source_manifest.csv"
    for path in (
        coverage_path,
        coverage_manifest_path,
        repair_source_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    coverage_manifest = _read_json(coverage_manifest_path)
    if coverage_manifest.get("acceptance") != "PASS" or str(
        (coverage_manifest.get("artifacts") or {}).get("ticker_metric_coverage", {}).get("sha256") or ""
    ) != file_sha256(coverage_path):
        raise ValueError("Repaired SEC union coverage is not sealed")
    delta_run_id = int(coverage_manifest.get("delta_run_id") or 0)
    repair_run_id = int(coverage_manifest.get("repair_run_id") or 0)
    additional_run_ids = tuple(
        int(value) for value in coverage_manifest.get("additional_run_ids") or () if int(value) > 0
    )
    raw_review_evaluation_ids = (
        coverage_manifest.get("review_evaluation_ids") or {}
    )
    review_evaluation_ids = {
        str(key): (
            int(value) if value is not None and int(value) > 0 else None
        )
        for key, value in raw_review_evaluation_ids.items()
    }
    if (
        review_evaluation_ids
        and review_evaluation_ids.get("base")
        != args.base_evaluation_id
    ):
        raise ValueError(
            "--base-evaluation-id does not match reviewed coverage"
        )
    repaired_keys = repaired_document_keys(read_csv(repair_source_path))
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        base_rows = _stage_evidence_rows(
            connection,
            run_id=int(coverage_manifest["base_run_id"]),
            evaluation_id=args.base_evaluation_id,
            source_stage="BASE_REVIEW_EVALUATION",
        )
        delta_rows = _stage_evidence_rows(
            connection,
            run_id=delta_run_id,
            evaluation_id=review_evaluation_ids.get("delta"),
            source_stage="SEC_DELTA_RUN",
            repaired_keys=repaired_keys,
        )
        repair_rows = _stage_evidence_rows(
            connection,
            run_id=repair_run_id,
            evaluation_id=review_evaluation_ids.get("repair"),
            source_stage="TARGETED_PDF_REPAIR_RUN",
        )
        additional_rows: list[dict[str, object]] = []
        for additional_run_id in additional_run_ids:
            additional_rows.extend(
                _stage_evidence_rows(
                    connection,
                    run_id=additional_run_id,
                    evaluation_id=(
                        review_evaluation_ids.get("direct")
                        if additional_run_id
                        == int(
                            coverage_manifest.get(
                                "direct_document_run_id"
                            )
                            or 0
                        )
                        else None
                    ),
                    source_stage=(
                        f"ADDITIONAL_EVIDENCE_RUN_{additional_run_id}"
                    ),
                )
            )
        legacy_index = build_legacy_index(
            [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM fact_sec_metric_disclosure_candidate
                    WHERE model_family=?
                      AND candidate_status='ACCEPTED'
                    ORDER BY ticker, metric_name, period_end,
                             candidate_key
                    """,
                    (MODEL_FAMILY,),
                )
            ]
        )
    evidence_rows = [
        *base_rows,
        *delta_rows,
        *repair_rows,
        *additional_rows,
    ]
    decisions, fixtures = build_union_adjudication(
        coverage_rows=read_csv(coverage_path),
        evidence_rows=evidence_rows,
        legacy_index=legacy_index,
        reviewed_at=args.reviewed_at,
        reviewed_by=REVIEWED_BY,
    )
    summary = summarize_union_adjudication(decisions)
    expected_pairs = int(
        (coverage_manifest.get("coverage_status_counts") or {}).get(
            "COVERED_REVIEW_REQUIRED",
            0,
        )
    )
    errors: list[str] = []
    if len(decisions) != expected_pairs:
        errors.append("review-pair count does not match repaired coverage")
    if any(int(str(row["review_evidence_count"]) or "0") <= 0 for row in decisions):
        errors.append("a review-required pair has no review evidence")
    decision_path = output_dir / f"{artifact_prefix}_evidence_adjudication.csv"
    fixture_path = output_dir / f"{artifact_prefix}_metric_fixture_queue.csv"
    manifest_path = output_dir / f"{artifact_prefix}_evidence_adjudication_manifest.json"
    write_csv_atomic(
        decision_path,
        UNION_ADJUDICATION_FIELDS,
        decisions,
    )
    write_csv_atomic(
        fixture_path,
        FIXTURE_EVIDENCE_FIELDS,
        fixtures,
    )
    raw_decision_counts = summary.get("decision_counts")
    decision_counts = raw_decision_counts if isinstance(raw_decision_counts, Mapping) else {}
    accept_count = int(str(decision_counts.get("ACCEPT", 0)))
    payload = {
        "acceptance": ("PASS" if decisions and not errors else "FAIL"),
        "gate": "DP6I_SYSTEMATIC_UNION_EVIDENCE_ADJUDICATION",
        "adjudication_version": UNION_ADJUDICATION_VERSION,
        "model_family": MODEL_FAMILY,
        "reviewed_by": REVIEWED_BY,
        "reviewed_at": args.reviewed_at,
        "coverage_prefix": coverage_prefix,
        "artifact_prefix": artifact_prefix,
        "additional_run_ids": list(additional_run_ids),
        "review_evaluation_ids": review_evaluation_ids,
        "review_method": ("EXACT_PRIOR_ACCEPTED_SOURCE_CONFIRMATION_ONLY"),
        **summary,
        "fixture_evidence_row_count": len(fixtures),
        "policy_candidate_required": bool(accept_count),
        "policy_registry_mutated": False,
        "network_requests": 0,
        "source_document_open_count": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "input": {
            "path": str(coverage_path.resolve()),
            "sha256": file_sha256(coverage_path),
        },
        "artifacts": {
            "pair_adjudication": {
                "path": str(decision_path.resolve()),
                "row_count": len(decisions),
                "sha256": file_sha256(decision_path),
            },
            "metric_fixture_queue": {
                "path": str(fixture_path.resolve()),
                "row_count": len(fixtures),
                "sha256": file_sha256(fixture_path),
            },
        },
        "next_gate": ("BUILD_HASH_EXACT_POLICY_CANDIDATE" if accept_count else "SEAL_NON_SEC_ENDPOINT_REQUIREMENTS"),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
