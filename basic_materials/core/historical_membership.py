"""Governed Stage 2B historical membership and terminal-event reconciliation."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import assert_database_identity, database_counts, utc_now
from basic_materials.core.historical_candidates import (
    load_historical_candidate_policy,
    read_and_validate_historical_candidates,
    validate_historical_candidate_manifest,
)
from basic_materials.core.input_manifest import file_sha256


class HistoricalReconciliationPolicyError(ValueError):
    """Raised when the Stage 2B policy is invalid."""


class HistoricalReconciliationValidationError(ValueError):
    """Raised when Stage 2B governed inputs or database state are invalid."""


MEMBERSHIP_COLUMNS = (
    "sector",
    "cohort",
    "historical_ticker",
    "provider_symbol",
    "provider_asset_id",
    "sec_cik",
    "company_name",
    "industry",
    "exchange",
    "country",
    "trading_currency",
    "security_type",
    "membership_start_date",
    "membership_end_date",
    "membership_status",
    "membership_basis",
    "membership_source_id",
    "membership_source_url",
    "provider_evidence_label",
    "classification_evidence_label",
    "membership_confidence",
    "review_status",
    "reviewed_on",
    "include_in_historical_universe",
    "current_source_only",
    "survivorship_corrected",
    "calibration_eligible",
    "terminal_reconciliation_required",
    "notes",
)

ALIAS_COLUMNS = (
    "alias_key",
    "alias_ticker",
    "canonical_ticker",
    "security_scope",
    "relationship",
    "valid_from_date",
    "valid_to_date",
    "provider_history_owner",
    "provider_asset_id",
    "load_as_separate_security",
    "source_id",
    "source_url",
    "evidence_label",
    "review_status",
    "reviewed_on",
    "notes",
)

SECURITY_EVENT_COLUMNS = (
    "event_key",
    "canonical_ticker",
    "historical_ticker",
    "provider_symbol",
    "provider_asset_id",
    "event_type",
    "event_date",
    "last_trade_date",
    "old_value",
    "new_value",
    "counterparty",
    "successor_ticker",
    "terminal_type",
    "is_terminal_event",
    "source_id",
    "source_url",
    "source_document_date",
    "evidence_label",
    "reviewed",
    "review_status",
    "reviewed_on",
    "notes",
)

TERMINAL_EVENT_COLUMNS = (
    "event_key",
    "historical_ticker",
    "provider_symbol",
    "provider_asset_id",
    "event_type",
    "economic_event_date",
    "last_trade_date",
    "provider_last_quoted_date",
    "terminal_type",
    "cash_consideration",
    "cash_currency",
    "successor_ticker",
    "successor_share_ratio",
    "successor_security_type",
    "successor_reference_date",
    "successor_price_source_id",
    "successor_provider_symbol",
    "fixed_terminal_value",
    "terminal_value_method",
    "survivorship_complete",
    "calibration_eligible",
    "reconciliation_status",
    "source_id",
    "primary_source_url",
    "secondary_source_url",
    "source_document_date",
    "evidence_label",
    "reviewed",
    "review_status",
    "reviewed_on",
    "notes",
)

CSV_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "historical_membership": MEMBERSHIP_COLUMNS,
    "ticker_aliases": ALIAS_COLUMNS,
    "security_events": SECURITY_EVENT_COLUMNS,
    "terminal_events": TERMINAL_EVENT_COLUMNS,
}

_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,11}$")
_PROVIDER_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.]+-[0-9]{6}$")
_CIK_PATTERN = re.compile(r"^[0-9]{10}$")


@dataclass(frozen=True)
class ReconciliationFileContract:
    name: str
    path: str
    source_id: str
    expected_rows: int
    unique_key: str


@dataclass(frozen=True)
class HistoricalReconciliationPolicy:
    policy_version: str
    as_of_date: str
    sector: str
    review_status: str
    candidate_file: str
    files: Mapping[str, ReconciliationFileContract]
    expected_tickers: tuple[str, ...]
    expected_cohort_counts: Mapping[str, int]
    cohort_calibration_parents: Mapping[str, str]
    allowed_alias_relationships: tuple[str, ...]
    allowed_event_types: tuple[str, ...]
    allowed_terminal_types: tuple[str, ...]
    allowed_terminal_reconciliation_statuses: tuple[str, ...]
    required_flags: Mapping[str, bool]


@dataclass(frozen=True)
class ReconciliationManifestEntry:
    name: str
    path: Path
    source_id: str
    sha256: str
    byte_size: int
    row_count: int
    unique_key: str


@dataclass(frozen=True)
class HistoricalReconciliationManifest:
    manifest_version: int
    artifact_id: str
    policy_version: str
    as_of_date: str
    state: str
    calibration_eligible: bool
    path: Path
    checksum: str
    artifacts: Mapping[str, ReconciliationManifestEntry]


@dataclass(frozen=True)
class HistoricalReconciliationBundle:
    historical_membership: tuple[Mapping[str, str], ...]
    ticker_aliases: tuple[Mapping[str, str], ...]
    security_events: tuple[Mapping[str, str], ...]
    terminal_events: tuple[Mapping[str, str], ...]

    def rows(self, name: str) -> tuple[Mapping[str, str], ...]:
        return getattr(self, name)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "historical_membership_rows": len(self.historical_membership),
            "ticker_alias_rows": len(self.ticker_aliases),
            "security_event_rows": len(self.security_events),
            "terminal_event_rows": len(self.terminal_events),
            "cohort_counts": dict(
                sorted(Counter(row["cohort"] for row in self.historical_membership).items())
            ),
            "terminal_type_counts": dict(
                sorted(Counter(row["terminal_type"] for row in self.terminal_events).items())
            ),
            "survivorship_complete_rows": sum(
                row["survivorship_complete"] == "1" for row in self.terminal_events
            ),
            "calibration_eligible_rows": sum(
                row["calibration_eligible"] == "1" for row in self.terminal_events
            ),
        }


@dataclass(frozen=True)
class HistoricalLoadStats:
    policy_version: str
    manifest_checksum: str
    historical_memberships: int
    aliases: int
    security_events: int
    terminal_events: int
    raw_payloads: int
    cohort_counts: Mapping[str, int]
    calibration_eligible_rows: int

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cohort_counts"] = dict(self.cohort_counts)
        return payload


@dataclass(frozen=True)
class HistoricalValidationIssue:
    severity: str
    issue_code: str
    message: str
    ticker: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class HistoricalValidationReport:
    passed: bool
    validated_at_utc: str
    policy_version: str
    manifest_checksum: str
    expected_counts: Mapping[str, int]
    actual_counts: Mapping[str, int]
    database_counts: Mapping[str, int]
    unresolved_terminal_events: int
    calibration_eligible_rows: int
    issues: tuple[HistoricalValidationIssue, ...]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "validated_at_utc": self.validated_at_utc,
            "policy_version": self.policy_version,
            "manifest_checksum": self.manifest_checksum,
            "expected_counts": dict(self.expected_counts),
            "actual_counts": dict(self.actual_counts),
            "database_counts": dict(self.database_counts),
            "unresolved_terminal_events": self.unresolved_terminal_events,
            "calibration_eligible_rows": self.calibration_eligible_rows,
            "error_count": sum(issue.severity == "error" for issue in self.issues),
            "warning_count": sum(issue.severity == "warning" for issue in self.issues),
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _mapping(value: Any, context: str, error_type: type[ValueError]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise error_type(f"{context} must be a mapping")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    context: str,
    error_type: type[ValueError],
) -> None:
    if set(value) != expected:
        raise error_type(
            f"Invalid keys for {context}; missing={sorted(expected - set(value))}, "
            f"unexpected={sorted(set(value) - expected)}"
        )


def _string_sequence(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise HistoricalReconciliationPolicyError(f"{context} must be a non-empty list")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result) or len(result) != len(set(result)):
        raise HistoricalReconciliationPolicyError(f"{context} must contain unique non-empty values")
    return result


def _nonnegative_int(value: Any, context: str, error_type: type[ValueError]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error_type(f"{context} must be a non-negative integer")
    return value


def _policy_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise HistoricalReconciliationPolicyError(f"{context} must be true or false")
    return value


def _iso_date(value: str, context: str, *, required: bool = True) -> str:
    normalized = value.strip()
    if not normalized and not required:
        return ""
    if not normalized:
        raise HistoricalReconciliationValidationError(f"{context} is required")
    try:
        date.fromisoformat(normalized)
    except ValueError as exc:
        raise HistoricalReconciliationValidationError(f"{context} must be ISO YYYY-MM-DD") from exc
    return normalized


def _required(row: Mapping[str, str], field: str, context: str) -> str:
    value = row[field].strip()
    if not value:
        raise HistoricalReconciliationValidationError(f"{context}.{field} is required")
    return value


def _flag(value: str, expected: bool, context: str) -> None:
    required = "1" if expected else "0"
    if value.strip() != required:
        raise HistoricalReconciliationValidationError(f"{context} must be {required}")


def _positive_number(value: str, context: str, *, required: bool) -> float | None:
    normalized = value.strip()
    if not normalized and not required:
        return None
    if not normalized:
        raise HistoricalReconciliationValidationError(f"{context} is required")
    try:
        parsed = float(normalized)
    except ValueError as exc:
        raise HistoricalReconciliationValidationError(f"{context} must be numeric") from exc
    if parsed <= 0:
        raise HistoricalReconciliationValidationError(f"{context} must be positive")
    return parsed


def _validate_https(value: str, context: str, *, sec_only: bool) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HistoricalReconciliationValidationError(f"{context} must be an HTTPS URL")
    if sec_only and (parsed.hostname or "").lower() not in {"sec.gov", "www.sec.gov"}:
        raise HistoricalReconciliationValidationError(f"{context} must point to SEC.gov")


def load_historical_reconciliation_policy(path: str | Path) -> HistoricalReconciliationPolicy:
    policy_path = Path(path).resolve()
    if not policy_path.is_file():
        raise HistoricalReconciliationPolicyError(f"Historical reconciliation policy not found: {policy_path}")
    root = _mapping(
        yaml.safe_load(policy_path.read_text(encoding="utf-8")),
        "historical reconciliation policy",
        HistoricalReconciliationPolicyError,
    )
    expected_root = {
        "policy_version",
        "as_of_date",
        "sector",
        "review_status",
        "candidate_file",
        "files",
        "expected_tickers",
        "expected_cohort_counts",
        "cohort_calibration_parents",
        "allowed_alias_relationships",
        "allowed_event_types",
        "allowed_terminal_types",
        "allowed_terminal_reconciliation_statuses",
        "required_flags",
    }
    _exact_keys(root, expected_root, "historical reconciliation policy", HistoricalReconciliationPolicyError)

    files_raw = _mapping(root["files"], "files", HistoricalReconciliationPolicyError)
    if set(files_raw) != set(CSV_COLUMNS):
        raise HistoricalReconciliationPolicyError("files must define exactly the four Stage 2B governed CSVs")
    files: dict[str, ReconciliationFileContract] = {}
    for name, raw_value in files_raw.items():
        raw = _mapping(raw_value, f"files.{name}", HistoricalReconciliationPolicyError)
        _exact_keys(
            raw,
            {"path", "source_id", "expected_rows", "unique_key"},
            f"files.{name}",
            HistoricalReconciliationPolicyError,
        )
        contract = ReconciliationFileContract(
            name=name,
            path=str(raw["path"]).strip(),
            source_id=str(raw["source_id"]).strip(),
            expected_rows=_nonnegative_int(
                raw["expected_rows"], f"files.{name}.expected_rows", HistoricalReconciliationPolicyError
            ),
            unique_key=str(raw["unique_key"]).strip(),
        )
        if not contract.path.startswith("system_csvs/") or not contract.path.endswith(".csv"):
            raise HistoricalReconciliationPolicyError(f"files.{name}.path must be package-owned system_csvs")
        if contract.unique_key not in CSV_COLUMNS[name]:
            raise HistoricalReconciliationPolicyError(f"files.{name}.unique_key is not a CSV column")
        if not contract.source_id:
            raise HistoricalReconciliationPolicyError(f"files.{name}.source_id is required")
        files[name] = contract

    cohort_counts_raw = _mapping(
        root["expected_cohort_counts"], "expected_cohort_counts", HistoricalReconciliationPolicyError
    )
    cohort_counts = {
        str(key): _nonnegative_int(
            value,
            f"expected_cohort_counts.{key}",
            HistoricalReconciliationPolicyError,
        )
        for key, value in cohort_counts_raw.items()
    }
    parent_raw = _mapping(
        root["cohort_calibration_parents"],
        "cohort_calibration_parents",
        HistoricalReconciliationPolicyError,
    )
    parents = {str(key): str(value).strip() for key, value in parent_raw.items()}
    flags_raw = _mapping(root["required_flags"], "required_flags", HistoricalReconciliationPolicyError)
    expected_flag_keys = {
        "include_in_historical_universe",
        "current_source_only",
        "survivorship_corrected",
        "membership_calibration_eligible",
        "terminal_reconciliation_required",
        "terminal_survivorship_complete",
        "terminal_calibration_eligible",
        "require_all_terminal_values_resolved_before_promotion",
    }
    _exact_keys(flags_raw, expected_flag_keys, "required_flags", HistoricalReconciliationPolicyError)
    flags = {str(key): _policy_bool(value, f"required_flags.{key}") for key, value in flags_raw.items()}

    policy = HistoricalReconciliationPolicy(
        policy_version=str(root["policy_version"]).strip(),
        as_of_date=str(root["as_of_date"]).strip(),
        sector=str(root["sector"]).strip(),
        review_status=str(root["review_status"]).strip(),
        candidate_file=str(root["candidate_file"]).strip(),
        files=files,
        expected_tickers=_string_sequence(root["expected_tickers"], "expected_tickers"),
        expected_cohort_counts=cohort_counts,
        cohort_calibration_parents=parents,
        allowed_alias_relationships=_string_sequence(
            root["allowed_alias_relationships"], "allowed_alias_relationships"
        ),
        allowed_event_types=_string_sequence(root["allowed_event_types"], "allowed_event_types"),
        allowed_terminal_types=_string_sequence(root["allowed_terminal_types"], "allowed_terminal_types"),
        allowed_terminal_reconciliation_statuses=_string_sequence(
            root["allowed_terminal_reconciliation_statuses"],
            "allowed_terminal_reconciliation_statuses",
        ),
        required_flags=flags,
    )
    _validate_policy(policy)
    return policy


def _validate_policy(policy: HistoricalReconciliationPolicy) -> None:
    if policy.policy_version != "basic_materials_historical_reconciliation_policy_v1":
        raise HistoricalReconciliationPolicyError(f"Unsupported policy version: {policy.policy_version}")
    if policy.sector != SECTOR:
        raise HistoricalReconciliationPolicyError(f"sector must be {SECTOR!r}")
    try:
        date.fromisoformat(policy.as_of_date)
    except ValueError as exc:
        raise HistoricalReconciliationPolicyError("as_of_date must be ISO YYYY-MM-DD") from exc
    if policy.review_status != "approved_stage2b_pilot":
        raise HistoricalReconciliationPolicyError("Stage 2B pilot review status is invalid")
    if policy.candidate_file != "system_csvs/basic_materials_deactivated_candidates.csv":
        raise HistoricalReconciliationPolicyError("candidate_file must reference the immutable census")
    if set(policy.expected_cohort_counts) != set(policy.cohort_calibration_parents):
        raise HistoricalReconciliationPolicyError("Cohort counts and calibration-parent keys must match")
    if sum(policy.expected_cohort_counts.values()) != len(policy.expected_tickers):
        raise HistoricalReconciliationPolicyError("Expected cohort counts must sum to expected tickers")
    if policy.files["historical_membership"].expected_rows != len(policy.expected_tickers):
        raise HistoricalReconciliationPolicyError("Historical membership count must equal expected tickers")
    if policy.files["terminal_events"].expected_rows != len(policy.expected_tickers):
        raise HistoricalReconciliationPolicyError("Each pilot member must have one terminal record")
    required_flags = {
        "include_in_historical_universe": True,
        "current_source_only": False,
        "survivorship_corrected": True,
        "membership_calibration_eligible": False,
        "terminal_reconciliation_required": True,
        "terminal_survivorship_complete": False,
        "terminal_calibration_eligible": False,
        "require_all_terminal_values_resolved_before_promotion": True,
    }
    if dict(policy.required_flags) != required_flags:
        raise HistoricalReconciliationPolicyError("Stage 2B activation flags must remain fail-closed")


def validate_historical_reconciliation_manifest(
    path: str | Path,
    policy: HistoricalReconciliationPolicy,
    package_root: str | Path,
) -> HistoricalReconciliationManifest:
    manifest_path = Path(path).resolve()
    if not manifest_path.is_file():
        raise HistoricalReconciliationValidationError(f"Historical manifest not found: {manifest_path}")
    payload = manifest_path.read_bytes()
    root = _mapping(
        yaml.safe_load(payload.decode("utf-8")),
        "historical reconciliation manifest",
        HistoricalReconciliationValidationError,
    )
    _exact_keys(
        root,
        {
            "manifest_version",
            "artifact_id",
            "policy_version",
            "as_of_date",
            "state",
            "calibration_eligible",
            "artifacts",
        },
        "historical reconciliation manifest",
        HistoricalReconciliationValidationError,
    )
    if root["manifest_version"] != 1:
        raise HistoricalReconciliationValidationError("Unsupported historical manifest_version")
    if str(root["artifact_id"]) != "basic_materials_historical_reconciliation_pilot_v1":
        raise HistoricalReconciliationValidationError("Unexpected historical manifest artifact_id")
    if str(root["policy_version"]) != policy.policy_version or str(root["as_of_date"]) != policy.as_of_date:
        raise HistoricalReconciliationValidationError("Historical manifest does not match policy version/date")
    if str(root["state"]) != "stage2b_pilot_loaded_calibration_blocked":
        raise HistoricalReconciliationValidationError("Historical manifest state must remain calibration blocked")
    if root["calibration_eligible"] is not False:
        raise HistoricalReconciliationValidationError("Historical manifest cannot enable calibration")

    artifacts_raw = _mapping(root["artifacts"], "artifacts", HistoricalReconciliationValidationError)
    if set(artifacts_raw) != set(policy.files):
        raise HistoricalReconciliationValidationError("Historical manifest artifact set differs from policy")
    package = Path(package_root).resolve()
    entries: dict[str, ReconciliationManifestEntry] = {}
    for name, contract in policy.files.items():
        raw = _mapping(artifacts_raw[name], f"artifacts.{name}", HistoricalReconciliationValidationError)
        _exact_keys(
            raw,
            {"path", "source_id", "sha256", "byte_size", "row_count", "unique_key"},
            f"artifacts.{name}",
            HistoricalReconciliationValidationError,
        )
        resolved = (manifest_path.parent / str(raw["path"])).resolve()
        expected_path = (package / contract.path).resolve()
        if resolved != expected_path:
            raise HistoricalReconciliationValidationError(f"artifacts.{name}.path does not match policy")
        if str(raw["source_id"]) != contract.source_id:
            raise HistoricalReconciliationValidationError(f"artifacts.{name}.source_id does not match policy")
        if str(raw["unique_key"]) != contract.unique_key:
            raise HistoricalReconciliationValidationError(f"artifacts.{name}.unique_key does not match policy")
        if not resolved.is_file():
            raise HistoricalReconciliationValidationError(f"Governed CSV not found: {resolved}")
        csv_payload = resolved.read_bytes()
        actual_hash = hashlib.sha256(csv_payload).hexdigest()
        expected_hash = str(raw["sha256"]).lower().strip()
        if actual_hash != expected_hash:
            raise HistoricalReconciliationValidationError(
                f"artifacts.{name} hash mismatch; expected {expected_hash}, found {actual_hash}"
            )
        expected_size = _nonnegative_int(
            raw["byte_size"], f"artifacts.{name}.byte_size", HistoricalReconciliationValidationError
        )
        if len(csv_payload) != expected_size:
            raise HistoricalReconciliationValidationError(f"artifacts.{name} byte-size mismatch")
        expected_rows = _nonnegative_int(
            raw["row_count"], f"artifacts.{name}.row_count", HistoricalReconciliationValidationError
        )
        if expected_rows != contract.expected_rows:
            raise HistoricalReconciliationValidationError(f"artifacts.{name} row count differs from policy")
        with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            actual_rows = list(reader)
        if len(actual_rows) != expected_rows:
            raise HistoricalReconciliationValidationError(f"artifacts.{name} physical row count mismatch")
        keys = [str(row.get(contract.unique_key, "")).strip() for row in actual_rows]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise HistoricalReconciliationValidationError(f"artifacts.{name} unique key is blank or duplicated")
        entries[name] = ReconciliationManifestEntry(
            name=name,
            path=resolved,
            source_id=contract.source_id,
            sha256=actual_hash,
            byte_size=len(csv_payload),
            row_count=len(actual_rows),
            unique_key=contract.unique_key,
        )
    return HistoricalReconciliationManifest(
        manifest_version=1,
        artifact_id=str(root["artifact_id"]),
        policy_version=policy.policy_version,
        as_of_date=policy.as_of_date,
        state=str(root["state"]),
        calibration_eligible=False,
        path=manifest_path,
        checksum=hashlib.sha256(payload).hexdigest(),
        artifacts=entries,
    )


def _read_csv(entry: ReconciliationManifestEntry, expected_columns: tuple[str, ...]) -> list[dict[str, str]]:
    with entry.path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual_columns = tuple(reader.fieldnames or ())
        if actual_columns != expected_columns:
            raise HistoricalReconciliationValidationError(
                f"{entry.name} columns differ from contract; expected={expected_columns}, actual={actual_columns}"
            )
        rows: list[dict[str, str]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw or any(value is None for value in raw.values()):
                raise HistoricalReconciliationValidationError(f"{entry.name} row {row_number} is malformed")
            rows.append({str(key): str(value).strip() for key, value in raw.items()})
    if len(rows) != entry.row_count:
        raise HistoricalReconciliationValidationError(f"{entry.name} row count changed after manifest validation")
    return rows


def _validate_memberships(
    rows: list[dict[str, str]],
    policy: HistoricalReconciliationPolicy,
    candidates_by_ticker: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    by_ticker: dict[str, dict[str, str]] = {}
    provider_symbols: set[str] = set()
    provider_asset_ids: set[str] = set()
    ciks: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        context = f"historical_membership row {row_number}"
        ticker = _required(row, "historical_ticker", context).upper()
        if not _TICKER_PATTERN.fullmatch(ticker) or ticker in by_ticker:
            raise HistoricalReconciliationValidationError(f"{context}.historical_ticker is invalid or duplicated")
        if row["sector"] != policy.sector:
            raise HistoricalReconciliationValidationError(f"{context}.sector must be {policy.sector!r}")
        cohort = _required(row, "cohort", context)
        if cohort not in policy.expected_cohort_counts:
            raise HistoricalReconciliationValidationError(f"{context}.cohort is not policy approved")
        provider_symbol = _required(row, "provider_symbol", context).upper()
        provider_asset_id = _required(row, "provider_asset_id", context)
        cik = _required(row, "sec_cik", context)
        if not _PROVIDER_SYMBOL_PATTERN.fullmatch(provider_symbol):
            raise HistoricalReconciliationValidationError(f"{context}.provider_symbol is invalid")
        if not provider_asset_id.isdigit() or provider_asset_id in provider_asset_ids:
            raise HistoricalReconciliationValidationError(f"{context}.provider_asset_id is invalid or duplicated")
        if provider_symbol in provider_symbols:
            raise HistoricalReconciliationValidationError(f"{context}.provider_symbol is duplicated")
        if not _CIK_PATTERN.fullmatch(cik) or cik in ciks:
            raise HistoricalReconciliationValidationError(f"{context}.sec_cik is invalid or duplicated")
        start = _iso_date(row["membership_start_date"], f"{context}.membership_start_date")
        end = _iso_date(row["membership_end_date"], f"{context}.membership_end_date")
        if end < start:
            raise HistoricalReconciliationValidationError(f"{context} membership interval is reversed")
        if row["membership_status"] != "historical":
            raise HistoricalReconciliationValidationError(f"{context}.membership_status must be historical")
        if row["membership_source_id"] != policy.files["historical_membership"].source_id:
            raise HistoricalReconciliationValidationError(f"{context}.membership_source_id differs from policy")
        _validate_https(row["membership_source_url"], f"{context}.membership_source_url", sec_only=False)
        confidence = _positive_number(row["membership_confidence"], f"{context}.membership_confidence", required=True)
        if confidence is None or confidence > 1:
            raise HistoricalReconciliationValidationError(f"{context}.membership_confidence must be in (0, 1]")
        if row["review_status"] != policy.review_status or row["reviewed_on"] != policy.as_of_date:
            raise HistoricalReconciliationValidationError(f"{context} is not approved under the current pilot")
        _flag(
            row["include_in_historical_universe"],
            policy.required_flags["include_in_historical_universe"],
            f"{context}.include_in_historical_universe",
        )
        _flag(row["current_source_only"], False, f"{context}.current_source_only")
        _flag(row["survivorship_corrected"], True, f"{context}.survivorship_corrected")
        _flag(row["calibration_eligible"], False, f"{context}.calibration_eligible")
        _flag(row["terminal_reconciliation_required"], True, f"{context}.terminal_reconciliation_required")
        for field in (
            "company_name",
            "industry",
            "exchange",
            "country",
            "trading_currency",
            "security_type",
            "membership_basis",
            "provider_evidence_label",
            "classification_evidence_label",
        ):
            _required(row, field, context)

        candidate = candidates_by_ticker.get(ticker)
        if candidate is None:
            raise HistoricalReconciliationValidationError(f"{context} is absent from immutable candidate census")
        candidate_values = {
            "cohort": candidate.cohort,
            "provider_symbol": candidate.provider_symbol,
            "provider_asset_id": candidate.provider_asset_id,
            "company_name": candidate.company_name,
            "industry": candidate.provider_industry,
            "membership_start_date": candidate.first_quoted_date,
            "membership_end_date": candidate.provider_last_quoted_date,
        }
        for field, expected in candidate_values.items():
            if row[field] != expected:
                raise HistoricalReconciliationValidationError(
                    f"{context}.{field} differs from candidate census: {row[field]!r} != {expected!r}"
                )
        by_ticker[ticker] = row
        provider_symbols.add(provider_symbol)
        provider_asset_ids.add(provider_asset_id)
        ciks.add(cik)

    if set(by_ticker) != set(policy.expected_tickers):
        raise HistoricalReconciliationValidationError("Historical membership ticker set differs from policy")
    actual_counts = dict(sorted(Counter(row["cohort"] for row in rows).items()))
    if actual_counts != dict(sorted(policy.expected_cohort_counts.items())):
        raise HistoricalReconciliationValidationError("Historical membership cohort counts differ from policy")
    return by_ticker


def _validate_aliases(
    rows: list[dict[str, str]],
    policy: HistoricalReconciliationPolicy,
) -> dict[str, dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    intervals: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row_number, row in enumerate(rows, start=2):
        context = f"ticker_aliases row {row_number}"
        key = _required(row, "alias_key", context)
        alias = _required(row, "alias_ticker", context).upper()
        canonical = _required(row, "canonical_ticker", context).upper()
        if key in by_key or not _TICKER_PATTERN.fullmatch(alias) or not _TICKER_PATTERN.fullmatch(canonical):
            raise HistoricalReconciliationValidationError(f"{context} has an invalid key or ticker")
        if row["relationship"] not in policy.allowed_alias_relationships:
            raise HistoricalReconciliationValidationError(f"{context}.relationship is not policy approved")
        start = _iso_date(row["valid_from_date"], f"{context}.valid_from_date")
        end = _iso_date(row["valid_to_date"], f"{context}.valid_to_date")
        if end < start:
            raise HistoricalReconciliationValidationError(f"{context} alias interval is reversed")
        if row["source_id"] != policy.files["ticker_aliases"].source_id:
            raise HistoricalReconciliationValidationError(f"{context}.source_id differs from policy")
        _validate_https(row["source_url"], f"{context}.source_url", sec_only=True)
        if row["review_status"] != policy.review_status or row["reviewed_on"] != policy.as_of_date:
            raise HistoricalReconciliationValidationError(f"{context} is not approved under the current pilot")
        _flag(row["load_as_separate_security"], False, f"{context}.load_as_separate_security")
        _required(row, "security_scope", context)
        _required(row, "provider_history_owner", context)
        _required(row, "evidence_label", context)
        provider_asset_id = row["provider_asset_id"]
        if provider_asset_id and not provider_asset_id.isdigit():
            raise HistoricalReconciliationValidationError(f"{context}.provider_asset_id must be numeric")
        if row["security_scope"].startswith("provider_asset:"):
            if row["security_scope"].split(":", 1)[1] != provider_asset_id:
                raise HistoricalReconciliationValidationError(f"{context} provider scope and asset ID differ")
        elif row["security_scope"].startswith("sec_cik:"):
            if not _CIK_PATTERN.fullmatch(row["security_scope"].split(":", 1)[1]):
                raise HistoricalReconciliationValidationError(f"{context} SEC CIK scope is invalid")
        else:
            raise HistoricalReconciliationValidationError(f"{context}.security_scope is invalid")
        intervals[alias].append((start, end, key))
        by_key[key] = row

    for alias, values in intervals.items():
        ordered = sorted(values)
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] <= previous[1]:
                raise HistoricalReconciliationValidationError(
                    f"Alias intervals overlap for {alias}: {previous[2]} and {current[2]}"
                )
    return by_key


def _validate_security_events(
    rows: list[dict[str, str]],
    policy: HistoricalReconciliationPolicy,
    membership_by_ticker: Mapping[str, Mapping[str, str]],
    aliases: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    terminal_tickers: set[str] = set()
    alias_pairs = {(row["alias_ticker"], row["canonical_ticker"]) for row in aliases.values()}
    for row_number, row in enumerate(rows, start=2):
        context = f"security_events row {row_number}"
        key = _required(row, "event_key", context)
        canonical = _required(row, "canonical_ticker", context).upper()
        historical = _required(row, "historical_ticker", context).upper()
        event_type = _required(row, "event_type", context)
        if key in by_key or not _TICKER_PATTERN.fullmatch(canonical) or not _TICKER_PATTERN.fullmatch(historical):
            raise HistoricalReconciliationValidationError(f"{context} has an invalid key or ticker")
        if event_type not in policy.allowed_event_types:
            raise HistoricalReconciliationValidationError(f"{context}.event_type is not policy approved")
        _iso_date(row["event_date"], f"{context}.event_date")
        _iso_date(row["last_trade_date"], f"{context}.last_trade_date")
        _iso_date(row["source_document_date"], f"{context}.source_document_date")
        if row["source_id"] != policy.files["security_events"].source_id:
            raise HistoricalReconciliationValidationError(f"{context}.source_id differs from policy")
        _validate_https(row["source_url"], f"{context}.source_url", sec_only=True)
        _flag(row["reviewed"], True, f"{context}.reviewed")
        if row["review_status"] != policy.review_status or row["reviewed_on"] != policy.as_of_date:
            raise HistoricalReconciliationValidationError(f"{context} is not approved under the current pilot")
        _required(row, "evidence_label", context)
        _required(row, "old_value", context)
        _required(row, "new_value", context)
        _required(row, "counterparty", context)
        provider_symbol = row["provider_symbol"]
        provider_asset_id = row["provider_asset_id"]
        if bool(provider_symbol) != bool(provider_asset_id):
            raise HistoricalReconciliationValidationError(f"{context} provider identity must be complete or blank")
        if provider_symbol and (
            not _PROVIDER_SYMBOL_PATTERN.fullmatch(provider_symbol) or not provider_asset_id.isdigit()
        ):
            raise HistoricalReconciliationValidationError(f"{context} provider identity is invalid")
        if row["successor_ticker"] and not _TICKER_PATTERN.fullmatch(row["successor_ticker"]):
            raise HistoricalReconciliationValidationError(f"{context}.successor_ticker is invalid")

        is_terminal = row["is_terminal_event"] == "1"
        if row["is_terminal_event"] not in {"0", "1"}:
            raise HistoricalReconciliationValidationError(f"{context}.is_terminal_event must be 0 or 1")
        if is_terminal:
            if historical not in membership_by_ticker or canonical != historical:
                raise HistoricalReconciliationValidationError(f"{context} terminal event must match a pilot security")
            member = membership_by_ticker[historical]
            if provider_symbol != member["provider_symbol"] or provider_asset_id != member["provider_asset_id"]:
                raise HistoricalReconciliationValidationError(f"{context} provider identity differs from membership")
            if row["terminal_type"] != event_type or event_type not in policy.allowed_terminal_types:
                raise HistoricalReconciliationValidationError(f"{context} terminal type does not match event type")
            terminal_tickers.add(historical)
        else:
            if event_type != "ticker_change" or row["terminal_type"]:
                raise HistoricalReconciliationValidationError(f"{context} non-terminal record must be a ticker change")
            if (historical, canonical) not in alias_pairs:
                raise HistoricalReconciliationValidationError(f"{context} ticker change has no matching alias")
        by_key[key] = row

    if terminal_tickers != set(policy.expected_tickers):
        raise HistoricalReconciliationValidationError("Security events do not cover every pilot terminal ticker")
    if sum(row["is_terminal_event"] == "1" for row in rows) != len(policy.expected_tickers):
        raise HistoricalReconciliationValidationError("Expected exactly one terminal security event per pilot member")
    return by_key


def _validate_terminal_events(
    rows: list[dict[str, str]],
    policy: HistoricalReconciliationPolicy,
    membership_by_ticker: Mapping[str, Mapping[str, str]],
    events_by_key: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    for row_number, row in enumerate(rows, start=2):
        context = f"terminal_events row {row_number}"
        key = _required(row, "event_key", context)
        ticker = _required(row, "historical_ticker", context).upper()
        if key in by_key or ticker not in membership_by_ticker:
            raise HistoricalReconciliationValidationError(f"{context} has an invalid key or historical ticker")
        event = events_by_key.get(key)
        if event is None or event["is_terminal_event"] != "1":
            raise HistoricalReconciliationValidationError(f"{context} has no matching terminal security event")
        member = membership_by_ticker[ticker]
        if row["provider_symbol"] != member["provider_symbol"] or row["provider_asset_id"] != member["provider_asset_id"]:
            raise HistoricalReconciliationValidationError(f"{context} provider identity differs from membership")
        if row["event_type"] != event["event_type"] or row["terminal_type"] != event["terminal_type"]:
            raise HistoricalReconciliationValidationError(f"{context} event type differs from security event")
        if row["economic_event_date"] != event["event_date"] or row["last_trade_date"] != event["last_trade_date"]:
            raise HistoricalReconciliationValidationError(f"{context} dates differ from security event")
        if row["provider_last_quoted_date"] != member["membership_end_date"]:
            raise HistoricalReconciliationValidationError(f"{context} provider end date differs from membership")
        for field in ("economic_event_date", "last_trade_date", "provider_last_quoted_date", "source_document_date"):
            _iso_date(row[field], f"{context}.{field}")
        if row["terminal_type"] not in policy.allowed_terminal_types:
            raise HistoricalReconciliationValidationError(f"{context}.terminal_type is not policy approved")
        if row["reconciliation_status"] not in policy.allowed_terminal_reconciliation_statuses:
            raise HistoricalReconciliationValidationError(f"{context}.reconciliation_status is invalid")
        if row["source_id"] != policy.files["terminal_events"].source_id:
            raise HistoricalReconciliationValidationError(f"{context}.source_id differs from policy")
        _validate_https(row["primary_source_url"], f"{context}.primary_source_url", sec_only=True)
        if row["secondary_source_url"]:
            _validate_https(row["secondary_source_url"], f"{context}.secondary_source_url", sec_only=True)
        _flag(row["reviewed"], True, f"{context}.reviewed")
        _flag(row["survivorship_complete"], False, f"{context}.survivorship_complete")
        _flag(row["calibration_eligible"], False, f"{context}.calibration_eligible")
        if row["review_status"] != policy.review_status or row["reviewed_on"] != policy.as_of_date:
            raise HistoricalReconciliationValidationError(f"{context} is not approved under the current pilot")
        if row["fixed_terminal_value"]:
            raise HistoricalReconciliationValidationError(f"{context}.fixed_terminal_value must remain blank")
        _required(row, "terminal_value_method", context)
        _required(row, "evidence_label", context)

        terminal_type = row["terminal_type"]
        if terminal_type == "cash_acquisition":
            _positive_number(row["cash_consideration"], f"{context}.cash_consideration", required=True)
            if row["cash_currency"] != "USD" or row["successor_share_ratio"]:
                raise HistoricalReconciliationValidationError(f"{context} cash terms are inconsistent")
            if row["terminal_value_method"] != "fixed_cash_close_consideration":
                raise HistoricalReconciliationValidationError(f"{context} cash terminal method is invalid")
        elif terminal_type in {"stock_acquisition", "stock_merger"}:
            _positive_number(row["successor_share_ratio"], f"{context}.successor_share_ratio", required=True)
            if row["cash_consideration"] or not row["successor_ticker"]:
                raise HistoricalReconciliationValidationError(f"{context} stock terms are inconsistent")
            if row["terminal_value_method"] != "successor_share_conversion_pending_price":
                raise HistoricalReconciliationValidationError(f"{context} stock terminal method is invalid")
            _iso_date(row["successor_reference_date"], f"{context}.successor_reference_date")
            _required(row, "successor_price_source_id", context)
        elif terminal_type == "mixed_acquisition":
            _positive_number(row["cash_consideration"], f"{context}.cash_consideration", required=True)
            _positive_number(row["successor_share_ratio"], f"{context}.successor_share_ratio", required=True)
            if row["cash_currency"] != "USD" or not row["successor_ticker"]:
                raise HistoricalReconciliationValidationError(f"{context} mixed terms are inconsistent")
            if row["terminal_value_method"] != "mixed_prorated_cash_or_stock":
                raise HistoricalReconciliationValidationError(f"{context} mixed terminal method is invalid")
        else:
            if row["cash_consideration"] or row["successor_share_ratio"] or row["successor_ticker"]:
                raise HistoricalReconciliationValidationError(f"{context} bankruptcy terms cannot imply recovery")
            if row["terminal_value_method"] != "bankruptcy_distribution_pending":
                raise HistoricalReconciliationValidationError(f"{context} bankruptcy method is invalid")
        by_key[key] = row

    terminal_event_keys = {
        key for key, row in events_by_key.items() if row["is_terminal_event"] == "1"
    }
    if set(by_key) != terminal_event_keys:
        raise HistoricalReconciliationValidationError("Terminal and security event keys differ")
    if {row["historical_ticker"] for row in rows} != set(policy.expected_tickers):
        raise HistoricalReconciliationValidationError("Terminal events do not cover exact pilot ticker set")
    return by_key


def read_and_validate_historical_reconciliation(
    *,
    policy: HistoricalReconciliationPolicy,
    manifest: HistoricalReconciliationManifest,
    candidate_policy_path: str | Path,
    candidate_manifest_path: str | Path,
    candidate_path: str | Path,
) -> HistoricalReconciliationBundle:
    """Validate all four governed CSVs and reconcile promoted rows to the candidate census."""

    candidate_policy = load_historical_candidate_policy(candidate_policy_path)
    validate_historical_candidate_manifest(candidate_manifest_path, candidate_path)
    candidates = read_and_validate_historical_candidates(candidate_path, candidate_policy)
    candidates_by_ticker = {candidate.historical_ticker: candidate for candidate in candidates}

    raw = {
        name: _read_csv(manifest.artifacts[name], CSV_COLUMNS[name])
        for name in CSV_COLUMNS
    }
    memberships = _validate_memberships(raw["historical_membership"], policy, candidates_by_ticker)
    aliases = _validate_aliases(raw["ticker_aliases"], policy)
    events = _validate_security_events(raw["security_events"], policy, memberships, aliases)
    _validate_terminal_events(raw["terminal_events"], policy, memberships, events)
    return HistoricalReconciliationBundle(
        historical_membership=tuple(raw["historical_membership"]),
        ticker_aliases=tuple(raw["ticker_aliases"]),
        security_events=tuple(raw["security_events"]),
        terminal_events=tuple(raw["terminal_events"]),
    )


def _ensure_sources_registered(conn: sqlite3.Connection, policy: HistoricalReconciliationPolicy) -> None:
    for contract in policy.files.values():
        row = conn.execute(
            "SELECT active FROM source_registry WHERE source_id = ?",
            (contract.source_id,),
        ).fetchone()
        if row is None or int(row["active"]) != 1:
            raise HistoricalReconciliationValidationError(
                f"Active source registry entry is required for {contract.source_id}"
            )


def _resolve_security(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT s.security_id, s.company_id, c.cik
        FROM dim_security AS s
        JOIN dim_company AS c ON c.company_id = s.company_id
        WHERE upper(s.ticker) = upper(?)
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        raise HistoricalReconciliationValidationError(f"Canonical security is not loaded: {ticker}")
    return row


def _upsert_historical_identifier(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    security_id: int | None,
    identifier_type: str,
    identifier_value: str,
    valid_from_date: str,
    valid_to_date: str,
    source_id: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO dim_identifier (
            company_id, security_id, identifier_type, identifier_value, is_primary,
            valid_from_date, valid_to_date, source_id, created_at_utc
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(identifier_type, identifier_value, valid_from_date) DO UPDATE SET
            company_id = excluded.company_id,
            security_id = excluded.security_id,
            is_primary = 1,
            valid_to_date = excluded.valid_to_date,
            source_id = excluded.source_id
        """,
        (
            company_id,
            security_id,
            identifier_type,
            identifier_value,
            valid_from_date,
            valid_to_date,
            source_id,
            now,
        ),
    )


