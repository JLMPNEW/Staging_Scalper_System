#!/usr/bin/env python3
"""Publish a v8 subgroup rank table with production-compatible lineage.

This is intentionally a shadow-only bridge.  It consumes frozen v8 subgroup
scores and the outcome-blind policy, verifies the complete recipe/ticker
census, and emits the dedicated Portfolio Layer contract with all investable
and OOS gates held at zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import (  # noqa: E402
    FINAL_RANK_FIELDS,
    file_sha256,
    read_rows,
    validate_rank_rows,
    write_manifest,
    write_rank_rows,
)
from industrials.transportation.subgroup_production_lock import (  # noqa: E402
    build_subgroup_lock_payload,
    canonical_sha256,
    validate_subgroup_lock_payload,
)
from industrials.transportation.subgroup_production_scoring import (  # noqa: E402
    build_shadow_subgroup_rank_rows,
)
from industrials.transportation.subgroup_scoring import (  # noqa: E402
    load_subgroup_score_policy,
)


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_subgroup_score_policy_v8.yaml"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "subgroup_v8_shadow"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish the fail-closed Transportation v8 subgroup shadow contract."
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--source-rank-csv", type=Path, required=True)
    parser.add_argument(
        "--source-supplement-csv",
        type=Path,
        default=None,
        help=(
            "Optional canonical exact-date scoring snapshot used only to fill "
            "locked policy tickers absent from the primary dashboard."
        ),
    )
    parser.add_argument("--subgroup-score-csv", type=Path, required=True)
    parser.add_argument("--conflict-audit", type=Path, required=True)
    parser.add_argument("--score-manifest", type=Path, required=True)
    parser.add_argument("--calibration-manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _read_json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return payload


def verify_research_lineage(
    *,
    conflict_audit_path: Path,
    score_manifest_path: Path,
    calibration_manifest_path: Path,
    subgroup_score_path: Path,
) -> dict[str, object]:
    """Verify the shadow is bound to the final non-authorizing evidence chain."""
    paths = {
        "conflict_audit": conflict_audit_path.resolve(),
        "score_manifest": score_manifest_path.resolve(),
        "calibration_manifest": calibration_manifest_path.resolve(),
        "subgroup_score_csv": subgroup_score_path.resolve(),
    }
    missing = [f"{name}={path}" for name, path in paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Transportation shadow research lineage is incomplete: "
            + "; ".join(missing)
        )
    hashes = {name: file_sha256(path) for name, path in paths.items()}
    conflict = _read_json_object(paths["conflict_audit"], label="conflict audit")
    score = _read_json_object(paths["score_manifest"], label="score manifest")
    calibration = _read_json_object(
        paths["calibration_manifest"],
        label="calibration manifest",
    )

    errors: list[str] = []
    if conflict.get("policy_version") != (
        "transportation_accepted_fact_conflict_resolution_v3"
    ):
        errors.append("conflict audit is not strict policy v3")
    if conflict.get("period_start_boundary_policy") != (
        "complete_and_equal_for_every_deterministic_resolution_rule"
    ):
        errors.append("conflict audit lacks the strict period-start boundary")
    if int(conflict.get("unresolved_fail_closed_count") or -1) != int(
        conflict.get("resolver_conflict_count_after") or -2
    ):
        errors.append("conflict audit does not fail closed on every residual")
    if bool(conflict.get("production_activation_authorized")):
        errors.append("conflict audit improperly authorizes production")

    bridge = dict(score.get("conflict_resolution_bridge") or {})
    score_lineage = dict(score.get("lineage") or {})
    score_conflict = dict(score_lineage.get("conflict_audit") or {})
    score_artifact = dict((score.get("artifacts") or {}).get("score_history") or {})
    if bridge.get("status") != "VERIFIED":
        errors.append("score manifest conflict bridge is not VERIFIED")
    if bridge.get("audit_sha256") != hashes["conflict_audit"]:
        errors.append("score conflict bridge hash does not match conflict audit")
    if score_conflict.get("sha256") != hashes["conflict_audit"]:
        errors.append("score lineage conflict hash does not match conflict audit")
    if score_artifact.get("sha256") != hashes["subgroup_score_csv"]:
        errors.append("score-history CSV hash does not match score manifest")
    try:
        recorded_score_path = Path(str(score_artifact.get("path") or "")).resolve()
    except OSError:
        recorded_score_path = Path()
    if recorded_score_path != paths["subgroup_score_csv"]:
        errors.append("score-history CSV path does not match score manifest")
    if bool(score.get("production_activation_authorized")):
        errors.append("score manifest improperly authorizes production")

    calibration_lineage = dict(calibration.get("lineage") or {})
    calibration_score = dict(calibration_lineage.get("score_manifest") or {})
    calibration_conflict = dict(calibration_lineage.get("conflict_audit") or {})
    if "acceptance" in calibration:
        errors.append("calibration retains an ambiguous top-level acceptance label")
    if calibration.get("contract_version") != (
        "transportation_v8_subgroup_diagnostic_calibration_v3"
    ):
        errors.append("calibration is not the truth-labeled v3 contract")
    if calibration.get("execution_acceptance") != "PASS":
        errors.append("calibration execution did not pass")
    if calibration.get("predictive_acceptance") != "FAIL":
        errors.append("shadow requires an explicit failed predictive verdict")
    if calibration.get("production_promotion_eligible") is not False:
        errors.append("calibration is not explicitly production-ineligible")
    if bool(calibration.get("production_activation_authorized")):
        errors.append("calibration improperly authorizes production")
    if calibration_score.get("sha256") != hashes["score_manifest"]:
        errors.append("calibration score-manifest hash does not match")
    if calibration_conflict.get("sha256") != hashes["conflict_audit"]:
        errors.append("calibration conflict-audit hash does not match")
    if errors:
        raise ValueError("; ".join(errors))

    return {
        name: {"path": str(paths[name]), "sha256": hashes[name]}
        for name in (
            "conflict_audit",
            "score_manifest",
            "subgroup_score_csv",
            "calibration_manifest",
        )
    } | {
        "execution_acceptance": calibration["execution_acceptance"],
        "predictive_acceptance": calibration["predictive_acceptance"],
        "production_promotion_eligible": False,
        "production_activation_authorized": False,
    }


def _exact_date_ticker_index(
    rows: Sequence[Mapping[str, object]],
    *,
    asof: str,
    label: str,
) -> dict[str, dict[str, object]]:
    selected = [
        dict(row)
        for row in rows
        if str(row.get("asof_date") or "")[:10] == asof
    ]
    if not selected:
        raise ValueError(f"{asof}: {label} has no exact-date rows")
    indexed: dict[str, dict[str, object]] = {}
    duplicates: set[str] = set()
    for row in selected:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            raise ValueError(f"{asof}: {label} has a blank ticker")
        row["ticker"] = ticker
        if ticker in indexed:
            duplicates.add(ticker)
        indexed[ticker] = row
    if duplicates:
        raise ValueError(
            f"{asof}: {label} has duplicate tickers={sorted(duplicates)}"
        )
    return indexed


def select_policy_census_rows(
    *,
    source_rows: Sequence[Mapping[str, object]],
    source_supplement_rows: Sequence[Mapping[str, object]] | None = None,
    subgroup_rows: Sequence[Mapping[str, object]],
    asof: str,
    expected_tickers: set[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """Select the locked 35-name census and audit all non-policy source rows."""
    expected = {
        str(ticker).strip().upper() for ticker in expected_tickers
    }
    if not expected or "" in expected:
        raise ValueError("locked current subgroup ticker census is invalid")
    source = _exact_date_ticker_index(
        source_rows,
        asof=asof,
        label="source rank CSV",
    )
    supplement = (
        _exact_date_ticker_index(
            source_supplement_rows,
            asof=asof,
            label="source supplement CSV",
        )
        if source_supplement_rows is not None
        else {}
    )
    subgroup = _exact_date_ticker_index(
        subgroup_rows,
        asof=asof,
        label="subgroup score CSV",
    )
    primary_missing = expected - set(source)
    supplement_fills = sorted(primary_missing & set(supplement))
    missing_source = sorted(primary_missing - set(supplement))
    missing_subgroup = sorted(expected - set(subgroup))
    extra_subgroup = sorted(set(subgroup) - expected)
    if missing_source:
        raise ValueError(
            f"{asof}: source inputs are missing locked tickers={missing_source}"
        )
    if missing_subgroup or extra_subgroup:
        raise ValueError(
            f"{asof}: subgroup score census mismatch "
            f"missing={missing_subgroup} extra={extra_subgroup}"
        )
    excluded = sorted(set(source) - expected)
    supplement_overlap = sorted(set(supplement) & set(source))
    supplement_unused = sorted(set(supplement) - set(supplement_fills))
    merged_source = dict(source)
    for ticker in supplement_fills:
        merged_source[ticker] = supplement[ticker]
    audit: dict[str, object] = {
        "source_asof_row_count": len(source),
        "source_primary_selected_ticker_count": len(expected - primary_missing),
        "selected_policy_ticker_count": len(expected),
        "selected_policy_tickers_sha256": canonical_sha256(sorted(expected)),
        "excluded_source_row_count": len(excluded),
        "excluded_source_tickers": excluded,
        "excluded_source_tickers_sha256": canonical_sha256(excluded),
        "excluded_source_reason": "outside_locked_current_subgroup_census",
        "source_supplement_asof_row_count": len(supplement),
        "source_supplement_fill_count": len(supplement_fills),
        "source_supplement_fill_tickers": supplement_fills,
        "source_supplement_fill_tickers_sha256": canonical_sha256(
            supplement_fills
        ),
        "source_supplement_overlap_tickers": supplement_overlap,
        "source_supplement_unused_tickers": supplement_unused,
        "source_selection_precedence": (
            "primary_dashboard_then_supplement_only_for_missing_policy_tickers"
        ),
        "subgroup_asof_row_count": len(subgroup),
        "subgroup_exact_census_match": True,
    }
    return (
        [merged_source[ticker] for ticker in sorted(expected)],
        [subgroup[ticker] for ticker in sorted(expected)],
        audit,
    )


def publish_subgroup_shadow_dashboard(
    *,
    output_dir: Path,
    rows: list[dict[str, str]],
    asof: str,
) -> dict[str, object]:
    """Publish only the rank artifact; never synthesize a Stage 11 sidecar."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rank_path = output_dir / "transportation_final_rank_table.csv"
    manifest_path = output_dir / "transportation_final_rank_table_manifest.json"
    existing = [
        path for path in (rank_path, manifest_path) if path.exists()
    ]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite immutable Transportation subgroup shadow "
            f"artifacts: {existing}"
        )
    errors = validate_rank_rows(rows, asof=asof)
    if errors:
        raise ValueError("; ".join(errors[:20]))
    zero_gate_fields = (
        "portfolio_candidate_gate",
        "oos_score_valid_flag",
        "research_calibration_input_eligible_flag",
        "stage11_calibration_input_eligible_flag",
        "survivorship_corrected_panel_flag",
    )
    for field in zero_gate_fields:
        asserted = [
            row["ticker"] for row in rows if str(row.get(field) or "") != "0"
        ]
        if asserted:
            raise ValueError(
                f"shadow subgroup publish cannot assert {field}: {asserted[:10]}"
            )
    if any(
        row.get("transportation_production_state") != "shadow"
        for row in rows
    ):
        raise ValueError("subgroup shadow publish contains a non-shadow row")
    write_rank_rows(rank_path, rows)
    manifest: dict[str, object] = {
        "acceptance": "PASS",
        "acceptance_scope": "CONTRACT_EXECUTION_ONLY",
        "predictive_acceptance": "FAIL",
        "production_promotion_eligible": False,
        "model_family": "transportation",
        "asof_date": asof,
        "rank_table": str(rank_path),
        "rank_table_sha256": file_sha256(rank_path),
        "stage11_survivorship_calibration_panel": "",
        "stage11_survivorship_calibration_panel_sha256": "",
        "stage11_survivorship_calibration_panel_row_count": 0,
        "stage11_calibration_input_eligible_count": 0,
        "row_count": len(rows),
        "rank_ready_count": sum(
            row["rank_ready_flag"] == "1" for row in rows
        ),
        "portfolio_candidate_count": 0,
        "oos_score_valid_count": 0,
        "research_calibration_input_eligible_count": 0,
        "survivorship_corrected_panel_count": 0,
        "contract_fields": FINAL_RANK_FIELDS,
        "scoring_contract_versions": sorted(
            {row["scoring_contract_version"] for row in rows}
        ),
        "published_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    write_manifest(manifest_path, manifest)
    return manifest


def main() -> int:
    args = parse_args()
    asof = date.fromisoformat(str(args.asof)[:10]).isoformat()
    source_path = args.source_rank_csv.expanduser().resolve()
    subgroup_path = args.subgroup_score_csv.expanduser().resolve()
    conflict_audit_path = args.conflict_audit.expanduser().resolve()
    score_manifest_path = args.score_manifest.expanduser().resolve()
    calibration_manifest_path = args.calibration_manifest.expanduser().resolve()
    supplement_path = (
        args.source_supplement_csv.expanduser().resolve()
        if args.source_supplement_csv is not None
        else None
    )
    policy_path = args.policy.expanduser().resolve()
    for label, path in (
        ("source rank CSV", source_path),
        ("subgroup score CSV", subgroup_path),
        ("subgroup policy", policy_path),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} is missing: {path}")
    if supplement_path is not None and not supplement_path.is_file():
        raise FileNotFoundError(
            f"source supplement CSV is missing: {supplement_path}"
        )

    research_lineage = verify_research_lineage(
        conflict_audit_path=conflict_audit_path,
        score_manifest_path=score_manifest_path,
        calibration_manifest_path=calibration_manifest_path,
        subgroup_score_path=subgroup_path,
    )

    policy = load_subgroup_score_policy(policy_path)
    lock_payload = build_subgroup_lock_payload(
        policy,
        policy_sha256=file_sha256(policy_path),
    )
    lock_spec = validate_subgroup_lock_payload(lock_payload)
    expected_current = {
        ticker
        for ticker, memberships in lock_spec.memberships.items()
        if any(
            membership.membership_scope == "current_recipe"
            for membership in memberships
        )
    }
    source_rows, subgroup_rows, census_audit = select_policy_census_rows(
        source_rows=read_rows(source_path),
        source_supplement_rows=(
            read_rows(supplement_path)
            if supplement_path is not None
            else None
        ),
        subgroup_rows=read_rows(subgroup_path),
        asof=asof,
        expected_tickers=expected_current,
    )
    rank_rows = build_shadow_subgroup_rank_rows(
        source_rows=source_rows,
        subgroup_score_rows=subgroup_rows,
        lock_payload=lock_payload,
        activation_enabled=False,
        allow_pre_effective_diagnostic_replay=(
            date.fromisoformat(asof) < lock_spec.policy_effective_from
        ),
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else (DEFAULT_OUTPUT_ROOT / asof).resolve()
    )
    package_paths = (
        output_dir / "transportation_final_rank_table.csv",
        output_dir / "transportation_final_rank_table_manifest.json",
        output_dir / "transportation_subgroup_shadow_lock_payload.json",
        output_dir / "transportation_v8_subgroup_shadow_manifest.json",
    )
    existing_package_paths = [path for path in package_paths if path.exists()]
    if existing_package_paths:
        raise FileExistsError(
            "Refusing to overwrite immutable Transportation subgroup shadow "
            f"package: {existing_package_paths}"
        )
    dashboard = publish_subgroup_shadow_dashboard(
        output_dir=output_dir,
        rows=rank_rows,
        asof=asof,
    )
    payload_path = output_dir / "transportation_subgroup_shadow_lock_payload.json"
    write_text_atomic(
        payload_path,
        json.dumps(lock_payload, indent=2, sort_keys=True) + "\n",
    )
    result = {
        "artifact_family": (
            "transportation_v8_subgroup_shadow_publish_v3_truth_bound"
        ),
        "model_family": "transportation",
        "execution_acceptance": "PASS",
        "predictive_acceptance": "FAIL",
        "production_promotion_eligible": False,
        "asof_date": asof,
        "production_activation_authorized": False,
        "future_only_evidence_passed": False,
        "promotion_evidence_eligible": False,
        "evidence_role": (
            "integration_only_posthoc_pre_effective_diagnostic"
            if date.fromisoformat(asof) < lock_spec.policy_effective_from
            else "forward_shadow_integration_not_yet_matured"
        ),
        "policy_effective_from": lock_spec.policy_effective_from.isoformat(),
        "portfolio_candidate_count": 0,
        "oos_score_valid_count": 0,
        "rank_row_count": len(rank_rows),
        "rank_ready_row_count": sum(
            row["rank_ready_flag"] == "1" for row in rank_rows
        ),
        "locked_policy_ticker_count": len(expected_current),
        "zero_cap_shadow_contract": True,
        "all_allocation_and_research_gates_zero": True,
        "research_lineage": research_lineage,
        "policy_census_audit": census_audit,
        "source_rank_csv": str(source_path),
        "source_rank_sha256": file_sha256(source_path),
        "source_supplement_csv": (
            str(supplement_path) if supplement_path is not None else ""
        ),
        "source_supplement_sha256": (
            file_sha256(supplement_path)
            if supplement_path is not None
            else ""
        ),
        "subgroup_score_csv": str(subgroup_path),
        "subgroup_score_sha256": file_sha256(subgroup_path),
        "subgroup_policy": str(policy_path),
        "subgroup_policy_sha256": file_sha256(policy_path),
        "shadow_lock_payload": str(payload_path),
        "shadow_lock_payload_sha256": file_sha256(payload_path),
        "dashboard": dashboard,
        "created_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
    }
    manifest_path = output_dir / "transportation_v8_subgroup_shadow_manifest.json"
    write_text_atomic(
        manifest_path,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
