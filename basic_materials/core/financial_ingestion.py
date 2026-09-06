"""Immutable SEC ingestion and exception resolution for Basic Materials Stage 4B."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Mapping
import xml.etree.ElementTree as ET

import requests
import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_bytes, atomic_write_csv, atomic_write_json
from basic_materials.core.config import BasicMaterialsConfig
from basic_materials.core.db import assert_database_identity, utc_now


class FinancialIngestionError(RuntimeError):
    """Raised when immutable Stage 4B evidence or semantics are invalid."""


@dataclass(frozen=True)
class FinancialIngestionPolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    history_start_date: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class FilingRecord:
    filing_key: str
    company_id: int
    security_id: int
    ticker: str
    cik: str
    accession_number: str
    form_type: str
    form_family: str
    filing_date: str
    accepted_at: str
    report_date: str
    primary_document: str
    source_id: str
    source_url: str
    payload_sha256: str
    fiscal_year: str
    fiscal_period: str
    is_amendment: int
    filing_payload_kind: str
    is_xbrl: bool
    is_inline_xbrl: bool


@dataclass(frozen=True)
class RawFinancialFact:
    source_observation_id: str
    filing_key: str | None
    company_id: int
    security_id: int
    ticker: str
    cik: str
    accession_number: str
    taxonomy: str
    concept: str
    value_text: str
    numeric_value: float | None
    unit: str
    period_start: str
    period_end: str
    filed_date: str
    accepted_at: str
    form_type: str
    frame: str
    dimensions_json: str
    source_id: str
    source_detail: str
    fiscal_year: str
    fiscal_period: str
    decimals: str
    context_id: str
    payload_sha256: str
    evidence_url: str
    quality_status: str
    quarantine_reason: str


@dataclass(frozen=True)
class ProfileResolution:
    profile_key: str
    company_id: int
    security_id: int
    ticker: str
    role_type: str
    original_profile_status: str
    ingestion_route: str
    resolution_status: str
    effective_annual_form: str
    effective_accounting_basis: str
    effective_taxonomy: str
    effective_reporting_currency: str
    canonical_eligible: int
    evidence_accession: str
    evidence_form: str
    evidence_accepted_at: str
    evidence_url: str
    evidence_sha256: str
    resolution_reason: str
    source_cutoff_date: str
    source_id: str
    policy_version: str
    policy_sha256: str
    resolution_sha256: str
    calibration_eligible: int = 0


@dataclass(frozen=True)
class SecIngestionStats:
    snapshot_key: str
    cache_manifest_sha256: str
    profiles: int
    standard_profiles: int
    exception_profiles: int
    resolutions_by_status: Mapping[str, int]
    filings: int
    raw_facts: int
    usable_raw_facts: int
    quarantined_raw_facts: int
    companyfacts_payloads: int
    fallback_payloads: int
    issues: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["resolutions_by_status"] = dict(self.resolutions_by_status)
        payload["issues"] = [dict(item) for item in self.issues]
        return payload


_EXCEPTION_TICKERS = {
    "AGU",
    "ASM",
    "AUGO",
    "BHP",
    "CGAU",
    "CMCL",
    "MAKO",
    "OGC",
    "PKX",
    "POT",
    "RMIX",
    "SCZM",
    "TII",
    "VMET",
}
_RESOLUTION_STATUSES = {
    "resolved_standard",
    "resolved_inline_xbrl",
    "resolved_xbrl_instance",
    "resolved_metadata_unstructured",
    "resolved_interim_only",
    "missing_required_source",
}
_REGULAR_FAMILIES = {"10-K", "10-Q", "20-F", "40-F"}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_hash(*values: Any) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FinancialIngestionError(f"{context} must be a mapping")
    return value


def _iso_date(value: Any, context: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise FinancialIngestionError(f"{context} must be an ISO date") from exc


def _normalize_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise FinancialIngestionError(f"Invalid SEC acceptance timestamp: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_date(value: str) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _boolish(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _form_family(form: Any) -> str:
    value = str(form or "").strip().upper()
    value = value[:-2] if value.endswith("/A") else value
    if value == "10-KT":
        return "10-K"
    if value == "10-QT":
        return "10-Q"
    return value


def load_financial_ingestion_policy(path: str | Path) -> FinancialIngestionPolicy:
    """Load and fail-closed validate the governed Stage 4B policy."""

    policy_path = Path(path).resolve()
    raw = policy_path.read_bytes()
    root = _as_mapping(yaml.safe_load(raw.decode("utf-8")), "Stage 4B policy")
    required = {
        "policy_version",
        "model_family",
        "sector",
        "snapshot_as_of_date",
        "history_start_date",
        "calibration_eligible",
        "stage4a_contract",
        "immutable_cache",
        "snapshot",
        "sec",
        "normalization",
        "features",
        "fx",
        "exception_resolutions",
        "gates",
    }
    if set(root) != required:
        raise FinancialIngestionError(
            f"Stage 4B policy keys differ: missing={sorted(required - set(root))}, "
            f"unexpected={sorted(set(root) - required)}"
        )
    if root["policy_version"] != "basic_materials_financial_ingestion_v1":
        raise FinancialIngestionError("Unsupported Stage 4B policy version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise FinancialIngestionError("Stage 4B policy identity mismatch")
    if root["calibration_eligible"] is not False:
        raise FinancialIngestionError("Stage 4B must remain calibration-ineligible")
    as_of = _iso_date(root["snapshot_as_of_date"], "snapshot_as_of_date")
    history_start = _iso_date(root["history_start_date"], "history_start_date")
    if history_start >= as_of:
        raise FinancialIngestionError("history_start_date must precede snapshot_as_of_date")
    snapshot = _as_mapping(root["snapshot"], "snapshot")
    expected_counts = (
        int(snapshot["expected_profiles"]),
        int(snapshot["expected_current_profiles"]),
        int(snapshot["expected_historical_profiles"]),
        int(snapshot["expected_standard_profiles"]),
        int(snapshot["expected_exception_profiles"]),
    )
    if expected_counts != (154, 134, 20, 140, 14):
        raise FinancialIngestionError("Stage 4B profile census is not the governed 154/140/14 contract")
    pilots = tuple(str(item) for item in snapshot["representative_pilot_tickers"])
    if pilots != ("NUE", "BHP", "AEM", "RMIX"):
        raise FinancialIngestionError("Representative normalization pilot must remain NUE/BHP/AEM/RMIX")
    exceptions = _as_mapping(root["exception_resolutions"], "exception_resolutions")
    if set(exceptions) != _EXCEPTION_TICKERS:
        raise FinancialIngestionError("Stage 4B exception-resolution ticker set is not exact")
    for ticker, raw_resolution in exceptions.items():
        resolution = _as_mapping(raw_resolution, f"exception_resolutions.{ticker}")
        if resolution.get("resolution_status") not in _RESOLUTION_STATUSES - {"resolved_standard"}:
            raise FinancialIngestionError(f"{ticker} has an invalid exception resolution status")
        if bool(resolution.get("canonical_eligible")) and not resolution.get("taxonomy"):
            raise FinancialIngestionError(f"{ticker} is canonical-eligible without a taxonomy")
        _normalize_timestamp(resolution.get("evidence_accepted_at"))
    gates = _as_mapping(root["gates"], "gates")
    if any(bool(value) for value in gates.values()):
        raise FinancialIngestionError("All Stage 4B promotion, calibration, and integration gates must remain closed")
    return FinancialIngestionPolicy(
        path=policy_path,
        checksum=_sha256_bytes(raw),
        version=str(root["policy_version"]),
        as_of_date=as_of,
        history_start_date=history_start,
        payload=root,
    )


def validate_ingestion_contract_files(
    config: BasicMaterialsConfig,
    policy: FinancialIngestionPolicy,
) -> dict[str, str]:
    """Verify Stage 4B is bound to the frozen Stage 4A files and cache."""

    stage4a = _as_mapping(policy.payload["stage4a_contract"], "stage4a_contract")
    expected = {
        "financial_manifest": (config.paths.financial_data_manifest, stage4a["manifest_file_sha256"]),
        "financial_policy": (config.paths.financial_data_policy, stage4a["policy_sha256"]),
        "reporting_profiles": (config.paths.reporting_profiles_csv, stage4a["reporting_profiles_sha256"]),
        "concept_map": (config.paths.financial_concept_map, stage4a["concept_map_sha256"]),
    }
    actual: dict[str, str] = {}
    for label, (path, checksum) in expected.items():
        if not path.is_file():
            raise FinancialIngestionError(f"Required frozen artifact is missing: {path}")
        digest = _sha256_path(path)
        if digest != str(checksum):
            raise FinancialIngestionError(f"{label} checksum mismatch: expected {checksum}, got {digest}")
        actual[label] = digest
    cache_policy = _as_mapping(policy.payload["immutable_cache"], "immutable_cache")
    cache_root = config.paths.cache_root / str(cache_policy["reporting_cache_relative_path"])
    manifest_path = cache_root / str(cache_policy["reporting_cache_manifest"])
    digest = _sha256_path(manifest_path)
    if digest != str(cache_policy["reporting_cache_manifest_file_sha256"]):
        raise FinancialIngestionError("Stage 4A reporting cache manifest file checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    embedded = str(manifest.get("manifest_sha256") or "")
    projection = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    computed = _sha256_bytes(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if embedded != computed or embedded != str(cache_policy["reporting_cache_manifest_internal_sha256"]):
        raise FinancialIngestionError("Stage 4A reporting cache manifest internal seal mismatch")
    manifest_records = list(manifest.get("records", []))
    for record in manifest_records:
        checksum = str(record.get("sha256") or "")
        if not checksum:
            continue
        path = Path(str(record["cache_path"])).resolve()
        if not path.is_file():
            raise FinancialIngestionError(f"Cached SEC payload checksum mismatch: {path}")
        if record.get("status") == "root_plus_archives" and record.get("kind") == "submissions":
            root_sha = _sha256_path(path)
            archive_shas = [
                str(item["sha256"])
                for item in manifest_records
                if item.get("ticker") == record.get("ticker")
                and item.get("kind") == "submissions_archive"
                and str(item.get("sha256") or "")
            ]
            combined = _sha256_bytes(
                json.dumps(
                    {"root": root_sha, "archives": archive_shas},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if combined != checksum:
                raise FinancialIngestionError(f"Merged SEC submissions checksum mismatch: {path}")
        elif _sha256_path(path) != checksum:
            raise FinancialIngestionError(f"Cached SEC payload checksum mismatch: {path}")
    actual["reporting_cache_manifest_file"] = digest
    actual["reporting_cache_manifest_internal"] = embedded
    return actual


def _parallel_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expand either a submissions root or submissions archive column store."""

    candidate: Any = payload
    filings = payload.get("filings")
    if isinstance(filings, Mapping) and isinstance(filings.get("recent"), Mapping):
        candidate = filings["recent"]
    if not isinstance(candidate, Mapping):
        return []
    list_fields = {str(key): value for key, value in candidate.items() if isinstance(value, list)}
    count = max((len(value) for value in list_fields.values()), default=0)
    return [
        {key: values[index] if index < len(values) else "" for key, values in list_fields.items()}
        for index in range(count)
    ]


