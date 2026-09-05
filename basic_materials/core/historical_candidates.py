"""Strict validation for the Stage 2B deactivated-company review queue."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import yaml

from basic_materials import SECTOR


class HistoricalCandidatePolicyError(ValueError):
    """Raised when the candidate-census policy is malformed."""


class HistoricalCandidateValidationError(ValueError):
    """Raised when the candidate CSV violates its fail-closed contract."""


@dataclass(frozen=True)
class HistoricalCandidatePolicy:
    policy_version: str
    as_of_date: str
    sector: str
    candidate_file: str
    expected_candidate_rows: int
    expected_min_event_source_urls: int
    default_review_status: str
    include_in_historical_universe: bool
    calibration_eligible: bool
    required_columns: tuple[str, ...]
    allowed_terminal_types: tuple[str, ...]
    allowed_event_reconciliation_statuses: tuple[str, ...]
    expected_cohort_counts: Mapping[str, int]


@dataclass(frozen=True)
class HistoricalCandidate:
    sector: str
    cohort: str
    historical_ticker: str
    provider_symbol: str
    provider_asset_id: str
    company_name: str
    provider_industry: str
    first_quoted_date: str
    provider_last_quoted_date: str
    expected_terminal_type: str
    expected_event_date: str
    expected_counterparty: str
    successor_ticker: str
    candidate_tier: int
    review_status: str
    event_reconciliation_status: str
    pit_membership_status: str
    terminal_value_status: str
    include_in_historical_universe: bool
    calibration_eligible: bool
    discovery_source: str
    event_source_url: str
    selection_rationale: str
    notes: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HistoricalCandidateSummary:
    policy_version: str
    as_of_date: str
    candidate_rows: int
    cohort_counts: Mapping[str, int]
    tier_counts: Mapping[int, int]
    event_source_rows: int
    provider_mapping_blocked_rows: int
    include_in_historical_universe_rows: int
    calibration_eligible_rows: int

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cohort_counts"] = dict(self.cohort_counts)
        payload["tier_counts"] = dict(self.tier_counts)
        payload["passed"] = True
        return payload


_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,11}$")
_PROVIDER_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.]+-[0-9]{6}$")


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalCandidatePolicyError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise HistoricalCandidatePolicyError(
            f"Invalid keys for {context}; missing={sorted(expected - set(value))}, "
            f"unexpected={sorted(set(value) - expected)}"
        )


def _string_sequence(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise HistoricalCandidatePolicyError(f"{context} must be a non-empty list")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result) or len(result) != len(set(result)):
        raise HistoricalCandidatePolicyError(f"{context} must contain unique non-empty values")
    return result


def _count(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoricalCandidatePolicyError(f"{context} must be a non-negative integer")
    return value


def _policy_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise HistoricalCandidatePolicyError(f"{context} must be true or false")
    return value


def validate_historical_candidate_manifest(
    manifest_path: str | Path,
    candidate_path: str | Path,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    candidate_file = Path(candidate_path).resolve()
    if not manifest_file.is_file():
        raise HistoricalCandidateValidationError(f"Candidate manifest not found: {manifest_file}")
    root = _mapping(yaml.safe_load(manifest_file.read_text(encoding="utf-8")), "candidate manifest")
    expected_keys = {
        "manifest_version",
        "artifact_id",
        "path",
        "as_of_date",
        "sha256",
        "row_count",
        "byte_size",
        "state",
        "include_in_historical_universe",
        "calibration_eligible",
        "purpose",
    }
    _exact_keys(root, expected_keys, "candidate manifest")
    if root["manifest_version"] != 1:
        raise HistoricalCandidateValidationError("Unsupported candidate manifest_version")
    if str(root["artifact_id"]) != "basic_materials_deactivated_candidate_census_v1":
        raise HistoricalCandidateValidationError("Unexpected candidate manifest artifact_id")
    resolved_manifest_target = (manifest_file.parent / str(root["path"])).resolve()
    if resolved_manifest_target != candidate_file:
        raise HistoricalCandidateValidationError("Candidate manifest path does not resolve to the selected CSV")
    if not candidate_file.is_file():
        raise HistoricalCandidateValidationError(f"Candidate CSV not found: {candidate_file}")
    payload = candidate_file.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    expected_hash = str(root["sha256"]).strip().lower()
    if actual_hash != expected_hash:
        raise HistoricalCandidateValidationError(
            f"Candidate CSV hash mismatch; expected {expected_hash}, found {actual_hash}"
        )
    expected_size = _count(root["byte_size"], "candidate manifest byte_size")
    if len(payload) != expected_size:
        raise HistoricalCandidateValidationError(
            f"Candidate CSV byte-size mismatch; expected {expected_size}, found {len(payload)}"
        )
    expected_rows = _count(root["row_count"], "candidate manifest row_count")
    with candidate_file.open("r", encoding="utf-8-sig", newline="") as handle:
        actual_rows = sum(1 for _ in csv.DictReader(handle))
    if actual_rows != expected_rows:
        raise HistoricalCandidateValidationError(
            f"Candidate CSV row-count mismatch; expected {expected_rows}, found {actual_rows}"
        )
    try:
        date.fromisoformat(str(root["as_of_date"]))
    except ValueError as exc:
        raise HistoricalCandidateValidationError("Candidate manifest as_of_date must be ISO YYYY-MM-DD") from exc
    if (
        str(root["state"]) != "candidate_unapproved"
        or root["include_in_historical_universe"] is not False
        or root["calibration_eligible"] is not False
    ):
        raise HistoricalCandidateValidationError("Candidate manifest activation state must remain fail-closed")
    return {
        "artifact_id": str(root["artifact_id"]),
        "as_of_date": str(root["as_of_date"]),
        "sha256": actual_hash,
        "row_count": actual_rows,
        "byte_size": len(payload),
        "state": str(root["state"]),
    }


def load_historical_candidate_policy(path: str | Path) -> HistoricalCandidatePolicy:
    policy_path = Path(path).resolve()
    if not policy_path.is_file():
        raise HistoricalCandidatePolicyError(f"Historical candidate policy not found: {policy_path}")
    root = _mapping(yaml.safe_load(policy_path.read_text(encoding="utf-8")), "historical candidate policy")
    expected_keys = {
        "policy_version",
        "as_of_date",
        "sector",
        "candidate_file",
        "expected_candidate_rows",
        "expected_min_event_source_urls",
        "default_review_status",
        "include_in_historical_universe",
        "calibration_eligible",
        "required_columns",
        "allowed_terminal_types",
        "allowed_event_reconciliation_statuses",
        "expected_cohort_counts",
    }
    _exact_keys(root, expected_keys, "historical candidate policy")
    cohort_counts_raw = _mapping(root["expected_cohort_counts"], "expected_cohort_counts")
    cohort_counts = {
        str(cohort): _count(value, f"expected_cohort_counts.{cohort}")
        for cohort, value in cohort_counts_raw.items()
    }
    policy = HistoricalCandidatePolicy(
        policy_version=str(root["policy_version"]).strip(),
        as_of_date=str(root["as_of_date"]).strip(),
        sector=str(root["sector"]).strip(),
        candidate_file=str(root["candidate_file"]).strip(),
        expected_candidate_rows=_count(root["expected_candidate_rows"], "expected_candidate_rows"),
        expected_min_event_source_urls=_count(
            root["expected_min_event_source_urls"], "expected_min_event_source_urls"
        ),
        default_review_status=str(root["default_review_status"]).strip(),
        include_in_historical_universe=_policy_bool(
            root["include_in_historical_universe"], "include_in_historical_universe"
        ),
        calibration_eligible=_policy_bool(root["calibration_eligible"], "calibration_eligible"),
        required_columns=_string_sequence(root["required_columns"], "required_columns"),
        allowed_terminal_types=_string_sequence(root["allowed_terminal_types"], "allowed_terminal_types"),
        allowed_event_reconciliation_statuses=_string_sequence(
            root["allowed_event_reconciliation_statuses"], "allowed_event_reconciliation_statuses"
        ),
        expected_cohort_counts=cohort_counts,
    )
    validate_historical_candidate_policy(policy)
    return policy


def validate_historical_candidate_policy(policy: HistoricalCandidatePolicy) -> None:
    if policy.policy_version != "basic_materials_historical_candidate_policy_v1":
        raise HistoricalCandidatePolicyError(f"Unsupported policy_version: {policy.policy_version}")
    if policy.sector != SECTOR:
        raise HistoricalCandidatePolicyError(f"Policy sector must be {SECTOR!r}")
    try:
        date.fromisoformat(policy.as_of_date)
    except ValueError as exc:
        raise HistoricalCandidatePolicyError("as_of_date must be ISO YYYY-MM-DD") from exc
    if not policy.candidate_file.startswith("system_csvs/") or not policy.candidate_file.endswith(".csv"):
        raise HistoricalCandidatePolicyError("candidate_file must be a package-owned system_csvs CSV")
    if policy.default_review_status != "candidate_unapproved":
        raise HistoricalCandidatePolicyError("Candidate review status must remain fail-closed")
    if policy.include_in_historical_universe or policy.calibration_eligible:
        raise HistoricalCandidatePolicyError("Candidate policy cannot activate historical or calibration flags")
    if sum(policy.expected_cohort_counts.values()) != policy.expected_candidate_rows:
        raise HistoricalCandidatePolicyError("Cohort counts do not sum to expected_candidate_rows")


def _parse_bool_zero(value: str, context: str) -> bool:
    normalized = value.strip()
    if normalized != "0":
        raise HistoricalCandidateValidationError(f"{context} must be 0 in the review queue")
    return False


def _parse_date(value: str, context: str, *, required: bool) -> str:
    normalized = value.strip()
    if not normalized and not required:
        return ""
    if not normalized:
        raise HistoricalCandidateValidationError(f"{context} is required")
    try:
        date.fromisoformat(normalized)
    except ValueError as exc:
        raise HistoricalCandidateValidationError(f"{context} must be ISO YYYY-MM-DD") from exc
    return normalized


def _required(value: str, context: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HistoricalCandidateValidationError(f"{context} is required")
    return normalized


def read_and_validate_historical_candidates(
    path: str | Path,
    policy: HistoricalCandidatePolicy,
) -> list[HistoricalCandidate]:
    source_path = Path(path).resolve()
    if not source_path.is_file():
        raise HistoricalCandidateValidationError(f"Candidate CSV not found: {source_path}")
    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual_columns = tuple(reader.fieldnames or ())
        if actual_columns != policy.required_columns:
            raise HistoricalCandidateValidationError(
                f"Candidate columns do not match policy; expected {policy.required_columns}, got {actual_columns}"
            )
        raw_rows = list(reader)
    if len(raw_rows) != policy.expected_candidate_rows:
        raise HistoricalCandidateValidationError(
            f"Expected {policy.expected_candidate_rows} candidate rows; found {len(raw_rows)}"
        )

    candidates: list[HistoricalCandidate] = []
    historical_tickers: set[str] = set()
    provider_symbols: set[str] = set()
    provider_asset_ids: set[str] = set()
    for row_number, raw in enumerate(raw_rows, start=2):
        context = f"row {row_number}"
        sector = _required(raw["sector"], f"{context}.sector")
        cohort = _required(raw["cohort"], f"{context}.cohort")
        ticker = _required(raw["historical_ticker"], f"{context}.historical_ticker").upper()
        provider_symbol = raw["provider_symbol"].strip().upper()
        provider_asset_id = raw["provider_asset_id"].strip()
        event_status = _required(
            raw["event_reconciliation_status"], f"{context}.event_reconciliation_status"
        )
        if sector != policy.sector:
            raise HistoricalCandidateValidationError(f"{context}.sector must be {policy.sector!r}")
        if cohort not in policy.expected_cohort_counts:
            raise HistoricalCandidateValidationError(f"{context}.cohort is not policy-approved: {cohort}")
        if not _TICKER_PATTERN.fullmatch(ticker):
            raise HistoricalCandidateValidationError(f"{context}.historical_ticker is invalid: {ticker}")
        if ticker in historical_tickers:
            raise HistoricalCandidateValidationError(f"Duplicate historical_ticker: {ticker}")
        historical_tickers.add(ticker)
        if event_status not in policy.allowed_event_reconciliation_statuses:
            raise HistoricalCandidateValidationError(f"{context}.event_reconciliation_status is invalid")

        if event_status == "provider_mapping_blocked":
            if provider_symbol or provider_asset_id:
                raise HistoricalCandidateValidationError(
                    f"{context} provider_mapping_blocked requires blank provider identity"
                )
            first_date = _parse_date(raw["first_quoted_date"], f"{context}.first_quoted_date", required=False)
            last_date = _parse_date(
                raw["provider_last_quoted_date"], f"{context}.provider_last_quoted_date", required=False
            )
        else:
            if not _PROVIDER_SYMBOL_PATTERN.fullmatch(provider_symbol):
                raise HistoricalCandidateValidationError(f"{context}.provider_symbol is invalid: {provider_symbol}")
            if not provider_asset_id.isdigit():
                raise HistoricalCandidateValidationError(f"{context}.provider_asset_id must be numeric")
            if provider_symbol in provider_symbols or provider_asset_id in provider_asset_ids:
                raise HistoricalCandidateValidationError(f"Duplicate provider identity at {context}")
            provider_symbols.add(provider_symbol)
            provider_asset_ids.add(provider_asset_id)
            first_date = _parse_date(raw["first_quoted_date"], f"{context}.first_quoted_date", required=True)
            last_date = _parse_date(
                raw["provider_last_quoted_date"], f"{context}.provider_last_quoted_date", required=True
            )
            if first_date > last_date:
                raise HistoricalCandidateValidationError(f"{context} quote interval is reversed")

        terminal_type = _required(raw["expected_terminal_type"], f"{context}.expected_terminal_type")
        if terminal_type not in policy.allowed_terminal_types:
            raise HistoricalCandidateValidationError(f"{context}.expected_terminal_type is invalid")
        event_date = _parse_date(raw["expected_event_date"], f"{context}.expected_event_date", required=False)
        event_url = raw["event_source_url"].strip()
        if event_url and not event_url.startswith("https://"):
            raise HistoricalCandidateValidationError(f"{context}.event_source_url must use https")
        if event_status in {"source_found_terminal_pending", "provider_mapping_blocked"} and not event_url:
            raise HistoricalCandidateValidationError(f"{context} source status requires event_source_url")
        if event_status == "source_found_terminal_pending" and not event_date:
            if ticker not in {"ZINC"}:
                raise HistoricalCandidateValidationError(f"{context} sourced event requires expected_event_date")

        try:
            candidate_tier = int(_required(raw["candidate_tier"], f"{context}.candidate_tier"))
        except ValueError as exc:
            raise HistoricalCandidateValidationError(f"{context}.candidate_tier must be 1 or 2") from exc
        if candidate_tier not in {1, 2}:
            raise HistoricalCandidateValidationError(f"{context}.candidate_tier must be 1 or 2")
        review_status = _required(raw["review_status"], f"{context}.review_status")
        if review_status != policy.default_review_status:
            raise HistoricalCandidateValidationError(f"{context}.review_status must remain fail-closed")
        pit_status = _required(raw["pit_membership_status"], f"{context}.pit_membership_status")
        terminal_status = _required(raw["terminal_value_status"], f"{context}.terminal_value_status")
        if pit_status != "pending_reconstruction" or terminal_status != "pending_reconciliation":
            raise HistoricalCandidateValidationError(f"{context} historical review gates must remain pending")

        candidates.append(
            HistoricalCandidate(
                sector=sector,
                cohort=cohort,
                historical_ticker=ticker,
                provider_symbol=provider_symbol,
                provider_asset_id=provider_asset_id,
                company_name=_required(raw["company_name"], f"{context}.company_name"),
                provider_industry=_required(raw["provider_industry"], f"{context}.provider_industry"),
                first_quoted_date=first_date,
                provider_last_quoted_date=last_date,
                expected_terminal_type=terminal_type,
                expected_event_date=event_date,
                expected_counterparty=_required(
                    raw["expected_counterparty"], f"{context}.expected_counterparty"
                ),
                successor_ticker=raw["successor_ticker"].strip().upper(),
                candidate_tier=candidate_tier,
                review_status=review_status,
                event_reconciliation_status=event_status,
                pit_membership_status=pit_status,
                terminal_value_status=terminal_status,
                include_in_historical_universe=_parse_bool_zero(
                    raw["include_in_historical_universe"], f"{context}.include_in_historical_universe"
                ),
                calibration_eligible=_parse_bool_zero(
                    raw["calibration_eligible"], f"{context}.calibration_eligible"
                ),
                discovery_source=_required(raw["discovery_source"], f"{context}.discovery_source"),
                event_source_url=event_url,
                selection_rationale=_required(
                    raw["selection_rationale"], f"{context}.selection_rationale"
                ),
                notes=raw["notes"].strip(),
            )
        )

    actual_counts = dict(sorted(Counter(row.cohort for row in candidates).items()))
    expected_counts = dict(sorted(policy.expected_cohort_counts.items()))
    if actual_counts != expected_counts:
        raise HistoricalCandidateValidationError(
            f"Candidate cohort counts differ from policy; expected={expected_counts}, actual={actual_counts}"
        )
    event_source_rows = sum(bool(row.event_source_url) for row in candidates)
    if event_source_rows < policy.expected_min_event_source_urls:
        raise HistoricalCandidateValidationError(
            f"Expected at least {policy.expected_min_event_source_urls} event-source URLs; found {event_source_rows}"
        )
    return candidates


def summarize_historical_candidates(
    candidates: Sequence[HistoricalCandidate],
    policy: HistoricalCandidatePolicy,
) -> HistoricalCandidateSummary:
    return HistoricalCandidateSummary(
        policy_version=policy.policy_version,
        as_of_date=policy.as_of_date,
        candidate_rows=len(candidates),
        cohort_counts=dict(sorted(Counter(row.cohort for row in candidates).items())),
        tier_counts=dict(sorted(Counter(row.candidate_tier for row in candidates).items())),
        event_source_rows=sum(bool(row.event_source_url) for row in candidates),
        provider_mapping_blocked_rows=sum(
            row.event_reconciliation_status == "provider_mapping_blocked" for row in candidates
        ),
        include_in_historical_universe_rows=sum(row.include_in_historical_universe for row in candidates),
        calibration_eligible_rows=sum(row.calibration_eligible for row in candidates),
    )
