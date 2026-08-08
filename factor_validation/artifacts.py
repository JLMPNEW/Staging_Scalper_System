"""Atomic, immutable publication and verification of factor evidence packages."""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from factor_validation.core import CONTRACT_VERSION, FactorValidationResult
from factor_validation.evidence import (
    CONTENT_FILE_NAMES,
    PER_DATE_IC_HEADER,
    QUANTILE_DIAGNOSTICS_HEADER,
    EvidenceFiles,
    build_evidence_files,
)
from factor_validation.fdr import FDRFamily, apply_benjamini_hochberg
from factor_validation.registry import (
    CampaignRegistry,
    ObservedProvenance,
    canonical_json_bytes,
    sha256_bytes,
)


MANIFEST_FILE_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = "factor_validation_evidence_manifest_v1"
PACKAGE_FILE_NAMES = tuple(sorted((*CONTENT_FILE_NAMES, MANIFEST_FILE_NAME)))


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
    target = (
        root
        / registry.campaign_id
        / cell.sector_id
        / cell.factor_id
        / f"{cell.horizon_trading_days}d"
        / cell.cell_id
    ).resolve()
    if not target.is_relative_to(root):  # pragma: no cover - safe ID invariant
        raise ValueError("evidence package path escapes output_root")
    return target


def campaign_registry_path(output_root: str | Path, registry: CampaignRegistry) -> Path:
    root = Path(output_root).resolve()
    target = (root / registry.campaign_id / "campaign_registry.json").resolve()
    if not target.is_relative_to(root):  # pragma: no cover - safe ID invariant
        raise ValueError("campaign registry path escapes output_root")
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
) -> VerificationReport:
    """Recompute every package seal and cross-check its embedded contracts."""

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
    state = str(manifest.get("state")) if manifest is not None else None
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

    if manifest is not None:
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


def _unlink_lock(path: Path, *, attempts: int = 5) -> None:
    for attempt in range(attempts):
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


def register_campaign(output_root: str | Path, registry: CampaignRegistry) -> Path:
    """Publish the immutable campaign registry before any evidence is evaluated."""

    target = campaign_registry_path(output_root, registry)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = load_campaign_registry(target)
        if existing.registration_sha256 != registry.registration_sha256:
            raise FileExistsError(f"campaign ID is already registered with different content: {target}")
        return target
    lock_path = target.parent / ".campaign_registry.publication.lock"
    lock_descriptor: int | None = None
    staging: Path | None = None
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(lock_descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(lock_descriptor)
        if target.exists():
            existing = load_campaign_registry(target)
            if existing.registration_sha256 != registry.registration_sha256:
                raise FileExistsError(
                    f"campaign ID is already registered with different content: {target}"
                )
            return target
        staging = Path(tempfile.mkdtemp(prefix=".campaign_registry.draft-", dir=target.parent))
        staged_file = staging / target.name
        _write_exclusive(staged_file, canonical_json_bytes(registry.to_dict()))
        if load_campaign_registry(staged_file) != registry:
            raise ValueError("staged campaign registry failed exact verification")
        _publish_directory(staged_file, target)
        _remove_staging_directory(staging, expected_parent=target.parent)
        staging = None
        if load_campaign_registry(target) != registry:
            raise RuntimeError("published campaign registry failed exact verification")
        return target
    finally:
        acquired_lock = lock_descriptor is not None
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if staging is not None and staging.is_dir() and staging.parent == target.parent:
            _remove_staging_directory(staging, expected_parent=target.parent)
        if acquired_lock and lock_path.is_file() and not lock_path.is_symlink():
            _unlink_lock(lock_path)


def write_evidence_package(
    output_root: str | Path,
    registry: CampaignRegistry,
    *,
    cell_id: str,
    result: FactorValidationResult,
    family_p_values: Mapping[str, float | None],
    observed_provenance: ObservedProvenance,
    supersedes_manifest_sha256: str | None = None,
) -> EvidencePackage:
    """Atomically publish a complete package and refuse every overwrite.

    Draft files are built and verified in a sibling directory. The final path
    appears only after one directory rename, so consumers never observe a
    partial package. A new campaign/cell is required to supersede evidence.
    """

    registered_path = campaign_registry_path(output_root, registry)
    if not registered_path.is_file() or registered_path.is_symlink():
        raise ValueError("campaign must be immutably registered before evidence publication")
    registered = load_campaign_registry(registered_path)
    if registered.registration_sha256 != registry.registration_sha256 or registered != registry:
        raise ValueError("supplied campaign registry does not match immutable pre-registration")
    cell = registry.cell(cell_id)
    if observed_provenance != cell.registered_provenance:
        raise ValueError("runtime source, config, or code provenance drifted from pre-registration")
    target = evidence_package_path(output_root, registry, cell_id=cell_id)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / f".{target.name}.publication.lock"
    lock_descriptor: int | None = None
    staging: Path | None = None
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(lock_descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(lock_descriptor)
        if target.exists():
            raise FileExistsError(f"immutable evidence package already exists: {target}")
        evidence = build_evidence_files(
            registry,
            cell_id=cell_id,
            result=result,
            family_p_values=family_p_values,
            supersedes_manifest_sha256=supersedes_manifest_sha256,
        )
        manifest = _manifest_payload(
            registry,
            cell_id=cell_id,
            result=result,
            evidence=evidence,
            observed_provenance=observed_provenance,
        )
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.draft-", dir=parent))
        for item in evidence.files:
            _write_exclusive(staging / item.name, item.data)
        manifest_bytes = canonical_json_bytes(manifest)
        _write_exclusive(staging / MANIFEST_FILE_NAME, manifest_bytes)
        staged_report = verify_evidence_package(
            staging,
            expected_registry=registry,
            expected_cell_id=cell_id,
        )
        if not staged_report.ok:
            raise ValueError(f"staged evidence verification failed: {staged_report.errors}")
        _publish_directory(staging, target)
        staging = None
        final_report = verify_evidence_package(
            target,
            expected_registry=registry,
            expected_cell_id=cell_id,
        )
        if not final_report.ok or final_report.manifest_sha256 is None:
            raise RuntimeError(f"published evidence verification failed: {final_report.errors}")
        return EvidencePackage(
            path=target,
            state=evidence.acceptance.state,
            manifest_sha256=final_report.manifest_sha256,
            registry_sha256=registry.registration_sha256,
        )
    finally:
        acquired_lock = lock_descriptor is not None
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if staging is not None and staging.is_dir() and staging.parent == parent:
            _remove_staging_directory(staging, expected_parent=parent)
        if acquired_lock and lock_path.is_file() and not lock_path.is_symlink():
            _unlink_lock(lock_path)