def _load_cache_manifest(
    config: BasicMaterialsConfig,
    policy: FinancialIngestionPolicy,
) -> tuple[Path, Mapping[str, Any], dict[tuple[str, str], Mapping[str, Any]]]:
    cache_policy = _as_mapping(policy.payload["immutable_cache"], "immutable_cache")
    cache_root = config.paths.cache_root / str(cache_policy["reporting_cache_relative_path"])
    path = cache_root / str(cache_policy["reporting_cache_manifest"])
    manifest = _as_mapping(json.loads(path.read_text(encoding="utf-8")), "reporting cache manifest")
    records: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in manifest.get("records", []):
        record = _as_mapping(raw, "reporting cache record")
        ticker = str(record.get("ticker") or "").upper()
        kind = str(record.get("kind") or "")
        key = (ticker, kind)
        if kind == "submissions_archive":
            key = (ticker, f"{kind}:{Path(str(record['cache_path'])).name}")
        if key in records:
            raise FinancialIngestionError(f"Duplicate reporting cache record: {key}")
        records[key] = record
    return cache_root, manifest, records


def _issuer_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.*, c.company_id, s.security_id
        FROM dim_issuer_reporting_profile AS p
        JOIN dim_company AS c ON c.company_id = p.company_id
        JOIN dim_security AS s ON s.security_id = p.security_id
        ORDER BY p.role_type, p.ticker
        """
    ).fetchall()
    if len(rows) != 154:
        raise FinancialIngestionError(f"Expected 154 reporting profiles, found {len(rows)}")
    return [dict(row) for row in rows]


def _concept_contract(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[dict[str, Any], ...]]:
    rows = conn.execute(
        """
        SELECT m.canonical_metric, m.period_type, m.sign_policy,
               b.taxonomy, b.concept, b.priority
        FROM bridge_financial_metric_concept AS b
        JOIN dim_financial_metric AS m ON m.metric_id = b.metric_id
        ORDER BY b.taxonomy, b.concept, b.priority, m.canonical_metric
        """
    ).fetchall()
    contract: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        contract.setdefault((str(row["taxonomy"]), str(row["concept"])), []).append(dict(row))
    if len(rows) != 66:
        raise FinancialIngestionError(f"Expected 66 concept links, found {len(rows)}")
    return {key: tuple(value) for key, value in contract.items()}


def _filing_record(
    profile: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    source_url: str,
    payload_sha256: str,
    payload_kind: str,
) -> FilingRecord | None:
    accession = str(row.get("accessionNumber") or "").strip()
    form_type = str(row.get("form") or "").strip().upper()
    form_family = _form_family(form_type)
    accepted_at = _normalize_timestamp(row.get("acceptanceDateTime"))
    if not accession or form_family not in _REGULAR_FAMILIES | {"6-K"} or not accepted_at:
        return None
    accepted_date = _timestamp_date(accepted_at)
    cutoff = date.fromisoformat(str(profile["source_cutoff_date"]))
    if accepted_date is None or accepted_date > cutoff:
        return None
    filing_date = str(row.get("filingDate") or "")[:10]
    report_date = str(row.get("reportDate") or "")[:10]
    if filing_date:
        _iso_date(filing_date, f"{profile['ticker']} filing date")
    if report_date:
        _iso_date(report_date, f"{profile['ticker']} report date")
    filing_key = _stable_hash(profile["company_id"], accession)
    return FilingRecord(
        filing_key=filing_key,
        company_id=int(profile["company_id"]),
        security_id=int(profile["security_id"]),
        ticker=str(profile["ticker"]),
        cik=str(profile["cik"]),
        accession_number=accession,
        form_type=form_type,
        form_family=form_family,
        filing_date=filing_date,
        accepted_at=accepted_at,
        report_date=report_date,
        primary_document=str(row.get("primaryDocument") or ""),
        source_id="sec_submissions",
        source_url=source_url,
        payload_sha256=payload_sha256,
        fiscal_year=str(row.get("fy") or ""),
        fiscal_period=str(row.get("fp") or ""),
        is_amendment=int(form_type.endswith("/A")),
        filing_payload_kind=payload_kind,
        is_xbrl=_boolish(row.get("isXBRL")),
        is_inline_xbrl=_boolish(row.get("isInlineXBRL")),
    )


def _parse_submission_filings(
    profiles: Iterable[Mapping[str, Any]],
    cache_records: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    history_start_date: str,
) -> tuple[dict[tuple[str, str], FilingRecord], list[dict[str, Any]]]:
    """Return every cutoff-valid financial-form accession and cache issues."""

    history_start = date.fromisoformat(history_start_date)
    lookup: dict[tuple[str, str], FilingRecord] = {}
    issues: list[dict[str, Any]] = []
    for profile in profiles:
        ticker = str(profile["ticker"])
        matching = [
            record
            for (record_ticker, kind), record in cache_records.items()
            if record_ticker == ticker and (kind == "submissions" or kind.startswith("submissions_archive:"))
        ]
        if not matching:
            issues.append(
                {
                    "ticker": ticker,
                    "severity": "error",
                    "issue_code": "MISSING_SUBMISSIONS_PAYLOAD",
                    "message": "No immutable submissions payload is available.",
                }
            )
            continue
        for record in matching:
            checksum = str(record.get("sha256") or "")
            if not checksum:
                continue
            path = Path(str(record["cache_path"]))
            payload = _as_mapping(json.loads(path.read_text(encoding="utf-8")), f"{ticker} submissions")
            exact_payload_sha256 = _sha256_path(path)
            payload_kind = "submissions_archive" if "archive" in str(record.get("kind")) else "submissions"
            for row in _parallel_rows(payload):
                filing = _filing_record(
                    profile,
                    row,
                    source_url=str(record.get("url") or ""),
                    payload_sha256=exact_payload_sha256,
                    payload_kind=payload_kind,
                )
                if filing is None:
                    continue
                relevance_date = filing.report_date or filing.filing_date or filing.accepted_at[:10]
                if relevance_date and date.fromisoformat(relevance_date) < history_start:
                    continue
                key = (ticker, filing.accession_number)
                existing = lookup.get(key)
                if existing is not None and existing != filing:
                    raise FinancialIngestionError(f"Conflicting submission metadata for {ticker} {filing.accession_number}")
                lookup[key] = filing
    return lookup, issues


def _companyfacts_raw_rows(
    profile: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    payload_sha256: str,
    evidence_url: str,
    filing_lookup: Mapping[tuple[str, str], FilingRecord],
    concept_contract: Mapping[tuple[str, str], tuple[dict[str, Any], ...]],
    history_start_date: str,
) -> tuple[list[RawFinancialFact], set[str], list[dict[str, Any]]]:
    facts_root = payload.get("facts")
    if not isinstance(facts_root, Mapping):
        return [], set(), [
            {
                "ticker": profile["ticker"],
                "severity": "error",
                "issue_code": "INVALID_COMPANYFACTS_PAYLOAD",
                "message": "Company Facts payload has no facts mapping.",
            }
        ]
    ticker = str(profile["ticker"])
    cutoff = date.fromisoformat(str(profile["source_cutoff_date"]))
    history_start = date.fromisoformat(history_start_date)
    rows: list[RawFinancialFact] = []
    accessions: set[str] = set()
    issues: list[dict[str, Any]] = []
    missing_acceptance = 0
    future_excluded = 0
    for taxonomy, concepts in facts_root.items():
        if not isinstance(concepts, Mapping):
            continue
        taxonomy_name = str(taxonomy)
        for concept, concept_payload in concepts.items():
            if (taxonomy_name, str(concept)) not in concept_contract:
                continue
            if not isinstance(concept_payload, Mapping) or not isinstance(concept_payload.get("units"), Mapping):
                continue
            for unit, observations in concept_payload["units"].items():
                if not isinstance(observations, list):
                    continue
                for observation in observations:
                    if not isinstance(observation, Mapping):
                        continue
                    numeric_value = _safe_float(observation.get("val"))
                    if numeric_value is None:
                        continue
                    accession = str(observation.get("accn") or "").strip()
                    end = str(observation.get("end") or "")[:10]
                    start = str(observation.get("start") or "")[:10]
                    filed = str(observation.get("filed") or "")[:10]
                    if end:
                        try:
                            if date.fromisoformat(end) < history_start:
                                continue
                        except ValueError:
                            end = ""
                    filing = filing_lookup.get((ticker, accession))
                    accepted_at = filing.accepted_at if filing is not None else ""
                    if accepted_at and date.fromisoformat(accepted_at[:10]) > cutoff:
                        future_excluded += 1
                        continue
                    if not accepted_at and filed:
                        try:
                            if date.fromisoformat(filed) > cutoff:
                                future_excluded += 1
                                continue
                        except ValueError:
                            pass
                    reasons: list[str] = []
                    if filing is None:
                        reasons.append("missing_exact_accession_acceptance")
                        missing_acceptance += 1
                    if not end:
                        reasons.append("missing_or_invalid_period_end")
                    form_type = str(observation.get("form") or "").strip().upper()
                    if _form_family(form_type) not in _REGULAR_FAMILIES | {"6-K"}:
                        reasons.append("unsupported_financial_form")
                    value_text = json.dumps(observation.get("val"), ensure_ascii=False, separators=(",", ":"))
                    source_observation_id = _stable_hash(
                        "sec_companyfacts",
                        payload_sha256,
                        ticker,
                        taxonomy_name,
                        str(concept),
                        str(unit),
                        accession,
                        start,
                        end,
                        str(observation.get("frame") or ""),
                        value_text,
                    )
                    rows.append(
                        RawFinancialFact(
                            source_observation_id=source_observation_id,
                            filing_key=filing.filing_key if filing is not None else None,
                            company_id=int(profile["company_id"]),
                            security_id=int(profile["security_id"]),
                            ticker=ticker,
                            cik=str(profile["cik"]),
                            accession_number=accession,
                            taxonomy=taxonomy_name,
                            concept=str(concept),
                            value_text=value_text,
                            numeric_value=numeric_value,
                            unit=str(unit),
                            period_start=start or end,
                            period_end=end,
                            filed_date=filed,
                            accepted_at=accepted_at,
                            form_type=form_type,
                            frame=str(observation.get("frame") or ""),
                            dimensions_json="{}",
                            source_id="sec_companyfacts",
                            source_detail="companyfacts_api",
                            fiscal_year=str(observation.get("fy") or ""),
                            fiscal_period=str(observation.get("fp") or ""),
                            decimals="",
                            context_id="",
                            payload_sha256=payload_sha256,
                            evidence_url=evidence_url,
                            quality_status="quarantined" if reasons else "usable",
                            quarantine_reason=";".join(reasons),
                        )
                    )
                    if accession:
                        accessions.add(accession)
    if missing_acceptance:
        issues.append(
            {
                "ticker": ticker,
                "severity": "warning",
                "issue_code": "RAW_FACT_ACCEPTANCE_MISSING",
                "message": f"{missing_acceptance} mapped facts lack an exact submissions acceptance timestamp.",
            }
        )
    if future_excluded:
        issues.append(
            {
                "ticker": ticker,
                "severity": "info",
                "issue_code": "FUTURE_FACTS_EXCLUDED",
                "message": f"{future_excluded} mapped facts were excluded after the issuer cutoff.",
            }
        )
    return rows, accessions, issues


def _archive_document_url(cik: str, accession: str, document: str) -> str:
    cik_number = str(int(cik))
    accession_directory = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_number}/{accession_directory}/{document}"


def _request_bytes(
    url: str,
    *,
    user_agent: str,
    timeout_seconds: float,
    max_retries: int,
    request_interval_seconds: float,
    retry_statuses: set[int],
) -> bytes:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json,application/xhtml+xml,text/html,application/xml,text/xml,*/*",
        "Accept-Encoding": "gzip, deflate",
    }
    last_error = ""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout_seconds)
            if response.status_code == 200:
                if not response.content:
                    raise FinancialIngestionError(f"SEC returned an empty payload for {url}")
                time.sleep(request_interval_seconds)
                return response.content
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in retry_statuses:
                break
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < max_retries:
            time.sleep(max(request_interval_seconds, 0.25) * (attempt + 1))
    raise FinancialIngestionError(f"Unable to fetch SEC evidence {url}: {last_error}")


def _cached_sec_bytes(
    cache_root: Path,
    *,
    ticker: str,
    accession: str,
    document: str,
    url: str,
    user_agent: str,
    timeout_seconds: float,
    max_retries: int,
    request_interval_seconds: float,
    retry_statuses: set[int],
    cache_only: bool,
) -> tuple[bytes, dict[str, Any]]:
    if Path(document).name != document or document in {"", ".", ".."}:
        raise FinancialIngestionError(f"Unsafe SEC filing document name: {document!r}")
    path = (cache_root / ticker / accession.replace("-", "") / document).resolve()
    try:
        path.relative_to(cache_root.resolve())
    except ValueError as exc:
        raise FinancialIngestionError(f"SEC cache path escapes package cache root: {path}") from exc
    status = "cache_hit"
    if path.is_file():
        payload = path.read_bytes()
    else:
        if cache_only:
            raise FinancialIngestionError(f"Cache-only fallback payload is missing: {path}")
        payload = _request_bytes(
            url,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            request_interval_seconds=request_interval_seconds,
            retry_statuses=retry_statuses,
        )
        atomic_write_bytes(path, payload)
        status = "downloaded"
    return payload, {
        "ticker": ticker,
        "accession": accession,
        "document": document,
        "url": url,
        "cache_path": str(path),
        "sha256": _sha256_bytes(payload),
        "byte_size": len(payload),
        "status": status,
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _namespace_uri(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") and "}" in tag else ""


def _taxonomy_from_uri(uri: str) -> str:
    lowered = uri.lower()
    if "ifrs-full" in lowered or "xbrl.ifrs.org" in lowered:
        return "ifrs-full"
    if "us-gaap" in lowered or "fasb.org/us-gaap" in lowered:
        return "us-gaap"
    return ""


def _attr(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if _local_name(key).lower() == name.lower():
            return str(value)
    return ""


def _context_map(root: ET.Element) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for element in root.iter():
        if _local_name(element.tag).lower() != "context":
            continue
        context_id = _attr(element, "id")
        if not context_id:
            continue
        start = ""
        end = ""
        dimensions: list[dict[str, str]] = []
        for child in element.iter():
            local = _local_name(child.tag).lower()
            text = str(child.text or "").strip()
            if local == "startdate":
                start = text[:10]
            elif local == "enddate":
                end = text[:10]
            elif local == "instant":
                start = end = text[:10]
            elif local in {"explicitmember", "typedmember"}:
                dimensions.append(
                    {
                        "dimension": _attr(child, "dimension"),
                        "member": " ".join(child.itertext()).strip(),
                    }
                )
        contexts[context_id] = {
            "start": start,
            "end": end,
            "dimensions": dimensions,
        }
    return contexts


def _unit_map(root: ET.Element) -> dict[str, str]:
    units: dict[str, str] = {}
    for element in root.iter():
        if _local_name(element.tag).lower() != "unit":
            continue
        unit_id = _attr(element, "id")
        numerator: list[str] = []
        denominator: list[str] = []
        in_denominator = False
        for child in element.iter():
            local = _local_name(child.tag).lower()
            if local == "unitdenominator":
                in_denominator = True
            elif local == "unitnumerator":
                in_denominator = False
            elif local == "measure":
                measure = str(child.text or "").strip().split(":")[-1]
                (denominator if in_denominator else numerator).append(measure)
        value = "*".join(numerator)
        if denominator:
            value = f"{value}/{'*'.join(denominator)}"
        units[unit_id] = value or unit_id
    return units


def _inline_numeric_value(element: ET.Element) -> tuple[str, float | None]:
    raw_text = html.unescape(" ".join("".join(element.itertext()).split()))
    normalized = raw_text.strip()
    if not normalized or normalized.lower() in {"-", "—", "–", "nil", "n/a"}:
        return raw_text, None
    negative_parentheses = normalized.startswith("(") and normalized.endswith(")")
    normalized = normalized.strip("()").replace(",", "").replace("$", "").replace(" ", "")
    normalized = normalized.replace("−", "-").replace("—", "-")
    value = _safe_float(normalized)
    if value is None:
        return raw_text, None
    scale_raw = _attr(element, "scale")
    if scale_raw:
        try:
            value *= 10 ** int(scale_raw)
        except ValueError:
            return raw_text, None
    if negative_parentheses or _attr(element, "sign") == "-":
        value = -abs(value)
    return raw_text, value


def parse_structured_filing_document(
    payload: bytes,
    *,
    profile: Mapping[str, Any],
    filing: FilingRecord,
    source_url: str,
    payload_sha256: str,
    concept_contract: Mapping[tuple[str, str], tuple[dict[str, Any], ...]],
    source_detail: str,
) -> list[RawFinancialFact]:
    """Parse mapped standard-taxonomy facts from inline or instance XBRL."""

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FinancialIngestionError(
            f"{filing.ticker} {filing.accession_number} structured document is not parseable XML"
        ) from exc
    contexts = _context_map(root)
    units = _unit_map(root)
    rows: list[RawFinancialFact] = []
    for element in root.iter():
        local = _local_name(element.tag)
        lowered = local.lower()
        is_inline = lowered == "nonfraction"
        if is_inline:
            name = _attr(element, "name")
            if ":" not in name:
                continue
            taxonomy, concept = name.split(":", 1)
            if taxonomy not in {"us-gaap", "ifrs-full"}:
                continue
        else:
            context_ref = _attr(element, "contextRef")
            unit_ref = _attr(element, "unitRef")
            if not context_ref or not unit_ref:
                continue
            taxonomy = _taxonomy_from_uri(_namespace_uri(element.tag))
            concept = local
            if not taxonomy:
                continue
        if (taxonomy, concept) not in concept_contract:
            continue
        if _attr(element, "nil").lower() in {"true", "1"}:
            continue
        context_id = _attr(element, "contextRef")
        context = contexts.get(context_id, {"start": "", "end": "", "dimensions": []})
        unit_ref = _attr(element, "unitRef")
        unit = units.get(unit_ref, unit_ref)
        value_text, numeric_value = _inline_numeric_value(element)
        reasons: list[str] = []
        if numeric_value is None:
            reasons.append("non_numeric_structured_fact")
        if not context.get("end"):
            reasons.append("missing_or_invalid_period_end")
        dimensions = list(context.get("dimensions") or [])
        if dimensions:
            reasons.append("dimensioned_context")
        expected_period = str(concept_contract[(taxonomy, concept)][0]["period_type"])
        actual_period = "duration" if context.get("start") and context.get("start") != context.get("end") else "instant"
        if actual_period != expected_period:
            reasons.append(f"period_type_mismatch:{actual_period}")
        dimensions_json = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
        source_observation_id = _stable_hash(
            "sec_inline_xbrl_fallback",
            payload_sha256,
            filing.ticker,
            taxonomy,
            concept,
            unit,
            filing.accession_number,
            context.get("start"),
            context.get("end"),
            context_id,
            value_text,
        )
        fiscal_year = filing.fiscal_year or (filing.report_date[:4] if filing.report_date else "")
        fiscal_period = filing.fiscal_period or ("FY" if filing.form_family in {"10-K", "20-F", "40-F"} else "")
        rows.append(
            RawFinancialFact(
                source_observation_id=source_observation_id,
                filing_key=filing.filing_key,
                company_id=filing.company_id,
                security_id=filing.security_id,
                ticker=filing.ticker,
                cik=filing.cik,
                accession_number=filing.accession_number,
                taxonomy=taxonomy,
                concept=concept,
                value_text=value_text,
                numeric_value=numeric_value,
                unit=unit,
                period_start=str(context.get("start") or context.get("end") or ""),
                period_end=str(context.get("end") or ""),
                filed_date=filing.filing_date,
                accepted_at=filing.accepted_at,
                form_type=filing.form_type,
                frame="",
                dimensions_json=dimensions_json,
                source_id="sec_inline_xbrl_fallback",
                source_detail=source_detail,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                decimals=_attr(element, "decimals"),
                context_id=context_id,
                payload_sha256=payload_sha256,
                evidence_url=source_url,
                quality_status="quarantined" if reasons else "usable",
                quarantine_reason=";".join(reasons),
            )
        )
    return rows


def _exception_evidence(
    *,
    config: BasicMaterialsConfig,
    policy: FinancialIngestionPolicy,
    profiles: Mapping[str, Mapping[str, Any]],
    filing_lookup: Mapping[tuple[str, str], FilingRecord],
    concept_contract: Mapping[tuple[str, str], tuple[dict[str, Any], ...]],
    cache_only: bool,
    allow_partial: bool,
) -> tuple[list[RawFinancialFact], dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    cache_policy = _as_mapping(policy.payload["immutable_cache"], "immutable_cache")
    cache_root = config.paths.cache_root / str(cache_policy["ingestion_cache_relative_path"])
    sec_policy = _as_mapping(policy.payload["sec"], "sec")
    retry_statuses = {int(value) for value in sec_policy["request_retry_statuses"]}
    exception_policy = _as_mapping(policy.payload["exception_resolutions"], "exception_resolutions")
    raw_facts: list[RawFinancialFact] = []
    evidence: dict[str, dict[str, Any]] = {}
    cache_records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for ticker in sorted(exception_policy):
        resolution = _as_mapping(exception_policy[ticker], f"exception_resolutions.{ticker}")
        profile = profiles[ticker]
        accession = str(resolution["evidence_accession"])
        filing = filing_lookup.get((ticker, accession))
        if filing is None:
            message = f"Cutoff-valid filing metadata is missing for governed evidence accession {accession}."
            issues.append(
                {
                    "ticker": ticker,
                    "severity": "error",
                    "issue_code": "FALLBACK_FILING_METADATA_MISSING",
                    "message": message,
                }
            )
            if not allow_partial:
                raise FinancialIngestionError(f"{ticker}: {message}")
            continue
        route = str(resolution["route"])
        if route == "domestic_interim_companyfacts":
            evidence[ticker] = {
                "url": config.sec_fundamentals.companyfacts_url_template.format(cik=profile["cik"]),
                "sha256": str(profile.get("companyfacts_sha256") or ""),
                "structured_fact_count": 0,
            }
            continue
        document = str(resolution["evidence_document"])
        document_url = _archive_document_url(str(profile["cik"]), accession, document)
        index_url = _archive_document_url(str(profile["cik"]), accession, "index.json")
        try:
            index_payload, index_record = _cached_sec_bytes(
                cache_root,
                ticker=ticker,
                accession=accession,
                document="index.json",
                url=index_url,
                user_agent=config.sec_fundamentals.user_agent,
                timeout_seconds=config.sec_fundamentals.timeout_seconds,
                max_retries=config.sec_fundamentals.max_retries,
                request_interval_seconds=config.sec_fundamentals.request_interval_seconds,
                retry_statuses=retry_statuses,
                cache_only=cache_only,
            )
            index_json = _as_mapping(json.loads(index_payload.decode("utf-8")), f"{ticker} filing index")
            package_names = {
                str(item.get("name") or "")
                for item in _as_mapping(index_json.get("directory"), f"{ticker} filing directory").get("item", [])
                if isinstance(item, Mapping)
            }
            if document not in package_names:
                raise FinancialIngestionError(f"{ticker} evidence document {document} is absent from SEC index")
            payload, document_record = _cached_sec_bytes(
                cache_root,
                ticker=ticker,
                accession=accession,
                document=document,
                url=document_url,
                user_agent=config.sec_fundamentals.user_agent,
                timeout_seconds=config.sec_fundamentals.timeout_seconds,
                max_retries=config.sec_fundamentals.max_retries,
                request_interval_seconds=config.sec_fundamentals.request_interval_seconds,
                retry_statuses=retry_statuses,
                cache_only=cache_only,
            )
            cache_records.extend((index_record, document_record))
            parsed: list[RawFinancialFact] = []
            if route in {"inline_xbrl_fallback", "filing_package_xbrl_instance"}:
                source_detail = (
                    "filing_package_xbrl_instance"
                    if route == "filing_package_xbrl_instance"
                    else "inline_xbrl_fallback"
                )
                parsed = parse_structured_filing_document(
                    payload,
                    profile=profile,
                    filing=filing,
                    source_url=document_url,
                    payload_sha256=document_record["sha256"],
                    concept_contract=concept_contract,
                    source_detail=source_detail,
                )
                raw_facts.extend(parsed)
                usable_count = sum(item.quality_status == "usable" for item in parsed)
                if usable_count == 0:
                    issues.append(
                        {
                            "ticker": ticker,
                            "severity": "error",
                            "issue_code": "FALLBACK_NO_USABLE_MAPPED_FACTS",
                            "message": f"Governed structured fallback produced no usable mapped facts from {document}.",
                        }
                    )
            evidence[ticker] = {
                "url": document_url,
                "sha256": document_record["sha256"],
                "structured_fact_count": len(parsed),
                "usable_structured_fact_count": sum(item.quality_status == "usable" for item in parsed),
            }
        except (FinancialIngestionError, requests.RequestException, json.JSONDecodeError) as exc:
            issues.append(
                {
                    "ticker": ticker,
                    "severity": "error",
                    "issue_code": "FALLBACK_EVIDENCE_UNAVAILABLE",
                    "message": str(exc),
                }
            )
            if not allow_partial:
                raise
    return raw_facts, evidence, cache_records, issues


def _resolution_projection(values: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "profile_key",
        "ticker",
        "role_type",
        "original_profile_status",
        "ingestion_route",
        "resolution_status",
        "effective_annual_form",
        "effective_accounting_basis",
        "effective_taxonomy",
        "effective_reporting_currency",
        "canonical_eligible",
        "evidence_accession",
        "evidence_form",
        "evidence_accepted_at",
        "evidence_url",
        "evidence_sha256",
        "resolution_reason",
        "source_cutoff_date",
        "source_id",
        "policy_version",
        "policy_sha256",
        "calibration_eligible",
    )
    return {key: values[key] for key in keys}


def _build_resolutions(
    profiles: Iterable[Mapping[str, Any]],
    *,
    policy: FinancialIngestionPolicy,
    exception_evidence: Mapping[str, Mapping[str, Any]],
) -> tuple[list[ProfileResolution], str]:
    exceptions = _as_mapping(policy.payload["exception_resolutions"], "exception_resolutions")
    resolutions: list[ProfileResolution] = []
    for profile in profiles:
        ticker = str(profile["ticker"])
        if ticker not in exceptions:
            values: dict[str, Any] = {
                "profile_key": str(profile["profile_key"]),
                "company_id": int(profile["company_id"]),
                "security_id": int(profile["security_id"]),
                "ticker": ticker,
                "role_type": str(profile["role_type"]),
                "original_profile_status": str(profile["profile_status"]),
                "ingestion_route": "companyfacts_standard",
                "resolution_status": "resolved_standard",
                "effective_annual_form": str(profile["primary_annual_form"]),
                "effective_accounting_basis": str(profile["accounting_basis"]),
                "effective_taxonomy": str(profile["primary_taxonomy"]),
                "effective_reporting_currency": str(profile["reporting_currency"]),
                "canonical_eligible": 1,
                "evidence_accession": str(profile["latest_financial_accession"]),
                "evidence_form": str(profile["latest_financial_form"]),
                "evidence_accepted_at": str(profile["latest_financial_accepted_at"]),
                "evidence_url": (
                    f"https://data.sec.gov/api/xbrl/companyfacts/CIK{profile['cik']}.json"
                ),
                "evidence_sha256": str(profile["companyfacts_sha256"]),
                "resolution_reason": "Stage 4A profile and immutable Company Facts payload passed the standard route.",
                "source_cutoff_date": str(profile["source_cutoff_date"]),
                "source_id": "basic_materials_reporting_profile_review",
                "policy_version": policy.version,
                "policy_sha256": policy.checksum,
                "calibration_eligible": 0,
            }
        else:
            governed = _as_mapping(exceptions[ticker], f"exception_resolutions.{ticker}")
            observed = exception_evidence.get(ticker, {})
            evidence_sha = str(observed.get("sha256") or "")
            evidence_url = str(observed.get("url") or "")
            status = str(governed["resolution_status"])
            eligible = int(bool(governed["canonical_eligible"]))
            reason = str(governed["reason"])
            if not evidence_sha:
                status = "missing_required_source"
                eligible = 0
                reason += " Runtime evidence payload was unavailable; canonical use is blocked."
            values = {
                "profile_key": str(profile["profile_key"]),
                "company_id": int(profile["company_id"]),
                "security_id": int(profile["security_id"]),
                "ticker": ticker,
                "role_type": str(profile["role_type"]),
                "original_profile_status": str(profile["profile_status"]),
                "ingestion_route": str(governed["route"]),
                "resolution_status": status,
                "effective_annual_form": str(governed["annual_form"]),
                "effective_accounting_basis": str(governed["accounting_basis"]),
                "effective_taxonomy": str(governed["taxonomy"]),
                "effective_reporting_currency": str(governed["reporting_currency"]),
                "canonical_eligible": eligible,
                "evidence_accession": str(governed["evidence_accession"]),
                "evidence_form": str(governed["evidence_form"]),
                "evidence_accepted_at": str(governed["evidence_accepted_at"]),
                "evidence_url": evidence_url,
                "evidence_sha256": evidence_sha,
                "resolution_reason": reason,
                "source_cutoff_date": str(profile["source_cutoff_date"]),
                "source_id": "basic_materials_reporting_profile_review",
                "policy_version": policy.version,
                "policy_sha256": policy.checksum,
                "calibration_eligible": 0,
            }
        projection = _resolution_projection(values)
        values["resolution_sha256"] = _sha256_bytes(
            json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        resolutions.append(ProfileResolution(**values))
    resolutions.sort(key=lambda item: (item.role_type, item.ticker))
    combined = _sha256_bytes(
        json.dumps(
            [_resolution_projection(asdict(item)) | {"resolution_sha256": item.resolution_sha256} for item in resolutions],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return resolutions, combined


def _seal_ingestion_cache_manifest(
    *,
    config: BasicMaterialsConfig,
    policy: FinancialIngestionPolicy,
    fallback_records: Iterable[Mapping[str, Any]],
    contract_hashes: Mapping[str, str],
) -> tuple[str, Path]:
    cache_policy = _as_mapping(policy.payload["immutable_cache"], "immutable_cache")
    cache_root = config.paths.cache_root / str(cache_policy["ingestion_cache_relative_path"])
    records = [
        {
            "ticker": str(record["ticker"]),
            "accession": str(record["accession"]),
            "document": str(record["document"]),
            "url": str(record["url"]),
            "cache_path": str(Path(str(record["cache_path"])).resolve()),
            "sha256": str(record["sha256"]),
            "byte_size": int(record["byte_size"]),
            "status": "available",
        }
        for record in fallback_records
    ]
    records.sort(key=lambda item: (item["ticker"], item["accession"], item["document"]))
    projection = {
        "artifact_id": "basic_materials_financial_ingestion_cache_v1",
        "snapshot_as_of_date": policy.as_of_date,
        "policy_sha256": policy.checksum,
        "stage4a_contract_hashes": dict(sorted(contract_hashes.items())),
        "fallback_records": records,
    }
    digest = _sha256_bytes(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    payload = {**projection, "manifest_sha256": digest}
    path = cache_root / "financial_ingestion_cache_manifest.json"
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        existing_projection = {key: value for key, value in existing.items() if key != "manifest_sha256"}
        existing_digest = _sha256_bytes(
            json.dumps(existing_projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if existing.get("manifest_sha256") != existing_digest:
            raise FinancialIngestionError("Existing Stage 4B cache manifest has an invalid seal")
        if existing_digest != digest:
            raise FinancialIngestionError(
                "Existing Stage 4B immutable cache manifest differs; use a new as-of snapshot"
            )
    else:
        atomic_write_json(path, payload)
    return digest, path


def _reuse_verified_ingestion_snapshot(
    conn: sqlite3.Connection,
    *,
    config: BasicMaterialsConfig,
    policy: FinancialIngestionPolicy,
    contract_hashes: Mapping[str, str],
) -> SecIngestionStats | None:
    """Return an already-complete immutable ingestion load without row-by-row conflicts."""

    cache_policy = _as_mapping(policy.payload["immutable_cache"], "immutable_cache")
    cache_root = (
        config.paths.cache_root / str(cache_policy["ingestion_cache_relative_path"])
    ).resolve()
    manifest_path = cache_root / "financial_ingestion_cache_manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = _as_mapping(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        "financial ingestion cache manifest",
    )
    projection = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest_sha256 = _sha256_bytes(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if str(manifest.get("manifest_sha256") or "") != manifest_sha256:
        raise FinancialIngestionError("Existing Stage 4B cache manifest has an invalid seal")
    if str(manifest.get("policy_sha256") or "") != policy.checksum:
        raise FinancialIngestionError("Existing Stage 4B cache manifest policy mismatch")
    if dict(manifest.get("stage4a_contract_hashes") or {}) != dict(sorted(contract_hashes.items())):
        raise FinancialIngestionError("Existing Stage 4B cache manifest contract mismatch")
    for raw_record in manifest.get("fallback_records", []):
        record = _as_mapping(raw_record, "financial ingestion fallback record")
        path = Path(str(record["cache_path"])).resolve()
        try:
            path.relative_to(cache_root)
        except ValueError as exc:
            raise FinancialIngestionError(
                f"Fallback cache record is outside the governed cache root: {path}"
            ) from exc
        if not path.is_file() or _sha256_path(path) != str(record.get("sha256") or ""):
            raise FinancialIngestionError(f"Fallback cache checksum mismatch: {path}")

    snapshots = conn.execute(
        """
        SELECT *
        FROM fact_financial_ingestion_snapshot
        WHERE extraction_asof_date = ?
          AND contract_manifest_sha256 = ?
          AND cache_manifest_sha256 = ?
          AND policy_sha256 = ?
        ORDER BY snapshot_key
        """,
        (
            policy.as_of_date,
            contract_hashes["financial_manifest"],
            manifest_sha256,
            policy.checksum,
        ),
    ).fetchall()
    if not snapshots:
        return None
    if len(snapshots) != 1:
        raise FinancialIngestionError("Multiple snapshots match one immutable Stage 4B identity")
    snapshot = snapshots[0]
    snapshot_key = str(snapshot["snapshot_key"])
    resolution_rows = conn.execute(
        """
        SELECT profile_key, company_id, security_id, ticker, role_type,
               original_profile_status, ingestion_route, resolution_status,
               effective_annual_form, effective_accounting_basis, effective_taxonomy,
               effective_reporting_currency, canonical_eligible, evidence_accession,
               evidence_form, evidence_accepted_at, evidence_url, evidence_sha256,
               resolution_reason, source_cutoff_date, source_id, policy_version,
               policy_sha256, resolution_sha256, calibration_eligible
        FROM dim_financial_profile_resolution
        ORDER BY role_type, ticker
        """
    ).fetchall()
    if len(resolution_rows) != 154:
        return None
    if any(str(row["policy_sha256"]) != policy.checksum for row in resolution_rows):
        raise FinancialIngestionError("Stored financial profile resolutions have mixed policies")
    resolution_sha256 = _sha256_bytes(
        json.dumps(
            [
                _resolution_projection(dict(row))
                | {"resolution_sha256": str(row["resolution_sha256"])}
                for row in resolution_rows
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if resolution_sha256 != str(snapshot["resolution_sha256"]):
        raise FinancialIngestionError("Stored financial profile resolution seal mismatch")

    actual_filing_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_sec_filing WHERE snapshot_key = ?",
            (snapshot_key,),
        ).fetchone()[0]
    )
    raw_status_counts = Counter(
        str(row["quality_status"])
        for row in conn.execute(
            "SELECT quality_status FROM fact_sec_xbrl_fact_raw WHERE snapshot_key = ?",
            (snapshot_key,),
        ).fetchall()
    )
    actual_raw_count = sum(raw_status_counts.values())
    expected_counts = (
        int(snapshot["issuer_count"]),
        int(snapshot["filing_count"]),
        int(snapshot["raw_fact_count"]),
    )
    actual_counts = (154, actual_filing_count, actual_raw_count)
    if actual_counts != expected_counts or actual_filing_count == 0 or actual_raw_count == 0:
        return None
    issue_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ticker, severity, issue_code, message
            FROM fact_financial_normalization_issue
            WHERE snapshot_key = ? AND stage = 'sec_ingestion'
            ORDER BY ticker, severity, issue_code, message
            """,
            (snapshot_key,),
        ).fetchall()
    ]
    if any(str(row["severity"]) == "error" for row in issue_rows):
        raise FinancialIngestionError("Stored SEC ingestion audit contains error-severity rows")
    resolution_counts = Counter(str(row["resolution_status"]) for row in resolution_rows)
    if resolution_counts["resolved_standard"] != 140:
        return None
    return SecIngestionStats(
        snapshot_key=snapshot_key,
        cache_manifest_sha256=manifest_sha256,
        profiles=154,
        standard_profiles=140,
        exception_profiles=14,
        resolutions_by_status=dict(sorted(resolution_counts.items())),
        filings=actual_filing_count,
        raw_facts=actual_raw_count,
        usable_raw_facts=raw_status_counts["usable"],
        quarantined_raw_facts=actual_raw_count - raw_status_counts["usable"],
        companyfacts_payloads=int(snapshot["companyfacts_payload_count"]),
        fallback_payloads=int(snapshot["inline_payload_count"]),
        issues=tuple(issue_rows),
    )


