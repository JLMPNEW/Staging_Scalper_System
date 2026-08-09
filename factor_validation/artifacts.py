"""Atomic, immutable publication and verification of factor evidence packages."""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from factor_validation.core import (
    CONTRACT_VERSION,
    FactorObservation,
    FactorValidationConfig,
    FactorValidationResult,
    validate_factor,
)
from factor_validation.evidence import (
    CONTENT_FILE_NAMES,
    PER_DATE_IC_HEADER,
    QUANTILE_DIAGNOSTICS_HEADER,
    EvidenceFiles,
    build_evidence_files,
)
from factor_validation.fdr import FDRFamily, apply_benjamini_hochberg
from factor_validation.ledger import (
    LEDGER_FILE_NAME,
    EnvironmentProvenance,
    acquire_advisory_lock,
    append_campaign_ledger_event,
    read_campaign_ledger,
    release_advisory_lock,
    successful_ledger_entry,
)
from factor_validation.registry import (
    CampaignRegistry,
    ObservedProvenance,
    ProvenanceFileSet,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)


MANIFEST_FILE_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = "factor_validation_evidence_manifest_v2"
PACKAGE_FILE_NAMES = tuple(sorted((*CONTENT_FILE_NAMES, MANIFEST_FILE_NAME)))
MANIFEST_KEYS = frozenset(
    {
        "acceptance_record_sha256",
        "campaign_id",
        "cell_id",
        "cell_registration_sha256",
        "contract_version",
        "environment",
        "environment_sha256",
        "fdr",
        "file_sha256",
        "file_size_bytes",
        "observed_cadence",
        "observed_provenance_sha256",
        "provenance",
        "registry_sha256",
        "row_counts",
        "schema_version",
        "state",
        "supersedes_manifest_sha256",
        "validation_config",
        "validation_config_sha256",
    }
)


@dataclass(frozen=True)
class EvidencePackage:
    path: Path
    state: str
    manifest_sha256: str
    registry_sha256: str


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    errors: tuple[str, ...]
    state: str | None
    manifest_sha256: str | None
    registry_sha256: str | None


def evidence_package_path(
    output_root: str | Path,
    registry: CampaignRegistry,
    *,
    cell_id: str,
) -> Path:
    cell = registry.cell(cell_id)
    root = Path(output_root).resolve()
    compact = (
        root
        / registry.campaign_id
        / "packages"
        / cell.cell_id
    ).resolve()
    legacy = (
        root
        / registry.campaign_id
        / cell.sector_id
        / cell.factor_id
        / f"{cell.horizon_trading_days}d"
        / cell.cell_id
    ).resolve()
    if compact.exists() and legacy.exists():
        raise ValueError("both compact and legacy evidence package paths exist")
    # New publications use the compact layout. Existing immutable campaigns
    # remain readable at the original descriptive layout.
    target = legacy if legacy.exists() else compact
    if not target.is_relative_to(root):  # pragma: no cover - safe ID invariant
        raise ValueError("evidence package path escapes output_root")
    if os.name == "nt":
        longest_file_path = max(
            (target / name for name in PACKAGE_FILE_NAMES),
            key=lambda path: len(str(path)),
        )
        if len(str(longest_file_path)) > 240:
            raise ValueError(
                "evidence package file path exceeds the conservative Windows path limit: "
                f"{longest_file_path}"
            )
    return target


def campaign_registry_path(output_root: str | Path, registry: CampaignRegistry) -> Path:
    root = Path(output_root).resolve()
    target = (root / registry.campaign_id / "campaign_registry.json").resolve()
    if not target.is_relative_to(root):  # pragma: no cover - safe ID invariant
        raise ValueError("campaign registry path escapes output_root")
    if os.name == "nt" and len(str(target)) > 240:
        raise ValueError(
            "campaign registry path exceeds the conservative Windows path limit: "
            f"{target}"
        )
    return target


def load_campaign_registry(path: str | Path) -> CampaignRegistry:
    payload, raw = _read_canonical_json(Path(path))
    registry = CampaignRegistry.from_dict(payload)
    if canonical_json_bytes(registry.to_dict()) != raw:
        raise ValueError("campaign registry does not match the exact registered schema")
    return registry


def _manifest_payload(
    registry: CampaignRegistry,
    *,
    cell_id: str,
    result: FactorValidationResult,
    evidence: EvidenceFiles,
    observed_provenance: ObservedProvenance,
    environment: EnvironmentProvenance,
) -> dict[str, Any]:
    cell = registry.cell(cell_id)
    family = registry.family(cell.fdr_family_id)
    files = evidence.by_name()
    return {
        "acceptance_record_sha256": evidence.acceptance.record_sha256,
        "campaign_id": registry.campaign_id,
        "cell_id": cell.cell_id,
        "cell_registration_sha256": cell.registration_sha256,
        "contract_version": CONTRACT_VERSION,
        "environment": environment.to_dict(),
        "environment_sha256": environment.sha256,
        "fdr": {
            "alpha": family.alpha,
            "family_id": family.family_id,
            "family_registration_sha256": family.registration_sha256,
            "member_id": cell.fdr_member_id,
        },
        "file_sha256": {name: sha256_bytes(data) for name, data in sorted(files.items())},
        "file_size_bytes": {name: len(data) for name, data in sorted(files.items())},
        "observed_cadence": result.to_dict()["evaluation_cadence"],
        "observed_provenance_sha256": observed_provenance.observed_sha256,
        "provenance": observed_provenance.to_dict(),
        "registry_sha256": registry.registration_sha256,
        "row_counts": {
            "per_date_ic.csv": len(result.per_date),
            "quantile_diagnostics.csv": len(result.per_date),
        },
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "state": evidence.acceptance.state,
        "supersedes_manifest_sha256": evidence.acceptance.supersedes_manifest_sha256,
        "validation_config": cell.validation_config.to_dict(),
        "validation_config_sha256": cell.validation_config_sha256,
    }


