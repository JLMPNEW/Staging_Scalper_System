"""Fixed-spec v1.4 confirmation controls for the machinery model."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from industrials.core.cross_family_validation import (
    compare_replication_contracts,
    csv_header,
    sha256_file,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.machinery.production_universe import (
    production_universe_eligible,
)
from industrials.machinery.stage8_calibration import (
    COMPONENT_FIELDS,
    as_float,
    read_csv_rows,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
PROTOCOL_VERSION = "machinery_oos_v1.4.0"
DEFAULT_PROTOCOL_PATH = (
    PACKAGE_ROOT / "model_protocols" / f"{PROTOCOL_VERSION}.json"
)
DEFAULT_V13_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "machinery"
    / "model_cycles"
    / "machinery_oos_v1.3.0"
    / "stage8"
)
DEFAULT_V14_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "machinery"
    / "model_cycles"
    / PROTOCOL_VERSION
)
DEFAULT_DEFENSE_PANEL = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "defense"
    / "stage8"
    / "oos_calibration_panel_weekly"
    / "defense_oos_calibration_panel.csv"
)
DEFENSE_COMPONENT_FIELDS = (
    "valuation_score",
    "quality_score",
    "risk_control_score",
    "positioning_score",
    "market_behavior_score",
    "growth_score",
    "sector_cycle_score",
    "defense_budget_backlog_score",
)
DEFENSE_SEMANTIC_MAPPING: dict[str, str | None] = {
    "quality_score": "quality_score",
    "growth_score": "growth_score",
    "valuation_score": "valuation_score",
    "risk_control_score": "risk_control_score",
    "market_behavior_score": "market_behavior_score",
    "positioning_score": "positioning_score",
    "industrial_cycle_score": "sector_cycle_score",
    "orders_backlog_score": "defense_budget_backlog_score",
    "capex_cycle_score": None,
}
SIGNAL_FIELDS = (
    "protocol_version",
    "protocol_definition_sha256",
    "asof_date",
    "ticker",
    "company_name",
    "calibration_cohort",
    "membership_start_date",
    "membership_end_date",
    "universe_policy",
    *COMPONENT_FIELDS,
    "fixed_score",
    "fixed_rank",
    "ranked_cross_section",
    "sleeve_selected_flag",
    "sleeve_weight",
    "source_rank_table_sha256",
)
FORBIDDEN_SIGNAL_FIELD_TOKENS = (
    "benchmark",
    "execution",
    "exit",
    "forward",
    "outcome",
    "return",
)
DEFECT_FIELDS = ("severity", "code", "detail")
COMPATIBILITY_FIELDS = (
    "target_component",
    "source_component",
    "relation",
    "available_flag",
)


@dataclass(frozen=True)
class ConfirmatoryPaths:
    root: Path
    freeze_manifest: Path
    freeze_validation: Path
    defect_report: Path
    defect_issues: Path
    defense_compatibility_report: Path
    defense_component_matrix: Path


def confirmatory_paths(root: Path) -> ConfirmatoryPaths:
    return ConfirmatoryPaths(
        root=root,
        freeze_manifest=(
            root / "protocol" / "machinery_v14_protocol_freeze_manifest.json"
        ),
        freeze_validation=(
            root / "protocol" / "machinery_v14_protocol_validation.json"
        ),
        defect_report=(
            root / "audit" / "machinery_v13_defect_only_audit.json"
        ),
        defect_issues=(
            root / "audit" / "machinery_v13_defect_only_issues.csv"
        ),
        defense_compatibility_report=(
            root
            / "external_replication"
            / "machinery_v14_defense_compatibility.json"
        ),
        defense_component_matrix=(
            root
            / "external_replication"
            / "machinery_v14_defense_component_matrix.csv"
        ),
    )


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _parse_date(raw: object, *, field: str) -> date:
    try:
        return date.fromisoformat(str(raw or "").strip()[:10])
    except ValueError as exc:
        raise ValueError(f"Invalid {field}: {raw!r}") from exc


def _weights(payload: Mapping[str, Any]) -> dict[str, float]:
    raw = payload.get("weights")
    if not isinstance(raw, Mapping):
        raise ValueError("v1.4 protocol weights must be a mapping")
    fields = tuple(str(field) for field in payload.get("component_fields", ()))
    if fields != COMPONENT_FIELDS:
        raise ValueError(
            "v1.4 component fields must exactly match the machinery contract"
        )
    if set(raw) != set(COMPONENT_FIELDS):
        raise ValueError("v1.4 weights do not cover the exact component set")
    weights = {field: float(raw[field]) for field in COMPONENT_FIELDS}
    if any(not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ValueError("v1.4 weights must be finite and nonnegative")
    if abs(sum(weights.values()) - 1.0) > 1e-10:
        raise ValueError("v1.4 weights must sum to one")
    return weights


def load_protocol_definition(path: Path = DEFAULT_PROTOCOL_PATH) -> dict[str, Any]:
    payload = _load_json(path)
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Unexpected machinery confirmatory protocol version")
    if payload.get("model_family") != "machinery":
        raise ValueError("Confirmatory protocol model family is not machinery")
    if payload.get("candidate_id") != "equal_components":
        raise ValueError("v1.4 must freeze equal_components")
    if payload.get("optimizer_enabled") is not False:
        raise ValueError("v1.4 optimizer must be disabled")
    if int(payload.get("specification_count") or 0) != 1:
        raise ValueError("v1.4 must contain exactly one specification")
    origin = str(payload.get("candidate_origin") or "")
    if "not_independent_confirmation" not in origin:
        raise ValueError("v1.4 must disclose its selected-candidate origin")
    _weights(payload)
    policy = payload.get("confirmation_policy")
    if not isinstance(policy, Mapping):
        raise ValueError("v1.4 confirmation policy is missing")
    freeze = _parse_date(payload.get("freeze_date"), field="freeze_date")
    first_signal = _parse_date(
        policy.get("first_signal_date"),
        field="first_signal_date",
    )
    lockbox_end = _parse_date(
        policy.get("pre_freeze_lockbox_end_date"),
        field="pre_freeze_lockbox_end_date",
    )
    if lockbox_end != freeze or first_signal <= freeze:
        raise ValueError("v1.4 evidence partitions are not strictly ordered")
    if policy.get("sequential_peeking_permitted") is not False:
        raise ValueError("v1.4 must prohibit sequential outcome peeking")
    if policy.get("outcome_evaluation_requires_separate_approved_protocol") is not True:
        raise ValueError("v1.4 outcome access must require separate approval")
    return payload


def _manifest_hash_issues(
    source_root: Path,
    manifest: Mapping[str, Any],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return [
            {
                "severity": "ERROR",
                "code": "run_manifest_files_missing",
                "detail": "v1.3 run manifest has no files mapping",
            }
        ]
    for filename, metadata in files.items():
        expected = (
            str(metadata.get("sha256") or "")
            if isinstance(metadata, Mapping)
            else ""
        )
        path = source_root / str(filename)
        if not path.is_file():
            issues.append(
                {
                    "severity": "ERROR",
                    "code": "sealed_artifact_missing",
                    "detail": str(path),
                }
            )
        elif sha256_file(path) != expected:
            issues.append(
                {
                    "severity": "ERROR",
                    "code": "sealed_artifact_hash_mismatch",
                    "detail": str(path),
                }
            )
    return issues


def validate_v13_origin(
    protocol: Mapping[str, Any],
    *,
    source_root: Path,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    run_manifest_path = source_root / "machinery_stage8_run_manifest.json"
    registry_path = source_root / "machinery_stage8_candidate_registry.json"
    static_path = source_root / "machinery_stage8_static_summary.json"
    acceptance_path = source_root / "machinery_stage8_acceptance.json"
    walk_path = source_root / "machinery_stage8_walk_forward_summary.json"
    required = (
        run_manifest_path,
        registry_path,
        static_path,
        acceptance_path,
        walk_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return (
            [
                {
                    "severity": "ERROR",
                    "code": "v13_origin_artifact_missing",
                    "detail": ";".join(missing),
                }
            ],
            {},
        )
    run_manifest = _load_json(run_manifest_path)
    registry = _load_json(registry_path)
    static = _load_json(static_path)
    acceptance = _load_json(acceptance_path)
    walk = _load_json(walk_path)
    issues.extend(_manifest_hash_issues(source_root, run_manifest))
    expected_registry = str(protocol["origin_candidate_registry_sha256"])
    if registry.get("candidate_registry_sha256") != expected_registry:
        issues.append(
            {
                "severity": "ERROR",
                "code": "candidate_registry_identity_mismatch",
                "detail": "v1.3 logical candidate registry hash changed",
            }
        )
    if registry.get("lockbox_outcomes_accessed") is not False:
        issues.append(
            {
                "severity": "ERROR",
                "code": "origin_lockbox_accessed",
                "detail": "v1.3 registry does not prove lockbox exclusion",
            }
        )
    origin_candidates = registry.get("candidates")
    origin_weights = (
        origin_candidates.get("equal_components")
        if isinstance(origin_candidates, Mapping)
        else None
    )
    fixed_weights = _weights(protocol)
    if not isinstance(origin_weights, Mapping) or any(
        abs(float(origin_weights.get(field, -1.0)) - fixed_weights[field])
        > 1e-10
        for field in COMPONENT_FIELDS
    ):
        issues.append(
            {
                "severity": "ERROR",
                "code": "fixed_weights_do_not_match_v13_candidate",
                "detail": "v1.4 weights differ from v1.3 equal_components",
            }
        )
    if static.get("selected_candidate_id") != "equal_components":
        issues.append(
            {
                "severity": "ERROR",
                "code": "v13_selected_candidate_mismatch",
                "detail": str(static.get("selected_candidate_id") or ""),
            }
        )
    if acceptance.get("lockbox_outcomes_accessed") is not False:
        issues.append(
            {
                "severity": "ERROR",
                "code": "acceptance_lockbox_accessed",
                "detail": "v1.3 acceptance does not prove lockbox exclusion",
            }
        )
    if acceptance.get("stage9_readiness") != "BLOCKED":
        issues.append(
            {
                "severity": "ERROR",
                "code": "v13_readiness_not_blocked",
                "detail": str(acceptance.get("stage9_readiness") or ""),
            }
        )
    if acceptance.get("recommended_model_for_stage9") != "none":
        issues.append(
            {
                "severity": "ERROR",
                "code": "v13_model_was_recommended",
                "detail": str(
                    acceptance.get("recommended_model_for_stage9") or ""
                ),
            }
        )
    if walk.get("selected_candidate_id") != "equal_components":
        issues.append(
            {
                "severity": "ERROR",
                "code": "walk_forward_candidate_mismatch",
                "detail": str(walk.get("selected_candidate_id") or ""),
            }
        )
    evidence = {
        "run_manifest_sha256": sha256_file(run_manifest_path),
        "candidate_registry_file_sha256": sha256_file(registry_path),
        "candidate_registry_logical_sha256": registry.get(
            "candidate_registry_sha256"
        ),
        "static_summary_sha256": sha256_file(static_path),
        "acceptance_sha256": sha256_file(acceptance_path),
        "walk_forward_summary_sha256": sha256_file(walk_path),
        "v13_stage9_readiness": acceptance.get("stage9_readiness"),
        "v13_lockbox_outcomes_accessed": acceptance.get(
            "lockbox_outcomes_accessed"
        ),
        "prior_same_panel_total_trials": registry.get(
            "prior_same_panel_total_trials"
        ),
    }
    return issues, evidence


def freeze_protocol(
    *,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    source_root: Path = DEFAULT_V13_ROOT,
    output_root: Path = DEFAULT_V14_ROOT,
) -> dict[str, Any]:
    protocol = load_protocol_definition(protocol_path)
    paths = confirmatory_paths(output_root)
    if paths.freeze_manifest.exists():
        existing = validate_protocol_freeze(
            protocol_path=protocol_path,
            source_root=source_root,
            output_root=output_root,
        )
        if existing["acceptance"] == "PASS":
            return existing
        raise FileExistsError(
            "Refusing to replace an invalid or changed v1.4 protocol freeze"
        )
    issues, evidence = validate_v13_origin(protocol, source_root=source_root)
    payload = {
        "acceptance": "PASS" if not issues else "FAIL",
        "artifact_family": "machinery_v14_protocol_freeze",
        "candidate_id": protocol["candidate_id"],
        "candidate_origin": protocol["candidate_origin"],
        "created_at_utc": utc_now(),
        "defense_artifacts_modified": False,
        "lockbox_outcomes_accessed": False,
        "model_family": "machinery",
        "optimizer_enabled": False,
        "origin_evidence": evidence,
        "production_promotion_performed": False,
        "protocol_definition_path": str(protocol_path.resolve()),
        "protocol_definition_sha256": sha256_file(protocol_path),
        "protocol_version": PROTOCOL_VERSION,
        "source_root": str(source_root.resolve()),
        "specification_count": 1,
        "weights": _weights(protocol),
        "issues": issues,
    }
    _write_json(paths.freeze_manifest, payload)
    validation = validate_protocol_freeze(
        protocol_path=protocol_path,
        source_root=source_root,
        output_root=output_root,
    )
    return validation


def validate_protocol_freeze(
    *,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    source_root: Path = DEFAULT_V13_ROOT,
    output_root: Path = DEFAULT_V14_ROOT,
) -> dict[str, Any]:
    paths = confirmatory_paths(output_root)
    issues: list[str] = []
    if not paths.freeze_manifest.is_file():
        issues.append("v1.4 freeze manifest is missing")
        payload: dict[str, Any] = {}
    else:
        payload = _load_json(paths.freeze_manifest)
    try:
        protocol = load_protocol_definition(protocol_path)
    except ValueError as exc:
        protocol = {}
        issues.append(str(exc))
    if payload.get("protocol_definition_sha256") != sha256_file(protocol_path):
        issues.append("v1.4 protocol definition changed after freeze")
    if payload.get("lockbox_outcomes_accessed") is not False:
        issues.append("v1.4 freeze does not prove lockbox exclusion")
    if payload.get("production_promotion_performed") is not False:
        issues.append("v1.4 freeze unexpectedly performed promotion")
    if payload.get("optimizer_enabled") is not False:
        issues.append("v1.4 freeze unexpectedly enables optimization")
    if int(payload.get("specification_count") or 0) != 1:
        issues.append("v1.4 freeze does not contain one specification")
    if protocol:
        origin_issues, evidence = validate_v13_origin(
            protocol,
            source_root=source_root,
        )
        issues.extend(item["detail"] for item in origin_issues)
        if payload.get("origin_evidence") != evidence:
            issues.append("v1.4 origin evidence changed after freeze")
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "artifact_family": "machinery_v14_protocol_validation",
        "candidate_id": payload.get("candidate_id", ""),
        "lockbox_outcomes_accessed": False,
        "optimizer_enabled": False,
        "protocol_version": PROTOCOL_VERSION,
        "specification_count": 1,
        "issues": issues,
    }
    _write_json(paths.freeze_validation, result)
    return result


def _append_issue(
    issues: list[dict[str, str]],
    code: str,
    detail: str,
    *,
    severity: str = "ERROR",
) -> None:
    issues.append({"severity": severity, "code": code, "detail": detail})


def run_v13_defect_audit(
    *,
    source_root: Path = DEFAULT_V13_ROOT,
    output_root: Path = DEFAULT_V14_ROOT,
) -> dict[str, Any]:
    paths = confirmatory_paths(output_root)
    protocol = load_protocol_definition()
    issues, evidence = validate_v13_origin(protocol, source_root=source_root)
    panel = read_csv_rows(source_root / "machinery_stage8_panel.csv")
    membership = read_csv_rows(
        source_root / "machinery_stage8_sleeve_membership.csv"
    )
    folds = read_csv_rows(
        source_root / "machinery_stage8_candidate_fold_comparison.csv"
    )
    registry = _load_json(
        source_root / "machinery_stage8_candidate_registry.json"
    )
    walk = _load_json(
        source_root / "machinery_stage8_walk_forward_summary.json"
    )
    sealed_start = date(2026, 1, 1)
    panel_keys: set[tuple[str, str]] = set()
    for row in panel:
        key = (str(row.get("asof_date") or ""), str(row.get("ticker") or ""))
        if key in panel_keys:
            _append_issue(issues, "duplicate_panel_key", repr(key))
            break
        panel_keys.add(key)
        asof = _parse_date(key[0], field="panel.asof_date")
        if asof >= sealed_start:
            _append_issue(issues, "panel_crosses_lockbox", repr(key))
            break
        for field, raw in row.items():
            if (
                ("execution_exit_date_" in field or "forward_date_" in field)
                and raw
                and _parse_date(raw, field=field) >= sealed_start
            ):
                _append_issue(
                    issues,
                    "outcome_label_crosses_lockbox",
                    f"{key}:{field}={raw}",
                )
                break
    membership_keys: set[tuple[str, str, str, str, str]] = set()
    group_counts: Counter[tuple[str, str, str, str]] = Counter()
    group_declared: dict[tuple[str, str, str, str], set[int]] = defaultdict(set)
    for row in membership:
        group = (
            str(row.get("model") or ""),
            str(row.get("split_name") or ""),
            str(row.get("asof_date") or ""),
            str(row.get("horizon_days") or ""),
        )
        key = (*group, str(row.get("ticker") or ""))
        if key in membership_keys:
            _append_issue(issues, "duplicate_membership_key", repr(key))
            break
        membership_keys.add(key)
        group_counts[group] += 1
        declared = int(float(str(row.get("ranked_cross_section") or "0")))
        group_declared[group].add(declared)
        if row.get("rank_direction") != "1_is_highest_score":
            _append_issue(
                issues,
                "rank_direction_mismatch",
                repr(key),
            )
            break
        if row.get("universe_policy") != "operating_only":
            _append_issue(
                issues,
                "universe_policy_mismatch",
                repr(key),
            )
            break
    for group, count in group_counts.items():
        if group_declared[group] != {count}:
            _append_issue(
                issues,
                "ranked_cross_section_count_mismatch",
                f"{group}:actual={count}:declared={sorted(group_declared[group])}",
            )
            break
    candidate_ids = tuple(registry.get("evaluated_candidate_ids") or ())
    block_count = int(walk.get("block_count") or 0)
    expected_fold_rows = len(candidate_ids) * block_count
    if len(folds) != expected_fold_rows:
        _append_issue(
            issues,
            "candidate_fold_matrix_incomplete",
            f"actual={len(folds)} expected={expected_fold_rows}",
        )
    fold_keys = {
        (row.get("candidate_id", ""), row.get("block", "")) for row in folds
    }
    if len(fold_keys) != len(folds):
        _append_issue(
            issues,
            "duplicate_candidate_fold_key",
            f"rows={len(folds)} keys={len(fold_keys)}",
        )
    errors = [item for item in issues if item["severity"] == "ERROR"]
    report = {
        "acceptance": "PASS" if not errors else "FAIL",
        "allowed_scope": "defect_detection_only",
        "artifact_family": "machinery_v13_defect_only_audit",
        "candidate_or_gate_changes_performed": False,
        "created_at_utc": utc_now(),
        "defect_count": len(errors),
        "defense_artifacts_modified": False,
        "lockbox_outcomes_accessed": False,
        "manifest_file_count": len(
            _load_json(source_root / "machinery_stage8_run_manifest.json").get(
                "files", {}
            )
        ),
        "membership_rows": len(membership),
        "model_tuning_performed": False,
        "origin_evidence": evidence,
        "panel_rows": len(panel),
        "candidate_fold_rows": len(folds),
        "production_promotion_performed": False,
        "source_root": str(source_root.resolve()),
        "issues": issues,
    }
    write_csv_atomic(paths.defect_issues, DEFECT_FIELDS, issues)
    _write_json(paths.defect_report, report)
    return report


def _fixed_score(
    row: Mapping[str, str],
    weights: Mapping[str, float],
) -> float | None:
    weighted = 0.0
    available = 0.0
    for field, weight in weights.items():
        value = as_float(row.get(field))
        if value is None:
            continue
        weighted += value * weight
        available += weight
    return weighted / available if available > 0 else None


def _membership_covers(row: Mapping[str, str], asof: date) -> bool:
    start = _parse_date(
        row.get("membership_start_date"),
        field="membership_start_date",
    )
    end_raw = str(row.get("membership_end_date") or "").strip()
    end = (
        _parse_date(end_raw, field="membership_end_date")
        if end_raw
        else None
    )
    return start <= asof and (end is None or end > asof)


def _signal_path(output_root: Path, asof: str) -> tuple[Path, Path]:
    root = output_root / "forward_shadow" / "signals" / asof
    return (
        root / "machinery_v14_signal_snapshot.csv",
        root / "machinery_v14_signal_snapshot_manifest.json",
    )


def validate_signal_snapshot(
    *,
    signal_path: Path,
    manifest_path: Path,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
) -> dict[str, Any]:
    issues: list[str] = []
    if not signal_path.is_file() or not manifest_path.is_file():
        return {
            "acceptance": "FAIL",
            "issues": ["signal snapshot or manifest is missing"],
        }
    header = csv_header(signal_path)
    if header != SIGNAL_FIELDS:
        issues.append("signal snapshot schema mismatch")
    forbidden = [
        field
        for field in header
        if any(token in field.lower() for token in FORBIDDEN_SIGNAL_FIELD_TOKENS)
    ]
    if forbidden:
        issues.append(f"signal snapshot contains outcome fields: {forbidden}")
    manifest = _load_json(manifest_path)
    if manifest.get("signal_snapshot_sha256") != sha256_file(signal_path):
        issues.append("signal snapshot hash mismatch")
    if manifest.get("protocol_definition_sha256") != sha256_file(protocol_path):
        issues.append("signal protocol hash mismatch")
    if manifest.get("outcomes_accessed") is not False:
        issues.append("signal snapshot does not prove outcome exclusion")
    if manifest.get("outcome_fields_written") is not False:
        issues.append("signal snapshot claims outcome fields were written")
    rows = read_csv_rows(signal_path)
    if len(rows) != int(manifest.get("row_count") or -1):
        issues.append("signal snapshot row count mismatch")
    selected = sum(
        str(row.get("sleeve_selected_flag") or "") == "1" for row in rows
    )
    if selected != int(manifest.get("selected_count") or -1):
        issues.append("signal snapshot selected count mismatch")
    return {
        "acceptance": "PASS" if not issues else "FAIL",
        "row_count": len(rows),
        "selected_count": selected,
        "outcomes_accessed": False,
        "issues": issues,
    }


def capture_forward_signals(
    *,
    asof: str,
    rank_table: Path,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    output_root: Path = DEFAULT_V14_ROOT,
) -> dict[str, Any]:
    protocol = load_protocol_definition(protocol_path)
    paths = confirmatory_paths(output_root)
    freeze = _load_json(paths.freeze_manifest)
    if freeze.get("acceptance") != "PASS":
        raise ValueError("v1.4 protocol freeze is not valid")
    if freeze.get("protocol_definition_sha256") != sha256_file(protocol_path):
        raise ValueError("v1.4 protocol changed after freeze")
    asof_date = _parse_date(asof, field="asof")
    confirmation = protocol["confirmation_policy"]
    first_signal = _parse_date(
        confirmation["first_signal_date"],
        field="first_signal_date",
    )
    if asof_date < first_signal:
        raise ValueError(
            f"v1.4 signal date {asof} precedes frozen start {first_signal}"
        )
    if not rank_table.is_file():
        raise FileNotFoundError(f"Rank table does not exist: {rank_table}")
    source_hash = sha256_file(rank_table)
    rows = read_csv_rows(rank_table)
    if not rows:
        raise ValueError("Rank table is empty")
    source_dates = {str(row.get("asof_date") or "") for row in rows}
    if source_dates != {asof}:
        raise ValueError(
            f"Rank table asof dates do not equal requested date: {source_dates}"
        )
    weights = _weights(protocol)
    universe_policy = str(
        protocol["evaluation_contract"]["production_universe_policy"]
    )
    scored: list[tuple[dict[str, str], float]] = []
    for row in rows:
        if (
            str(row.get("rank_ready_flag") or "") != "1"
            or str(row.get("model_status") or "") != "complete"
            or not production_universe_eligible(row, policy=universe_policy)
            or not _membership_covers(row, asof_date)
        ):
            continue
        score = _fixed_score(row, weights)
        if score is not None:
            scored.append((row, score))
    evaluation = protocol["evaluation_contract"]
    minimum_cross_section = int(evaluation["minimum_cross_section"])
    if len(scored) < minimum_cross_section:
        raise ValueError(
            f"Signal cross-section {len(scored)} is below {minimum_cross_section}"
        )
    ordered = sorted(
        scored,
        key=lambda item: (-item[1], str(item[0].get("ticker") or "")),
    )
    count = min(
        len(ordered),
        max(
            int(evaluation["minimum_positions"]),
            math.ceil(len(ordered) * float(evaluation["top_quantile"])),
        ),
    )
    selected = {str(row.get("ticker") or "") for row, _ in ordered[:count]}
    protocol_hash = sha256_file(protocol_path)
    output_rows: list[dict[str, str]] = []
    for rank, (row, score) in enumerate(ordered, start=1):
        ticker = str(row.get("ticker") or "")
        is_selected = ticker in selected
        output = {
            "protocol_version": PROTOCOL_VERSION,
            "protocol_definition_sha256": protocol_hash,
            "asof_date": asof,
            "ticker": ticker,
            "company_name": str(row.get("company_name") or ""),
            "calibration_cohort": str(
                row.get("calibration_cohort") or ""
            ),
            "membership_start_date": str(
                row.get("membership_start_date") or ""
            ),
            "membership_end_date": str(
                row.get("membership_end_date") or ""
            ),
            "universe_policy": universe_policy,
            **{
                field: str(row.get(field) or "") for field in COMPONENT_FIELDS
            },
            "fixed_score": f"{score:.10f}",
            "fixed_rank": str(rank),
            "ranked_cross_section": str(len(ordered)),
            "sleeve_selected_flag": "1" if is_selected else "0",
            "sleeve_weight": f"{1.0 / count:.10f}" if is_selected else "0",
            "source_rank_table_sha256": source_hash,
        }
        output_rows.append(output)
    signal_path, manifest_path = _signal_path(output_root, asof)
    if signal_path.exists() or manifest_path.exists():
        existing = validate_signal_snapshot(
            signal_path=signal_path,
            manifest_path=manifest_path,
            protocol_path=protocol_path,
        )
        if (
            existing["acceptance"] == "PASS"
            and _load_json(manifest_path).get("source_rank_table_sha256")
            == source_hash
        ):
            return existing
        raise FileExistsError(
            f"Refusing to overwrite non-identical v1.4 signals: {signal_path}"
        )
    write_csv_atomic(signal_path, SIGNAL_FIELDS, output_rows)
    manifest = {
        "artifact_family": "machinery_v14_forward_signal_snapshot",
        "asof_date": asof,
        "created_at_utc": utc_now(),
        "lockbox_outcomes_accessed": False,
        "outcome_fields_written": False,
        "outcomes_accessed": False,
        "production_promotion_performed": False,
        "protocol_definition_sha256": protocol_hash,
        "protocol_version": PROTOCOL_VERSION,
        "ranked_cross_section": len(ordered),
        "row_count": len(output_rows),
        "selected_count": count,
        "signal_snapshot_sha256": sha256_file(signal_path),
        "source_rank_table_path": str(rank_table.resolve()),
        "source_rank_table_sha256": source_hash,
    }
    _write_json(manifest_path, manifest)
    return validate_signal_snapshot(
        signal_path=signal_path,
        manifest_path=manifest_path,
        protocol_path=protocol_path,
    )


def _read_distinct_values(
    path: Path,
    fields: Sequence[str],
) -> dict[str, set[str]]:
    values = {field: set() for field in fields}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            for field in fields:
                raw = str(row.get(field) or "").strip()
                if raw:
                    values[field].add(raw)
    return values


def assess_defense_compatibility(
    *,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    defense_panel: Path = DEFAULT_DEFENSE_PANEL,
    output_root: Path = DEFAULT_V14_ROOT,
) -> dict[str, Any]:
    protocol = load_protocol_definition(protocol_path)
    if not defense_panel.is_file():
        raise FileNotFoundError(f"Defense panel does not exist: {defense_panel}")
    defense_manifest_path = (
        defense_panel.parent / "defense_oos_calibration_panel_manifest.json"
    )
    if not defense_manifest_path.is_file():
        raise FileNotFoundError(
            f"Defense panel manifest does not exist: {defense_manifest_path}"
        )
    panel_hash_before = sha256_file(defense_panel)
    manifest_hash_before = sha256_file(defense_manifest_path)
    header = csv_header(defense_panel)
    manifest = _load_json(defense_manifest_path)
    missing_source_components = [
        field for field in DEFENSE_COMPONENT_FIELDS if field not in header
    ]
    distinct = _read_distinct_values(
        defense_panel,
        ("forward_days", "price_basis", "benchmark_ticker"),
    )
    source_horizons = sorted(
        int(value) for value in distinct["forward_days"] if value.isdigit()
    )
    price_bases = sorted(distinct["price_basis"])
    source_basis = (
        "adjusted_close_to_adjusted_close"
        if price_bases == ["adj_close"]
        else ",".join(price_bases)
    )
    benchmarks = sorted(distinct["benchmark_ticker"])
    source_benchmark = (
        benchmarks[0] if len(benchmarks) == 1 else ",".join(benchmarks)
    )
    evaluation = protocol["evaluation_contract"]
    comparison = compare_replication_contracts(
        target_components=COMPONENT_FIELDS,
        source_components=DEFENSE_COMPONENT_FIELDS,
        semantic_mapping=DEFENSE_SEMANTIC_MAPPING,
        target_horizons=tuple(
            int(value) for value in evaluation["horizons_trading_days"]
        ),
        source_horizons=source_horizons,
        target_return_basis=str(evaluation["return_basis"]),
        source_return_basis=source_basis,
        target_cost_bps=float(evaluation["turnover_cost_bps"]),
        source_cost_bps=None,
        target_benchmark=str(protocol["benchmark_ticker"]),
        source_benchmark=source_benchmark,
    )
    panel_hash_after = sha256_file(defense_panel)
    manifest_hash_after = sha256_file(defense_manifest_path)
    unchanged = (
        panel_hash_before == panel_hash_after
        and manifest_hash_before == manifest_hash_after
    )
    paths = confirmatory_paths(output_root)
    write_csv_atomic(
        paths.defense_component_matrix,
        COMPATIBILITY_FIELDS,
        comparison["component_rows"],
    )
    blockers = list(comparison["blockers"])
    if missing_source_components:
        blockers.append(
            "defense_panel_missing_components:"
            + ",".join(missing_source_components)
        )
    if not unchanged:
        blockers.append("defense_source_hash_changed_during_read")
    report = {
        "acceptance": "PASS" if unchanged else "FAIL",
        "artifact_family": "machinery_v14_defense_compatibility",
        "assessment_only": True,
        "created_at_utc": utc_now(),
        "defense_artifacts_modified": not unchanged,
        "defense_manifest_path": str(defense_manifest_path.resolve()),
        "defense_manifest_sha256_after": manifest_hash_after,
        "defense_manifest_sha256_before": manifest_hash_before,
        "defense_panel_path": str(defense_panel.resolve()),
        "defense_panel_sha256_after": panel_hash_after,
        "defense_panel_sha256_before": panel_hash_before,
        "defense_snapshot_count": manifest.get("snapshot_count"),
        "direct_replication_ready": comparison[
            "direct_replication_ready"
        ],
        "lockbox_outcomes_accessed": False,
        "machinery_acceptance_eligible": False,
        "missing_source_components": missing_source_components,
        "production_promotion_performed": False,
        "protocol_definition_sha256": sha256_file(protocol_path),
        "protocol_version": PROTOCOL_VERSION,
        "source_benchmark": source_benchmark,
        "source_horizons": source_horizons,
        "source_price_basis": price_bases,
        "supporting_evidence_only": True,
        "blockers": blockers,
        "recommended_next_action": (
            "build_shared_read_only_d1_open_21d_63d_adapter_before_replication"
        ),
    }
    _write_json(paths.defense_compatibility_report, report)
    return report