def _insert_resolution(
    conn: sqlite3.Connection,
    resolution: ProfileResolution,
    *,
    now: str,
) -> None:
    values = asdict(resolution)
    conn.execute(
        """
        INSERT INTO dim_financial_profile_resolution (
            profile_key, company_id, security_id, ticker, role_type,
            original_profile_status, ingestion_route, resolution_status,
            effective_annual_form, effective_accounting_basis, effective_taxonomy,
            effective_reporting_currency, canonical_eligible, evidence_accession,
            evidence_form, evidence_accepted_at, evidence_url, evidence_sha256,
            resolution_reason, source_cutoff_date, source_id, policy_version,
            policy_sha256, resolution_sha256, calibration_eligible,
            created_at_utc, updated_at_utc
        ) VALUES (
            :profile_key, :company_id, :security_id, :ticker, :role_type,
            :original_profile_status, :ingestion_route, :resolution_status,
            :effective_annual_form, :effective_accounting_basis, :effective_taxonomy,
            :effective_reporting_currency, :canonical_eligible, :evidence_accession,
            :evidence_form, :evidence_accepted_at, :evidence_url, :evidence_sha256,
            :resolution_reason, :source_cutoff_date, :source_id, :policy_version,
            :policy_sha256, :resolution_sha256, :calibration_eligible,
            :created_at_utc, :updated_at_utc
        )
        ON CONFLICT(profile_key) DO UPDATE SET
            ingestion_route = excluded.ingestion_route,
            resolution_status = excluded.resolution_status,
            effective_annual_form = excluded.effective_annual_form,
            effective_accounting_basis = excluded.effective_accounting_basis,
            effective_taxonomy = excluded.effective_taxonomy,
            effective_reporting_currency = excluded.effective_reporting_currency,
            canonical_eligible = excluded.canonical_eligible,
            evidence_accession = excluded.evidence_accession,
            evidence_form = excluded.evidence_form,
            evidence_accepted_at = excluded.evidence_accepted_at,
            evidence_url = excluded.evidence_url,
            evidence_sha256 = excluded.evidence_sha256,
            resolution_reason = excluded.resolution_reason,
            source_cutoff_date = excluded.source_cutoff_date,
            source_id = excluded.source_id,
            policy_version = excluded.policy_version,
            policy_sha256 = excluded.policy_sha256,
            resolution_sha256 = excluded.resolution_sha256,
            calibration_eligible = 0,
            updated_at_utc = excluded.updated_at_utc
        """,
        {**values, "created_at_utc": now, "updated_at_utc": now},
    )