def _raise_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _read_canonical_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_raise_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON in {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a top-level object")
    if canonical_json_bytes(value) != raw:
        raise ValueError(f"{path.name} is not canonical deterministic JSON")
    return value, raw


def _csv_rows(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError("missing header")
            fieldnames = tuple(reader.fieldnames)
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error, ValueError) as exc:
        raise ValueError(f"invalid CSV in {path.name}: {exc}") from exc
    if any(None in row for row in rows):
        raise ValueError(f"invalid CSV in {path.name}: row has extra columns")
    return fieldnames, rows


def verify_evidence_package(
    package_dir: str | Path,
    *,
    expected_registry: CampaignRegistry | None = None,
    expected_cell_id: str | None = None,
    ledger_root: str | Path | None = None,
    require_ledger: bool = True,
    provenance_files: ProvenanceFileSet | None = None,
) -> VerificationReport:
    """Recompute contracts and require an independent successful ledger entry."""

    package = Path(package_dir)
    errors: list[str] = []
    if not package.is_dir() or package.is_symlink():
        return VerificationReport(False, ("package_not_regular_directory",), None, None, None)
    entries = {item.name for item in package.iterdir()}
    expected_entries = set(PACKAGE_FILE_NAMES)
    if entries != expected_entries:
        errors.append(
            f"package_file_set_mismatch:missing={sorted(expected_entries - entries)}:"
            f"extra={sorted(entries - expected_entries)}"
        )
    for name in sorted(entries & expected_entries):
        path = package / name
        if not path.is_file() or path.is_symlink():
            errors.append(f"package_file_not_regular:{name}")

    json_payloads: dict[str, dict[str, Any]] = {}
    json_bytes: dict[str, bytes] = {}
    for name in ("acceptance.json", "campaign_registry.json", "fdr_family.json", "summary.json", "manifest.json"):
        path = package / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            payload, raw = _read_canonical_json(path)
            json_payloads[name] = payload
            json_bytes[name] = raw
        except ValueError as exc:
            errors.append(str(exc))

    manifest = json_payloads.get("manifest.json")
    manifest_raw = json_bytes.get("manifest.json")
    manifest_sha256 = sha256_bytes(manifest_raw) if manifest_raw is not None else None
    manifest_state = manifest.get("state") if manifest is not None else None
    state = manifest_state if isinstance(manifest_state, str) else None
    registry_sha256: str | None = None
    embedded_registry: CampaignRegistry | None = None
    registry_payload = json_payloads.get("campaign_registry.json")
    if registry_payload is not None:
        try:
            embedded_registry = CampaignRegistry.from_dict(registry_payload)
            registry_sha256 = embedded_registry.registration_sha256
            if canonical_json_bytes(embedded_registry.to_dict()) != json_bytes.get(
                "campaign_registry.json"
            ):
                errors.append("campaign_registry_contract_shape_mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"invalid_campaign_registry:{exc}")

    if expected_registry is not None:
        if registry_sha256 != expected_registry.registration_sha256:
            errors.append("registry_sha256_mismatch")
        elif embedded_registry != expected_registry:
            errors.append("registry_payload_mismatch")
    if expected_cell_id is not None and embedded_registry is not None:
        try:
            embedded_registry.cell(expected_cell_id)
        except (KeyError, ValueError):
            errors.append("expected_cell_id_not_registered")

    if provenance_files is not None:
        if type(provenance_files) is not ProvenanceFileSet:
            errors.append("runtime_provenance_file_set_type_invalid")
        elif embedded_registry is None:
            errors.append("runtime_provenance_registry_unavailable")
        else:
            runtime_cell_id = expected_cell_id
            if runtime_cell_id is None and manifest is not None:
                candidate_cell_id = manifest.get("cell_id")
                runtime_cell_id = candidate_cell_id if isinstance(candidate_cell_id, str) else None
            try:
                if runtime_cell_id is None:
                    raise KeyError("cell ID unavailable")
                registered_cell = embedded_registry.cell(runtime_cell_id)
                if provenance_files.observe() != registered_cell.registered_provenance:
                    errors.append("runtime_provenance_drift")
            except (KeyError, OSError, TypeError, ValueError) as exc:
                errors.append(f"runtime_provenance_unverifiable:{exc.__class__.__name__}")

    if manifest is not None:
        if set(manifest) != MANIFEST_KEYS:
            errors.append(
                "manifest_schema_keys_mismatch:"
                f"missing={sorted(MANIFEST_KEYS - set(manifest))}:"
                f"extra={sorted(set(manifest) - MANIFEST_KEYS)}"
            )
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            errors.append("manifest_schema_version_mismatch")
        if manifest.get("contract_version") != CONTRACT_VERSION:
            errors.append("manifest_contract_version_mismatch")
        if manifest.get("registry_sha256") != registry_sha256:
            errors.append("manifest_registry_sha256_mismatch")
        if expected_cell_id is not None and manifest.get("cell_id") != expected_cell_id:
            errors.append("manifest_cell_id_mismatch")
        hash_map = manifest.get("file_sha256")
        size_map = manifest.get("file_size_bytes")
        if not isinstance(hash_map, dict) or set(hash_map) != set(CONTENT_FILE_NAMES):
            errors.append("manifest_file_sha256_contract_mismatch")
        else:
            for name in CONTENT_FILE_NAMES:
                path = package / name
                if path.is_file() and not path.is_symlink():
                    actual = sha256_bytes(path.read_bytes())
                    if hash_map.get(name) != actual:
                        errors.append(f"artifact_sha256_mismatch:{name}")
        if not isinstance(size_map, dict) or set(size_map) != set(CONTENT_FILE_NAMES):
            errors.append("manifest_file_size_contract_mismatch")
        else:
            for name in CONTENT_FILE_NAMES:
                path = package / name
                if path.is_file() and not path.is_symlink() and size_map.get(name) != path.stat().st_size:
                    errors.append(f"artifact_size_mismatch:{name}")
        environment_value = manifest.get("environment")
        try:
            if not isinstance(environment_value, dict):
                raise TypeError("environment must be an object")
            environment = EnvironmentProvenance.from_dict(environment_value)
            if manifest.get("environment_sha256") != environment.sha256:
                errors.append("manifest_environment_sha256_mismatch")
        except (TypeError, ValueError) as exc:
            errors.append(f"manifest_environment_invalid:{exc}")

    acceptance = json_payloads.get("acceptance.json")
    summary = json_payloads.get("summary.json")
    fdr = json_payloads.get("fdr_family.json")
    if acceptance is not None:
        acceptance_state = acceptance.get("state")
        gates = acceptance.get("gates")
        if acceptance.get("schema_version") != "factor_validation_acceptance_v1":
            errors.append("acceptance_schema_version_mismatch")
        if acceptance.get("state_history") != ["draft", "validated", acceptance_state]:
            errors.append("acceptance_state_history_mismatch")
        if acceptance_state not in {"accepted", "rejected"}:
            errors.append("acceptance_state_invalid")
        if not isinstance(gates, list) or not gates:
            errors.append("acceptance_gates_invalid")
        else:
            passed = [item.get("passed") for item in gates if isinstance(item, dict)]
            if len(passed) != len(gates) or any(not isinstance(item, bool) for item in passed):
                errors.append("acceptance_gate_values_invalid")
            elif acceptance_state == "accepted" and not all(passed):
                errors.append("accepted_package_has_failed_gate")
            elif acceptance_state == "rejected" and all(passed):
                errors.append("rejected_package_has_no_failed_gate")
        if manifest is not None:
            if manifest.get("state") != acceptance_state:
                errors.append("manifest_acceptance_state_mismatch")
            if manifest.get("acceptance_record_sha256") != sha256_bytes(json_bytes["acceptance.json"]):
                errors.append("acceptance_record_sha256_mismatch")
            for key in ("campaign_id", "cell_id", "registry_sha256", "cell_registration_sha256"):
                if manifest.get(key) != acceptance.get(key):
                    errors.append(f"manifest_acceptance_{key}_mismatch")
            if manifest.get("supersedes_manifest_sha256") != acceptance.get(
                "supersedes_manifest_sha256"
            ):
                errors.append("manifest_acceptance_supersedes_mismatch")

    if embedded_registry is not None and manifest is not None:
        cell_id = manifest.get("cell_id")
        try:
            cell = embedded_registry.cell(str(cell_id))
            family = embedded_registry.family(cell.fdr_family_id)
            if manifest.get("campaign_id") != embedded_registry.campaign_id:
                errors.append("manifest_campaign_id_mismatch")
            if manifest.get("cell_registration_sha256") != cell.registration_sha256:
                errors.append("manifest_cell_registration_sha256_mismatch")
            if manifest.get("provenance") != {
                "code_files": [item.to_dict() for item in cell.code_files],
                "config_sha256": cell.config_sha256,
                "source_files": [item.to_dict() for item in cell.source_files],
            }:
                errors.append("manifest_provenance_mismatch")
            if manifest.get("observed_provenance_sha256") != cell.registered_provenance.observed_sha256:
                errors.append("manifest_observed_provenance_sha256_mismatch")
            if manifest.get("validation_config") != cell.validation_config.to_dict():
                errors.append("manifest_validation_config_mismatch")
            if manifest.get("validation_config_sha256") != cell.validation_config_sha256:
                errors.append("manifest_validation_config_sha256_mismatch")
            if not isinstance(manifest.get("fdr"), dict) or manifest["fdr"] != {
                "alpha": family.alpha,
                "family_id": family.family_id,
                "family_registration_sha256": family.registration_sha256,
                "member_id": cell.fdr_member_id,
            }:
                errors.append("manifest_fdr_registration_mismatch")
        except (KeyError, ValueError):
            errors.append("manifest_cell_not_registered")

    if summary is not None and manifest is not None:
        if summary.get("schema_version") != "factor_validation_evidence_v1":
            errors.append("summary_schema_version_mismatch")
        if summary.get("contract_version") != CONTRACT_VERSION:
            errors.append("summary_contract_version_mismatch")
        if summary.get("primary_inference") != "independent_window":
            errors.append("summary_primary_inference_mismatch")
        if summary.get("evidence_eligible") is False and summary.get("primary_p_value") is not None:
            errors.append("ineligible_summary_exposes_primary_p_value")
        for key in ("campaign_id", "cell_id", "registry_sha256", "cell_registration_sha256"):
            if summary.get(key) != manifest.get(key):
                errors.append(f"summary_manifest_{key}_mismatch")
        if summary.get("evaluation_cadence") != manifest.get("observed_cadence"):
            errors.append("summary_manifest_cadence_mismatch")
        if embedded_registry is not None:
            try:
                cell = embedded_registry.cell(str(manifest.get("cell_id")))
                config = cell.validation_config
                independent = summary.get("independent_window")
                if not isinstance(independent, dict):
                    raise TypeError("independent_window must be an object")
                expected_reasons: list[str] = []
                ic_date_count = summary.get("ic_date_count")
                independent_count = independent.get("independent_window_count")
                diagnostic_p = independent.get("two_sided_p_value")
                if not isinstance(ic_date_count, int) or isinstance(ic_date_count, bool):
                    raise TypeError("ic_date_count must be an integer")
                if not isinstance(independent_count, int) or isinstance(
                    independent_count, bool
                ):
                    raise TypeError("independent_window_count must be an integer")
                if ic_date_count < config.min_dates:
                    expected_reasons.append("insufficient_ic_dates")
                if independent_count < config.min_independent_windows:
                    expected_reasons.append("insufficient_independent_windows")
                if diagnostic_p is None:
                    expected_reasons.append("independent_window_inference_unavailable")
                expected_eligible = not expected_reasons
                expected_primary = diagnostic_p if expected_eligible else None
                if summary.get("insufficiency_reasons") != expected_reasons:
                    errors.append("summary_insufficiency_reasons_mismatch")
                if summary.get("evidence_eligible") is not expected_eligible:
                    errors.append("summary_evidence_eligibility_mismatch")
                if summary.get("primary_p_value") != expected_primary:
                    errors.append("summary_primary_p_value_inference_mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"summary_evidence_contract_invalid:{exc}")
    if fdr is not None and manifest is not None:
        if fdr.get("schema_version") != "factor_validation_fdr_evidence_v1":
            errors.append("fdr_schema_version_mismatch")
        manifest_fdr = manifest.get("fdr")
        for key in ("alpha", "family_id", "family_registration_sha256"):
            if not isinstance(manifest_fdr, dict) or fdr.get(key) != manifest_fdr.get(key):
                errors.append(f"fdr_manifest_{key}_mismatch")
        family_decisions = fdr.get("family_decisions")
        member_ids = fdr.get("member_ids")
        try:
            if not isinstance(family_decisions, list) or not isinstance(member_ids, list):
                raise TypeError("family decisions and member IDs must be lists")
            family = FDRFamily(
                family_id=fdr.get("family_id", ""),
                member_ids=tuple(member_ids),
                alpha=fdr.get("alpha", math.nan),
            )
            if fdr.get("family_registration_sha256") != family.registration_sha256:
                raise ValueError("family registration digest mismatch")
            p_values = {
                str(item.get("member_id")): item.get("p_value")
                for item in family_decisions
                if isinstance(item, dict)
            }
            if len(p_values) != len(family_decisions):
                raise ValueError("family decisions contain invalid or duplicate members")
            recomputed = apply_benjamini_hochberg(family, p_values)
            expected_family_decisions = [
                {
                    "accepted": item.accepted,
                    "member_id": item.member_id,
                    "p_value": item.p_value,
                    "q_value": item.q_value,
                    "testable": item.testable,
                }
                for item in recomputed
            ]
            if family_decisions != expected_family_decisions:
                errors.append("fdr_family_decisions_recomputation_mismatch")
            selected = next(
                item
                for item in expected_family_decisions
                if isinstance(manifest_fdr, dict) and item["member_id"] == manifest_fdr.get("member_id")
            )
            if fdr.get("decision") != selected:
                errors.append("fdr_selected_decision_mismatch")
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            errors.append(f"fdr_family_decisions_invalid:{exc}")
        decision = fdr.get("decision")
        if isinstance(decision, dict) and summary is not None:
            if decision.get("p_value") != summary.get("primary_p_value"):
                errors.append("fdr_summary_primary_p_value_mismatch")
            p_value = decision.get("p_value")
            q_value = decision.get("q_value")
            testable = decision.get("testable")
            accepted = decision.get("accepted")
            alpha = fdr.get("alpha")
            valid_q = isinstance(q_value, (int, float)) and not isinstance(q_value, bool) and math.isfinite(q_value) and 0.0 <= q_value <= 1.0
            valid_alpha = isinstance(alpha, (int, float)) and not isinstance(alpha, bool) and math.isfinite(alpha) and 0.0 < alpha < 1.0
            if not valid_q or not valid_alpha:
                errors.append("fdr_numeric_contract_invalid")
            else:
                parsed_q = float(cast(int | float, q_value))
                parsed_alpha = float(cast(int | float, alpha))
                expected_testable = isinstance(p_value, (int, float)) and not isinstance(p_value, bool) and math.isfinite(p_value) and 0.0 <= p_value <= 1.0
                if testable is not expected_testable:
                    errors.append("fdr_testable_contract_mismatch")
                expected_accepted = expected_testable and parsed_q <= parsed_alpha
                if accepted is not expected_accepted:
                    errors.append("fdr_accepted_contract_mismatch")
        else:
            errors.append("fdr_decision_invalid")

    if acceptance is not None and summary is not None and fdr is not None:
        decision = fdr.get("decision")
        gates = acceptance.get("gates")
        if isinstance(decision, dict) and isinstance(gates, list):
            gate_map = {
                item.get("name"): item.get("passed")
                for item in gates
                if isinstance(item, dict)
            }
            mean_ic = summary.get("mean_ic")
            direction = summary.get("factor_direction")
            direction_passed = isinstance(mean_ic, (int, float)) and not isinstance(mean_ic, bool) and (
                (direction == "higher_is_better" and mean_ic > 0.0)
                or (direction == "lower_is_better" and mean_ic < 0.0)
            )
            expected_gates = {
                "evidence_eligible": summary.get("evidence_eligible") is True,
                "factor_direction_consistent": direction_passed,
                "fdr_accepted": decision.get("accepted") is True,
                "fdr_testable": decision.get("testable") is True,
                "primary_p_value_available": summary.get("primary_p_value") is not None,
            }
            if gate_map != expected_gates:
                errors.append("acceptance_gate_contract_mismatch")

    row_counts = manifest.get("row_counts") if manifest is not None else None
    csv_results: dict[str, tuple[tuple[str, ...], list[dict[str, str]]]] = {}
    for name, expected_header in (
        ("per_date_ic.csv", PER_DATE_IC_HEADER),
        ("quantile_diagnostics.csv", QUANTILE_DIAGNOSTICS_HEADER),
    ):
        path = package / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            header, rows = _csv_rows(path)
            csv_results[name] = (header, rows)
            if header != expected_header:
                errors.append(f"csv_header_mismatch:{name}")
            if not isinstance(row_counts, dict) or row_counts.get(name) != len(rows):
                errors.append(f"csv_row_count_mismatch:{name}")
        except ValueError as exc:
            errors.append(str(exc))
    if set(csv_results) == {"per_date_ic.csv", "quantile_diagnostics.csv"}:
        ic_dates = [item["as_of_date"] for item in csv_results["per_date_ic.csv"][1]]
        quantile_dates = [item["as_of_date"] for item in csv_results["quantile_diagnostics.csv"][1]]
        if ic_dates != quantile_dates:
            errors.append("per_date_csv_date_set_mismatch")

    if require_ledger and manifest_sha256 is not None and manifest is not None:
        root = Path(ledger_root).resolve() if ledger_root is not None else None
        if root is None:
            for candidate in (package, *package.parents):
                if (candidate / LEDGER_FILE_NAME).is_file():
                    root = candidate
                    break
        if root is None:
            errors.append("campaign_ledger_not_found")
        else:
            try:
                ledger_entry = successful_ledger_entry(root, manifest_sha256)
                ledger_entries = read_campaign_ledger(root)
            except ValueError as exc:
                errors.append(f"campaign_ledger_invalid:{exc}")
            else:
                if ledger_entry is None:
                    errors.append("manifest_not_anchored_in_campaign_ledger")
                else:
                    matching_registrations = [
                        entry
                        for entry in ledger_entries
                        if entry.get("event_type") == "campaign_registered"
                        and entry.get("campaign_id") == manifest.get("campaign_id")
                        and entry.get("registry_sha256")
                        == manifest.get("registry_sha256")
                    ]
                    if len(matching_registrations) != 1:
                        errors.append("ledger_campaign_registration_mismatch")
                    else:
                        registry_relative = matching_registrations[0].get(
                            "package_relative_path"
                        )
                        if not isinstance(registry_relative, str):
                            errors.append("ledger_registry_path_invalid")
                        else:
                            registry_path = (root / registry_relative).resolve()
                            if not registry_path.is_relative_to(root):
                                errors.append("ledger_registry_path_invalid")
                            elif (
                                not registry_path.is_file()
                                or registry_path.is_symlink()
                                or sha256_file(registry_path)
                                != manifest.get("registry_sha256")
                            ):
                                errors.append("ledger_registry_file_mismatch")
                    try:
                        relative = package.resolve().relative_to(root).as_posix()
                    except ValueError:
                        errors.append("package_outside_campaign_ledger_root")
                        relative = None
                    expected_ledger = {
                        "campaign_id": manifest.get("campaign_id"),
                        "cell_id": manifest.get("cell_id"),
                        "environment_sha256": manifest.get("environment_sha256"),
                        "manifest_sha256": manifest_sha256,
                        "package_relative_path": relative,
                        "registry_sha256": manifest.get("registry_sha256"),
                        "state": manifest.get("state"),
                        "supersedes_manifest_sha256": manifest.get(
                            "supersedes_manifest_sha256"
                        ),
                    }
                    for key, expected_value in expected_ledger.items():
                        if ledger_entry.get(key) != expected_value:
                            errors.append(f"ledger_manifest_{key}_mismatch")
                    if isinstance(fdr, dict):
                        decisions = fdr.get("family_decisions")
                        if isinstance(decisions, list):
                            family_p_values = {
                                str(item.get("member_id")): item.get("p_value")
                                for item in decisions
                                if isinstance(item, dict)
                            }
                            if ledger_entry.get("family_p_values") != family_p_values:
                                errors.append("ledger_family_p_values_mismatch")

    return VerificationReport(
        ok=not errors,
        errors=tuple(errors),
        state=state,
        manifest_sha256=manifest_sha256,
        registry_sha256=registry_sha256,
    )


def _write_exclusive(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_directory(source: Path, target: Path, *, attempts: int = 5) -> None:
    for attempt in range(attempts):
        if target.exists():
            raise FileExistsError(f"immutable evidence package already exists: {target}")
        try:
            os.rename(source, target)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("atomic evidence publication exhausted retries")  # pragma: no cover


def _remove_staging_directory(path: Path, *, expected_parent: Path, attempts: int = 5) -> None:
    if path.parent != expected_parent or not path.name.startswith("."):
        raise ValueError("refusing to remove an unverified staging directory")
    for attempt in range(attempts):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


def _remove_unanchored_package(path: Path, *, expected_parent: Path, attempts: int = 5) -> None:
    """Remove only the exact package directory exposed by the current transaction."""

    if path.parent != expected_parent or not path.name:
        raise ValueError("refusing to remove an unverified evidence package")
    for attempt in range(attempts):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


def _observe_provenance_files(
    registry: CampaignRegistry,
    provenance_files: Mapping[str, ProvenanceFileSet],
) -> dict[str, ObservedProvenance]:
    expected = {cell.cell_id for cell in registry.cells}
    supplied = set(provenance_files)
    if supplied != expected:
        raise ValueError(
            "campaign provenance file-set mismatch: "
            f"missing={sorted(expected - supplied)}; extra={sorted(supplied - expected)}"
        )
    observed: dict[str, ObservedProvenance] = {}
    for cell in registry.cells:
        file_set = provenance_files[cell.cell_id]
        if type(file_set) is not ProvenanceFileSet:
            raise TypeError("provenance_files values must be ProvenanceFileSet instances")
        current = file_set.observe()
        if current != cell.registered_provenance:
            raise ValueError(
                f"real source, config, or code bytes do not match cell {cell.cell_id!r}"
            )
        observed[cell.cell_id] = current
    return observed


def _logical_cell_sha256(registry: CampaignRegistry, cell_id: str) -> str:
    cell = registry.cell(cell_id)
    return sha256_bytes(
        canonical_json_bytes(
            {
                "factor_id": cell.factor_id.casefold(),
                "horizon_trading_days": cell.horizon_trading_days,
                "sector_id": cell.sector_id.casefold(),
                "target_name": cell.target_name.casefold(),
            }
        )
    )


def register_campaign(
    output_root: str | Path,
    registry: CampaignRegistry,
    *,
    provenance_files: Mapping[str, ProvenanceFileSet],
) -> Path:
    """Hash real files, publish the registry, and anchor it in the root ledger."""

    root = Path(output_root).resolve()
    _observe_provenance_files(registry, provenance_files)
    target = campaign_registry_path(root, registry)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_entries = read_campaign_ledger(root)
    for entry in existing_entries:
        if (
            entry.get("event_type") == "campaign_registered"
            and entry.get("campaign_id") == registry.campaign_id
            and entry.get("registry_sha256") != registry.registration_sha256
        ):
            raise ValueError(
                "campaign_id is already anchored to a different registry digest"
            )
        if (
            entry.get("event_type") == "campaign_registered"
            and str(entry.get("campaign_id", "")).casefold()
            == registry.campaign_id.casefold()
            and entry.get("campaign_id") != registry.campaign_id
        ):
            raise ValueError("campaign_id collides case-insensitively with an anchored campaign")
    already_anchored = any(
        entry.get("event_type") == "campaign_registered"
        and entry.get("campaign_id") == registry.campaign_id
        and entry.get("registry_sha256") == registry.registration_sha256
        for entry in existing_entries
    )
    lock_path = target.parent / ".campaign_registry.publication.lock"
    lock_descriptor: int | None = None
    staging: Path | None = None
    try:
        lock_descriptor = acquire_advisory_lock(lock_path)
        locked_entries = read_campaign_ledger(root)
        for entry in locked_entries:
            if (
                entry.get("event_type") == "campaign_registered"
                and entry.get("campaign_id") == registry.campaign_id
                and entry.get("registry_sha256") != registry.registration_sha256
            ):
                raise ValueError(
                    "campaign_id is already anchored to a different registry digest"
                )
            if (
                entry.get("event_type") == "campaign_registered"
                and str(entry.get("campaign_id", "")).casefold()
                == registry.campaign_id.casefold()
                and entry.get("campaign_id") != registry.campaign_id
            ):
                raise ValueError(
                    "campaign_id collides case-insensitively with an anchored campaign"
                )
        already_anchored = any(
            entry.get("event_type") == "campaign_registered"
            and entry.get("campaign_id") == registry.campaign_id
            and entry.get("registry_sha256") == registry.registration_sha256
            for entry in locked_entries
        )
        if target.exists():
            existing = load_campaign_registry(target)
            if existing.registration_sha256 != registry.registration_sha256:
                raise FileExistsError(
                    f"campaign ID is already registered with different content: {target}"
                )
        else:
            staging = Path(
                tempfile.mkdtemp(prefix=".campaign_registry.draft-", dir=target.parent)
            )
            staged_file = staging / target.name
            _write_exclusive(staged_file, canonical_json_bytes(registry.to_dict()))
            if load_campaign_registry(staged_file) != registry:
                raise ValueError("staged campaign registry failed exact verification")
            _publish_directory(staged_file, target)
            _remove_staging_directory(staging, expected_parent=target.parent)
            staging = None
        if load_campaign_registry(target) != registry:
            raise RuntimeError("published campaign registry failed exact verification")
        if not already_anchored:
            append_campaign_ledger_event(
                root,
                event_type="campaign_registered",
                attempt_id=f"register:{registry.campaign_id}:{registry.registration_sha256}",
                campaign_id=registry.campaign_id,
                registry_sha256=registry.registration_sha256,
                state="registered",
                package_relative_path=target.relative_to(root).as_posix(),
            )
        return target
    finally:
        if staging is not None and staging.is_dir() and staging.parent == target.parent:
            _remove_staging_directory(staging, expected_parent=target.parent)
        release_advisory_lock(lock_path, lock_descriptor)


def _registered_campaign_or_raise(
    output_root: Path, registry: CampaignRegistry
) -> None:
    registered_path = campaign_registry_path(output_root, registry)
    if not registered_path.is_file() or registered_path.is_symlink():
        raise ValueError("campaign must be immutably registered before evidence publication")
    registered = load_campaign_registry(registered_path)
    if registered.registration_sha256 != registry.registration_sha256 or registered != registry:
        raise ValueError("supplied campaign registry does not match immutable pre-registration")
    registrations = [
        entry
        for entry in read_campaign_ledger(output_root)
        if entry.get("event_type") == "campaign_registered"
        and entry.get("campaign_id") == registry.campaign_id
        and entry.get("registry_sha256") == registry.registration_sha256
    ]
    if len(registrations) != 1:
        raise ValueError("campaign registration is not uniquely anchored in the ledger")


def _validate_config_and_provenance(
    registry: CampaignRegistry,
    *,
    cell_id: str,
    config: FactorValidationConfig,
    provenance_files: ProvenanceFileSet,
) -> ObservedProvenance:
    cell = registry.cell(cell_id)
    if not isinstance(config, FactorValidationConfig):
        raise TypeError("config must be a FactorValidationConfig")
    if config != cell.validation_config:
        raise ValueError("runtime FactorValidationConfig drifted from sealed registration")
    if type(provenance_files) is not ProvenanceFileSet:
        raise TypeError("provenance_files must be a ProvenanceFileSet")
    observed = provenance_files.observe()
    if observed != cell.registered_provenance:
        raise ValueError("runtime source, config, or code bytes drifted from registration")
    return observed


def _active_prior_manifest(
    output_root: Path,
    *,
    logical_cell_sha256: str,
) -> str | None:
    entries = read_campaign_ledger(output_root)
    abandoned = {
        (str(entry.get("campaign_id")), str(entry.get("family_id")))
        for entry in entries
        if entry.get("event_type") == "family_abandoned"
    }
    successes = {
        str(entry["manifest_sha256"]): entry
        for entry in entries
        if entry.get("event_type") == "publication_succeeded"
        and entry.get("logical_cell_sha256") == logical_cell_sha256
        and (str(entry.get("campaign_id")), str(entry.get("family_id")))
        not in abandoned
    }
    superseded = {
        str(entry["supersedes_manifest_sha256"])
        for entry in successes.values()
        if entry.get("supersedes_manifest_sha256") is not None
    }
    active = sorted(set(successes) - superseded)
    if len(active) > 1:
        raise ValueError("ledger contains multiple active publications for the logical cell")
    return active[0] if active else None


def _write_evidence_package(
    output_root: Path,
    registry: CampaignRegistry,
    *,
    cell_id: str,
    result: FactorValidationResult,
    config: FactorValidationConfig,
    family_results: Mapping[str, FactorValidationResult],
    family_p_values: dict[str, float | None],
    provenance_files: ProvenanceFileSet,
    supersedes_manifest_sha256: str | None,
) -> EvidencePackage:
    _registered_campaign_or_raise(output_root, registry)
    cell = registry.cell(cell_id)
    if any(
        entry.get("event_type") == "family_abandoned"
        and entry.get("campaign_id") == registry.campaign_id
        and entry.get("family_id") == cell.fdr_family_id
        for entry in read_campaign_ledger(output_root)
    ):
        raise ValueError("cannot publish into an abandoned evidence family")
    target = evidence_package_path(output_root, registry, cell_id=cell_id)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    relative = target.relative_to(output_root).as_posix()
    logical_sha = _logical_cell_sha256(registry, cell_id)
    environment = EnvironmentProvenance.capture()
    attempt_id = uuid.uuid4().hex
    append_campaign_ledger_event(
        output_root,
        event_type="publication_attempted",
        attempt_id=attempt_id,
        campaign_id=registry.campaign_id,
        registry_sha256=registry.registration_sha256,
        cell_id=cell.cell_id,
        state="draft",
        package_relative_path=relative,
        family_id=cell.fdr_family_id,
        fdr_member_id=cell.fdr_member_id,
        family_p_values=family_p_values,
        logical_cell_sha256=logical_sha,
        supersedes_manifest_sha256=supersedes_manifest_sha256,
        environment_sha256=environment.sha256,
    )
    lock_path = parent / f".{target.name}.publication.lock"
    lock_descriptor: int | None = None
    staging: Path | None = None
    evidence: EvidenceFiles | None = None
    try:
        if any(
            entry.get("event_type") == "publication_succeeded"
            and entry.get("campaign_id") == registry.campaign_id
            and entry.get("cell_id") == cell.cell_id
            for entry in read_campaign_ledger(output_root)
        ):
            raise FileExistsError(
                "immutable evidence publication already exists in the ledger for "
                f"{registry.campaign_id}/{cell.cell_id}"
            )
        active_prior = _active_prior_manifest(
            output_root, logical_cell_sha256=logical_sha
        )
        if active_prior != supersedes_manifest_sha256:
            raise ValueError(
                "supersedes_manifest_sha256 must identify the active ledger publication "
                f"for this logical cell; expected={active_prior!r}"
            )
        observed = _validate_config_and_provenance(
            registry,
            cell_id=cell_id,
            config=config,
            provenance_files=provenance_files,
        )
        lock_descriptor = acquire_advisory_lock(lock_path)
        if target.exists():
            raise FileExistsError(f"immutable evidence package already exists: {target}")
        evidence = build_evidence_files(
            registry,
            cell_id=cell_id,
            result=result,
            family_results=family_results,
            supersedes_manifest_sha256=supersedes_manifest_sha256,
        )
        manifest = _manifest_payload(
            registry,
            cell_id=cell_id,
            result=result,
            evidence=evidence,
            observed_provenance=observed,
            environment=environment,
        )
        # Keep the private draft name short. The public target already has a
        # conservative Windows path check; repeating a long cell ID here can
        # push only the temporary path over legacy MAX_PATH.
        staging = Path(tempfile.mkdtemp(prefix=".draft-", dir=parent))
        for item in evidence.files:
            _write_exclusive(staging / item.name, item.data)
        _write_exclusive(
            staging / MANIFEST_FILE_NAME,
            canonical_json_bytes(manifest),
        )
        staged_report = verify_evidence_package(
            staging,
            expected_registry=registry,
            expected_cell_id=cell_id,
            require_ledger=False,
        )
        if not staged_report.ok:
            raise ValueError(f"staged evidence verification failed: {staged_report.errors}")
        _publish_directory(staging, target)
        staging = None
        unanchored_report = verify_evidence_package(
            target,
            expected_registry=registry,
            expected_cell_id=cell_id,
            require_ledger=False,
        )
        if not unanchored_report.ok or unanchored_report.manifest_sha256 is None:
            raise RuntimeError(
                f"published evidence verification failed: {unanchored_report.errors}"
            )
        append_campaign_ledger_event(
            output_root,
            event_type="publication_succeeded",
            attempt_id=attempt_id,
            campaign_id=registry.campaign_id,
            registry_sha256=registry.registration_sha256,
            cell_id=cell.cell_id,
            state=evidence.acceptance.state,
            manifest_sha256=unanchored_report.manifest_sha256,
            package_relative_path=relative,
            family_id=cell.fdr_family_id,
            fdr_member_id=cell.fdr_member_id,
            family_p_values=family_p_values,
            logical_cell_sha256=logical_sha,
            supersedes_manifest_sha256=supersedes_manifest_sha256,
            environment_sha256=environment.sha256,
        )
        final_report = verify_evidence_package(
            target,
            expected_registry=registry,
            expected_cell_id=cell_id,
            ledger_root=output_root,
        )
        if not final_report.ok or final_report.manifest_sha256 is None:
            raise RuntimeError(f"anchored evidence verification failed: {final_report.errors}")
        return EvidencePackage(
            path=target,
            state=evidence.acceptance.state,
            manifest_sha256=final_report.manifest_sha256,
            registry_sha256=registry.registration_sha256,
        )
    except Exception as exc:
        terminal_exists = any(
            entry.get("attempt_id") == attempt_id
            and entry.get("event_type")
            in {"publication_succeeded", "publication_failed"}
            for entry in read_campaign_ledger(output_root)
        )
        if not terminal_exists:
            if target.is_dir() and not target.is_symlink():
                _remove_unanchored_package(target, expected_parent=parent)
            append_campaign_ledger_event(
                output_root,
                event_type="publication_failed",
                attempt_id=attempt_id,
                campaign_id=registry.campaign_id,
                registry_sha256=registry.registration_sha256,
                cell_id=cell.cell_id,
                state="failed",
                package_relative_path=relative,
                family_id=cell.fdr_family_id,
                fdr_member_id=cell.fdr_member_id,
                family_p_values=family_p_values,
                logical_cell_sha256=logical_sha,
                supersedes_manifest_sha256=supersedes_manifest_sha256,
                environment_sha256=environment.sha256,
                error_code=exc.__class__.__name__,
            )
        raise
    finally:
        if staging is not None and staging.is_dir() and staging.parent == parent:
            _remove_staging_directory(staging, expected_parent=parent)
        release_advisory_lock(lock_path, lock_descriptor)


def write_evidence_package(
    output_root: str | Path,
    registry: CampaignRegistry,
    *,
    cell_id: str,
    observations: Iterable[FactorObservation],
    config: FactorValidationConfig,
    provenance_files: ProvenanceFileSet,
    supersedes_manifest_sha256: str | None = None,
) -> EvidencePackage:
    """Recompute and publish a single-member family from raw observations."""

    cell = registry.cell(cell_id)
    family = registry.family(cell.fdr_family_id)
    if len(family.member_ids) != 1:
        raise ValueError(
            "multi-member FDR families must be published atomically with "
            "write_evidence_family"
        )
    supplied_observations = tuple(observations)
    result = validate_factor(
        supplied_observations,
        factor_id=cell.factor_id,
        config=config,
    )
    return _write_evidence_package(
        Path(output_root).resolve(),
        registry,
        cell_id=cell_id,
        result=result,
        config=config,
        family_results={cell.cell_id: result},
        family_p_values={cell.fdr_member_id: result.primary_p_value},
        provenance_files=provenance_files,
        supersedes_manifest_sha256=supersedes_manifest_sha256,
    )


def write_evidence_family(
    output_root: str | Path,
    registry: CampaignRegistry,
    *,
    family_id: str,
    observations: Mapping[str, Iterable[FactorObservation]],
    configs: Mapping[str, FactorValidationConfig],
    provenance_files: Mapping[str, ProvenanceFileSet],
    supersedes_manifest_sha256: Mapping[str, str | None] | None = None,
) -> tuple[EvidencePackage, ...]:
    """Recompute every member, derive the p-vector, and publish the family."""

    family = registry.family(family_id)
    cells = tuple(
        sorted(
            (cell for cell in registry.cells if cell.fdr_family_id == family.family_id),
            key=lambda cell: cell.cell_id,
        )
    )
    expected = {cell.cell_id for cell in cells}
    for name, supplied in (
        ("observations", observations),
        ("configs", configs),
        ("provenance_files", provenance_files),
    ):
        if set(supplied) != expected:
            raise ValueError(
                f"{name} family membership mismatch: "
                f"missing={sorted(expected - set(supplied))}; "
                f"extra={sorted(set(supplied) - expected)}"
            )
    supersedes = dict(supersedes_manifest_sha256 or {})
    if supersedes and set(supersedes) != expected:
        raise ValueError("supersession mapping must contain the exact family cell IDs")
    if not supersedes:
        supersedes = {cell_id: None for cell_id in expected}
    for cell in cells:
        _validate_config_and_provenance(
            registry,
            cell_id=cell.cell_id,
            config=configs[cell.cell_id],
            provenance_files=provenance_files[cell.cell_id],
        )
    results = {
        cell.cell_id: validate_factor(
            tuple(observations[cell.cell_id]),
            factor_id=cell.factor_id,
            config=configs[cell.cell_id],
        )
        for cell in cells
    }
    member_p_values = {
        cell.fdr_member_id: results[cell.cell_id].primary_p_value for cell in cells
    }
    apply_benjamini_hochberg(family, member_p_values)
    for cell in cells:
        build_evidence_files(
            registry,
            cell_id=cell.cell_id,
            result=results[cell.cell_id],
            family_results=results,
            supersedes_manifest_sha256=supersedes[cell.cell_id],
        )
    root = Path(output_root).resolve()
    packages: list[EvidencePackage] = []
    entries = read_campaign_ledger(root)
    if any(
        entry.get("event_type") == "family_abandoned"
        and entry.get("campaign_id") == registry.campaign_id
        and entry.get("family_id") == family.family_id
        for entry in entries
    ):
        raise ValueError("cannot resume an abandoned evidence family")
    for cell in cells:
        target = evidence_package_path(root, registry, cell_id=cell.cell_id)
        successes = [
            entry
            for entry in entries
            if entry.get("event_type") == "publication_succeeded"
            and entry.get("campaign_id") == registry.campaign_id
            and entry.get("cell_id") == cell.cell_id
        ]
        if len(successes) > 1:
            raise ValueError(f"multiple successful publications for {cell.cell_id}")
        if successes:
            if not target.is_dir() or target.is_symlink():
                raise ValueError(
                    f"ledger-anchored family package is missing or invalid: {cell.cell_id}"
                )
            expected_files = build_evidence_files(
                registry,
                cell_id=cell.cell_id,
                result=results[cell.cell_id],
                family_results=results,
                supersedes_manifest_sha256=supersedes[cell.cell_id],
            ).by_name()
            if any(
                (target / name).read_bytes() != expected_files[name]
                for name in CONTENT_FILE_NAMES
            ):
                raise ValueError(
                    f"existing family evidence differs from recomputed result: {cell.cell_id}"
                )
            report = verify_evidence_package(
                target,
                expected_registry=registry,
                expected_cell_id=cell.cell_id,
                ledger_root=root,
            )
            if not report.ok or report.state is None or report.manifest_sha256 is None:
                raise ValueError(
                    f"existing family evidence failed verification: {cell.cell_id}:"
                    f"{report.errors}"
                )
            packages.append(
                EvidencePackage(
                    path=target,
                    state=report.state,
                    manifest_sha256=report.manifest_sha256,
                    registry_sha256=registry.registration_sha256,
                )
            )
            continue
        if target.exists():
            raise ValueError(f"unanchored family package exists: {cell.cell_id}")
        package = _write_evidence_package(
            root,
            registry,
            cell_id=cell.cell_id,
            result=results[cell.cell_id],
            config=configs[cell.cell_id],
            family_results=results,
            family_p_values=member_p_values,
            provenance_files=provenance_files[cell.cell_id],
            supersedes_manifest_sha256=supersedes[cell.cell_id],
        )
        packages.append(package)
        entries = read_campaign_ledger(root)
    return tuple(packages)