def load_historical_reconciliation(
    conn: sqlite3.Connection,
    *,
    policy: HistoricalReconciliationPolicy,
    manifest: HistoricalReconciliationManifest,
    bundle: HistoricalReconciliationBundle,
) -> HistoricalLoadStats:
    """Load the fully validated Stage 2B bundle in one transaction."""

    assert_database_identity(conn)
    _ensure_sources_registered(conn, policy)
    if conn.in_transaction:
        raise RuntimeError("load_historical_reconciliation requires a clean connection")

    current_rows = conn.execute(
        """
        SELECT t.cohort_id, t.calibration_parent, COUNT(*) AS row_count
        FROM dim_universe_membership AS m
        JOIN dim_basic_materials_taxonomy AS t ON t.security_id = m.security_id
        WHERE m.membership_status = 'current' AND m.membership_end_date IS NULL
        GROUP BY t.cohort_id, t.calibration_parent
        """
    ).fetchall()
    current_parents = {str(row["cohort_id"]): str(row["calibration_parent"]) for row in current_rows}
    if current_parents != dict(policy.cohort_calibration_parents):
        raise HistoricalReconciliationValidationError(
            "Load the validated current universe before Stage 2B; cohort parent contract is incomplete"
        )

    now = utc_now()
    membership_source = policy.files["historical_membership"].source_id
    membership_hash = manifest.artifacts["historical_membership"].sha256
    conn.execute("BEGIN IMMEDIATE")
    try:
        for entry in manifest.artifacts.values():
            conn.execute(
                """
                INSERT INTO raw_source_payloads (
                    snapshot_id, source_id, source_snapshot_date, source_path, sha256,
                    byte_size, row_count, media_type, payload, manifest_version, ingested_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'text/csv', ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    source_path = excluded.source_path,
                    byte_size = excluded.byte_size,
                    row_count = excluded.row_count,
                    payload = excluded.payload,
                    manifest_version = excluded.manifest_version,
                    ingested_at_utc = excluded.ingested_at_utc
                """,
                (
                    f"{entry.source_id}:{entry.sha256}",
                    entry.source_id,
                    policy.as_of_date,
                    str(entry.path),
                    entry.sha256,
                    entry.byte_size,
                    entry.row_count,
                    entry.path.read_bytes(),
                    f"historical_reconciliation_manifest_v{manifest.manifest_version}",
                    now,
                ),
            )

        for row in bundle.historical_membership:
            ticker = row["historical_ticker"]
            collision = conn.execute(
                """
                SELECT c.cik
                FROM dim_security AS s
                JOIN dim_company AS c ON c.company_id = s.company_id
                WHERE upper(s.ticker) = upper(?)
                """,
                (ticker,),
            ).fetchone()
            if collision is not None and str(collision["cik"]) != row["sec_cik"]:
                raise HistoricalReconciliationValidationError(
                    f"Ticker {ticker} is already attached to CIK {collision['cik']}"
                )
            cik_collision = conn.execute(
                "SELECT primary_ticker FROM dim_company WHERE cik = ?",
                (row["sec_cik"],),
            ).fetchone()
            if cik_collision is not None and str(cik_collision["primary_ticker"]).upper() != ticker:
                raise HistoricalReconciliationValidationError(
                    f"CIK {row['sec_cik']} is already attached to {cik_collision['primary_ticker']}"
                )

            conn.execute(
                """
                INSERT INTO dim_company (
                    cik, legal_name, primary_ticker, domicile_country, universe_status,
                    is_active, first_seen_date, last_seen_date, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'historical_terminal_reviewed', 0, ?, ?, ?, ?)
                ON CONFLICT(cik) DO UPDATE SET
                    legal_name = excluded.legal_name,
                    primary_ticker = excluded.primary_ticker,
                    domicile_country = excluded.domicile_country,
                    universe_status = excluded.universe_status,
                    is_active = 0,
                    first_seen_date = excluded.first_seen_date,
                    last_seen_date = excluded.last_seen_date,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    row["sec_cik"],
                    row["company_name"],
                    ticker,
                    row["country"],
                    row["membership_start_date"],
                    row["membership_end_date"],
                    now,
                    now,
                ),
            )
            company_id = int(
                conn.execute("SELECT company_id FROM dim_company WHERE cik = ?", (row["sec_cik"],)).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO dim_security (
                    company_id, ticker, exchange, trading_currency, security_type,
                    listing_status, is_primary_listing, source_id, valid_from_date,
                    valid_to_date, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'inactive', 1, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    company_id = excluded.company_id,
                    exchange = excluded.exchange,
                    trading_currency = excluded.trading_currency,
                    security_type = excluded.security_type,
                    listing_status = 'inactive',
                    is_primary_listing = 1,
                    source_id = excluded.source_id,
                    valid_from_date = excluded.valid_from_date,
                    valid_to_date = excluded.valid_to_date,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    company_id,
                    ticker,
                    row["exchange"],
                    row["trading_currency"],
                    row["security_type"],
                    membership_source,
                    row["membership_start_date"],
                    row["membership_end_date"],
                    now,
                    now,
                ),
            )
            security_id = int(
                conn.execute("SELECT security_id FROM dim_security WHERE upper(ticker) = upper(?)", (ticker,)).fetchone()[0]
            )
            _upsert_historical_identifier(
                conn,
                company_id=company_id,
                security_id=None,
                identifier_type="cik",
                identifier_value=row["sec_cik"],
                valid_from_date=row["membership_start_date"],
                valid_to_date=row["membership_end_date"],
                source_id=membership_source,
                now=now,
            )
            _upsert_historical_identifier(
                conn,
                company_id=company_id,
                security_id=security_id,
                identifier_type="ticker",
                identifier_value=ticker,
                valid_from_date=row["membership_start_date"],
                valid_to_date=row["membership_end_date"],
                source_id=membership_source,
                now=now,
            )
            conn.execute(
                """
                INSERT INTO dim_basic_materials_taxonomy (
                    company_id, security_id, ticker, sector, industry, cohort_id,
                    calibration_group, calibration_parent, lifecycle_state,
                    classification_confidence, source_id, policy_version, input_sha256,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'terminal_review_pending', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id) DO UPDATE SET
                    company_id = excluded.company_id,
                    ticker = excluded.ticker,
                    sector = excluded.sector,
                    industry = excluded.industry,
                    cohort_id = excluded.cohort_id,
                    calibration_group = excluded.calibration_group,
                    calibration_parent = excluded.calibration_parent,
                    lifecycle_state = excluded.lifecycle_state,
                    classification_confidence = excluded.classification_confidence,
                    source_id = excluded.source_id,
                    policy_version = excluded.policy_version,
                    input_sha256 = excluded.input_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    company_id,
                    security_id,
                    ticker,
                    row["sector"],
                    row["industry"],
                    row["cohort"],
                    row["cohort"],
                    policy.cohort_calibration_parents[row["cohort"]],
                    float(row["membership_confidence"]),
                    membership_source,
                    policy.policy_version,
                    membership_hash,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO dim_universe_membership (
                    company_id, security_id, ticker, model_family, cohort_id,
                    membership_start_date, membership_end_date, membership_status,
                    membership_source_id, membership_basis, current_source_only,
                    survivorship_corrected, calibration_eligible, membership_confidence,
                    source_snapshot_date, policy_version, input_sha256, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'historical', ?, ?, 0, 1, 0, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id, membership_source_id, membership_start_date) DO UPDATE SET
                    company_id = excluded.company_id,
                    ticker = excluded.ticker,
                    cohort_id = excluded.cohort_id,
                    membership_end_date = excluded.membership_end_date,
                    membership_status = 'historical',
                    membership_basis = excluded.membership_basis,
                    current_source_only = 0,
                    survivorship_corrected = 1,
                    calibration_eligible = 0,
                    membership_confidence = excluded.membership_confidence,
                    source_snapshot_date = excluded.source_snapshot_date,
                    policy_version = excluded.policy_version,
                    input_sha256 = excluded.input_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    company_id,
                    security_id,
                    ticker,
                    MODEL_FAMILY,
                    row["cohort"],
                    row["membership_start_date"],
                    row["membership_end_date"],
                    membership_source,
                    row["membership_basis"],
                    float(row["membership_confidence"]),
                    policy.as_of_date,
                    policy.policy_version,
                    membership_hash,
                    now,
                    now,
                ),
            )

        for row in bundle.ticker_aliases:
            resolved = _resolve_security(conn, row["canonical_ticker"])
            conn.execute(
                """
                INSERT INTO dim_ticker_alias (
                    alias_key, company_id, security_id, alias_ticker, canonical_ticker,
                    security_scope, relationship, valid_from_date, valid_to_date,
                    provider_history_owner, provider_asset_id, load_as_separate_security,
                    source_id, evidence_json, reviewed, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?, ?)
                ON CONFLICT(alias_key) DO UPDATE SET
                    company_id = excluded.company_id,
                    security_id = excluded.security_id,
                    alias_ticker = excluded.alias_ticker,
                    canonical_ticker = excluded.canonical_ticker,
                    security_scope = excluded.security_scope,
                    relationship = excluded.relationship,
                    valid_from_date = excluded.valid_from_date,
                    valid_to_date = excluded.valid_to_date,
                    provider_history_owner = excluded.provider_history_owner,
                    provider_asset_id = excluded.provider_asset_id,
                    source_id = excluded.source_id,
                    evidence_json = excluded.evidence_json,
                    reviewed = 1,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    row["alias_key"],
                    int(resolved["company_id"]),
                    int(resolved["security_id"]),
                    row["alias_ticker"],
                    row["canonical_ticker"],
                    row["security_scope"],
                    row["relationship"],
                    row["valid_from_date"],
                    row["valid_to_date"],
                    row["provider_history_owner"],
                    row["provider_asset_id"] or None,
                    row["source_id"],
                    json.dumps(row, sort_keys=True),
                    now,
                    now,
                ),
            )

        for row in bundle.security_events:
            resolved = _resolve_security(conn, row["canonical_ticker"])
            conn.execute(
                """
                INSERT INTO fact_security_event (
                    company_id, security_id, event_type, effective_date, old_value,
                    new_value, source_id, evidence_json, reviewed, created_at_utc, event_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    company_id = excluded.company_id,
                    security_id = excluded.security_id,
                    event_type = excluded.event_type,
                    effective_date = excluded.effective_date,
                    old_value = excluded.old_value,
                    new_value = excluded.new_value,
                    source_id = excluded.source_id,
                    evidence_json = excluded.evidence_json,
                    reviewed = 1
                """,
                (
                    int(resolved["company_id"]),
                    int(resolved["security_id"]),
                    row["event_type"],
                    row["event_date"],
                    row["old_value"],
                    row["new_value"],
                    row["source_id"],
                    json.dumps(row, sort_keys=True),
                    now,
                    row["event_key"],
                ),
            )

        for row in bundle.terminal_events:
            resolved = _resolve_security(conn, row["historical_ticker"])
            conn.execute(
                """
                INSERT INTO fact_terminal_event_reconciliation (
                    company_id, security_id, event_date, terminal_event_type,
                    return_treatment, resolved, source_id, evidence_json, created_at_utc, event_key
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    company_id = excluded.company_id,
                    security_id = excluded.security_id,
                    event_date = excluded.event_date,
                    terminal_event_type = excluded.terminal_event_type,
                    return_treatment = excluded.return_treatment,
                    resolved = 0,
                    source_id = excluded.source_id,
                    evidence_json = excluded.evidence_json
                """,
                (
                    int(resolved["company_id"]),
                    int(resolved["security_id"]),
                    row["economic_event_date"],
                    row["terminal_type"],
                    row["terminal_value_method"],
                    row["source_id"],
                    json.dumps(row, sort_keys=True),
                    now,
                    row["event_key"],
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return HistoricalLoadStats(
        policy_version=policy.policy_version,
        manifest_checksum=manifest.checksum,
        historical_memberships=len(bundle.historical_membership),
        aliases=len(bundle.ticker_aliases),
        security_events=len(bundle.security_events),
        terminal_events=len(bundle.terminal_events),
        raw_payloads=len(manifest.artifacts),
        cohort_counts=dict(
            sorted(Counter(row["cohort"] for row in bundle.historical_membership).items())
        ),
        calibration_eligible_rows=0,
    )


def validate_historical_reconciliation_database(
    conn: sqlite3.Connection,
    *,
    policy: HistoricalReconciliationPolicy,
    manifest: HistoricalReconciliationManifest,
    bundle: HistoricalReconciliationBundle,
    expected_current_rows: int,
) -> HistoricalValidationReport:
    """Validate exact Stage 2B database state while preserving the current universe."""

    assert_database_identity(conn)
    issues: list[HistoricalValidationIssue] = []
    expected_counts = {
        "historical_memberships": policy.files["historical_membership"].expected_rows,
        "aliases": policy.files["ticker_aliases"].expected_rows,
        "security_events": policy.files["security_events"].expected_rows,
        "terminal_events": policy.files["terminal_events"].expected_rows,
        "raw_payloads": len(policy.files),
        "current_memberships": expected_current_rows,
    }
    source_ids = {name: contract.source_id for name, contract in policy.files.items()}
    actual_counts = {
        "historical_memberships": int(
            conn.execute(
                "SELECT COUNT(*) FROM dim_universe_membership WHERE membership_source_id = ?",
                (source_ids["historical_membership"],),
            ).fetchone()[0]
        ),
        "aliases": int(
            conn.execute(
                "SELECT COUNT(*) FROM dim_ticker_alias WHERE source_id = ?",
                (source_ids["ticker_aliases"],),
            ).fetchone()[0]
        ),
        "security_events": int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_security_event WHERE source_id = ?",
                (source_ids["security_events"],),
            ).fetchone()[0]
        ),
        "terminal_events": int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_terminal_event_reconciliation WHERE source_id = ?",
                (source_ids["terminal_events"],),
            ).fetchone()[0]
        ),
        "raw_payloads": int(
            conn.execute(
                f"SELECT COUNT(*) FROM raw_source_payloads WHERE source_id IN ({','.join('?' for _ in source_ids)})",
                tuple(source_ids.values()),
            ).fetchone()[0]
        ),
        "current_memberships": int(
            conn.execute(
                """
                SELECT COUNT(*) FROM dim_universe_membership
                WHERE membership_status = 'current' AND membership_end_date IS NULL
                """
            ).fetchone()[0]
        ),
    }
    for name, expected in expected_counts.items():
        actual = actual_counts[name]
        if actual != expected:
            issues.append(
                HistoricalValidationIssue(
                    "error",
                    "HISTORICAL_COUNT_MISMATCH",
                    f"{name} expected {expected} rows and found {actual}",
                )
            )

    membership_rows = conn.execute(
        """
        SELECT ticker, cohort_id, current_source_only, survivorship_corrected, calibration_eligible
        FROM dim_universe_membership
        WHERE membership_source_id = ?
        """,
        (source_ids["historical_membership"],),
    ).fetchall()
    actual_tickers = {str(row["ticker"]).upper() for row in membership_rows}
    if actual_tickers != set(policy.expected_tickers):
        issues.append(
            HistoricalValidationIssue(
                "error",
                "HISTORICAL_TICKER_SET_MISMATCH",
                "Loaded historical ticker set differs from policy",
            )
        )
    unsafe_memberships = [
        row
        for row in membership_rows
        if (
            int(row["current_source_only"]) != 0
            or int(row["survivorship_corrected"]) != 1
            or int(row["calibration_eligible"]) != 0
        )
    ]
    if unsafe_memberships:
        issues.append(
            HistoricalValidationIssue(
                "error",
                "HISTORICAL_ACTIVATION_UNSAFE",
                f"Found {len(unsafe_memberships)} historical memberships with unsafe flags",
            )
        )
    actual_cohorts = dict(sorted(Counter(str(row["cohort_id"]) for row in membership_rows).items()))
    if actual_cohorts != dict(sorted(policy.expected_cohort_counts.items())):
        issues.append(
            HistoricalValidationIssue(
                "error",
                "HISTORICAL_COHORT_MISMATCH",
                "Loaded historical cohort counts differ from policy",
            )
        )

    alias_keys = {
        str(row[0])
        for row in conn.execute(
            "SELECT alias_key FROM dim_ticker_alias WHERE source_id = ?",
            (source_ids["ticker_aliases"],),
        ).fetchall()
    }
    event_keys = {
        str(row[0])
        for row in conn.execute(
            "SELECT event_key FROM fact_security_event WHERE source_id = ?",
            (source_ids["security_events"],),
        ).fetchall()
    }
    terminal_rows = conn.execute(
        """
        SELECT event_key, resolved, evidence_json
        FROM fact_terminal_event_reconciliation
        WHERE source_id = ?
        """,
        (source_ids["terminal_events"],),
    ).fetchall()
    terminal_keys = {str(row["event_key"]) for row in terminal_rows}
    expected_alias_keys = {row["alias_key"] for row in bundle.ticker_aliases}
    expected_event_keys = {row["event_key"] for row in bundle.security_events}
    expected_terminal_keys = {row["event_key"] for row in bundle.terminal_events}
    for label, actual, expected in (
        ("alias", alias_keys, expected_alias_keys),
        ("security event", event_keys, expected_event_keys),
        ("terminal event", terminal_keys, expected_terminal_keys),
    ):
        if actual != expected:
            issues.append(
                HistoricalValidationIssue(
                    "error",
                    "HISTORICAL_KEY_SET_MISMATCH",
                    f"Loaded {label} keys differ from governed CSV",
                )
            )

    unresolved = sum(int(row["resolved"]) == 0 for row in terminal_rows)
    resolved_event_keys = {
        str(row["event_key"]) for row in terminal_rows if int(row["resolved"]) == 1
    }
    latest_calculation_rows = conn.execute(
        """
        SELECT c.event_key, c.resolved
        FROM fact_terminal_return_calculation AS c
        JOIN (
            SELECT event_key, MAX(calculation_asof_date) AS latest_asof
            FROM fact_terminal_return_calculation
            GROUP BY event_key
        ) AS latest
          ON latest.event_key = c.event_key
         AND latest.latest_asof = c.calculation_asof_date
        """
    ).fetchall()
    stage3_resolved_keys = {
        str(row["event_key"]) for row in latest_calculation_rows if int(row["resolved"]) == 1
    }
    terminal_calibration_rows = 0
    for row in terminal_rows:
        evidence = json.loads(str(row["evidence_json"]))
        terminal_calibration_rows += evidence.get("calibration_eligible") == "1"
    membership_calibration_rows = sum(int(row["calibration_eligible"]) for row in membership_rows)
    calibration_rows = membership_calibration_rows + terminal_calibration_rows
    if resolved_event_keys != stage3_resolved_keys:
        issues.append(
            HistoricalValidationIssue(
                "error",
                "TERMINAL_GATE_OPEN_UNEXPECTEDLY",
                "Resolved Stage 2B terminal flags must be backed exactly by latest Stage 3 calculations",
            )
        )
    if calibration_rows:
        issues.append(
            HistoricalValidationIssue(
                "error",
                "CALIBRATION_GATE_OPEN",
                f"Found {calibration_rows} calibration-eligible Stage 2B rows",
            )
        )

    for name, entry in manifest.artifacts.items():
        stored = conn.execute(
            "SELECT payload, row_count FROM raw_source_payloads WHERE source_id = ? AND sha256 = ?",
            (entry.source_id, entry.sha256),
        ).fetchone()
        if stored is None:
            issues.append(
                HistoricalValidationIssue("error", "RAW_STAGE2B_INPUT_MISSING", f"Raw payload is missing for {name}")
            )
        elif (
            hashlib.sha256(bytes(stored["payload"])).hexdigest() != entry.sha256
            or int(stored["row_count"]) != entry.row_count
        ):
            issues.append(
                HistoricalValidationIssue("error", "RAW_STAGE2B_INPUT_MISMATCH", f"Raw payload hash failed for {name}")
            )

    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        issues.append(
            HistoricalValidationIssue(
                "error",
                "FOREIGN_KEY_FAILURE",
                f"Found {len(foreign_key_errors)} foreign-key violations",
            )
        )
    issues.extend(
        (
            HistoricalValidationIssue(
                "warning",
                "TERMINAL_RECONCILIATION_OPEN",
                f"{unresolved} terminal events require Stage 3 quote or distribution reconciliation",
            ),
            HistoricalValidationIssue(
                "warning",
                "CALIBRATION_GATE_CLOSED",
                "Historical pilot rows are loaded for engineering validation but remain excluded from calibration",
            ),
        )
    )
    return HistoricalValidationReport(
        passed=not any(issue.severity == "error" for issue in issues),
        validated_at_utc=utc_now(),
        policy_version=policy.policy_version,
        manifest_checksum=manifest.checksum,
        expected_counts=expected_counts,
        actual_counts=actual_counts,
        database_counts=database_counts(conn),
        unresolved_terminal_events=unresolved,
        calibration_eligible_rows=calibration_rows,
        issues=tuple(issues),
    )


def write_historical_reconciliation_reports(
    report: HistoricalValidationReport,
    *,
    bundle: HistoricalReconciliationBundle,
    report_dir: str | Path,
) -> dict[str, str]:
    """Publish Stage 2B validation evidence atomically."""

    target = Path(report_dir).resolve(strict=False)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    written["summary"] = atomic_write_json(target / "historical_validation_summary.json", report.summary_dict())
    written["issues"] = atomic_write_csv(
        target / "historical_validation_issues.csv",
        (issue.as_dict() for issue in report.issues),
        ("severity", "issue_code", "message", "ticker"),
    )
    written["historical_membership"] = atomic_write_csv(
        target / "historical_membership_snapshot.csv",
        bundle.historical_membership,
        MEMBERSHIP_COLUMNS,
    )
    written["unresolved_terminal_events"] = atomic_write_csv(
        target / "unresolved_terminal_events.csv",
        bundle.terminal_events,
        TERMINAL_EVENT_COLUMNS,
    )
    artifact_rows = {
        name: {"path": str(path), "sha256": file_sha256(path), "byte_size": path.stat().st_size}
        for name, path in written.items()
    }
    written["artifact_manifest"] = atomic_write_json(
        target / "artifact_manifest.json",
        {
            "model_family": MODEL_FAMILY,
            "sector": SECTOR,
            "stage": "stage_2b_historical_reconciliation",
            "generated_at_utc": report.validated_at_utc,
            "artifacts": artifact_rows,
        },
    )
    return {name: str(path) for name, path in written.items()}
