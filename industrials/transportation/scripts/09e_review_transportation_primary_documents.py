#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


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
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
)
from industrials.transportation.primary_document_enumeration import (  # noqa: E402
    PRIMARY_DOCUMENT_ENUMERATION_VERSION,
)
from industrials.transportation.primary_document_review import (  # noqa: E402
    ENDPOINT_REVIEW_FIELDS,
    EXTERNAL_DOMAIN_ADJUDICATION_FIELDS,
    HYDRATION_REQUEST_FIELDS,
    PRIMARY_DOCUMENT_REVIEW_VERSION,
    REVIEWED_DOCUMENT_FIELDS,
    build_endpoint_review_rows,
    build_external_domain_adjudications,
    build_reviewed_document_and_hydration_rows,
    summarize_review,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


ENUMERATION_ARTIFACT_FILES = {
    "discovery_pages": (
        "transportation_primary_document_discovery_pages.csv"
    ),
    "primary_document_manifest": (
        "transportation_primary_document_manifest.csv"
    ),
    "endpoint_enumeration": (
        "transportation_primary_document_endpoint_enumeration.csv"
    ),
    "external_domain_review": (
        "transportation_primary_document_external_domain_review.csv"
    ),
    "future_document_exclusions": (
        "transportation_primary_document_future_exclusions.csv"
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Review every transportation zero, partial, access-limited, "
            "and external-domain primary-document enumeration result and "
            "freeze the exact one-time hydration plan. This command performs "
            "no network retrieval and does not authorize parser execution."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--external-domain-policy",
        type=Path,
        default=(
            PROJECT_ROOT
            / "industrials"
            / "transportation"
            / "review_policies"
            / "transportation_external_asset_domain_policy.csv"
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


def _validate_sealed_artifact(
    *,
    manifest: Mapping[str, Any],
    artifact_name: str,
    path: Path,
    rows: Sequence[Mapping[str, object]],
) -> list[str]:
    errors: list[str] = []
    sealed = (manifest.get("artifacts") or {}).get(artifact_name) or {}
    if str(sealed.get("sha256") or "") != file_sha256(path):
        errors.append(f"{artifact_name}: hash does not match DP6O seal")
    if int(sealed.get("row_count") or -1) != len(rows):
        errors.append(
            f"{artifact_name}: row count does not match DP6O seal"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Primary-document review requires parser execution disabled"
        )
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    enumeration_manifest_path = (
        output_dir
        / "transportation_primary_document_enumeration_manifest.json"
    )
    policy_path = args.external_domain_policy.expanduser().resolve()
    required = [
        enumeration_manifest_path,
        policy_path,
        *(
            output_dir / filename
            for filename in ENUMERATION_ARTIFACT_FILES.values()
        ),
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing primary-document review inputs: {missing}"
        )

    enumeration_manifest = _read_json(enumeration_manifest_path)
    errors: list[str] = []
    if enumeration_manifest.get("acceptance") != "PASS":
        errors.append("DP6O enumeration manifest is not PASS")
    if enumeration_manifest.get("asof_date") != asof_date:
        errors.append("DP6O enumeration as-of date does not match config")
    if (
        enumeration_manifest.get("enumeration_version")
        != PRIMARY_DOCUMENT_ENUMERATION_VERSION
    ):
        errors.append("DP6O enumeration version is not supported")
    if (
        enumeration_manifest.get("next_gate")
        != "REVIEW_ZERO_PARTIAL_AND_ACCESS_LIMITED_ENDPOINTS"
    ):
        errors.append("DP6O manifest is not at the DP6Q review gate")
    if (
        enumeration_manifest.get("retrieval_authorized")
        or enumeration_manifest.get("parser_execution_authorized")
    ):
        errors.append("DP6O execution authorization must remain disabled")

    artifact_rows: dict[str, list[dict[str, str]]] = {}
    artifact_paths: dict[str, Path] = {}
    for artifact_name, filename in ENUMERATION_ARTIFACT_FILES.items():
        path = output_dir / filename
        rows = read_csv(path)
        artifact_paths[artifact_name] = path
        artifact_rows[artifact_name] = rows
        errors.extend(
            _validate_sealed_artifact(
                manifest=enumeration_manifest,
                artifact_name=artifact_name,
                path=path,
                rows=rows,
            )
        )

    endpoint_reviews, endpoint_errors = build_endpoint_review_rows(
        endpoint_rows=artifact_rows["endpoint_enumeration"],
        discovery_rows=artifact_rows["discovery_pages"],
    )
    errors.extend(endpoint_errors)
    (
        external_adjudications,
        external_policy,
        external_errors,
    ) = build_external_domain_adjudications(
        external_rows=artifact_rows["external_domain_review"],
        policy_rows=read_csv(policy_path),
    )
    errors.extend(external_errors)
    (
        reviewed_documents,
        hydration_requests,
        document_errors,
    ) = build_reviewed_document_and_hydration_rows(
        document_rows=artifact_rows["primary_document_manifest"],
        external_policy=external_policy,
    )
    errors.extend(document_errors)

    summary = summarize_review(
        endpoint_rows=endpoint_reviews,
        external_rows=external_adjudications,
        document_rows=reviewed_documents,
        hydration_rows=hydration_requests,
    )
    zero_review_count = sum(
        row["enumeration_status"]
        == "NO_PRIMARY_DOCUMENT_CANDIDATES_REVIEW_REQUIRED"
        for row in endpoint_reviews
    )
    if len(endpoint_reviews) != int(
        enumeration_manifest.get("endpoint_count") or -1
    ):
        errors.append("endpoint review count does not reconcile")
    if summary["exception_endpoint_review_count"] != (
        int(
            enumeration_manifest.get(
                "zero_document_endpoint_count"
            )
            or 0
        )
        + int(
            enumeration_manifest.get(
                "partial_discovery_failure_endpoint_count"
            )
            or 0
        )
        + int(
            enumeration_manifest.get(
                "reviewed_root_access_limitation_count"
            )
            or 0
        )
    ):
        errors.append("exception endpoint queue does not reconcile")
    if zero_review_count != int(
        enumeration_manifest.get("zero_document_endpoint_count") or -1
    ):
        errors.append("zero-document review queue does not reconcile")
    if summary["partial_discovery_endpoint_count"] != int(
        enumeration_manifest.get(
            "partial_discovery_failure_endpoint_count"
        )
        or -1
    ):
        errors.append("partial-discovery queue does not reconcile")
    if summary["access_limited_endpoint_count"] != int(
        enumeration_manifest.get(
            "reviewed_root_access_limitation_count"
        )
        or -1
    ):
        errors.append("access-limited queue does not reconcile")
    if len(external_adjudications) != int(
        enumeration_manifest.get("external_domain_review_count") or -1
    ):
        errors.append("external-domain review count does not reconcile")
    if len(reviewed_documents) != int(
        enumeration_manifest.get("primary_document_count") or -1
    ):
        errors.append("reviewed document count does not reconcile")

    excluded_external_rows = sum(
        int(str(row["include_in_hydration"])) == 0
        for row in external_adjudications
    )
    excluded_documents = sum(
        int(str(row["include_in_hydration"])) == 0
        for row in reviewed_documents
    )
    if excluded_external_rows != excluded_documents:
        errors.append(
            "external exclusions do not reconcile to reviewed documents"
        )
    hydration_fanout = sum(
        int(str(row["fanout_document_count"]))
        for row in hydration_requests
    )
    if hydration_fanout != int(str(
        summary["hydration_required_document_count"]
    )):
        errors.append("hydration request fanout does not reconcile")
    if any(
        int(str(row["retrieval_authorized"])) != 0
        or int(str(row["parser_execution_authorized"])) != 0
        for row in (
            *endpoint_reviews,
            *external_adjudications,
            *reviewed_documents,
            *hydration_requests,
        )
    ):
        errors.append("review artifacts authorize execution")
    if any(
        int(str(row["parse_all_applicable_metrics"])) != 1
        for row in reviewed_documents
        if int(str(row["include_in_hydration"])) == 1
    ):
        errors.append("approved document lost all-metric parser scope")
    if any(
        int(str(row["parse_all_applicable_metrics"])) != 1
        for row in hydration_requests
    ):
        errors.append("hydration request lost all-metric parser scope")

    endpoint_review_path = (
        output_dir
        / "transportation_primary_document_endpoint_review.csv"
    )
    external_adjudication_path = (
        output_dir
        / "transportation_primary_document_external_domain_adjudication.csv"
    )
    reviewed_document_path = (
        output_dir
        / "transportation_primary_document_reviewed_manifest.csv"
    )
    hydration_request_path = (
        output_dir
        / "transportation_primary_document_hydration_requests.csv"
    )
    manifest_path = (
        output_dir
        / "transportation_primary_document_review_manifest.json"
    )
    write_csv_atomic(
        endpoint_review_path,
        ENDPOINT_REVIEW_FIELDS,
        endpoint_reviews,
    )
    write_csv_atomic(
        external_adjudication_path,
        EXTERNAL_DOMAIN_ADJUDICATION_FIELDS,
        external_adjudications,
    )
    write_csv_atomic(
        reviewed_document_path,
        REVIEWED_DOCUMENT_FIELDS,
        reviewed_documents,
    )
    write_csv_atomic(
        hydration_request_path,
        HYDRATION_REQUEST_FIELDS,
        hydration_requests,
    )

    acceptance = "PASS" if not errors else "FAIL"
    payload = {
        "acceptance": acceptance,
        "gate": (
            "DP6Q_ZERO_PARTIAL_ACCESS_AND_EXTERNAL_REVIEW_SEAL"
        ),
        "review_version": PRIMARY_DOCUMENT_REVIEW_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        **summary,
        "zero_document_review_queue_count": zero_review_count,
        "external_domain_policy_key_count": len(external_policy),
        "source_posture": (
            "research_grade_with_explicit_primary_source_gaps"
            if acceptance == "PASS"
            else "blocked"
        ),
        "primary_source_hierarchy_enforced": True,
        "point_in_time_cutoff_inherited": True,
        "retrieval_manifest_frozen": acceptance == "PASS",
        "retrieval_authorized": False,
        "parser_execution_authorized": False,
        "document_retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "fact_assumption_posture": {
            "enumerated_urls_and_referrers": "fact_source_reported",
            "external_domain_classifications": (
                "analyst_interpretation"
            ),
            "zero_and_access_limited_source_gaps": (
                "missing_required_source"
            ),
        },
        "support_handoff": {
            "owning_workflow": "standalone_support_request",
            "decision_impact": (
                "Prevents unrelated or secondary external assets and "
                "unreviewed endpoint gaps from entering the one-pass parse."
            ),
            "readiness_effect": (
                "research_grade"
                if acceptance == "PASS"
                else "blocked"
            ),
            "artifact_role": "standalone_support_artifact",
            "hidden_unless_requested": True,
        },
        "errors": errors,
        "inputs": {
            "primary_document_enumeration_manifest": {
                "path": str(enumeration_manifest_path.resolve()),
                "sha256": file_sha256(enumeration_manifest_path),
            },
            "external_asset_domain_policy": {
                "path": str(policy_path),
                "sha256": file_sha256(policy_path),
                "row_count": len(read_csv(policy_path)),
            },
            **{
                artifact_name: {
                    "path": str(path.resolve()),
                    "sha256": file_sha256(path),
                    "row_count": len(artifact_rows[artifact_name]),
                }
                for artifact_name, path in artifact_paths.items()
            },
        },
        "artifacts": {
            "endpoint_review": {
                "path": str(endpoint_review_path.resolve()),
                "row_count": len(endpoint_reviews),
                "sha256": file_sha256(endpoint_review_path),
            },
            "external_domain_adjudication": {
                "path": str(external_adjudication_path.resolve()),
                "row_count": len(external_adjudications),
                "sha256": file_sha256(external_adjudication_path),
            },
            "reviewed_document_manifest": {
                "path": str(reviewed_document_path.resolve()),
                "row_count": len(reviewed_documents),
                "sha256": file_sha256(reviewed_document_path),
            },
            "hydration_requests": {
                "path": str(hydration_request_path.resolve()),
                "row_count": len(hydration_requests),
                "sha256": file_sha256(hydration_request_path),
            },
        },
        "next_gate": (
            "HYDRATE_HASH_AND_CONTENT_DEDUPLICATE_PRIMARY_DOCUMENTS_ONCE"
            if acceptance == "PASS"
            else "REPAIR_PRIMARY_DOCUMENT_REVIEW_ERRORS"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