def _insert_filing(
    conn: sqlite3.Connection,
    filing: FilingRecord,
    *,
    snapshot_key: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO fact_sec_filing (
            filing_key, company_id, security_id, ticker, cik, accession_number,
            form_type, form_family, filing_date, accepted_at, report_date,
            primary_document, source_id, source_url, payload_sha256, snapshot_key,
            created_at_utc, updated_at_utc, fiscal_year, fiscal_period,
            is_amendment, filing_payload_kind
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(filing_key) DO UPDATE SET
            form_type = excluded.form_type,
            form_family = excluded.form_family,
            filing_date = excluded.filing_date,
            accepted_at = excluded.accepted_at,
            report_date = excluded.report_date,
            primary_document = excluded.primary_document,
            source_url = excluded.source_url,
            payload_sha256 = excluded.payload_sha256,
            fiscal_year = excluded.fiscal_year,
            fiscal_period = excluded.fiscal_period,
            is_amendment = excluded.is_amendment,
            filing_payload_kind = excluded.filing_payload_kind,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            filing.filing_key,
            filing.company_id,
            filing.security_id,
            filing.ticker,
            filing.cik,
            filing.accession_number,
            filing.form_type,
            filing.form_family,
            filing.filing_date,
            filing.accepted_at,
            filing.report_date,
            filing.primary_document,
            filing.source_id,
            filing.source_url,
            filing.payload_sha256,
            snapshot_key,
            now,
            now,
            filing.fiscal_year,
            filing.fiscal_period,
            filing.is_amendment,
            filing.filing_payload_kind,
        ),
    )


def _insert_raw_fact(
    conn: sqlite3.Connection,
    fact: RawFinancialFact,
    *,
    snapshot_key: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO fact_sec_xbrl_fact_raw (
            source_observation_id, filing_key, company_id, security_id, ticker, cik,
            accession_number, taxonomy, concept, value_text, numeric_value, unit,
            period_start, period_end, filed_date, accepted_at, form_type, frame,
            dimensions_json, source_id, source_detail, snapshot_key, created_at_utc,
            fiscal_year, fiscal_period, decimals, context_id, payload_sha256,
            evidence_url, quality_status, quarantine_reason
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(source_observation_id) DO NOTHING
        """,
        (
            fact.source_observation_id,
            fact.filing_key,
            fact.company_id,
            fact.security_id,
            fact.ticker,
            fact.cik,
            fact.accession_number,
            fact.taxonomy,
            fact.concept,
            fact.value_text,
            fact.numeric_value,
            fact.unit,
            fact.period_start,
            fact.period_end,
            fact.filed_date,
            fact.accepted_at,
            fact.form_type,
            fact.frame,
            fact.dimensions_json,
            fact.source_id,
            fact.source_detail,
            snapshot_key,
            now,
            fact.fiscal_year,
            fact.fiscal_period,
            fact.decimals,
            fact.context_id,
            fact.payload_sha256,
            fact.evidence_url,
            fact.quality_status,
            fact.quarantine_reason,
        ),
    )


def _insert_issues(
    conn: sqlite3.Connection,
    *,
    issues: Iterable[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
    snapshot_key: str,
    now: str,
) -> None:
    for issue in issues:
        ticker = str(issue.get("ticker") or "")
        profile = profiles.get(ticker)
        if profile is None:
            continue
        code = str(issue.get("issue_code") or "UNCLASSIFIED_INGESTION_ISSUE")
        message = str(issue.get("message") or "")
        issue_key = _stable_hash(snapshot_key, ticker, "sec_ingestion", code, message)
        conn.execute(
            """
            INSERT INTO fact_financial_normalization_issue (
                issue_key, snapshot_key, company_id, security_id, ticker, stage,
                severity, issue_code, canonical_metric, accession_number,
                source_observation_ids_json, evidence_json, message, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, 'sec_ingestion', ?, ?, '', '', '[]', '{}', ?, ?)
            ON CONFLICT(issue_key) DO NOTHING
            """,
            (
                issue_key,
                snapshot_key,
                int(profile["company_id"]),
                int(profile["security_id"]),
                ticker,
                str(issue.get("severity") or "warning"),
                code,
                message,
                now,
            ),
        )


def ingest_sec_financials(
    conn: sqlite3.Connection,
    *,
    config: BasicMaterialsConfig,
    policy: FinancialIngestionPolicy,
    cache_only: bool = False,
    allow_partial: bool = True,
) -> SecIngestionStats:
    """Load all governed SEC metadata and mapped raw facts in one transaction."""

    assert_database_identity(conn)
    contract_hashes = validate_ingestion_contract_files(config, policy)
    profiles_list = _issuer_profiles(conn)
    if cache_only:
        reused = _reuse_verified_ingestion_snapshot(
            conn,
            config=config,
            policy=policy,
            contract_hashes=contract_hashes,
        )
        if reused is not None:
            return reused
    profiles = {str(row["ticker"]): row for row in profiles_list}
    concept_contract = _concept_contract(conn)
    _, _, cache_records = _load_cache_manifest(config, policy)
    filing_lookup, issues = _parse_submission_filings(
        profiles_list,
        cache_records,
        history_start_date=policy.history_start_date,
    )
    companyfacts_rows: list[RawFinancialFact] = []
    referenced_accessions: set[tuple[str, str]] = set()
    companyfacts_payloads = 0
    for profile in profiles_list:
        ticker = str(profile["ticker"])
        record = cache_records.get((ticker, "companyfacts"))
        if record is None or not str(record.get("sha256") or ""):
            issues.append(
                {
                    "ticker": ticker,
                    "severity": "warning",
                    "issue_code": "COMPANYFACTS_PAYLOAD_MISSING",
                    "message": "No immutable Company Facts payload is available; governed fallback is required.",
                }
            )
            continue
        path = Path(str(record["cache_path"]))
        payload = _as_mapping(json.loads(path.read_text(encoding="utf-8")), f"{ticker} Company Facts")
        payload_cik = str(payload.get("cik") or "").zfill(10)
        if payload_cik != str(profile["cik"]):
            raise FinancialIngestionError(f"{ticker} Company Facts CIK mismatch")
        parsed, accessions, payload_issues = _companyfacts_raw_rows(
            profile,
            payload,
            payload_sha256=str(record["sha256"]),
            evidence_url=str(record.get("url") or ""),
            filing_lookup=filing_lookup,
            concept_contract=concept_contract,
            history_start_date=policy.history_start_date,
        )
        companyfacts_rows.extend(parsed)
        referenced_accessions.update((ticker, accession) for accession in accessions)
        issues.extend(payload_issues)
        companyfacts_payloads += 1
    fallback_rows, evidence, fallback_records, fallback_issues = _exception_evidence(
        config=config,
        policy=policy,
        profiles=profiles,
        filing_lookup=filing_lookup,
        concept_contract=concept_contract,
        cache_only=cache_only,
        allow_partial=allow_partial,
    )
    issues.extend(fallback_issues)
    resolutions, resolution_sha256 = _build_resolutions(
        profiles_list,
        policy=policy,
        exception_evidence=evidence,
    )
    cache_manifest_sha256, _ = _seal_ingestion_cache_manifest(
        config=config,
        policy=policy,
        fallback_records=fallback_records,
        contract_hashes=contract_hashes,
    )
    snapshot_prefix = str(_as_mapping(policy.payload["snapshot"], "snapshot")["snapshot_prefix"])
    snapshot_key = (
        f"{snapshot_prefix}:{policy.as_of_date}:"
        f"{_stable_hash(policy.checksum, cache_manifest_sha256, resolution_sha256)[:20]}"
    )
    evidence_accessions = {
        (resolution.ticker, resolution.evidence_accession)
        for resolution in resolutions
        if resolution.evidence_accession
    }
    filings = [
        filing
        for key, filing in filing_lookup.items()
        if filing.form_family in _REGULAR_FAMILIES
        or (
            filing.form_family == "6-K"
            and (filing.is_xbrl or filing.is_inline_xbrl or key in referenced_accessions or key in evidence_accessions)
        )
    ]
    filings.sort(key=lambda item: (item.ticker, item.accepted_at, item.accession_number))
    all_raw_by_id: dict[str, RawFinancialFact] = {}
    for fact in companyfacts_rows + fallback_rows:
        existing = all_raw_by_id.get(fact.source_observation_id)
        if existing is not None and existing != fact:
            raise FinancialIngestionError(f"Conflicting raw observation identity: {fact.source_observation_id}")
        all_raw_by_id[fact.source_observation_id] = fact
    raw_rows = sorted(
        all_raw_by_id.values(),
        key=lambda item: (
            item.ticker,
            item.accepted_at,
            item.accession_number,
            item.taxonomy,
            item.concept,
            item.period_end,
            item.source_observation_id,
        ),
    )
    now = utc_now()
    details = {
        "history_start_date": policy.history_start_date,
        "source_cutoff_date": policy.as_of_date,
        "standard_profile_count": 140,
        "exception_profile_count": 14,
        "cache_only": cache_only,
        "calibration_eligible": False,
        "portfolio_candidate_gate": False,
    }
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing_snapshot = conn.execute(
            "SELECT * FROM fact_financial_ingestion_snapshot WHERE snapshot_key = ?",
            (snapshot_key,),
        ).fetchone()
        if existing_snapshot is not None and (
            str(existing_snapshot["contract_manifest_sha256"])
            != contract_hashes["financial_manifest"]
            or str(existing_snapshot["cache_manifest_sha256"]) != cache_manifest_sha256
            or str(existing_snapshot["policy_sha256"]) != policy.checksum
            or str(existing_snapshot["resolution_sha256"]) != resolution_sha256
        ):
            raise FinancialIngestionError("Existing snapshot identity does not match immutable Stage 4B evidence")
        conn.execute(
            """
            INSERT INTO fact_financial_ingestion_snapshot (
                snapshot_key, extraction_asof_date, contract_manifest_sha256,
                cache_manifest_sha256, issuer_count, filing_count, raw_fact_count,
                fx_observation_count, cache_root, status, created_at_utc,
                policy_sha256, resolution_sha256, companyfacts_payload_count,
                inline_payload_count, canonical_fact_count, feature_count, details_json
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, 'partial', ?, ?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(snapshot_key) DO NOTHING
            """,
            (
                snapshot_key,
                policy.as_of_date,
                contract_hashes["financial_manifest"],
                cache_manifest_sha256,
                len(profiles_list),
                str(config.paths.cache_root),
                now,
                policy.checksum,
                resolution_sha256,
                companyfacts_payloads,
                len(fallback_records),
                json.dumps(details, sort_keys=True),
            ),
        )
        for resolution in resolutions:
            _insert_resolution(conn, resolution, now=now)
        for filing in filings:
            _insert_filing(conn, filing, snapshot_key=snapshot_key, now=now)
        for fact in raw_rows:
            _insert_raw_fact(conn, fact, snapshot_key=snapshot_key, now=now)
        _insert_issues(
            conn,
            issues=issues,
            profiles=profiles,
            snapshot_key=snapshot_key,
            now=now,
        )
        actual_filing_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_sec_filing WHERE snapshot_key = ?",
                (snapshot_key,),
            ).fetchone()[0]
        )
        actual_raw_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE snapshot_key = ?",
                (snapshot_key,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE fact_financial_ingestion_snapshot
            SET issuer_count = ?, filing_count = ?, raw_fact_count = ?,
                companyfacts_payload_count = ?, inline_payload_count = ?,
                details_json = ?
            WHERE snapshot_key = ?
            """,
            (
                len(profiles_list),
                actual_filing_count,
                actual_raw_count,
                companyfacts_payloads,
                len(fallback_records),
                json.dumps(details, sort_keys=True),
                snapshot_key,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    resolution_counts = Counter(item.resolution_status for item in resolutions)
    usable = sum(item.quality_status == "usable" for item in raw_rows)
    return SecIngestionStats(
        snapshot_key=snapshot_key,
        cache_manifest_sha256=cache_manifest_sha256,
        profiles=len(profiles_list),
        standard_profiles=sum(item.resolution_status == "resolved_standard" for item in resolutions),
        exception_profiles=sum(item.ticker in _EXCEPTION_TICKERS for item in resolutions),
        resolutions_by_status=dict(sorted(resolution_counts.items())),
        filings=len(filings),
        raw_facts=len(raw_rows),
        usable_raw_facts=usable,
        quarantined_raw_facts=len(raw_rows) - usable,
        companyfacts_payloads=companyfacts_payloads,
        fallback_payloads=len(fallback_records),
        issues=tuple(issues),
    )


def write_sec_ingestion_reports(
    conn: sqlite3.Connection,
    *,
    stats: SecIngestionStats,
    report_dir: str | Path,
) -> dict[str, str]:
    """Publish deterministic Stage 4B ingestion and resolution evidence."""

    target = Path(report_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    summary_path = atomic_write_json(target / "sec_ingestion_summary.json", stats.as_dict())
    resolutions = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ticker, role_type, original_profile_status, ingestion_route,
                   resolution_status, effective_annual_form, effective_accounting_basis,
                   effective_taxonomy, effective_reporting_currency, canonical_eligible,
                   evidence_accession, evidence_form, evidence_accepted_at, evidence_url,
                   evidence_sha256, resolution_reason, source_cutoff_date,
                   policy_version, policy_sha256, resolution_sha256, calibration_eligible
            FROM dim_financial_profile_resolution
            ORDER BY role_type, ticker
            """
        ).fetchall()
    ]
    resolution_path = atomic_write_csv(
        target / "financial_profile_resolutions.csv",
        resolutions,
        tuple(resolutions[0]) if resolutions else ("ticker",),
    )
    coverage = [
        dict(row)
        for row in conn.execute(
            """
            SELECT p.ticker, p.role_type, p.filing_regime, r.ingestion_route,
                   r.resolution_status, r.canonical_eligible,
                   COUNT(DISTINCT f.filing_key) AS filing_count,
                   COUNT(DISTINCT x.source_observation_id) AS raw_fact_count,
                   COUNT(DISTINCT CASE WHEN x.quality_status = 'usable'
                                       THEN x.source_observation_id END) AS usable_raw_fact_count,
                   COUNT(DISTINCT CASE WHEN x.quality_status = 'quarantined'
                                       THEN x.source_observation_id END) AS quarantined_raw_fact_count
            FROM dim_issuer_reporting_profile AS p
            JOIN dim_financial_profile_resolution AS r ON r.profile_key = p.profile_key
            LEFT JOIN fact_sec_filing AS f
              ON f.security_id = p.security_id AND f.snapshot_key = ?
            LEFT JOIN fact_sec_xbrl_fact_raw AS x
              ON x.security_id = p.security_id AND x.snapshot_key = ?
            GROUP BY p.ticker, p.role_type, p.filing_regime, r.ingestion_route,
                     r.resolution_status, r.canonical_eligible
            ORDER BY p.role_type, p.ticker
            """,
            (stats.snapshot_key, stats.snapshot_key),
        ).fetchall()
    ]
    coverage_path = atomic_write_csv(
        target / "sec_ingestion_coverage.csv",
        coverage,
        tuple(coverage[0]) if coverage else ("ticker",),
    )
    issue_rows = [dict(item) for item in stats.issues]
    issue_fields = ("ticker", "severity", "issue_code", "message")
    normalized_issues = [{key: item.get(key, "") for key in issue_fields} for item in issue_rows]
    issues_path = atomic_write_csv(target / "sec_ingestion_issues.csv", normalized_issues, issue_fields)
    artifacts = {
        "summary": str(summary_path),
        "resolutions": str(resolution_path),
        "coverage": str(coverage_path),
        "issues": str(issues_path),
    }
    manifest_rows = [
        {
            "artifact": name,
            "path": path,
            "sha256": _sha256_path(Path(path)),
            "byte_size": Path(path).stat().st_size,
        }
        for name, path in sorted(artifacts.items())
    ]
    manifest_path = atomic_write_json(
        target / "artifact_manifest.json",
        {
            "stage": "stage4b_sec_ingestion",
            "snapshot_key": stats.snapshot_key,
            "artifacts": manifest_rows,
        },
    )
    artifacts["artifact_manifest"] = str(manifest_path)
    return artifacts
