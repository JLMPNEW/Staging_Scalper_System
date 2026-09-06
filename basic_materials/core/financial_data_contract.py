"""Strict Stage 4A financial policy, artifacts, database load, and validation."""

from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import (
    assert_database_identity,
    database_counts,
    utc_now,
)
from basic_materials.core.reporting_profiles import (
    PROFILE_FIELDS,
    issuer_seeds,
    profile_hash,
)


class FinancialContractError(ValueError):
    """Raised when the Stage 4A financial contract is not exact."""


@dataclass(frozen=True)
class FinancialDataPolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    expected_current_profiles: int
    expected_historical_profiles: int
    expected_total_profiles: int
    expected_metrics: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class FinancialArtifact:
    name: str
    path: Path
    source_id: str
    sha256: str
    byte_size: int
    row_count: int
    unique_key: str


@dataclass(frozen=True)
class FinancialManifest:
    path: Path
    checksum: str
    policy_version: str
    as_of_date: str
    artifacts: Mapping[str, FinancialArtifact]


@dataclass(frozen=True)
class FinancialMetric:
    canonical_metric: str
    statement_type: str
    period_type: str
    sign_policy: str
    concepts: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class FinancialContractBundle:
    profiles: tuple[Mapping[str, str], ...]
    metrics: tuple[FinancialMetric, ...]
    overrides: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class FinancialContractLoadStats:
    profiles: int
    current_profiles: int
    historical_profiles: int
    metrics: int
    concept_links: int
    raw_contract_payloads: int
    contract_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "profiles": self.profiles,
            "current_profiles": self.current_profiles,
            "historical_profiles": self.historical_profiles,
            "metrics": self.metrics,
            "concept_links": self.concept_links,
            "raw_contract_payloads": self.raw_contract_payloads,
            "contract_sha256": self.contract_sha256,
        }


@dataclass(frozen=True)
class FinancialValidationIssue:
    severity: str
    issue_code: str
    message: str
    ticker: str = ""
    details: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "issue_code": self.issue_code,
            "ticker": self.ticker,
            "message": self.message,
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class FinancialContractValidationReport:
    passed: bool
    policy_version: str
    manifest_checksum: str
    as_of_date: str
    expected_counts: Mapping[str, int]
    actual_counts: Mapping[str, int]
    profile_status_counts: Mapping[str, int]
    filing_regime_counts: Mapping[str, int]
    accounting_basis_counts: Mapping[str, int]
    reporting_currency_counts: Mapping[str, int]
    issues: tuple[FinancialValidationIssue, ...]
    profiles: tuple[Mapping[str, str], ...]
    metrics: tuple[Mapping[str, Any], ...]
    database_table_counts: Mapping[str, int]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "policy_version": self.policy_version,
            "manifest_checksum": self.manifest_checksum,
            "as_of_date": self.as_of_date,
            "expected_counts": dict(self.expected_counts),
            "actual_counts": dict(self.actual_counts),
            "profile_status_counts": dict(self.profile_status_counts),
            "filing_regime_counts": dict(self.filing_regime_counts),
            "accounting_basis_counts": dict(self.accounting_basis_counts),
            "reporting_currency_counts": dict(self.reporting_currency_counts),
            "error_count": sum(issue.severity == "error" for issue in self.issues),
            "warning_count": sum(issue.severity == "warning" for issue in self.issues),
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FinancialContractError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise FinancialContractError(
            f"{context} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _date(value: Any, context: str) -> str:
    raw = str(value)
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise FinancialContractError(f"{context} must be an ISO date") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_csv(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return tuple(reader.fieldnames or ()), [dict(row) for row in reader]


def load_financial_data_policy(path: str | Path) -> FinancialDataPolicy:
    policy_path = Path(path).resolve()
    raw_bytes = policy_path.read_bytes()
    root = _mapping(yaml.safe_load(raw_bytes.decode("utf-8")), "financial policy")
    _exact_keys(
        root,
        {
            "policy_version",
            "contract_as_of_date",
            "model_family",
            "sector",
            "state",
            "calibration_eligible",
            "expected_counts",
            "sources",
            "sec",
            "reporting_profiles",
            "canonical_metrics",
            "availability",
            "fx",
            "features",
            "required_flags",
        },
        "financial policy",
    )
    version = str(root["policy_version"])
    if version != "basic_materials_financial_data_policy_v1":
        raise FinancialContractError("Unsupported financial policy_version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise FinancialContractError("Financial policy model family or sector is invalid")
    if (
        root["state"] != "stage4a_profile_contract_calibration_blocked"
        or root["calibration_eligible"] is not False
    ):
        raise FinancialContractError("Financial policy must remain calibration-blocked")
    counts = _mapping(root["expected_counts"], "expected_counts")
    _exact_keys(
        counts,
        {
            "current_profiles",
            "historical_profiles",
            "total_profiles",
            "canonical_metrics",
        },
        "expected_counts",
    )
    expected = tuple(int(counts[key]) for key in counts)
    if expected != (134, 20, 154, 22):
        raise FinancialContractError("Financial policy expected counts are invalid")
    sources = _mapping(root["sources"], "sources")
    required_source_keys = {
        "reporting_profile_source_id",
        "metric_policy_source_id",
        "submissions_source_id",
        "companyfacts_source_id",
        "inline_xbrl_source_id",
        "fx_source_id",
        "reviewed_override_source_id",
        "canonical_precedence",
    }
    _exact_keys(sources, required_source_keys, "sources")
    if list(sources["canonical_precedence"]) != [
        "sec_companyfacts",
        "sec_inline_xbrl_fallback",
        "basic_materials_manual_override",
    ]:
        raise FinancialContractError("Canonical financial source precedence is invalid")
    sec = _mapping(root["sec"], "sec")
    if (
        sec.get("acceptance_timestamp_is_earliest_availability") is not True
        or sec.get("preserve_amendments") is not True
        or sec.get("financial_6k_requires_xbrl_evidence") is not True
    ):
        raise FinancialContractError("SEC point-in-time controls are invalid")
    profiles = _mapping(root["reporting_profiles"], "reporting_profiles")
    if (
        list(profiles.get("required_roles", []))
        != ["current_universe", "historical_pilot"]
        or profiles.get("trading_currency_fallback_prohibited") is not True
        or profiles.get("reporting_currency_must_be_evidence_derived") is not True
    ):
        raise FinancialContractError("Reporting-profile controls are invalid")
    canonical = _mapping(root["canonical_metrics"], "canonical_metrics")
    required_metrics = list(canonical.get("required_metrics", []))
    duration = list(canonical.get("duration_metrics", []))
    instant = list(canonical.get("instant_metrics", []))
    if (
        canonical.get("concept_map_version") != "basic_materials_financial_concepts_v1"
        or len(required_metrics) != 22
        or len(set(required_metrics)) != 22
        or set(duration).intersection(instant)
        or set(duration).union(instant) != set(required_metrics)
    ):
        raise FinancialContractError("Canonical metric partition is invalid")
    availability = _mapping(root["availability"], "availability")
    if (
        availability.get("earliest_usable_timestamp") != "sec_acceptance_timestamp"
        or availability.get("period_end_is_not_availability") is not True
        or availability.get("future_availability_tolerance_seconds") != 0
    ):
        raise FinancialContractError("Financial availability controls are invalid")
    fx = _mapping(root["fx"], "fx")
    if (
        fx.get("quote_currency") != "USD"
        or fx.get("listing_currency_substitution_prohibited") is not True
        or fx.get("retain_reported_and_usd_values") is not True
    ):
        raise FinancialContractError("FX policy is invalid")
    features = _mapping(root["features"], "features")
    if (
        features.get("feature_definition_version")
        != "basic_materials_financial_features_v1"
        or features.get("undefined_or_loss_denominators_remain_null") is not True
        or features.get("daily_valuation_uses_stage3_prices_only") is not True
    ):
        raise FinancialContractError("Financial feature policy is invalid")
    flags = _mapping(root["required_flags"], "required_flags")
    if any(value is not False for value in flags.values()):
        raise FinancialContractError("Stage 4 financial policy activates a prohibited flag")
    return FinancialDataPolicy(
        path=policy_path,
        checksum=hashlib.sha256(raw_bytes).hexdigest(),
        version=version,
        as_of_date=_date(root["contract_as_of_date"], "contract_as_of_date"),
        expected_current_profiles=int(counts["current_profiles"]),
        expected_historical_profiles=int(counts["historical_profiles"]),
        expected_total_profiles=int(counts["total_profiles"]),
        expected_metrics=int(counts["canonical_metrics"]),
        payload=root,
    )


def load_financial_concept_map(
    path: str | Path,
    policy: FinancialDataPolicy,
) -> tuple[FinancialMetric, ...]:
    concept_path = Path(path).resolve()
    root = _mapping(yaml.safe_load(concept_path.read_text(encoding="utf-8")), "concept map")
    _exact_keys(root, {"definition_version", "metrics"}, "concept map")
    canonical = _mapping(policy.payload["canonical_metrics"], "canonical_metrics")
    if root["definition_version"] != canonical["concept_map_version"]:
        raise FinancialContractError("Concept-map version does not match financial policy")
    raw_metrics = _mapping(root["metrics"], "concept map metrics")
    required = list(canonical["required_metrics"])
    if set(raw_metrics) != set(required):
        raise FinancialContractError("Concept-map metric set does not match policy")
    duration = set(canonical["duration_metrics"])
    allowed_signs = {
        "preserve",
        "positive_expense",
        "positive_outflow",
        "positive_liability",
    }
    metrics: list[FinancialMetric] = []
    for metric_name in required:
        raw = _mapping(raw_metrics[metric_name], f"metric {metric_name}")
        _exact_keys(
            raw,
            {
                "statement_type",
                "period_type",
                "sign_policy",
                "us-gaap",
                "ifrs-full",
            },
            f"metric {metric_name}",
        )
        period_type = str(raw["period_type"])
        expected_period = "duration" if metric_name in duration else "instant"
        if (
            raw["statement_type"] not in {"income", "balance_sheet", "cash_flow"}
            or period_type != expected_period
            or raw["sign_policy"] not in allowed_signs
        ):
            raise FinancialContractError(f"Metric semantics are invalid for {metric_name}")
        concepts: dict[str, tuple[str, ...]] = {}
        for taxonomy in ("us-gaap", "ifrs-full"):
            values = tuple(str(value) for value in raw[taxonomy])
            if not values or len(values) != len(set(values)):
                raise FinancialContractError(
                    f"Metric {metric_name} has invalid {taxonomy} concepts"
                )
            concepts[taxonomy] = values
        metrics.append(
            FinancialMetric(
                canonical_metric=metric_name,
                statement_type=str(raw["statement_type"]),
                period_type=period_type,
                sign_policy=str(raw["sign_policy"]),
                concepts=concepts,
            )
        )
    return tuple(metrics)


def validate_financial_data_manifest(
    path: str | Path,
    policy: FinancialDataPolicy,
    package_root: str | Path,
) -> FinancialManifest:
    manifest_path = Path(path).resolve()
    raw_bytes = manifest_path.read_bytes()
    root = _mapping(yaml.safe_load(raw_bytes.decode("utf-8")), "financial manifest")
    _exact_keys(
        root,
        {
            "manifest_version",
            "artifact_id",
            "policy_version",
            "policy_sha256",
            "contract_as_of_date",
            "state",
            "calibration_eligible",
            "artifacts",
        },
        "financial manifest",
    )
    if (
        root["manifest_version"] != 1
        or root["artifact_id"] != "basic_materials_financial_contract_v1"
        or root["policy_version"] != policy.version
        or root["policy_sha256"] != policy.checksum
        or root["contract_as_of_date"] != policy.as_of_date
        or root["state"] != "stage4a_profile_contract_calibration_blocked"
        or root["calibration_eligible"] is not False
    ):
        raise FinancialContractError("Financial manifest header does not match policy")
    expected_artifacts = {
        "reporting_profiles": (
            "basic_materials_reporting_profile_review",
            policy.expected_total_profiles,
            "profile_key",
        ),
        "financial_concept_map": (
            "basic_materials_financial_metric_policy",
            policy.expected_metrics,
            "canonical_metric",
        ),
        "reporting_overrides": (
            "basic_materials_manual_override",
            0,
            "ticker|field_name|effective_from",
        ),
    }
    raw_artifacts = _mapping(root["artifacts"], "manifest artifacts")
    if set(raw_artifacts) != set(expected_artifacts):
        raise FinancialContractError("Financial manifest artifact set is invalid")
    owned_root = Path(package_root).resolve()
    artifacts: dict[str, FinancialArtifact] = {}
    for name, expected_values in expected_artifacts.items():
        raw = _mapping(raw_artifacts[name], f"artifact {name}")
        _exact_keys(
            raw,
            {"path", "source_id", "sha256", "byte_size", "row_count", "unique_key"},
            f"artifact {name}",
        )
        artifact_path = (manifest_path.parent / str(raw["path"])).resolve()
        try:
            artifact_path.relative_to(owned_root)
        except ValueError as exc:
            raise FinancialContractError(f"Artifact {name} escapes Basic Materials") from exc
        if not artifact_path.is_file():
            raise FinancialContractError(f"Artifact {name} is missing")
        expected_source, expected_rows, expected_key = expected_values
        if (
            raw["source_id"] != expected_source
            or int(raw["row_count"]) != expected_rows
            or raw["unique_key"] != expected_key
            or int(raw["byte_size"]) != artifact_path.stat().st_size
            or raw["sha256"] != _sha256(artifact_path)
        ):
            raise FinancialContractError(f"Artifact {name} does not match manifest")
        artifacts[name] = FinancialArtifact(
            name=name,
            path=artifact_path,
            source_id=str(raw["source_id"]),
            sha256=str(raw["sha256"]),
            byte_size=int(raw["byte_size"]),
            row_count=int(raw["row_count"]),
            unique_key=str(raw["unique_key"]),
        )
    return FinancialManifest(
        path=manifest_path,
        checksum=hashlib.sha256(raw_bytes).hexdigest(),
        policy_version=policy.version,
        as_of_date=policy.as_of_date,
        artifacts=artifacts,
    )


def _timestamp_on_or_before(value: str, cutoff: str, context: str) -> None:
    if not value:
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinancialContractError(f"{context} is not an ISO timestamp") from exc
    if value[:10] > cutoff:
        raise FinancialContractError(f"{context} is after the governed cutoff")


def read_and_validate_financial_contract(
    *,
    policy: FinancialDataPolicy,
    manifest: FinancialManifest,
    universe_path: str | Path,
    historical_membership_path: str | Path,
) -> FinancialContractBundle:
    profile_artifact = manifest.artifacts["reporting_profiles"]
    fieldnames, profiles = _read_csv(profile_artifact.path)
    if fieldnames != PROFILE_FIELDS:
        raise FinancialContractError("Reporting-profile columns do not match contract")
    if len(profiles) != policy.expected_total_profiles:
        raise FinancialContractError("Reporting-profile row count does not match policy")
    expected_seeds = {
        seed.profile_key: seed
        for seed in issuer_seeds(
            universe_path,
            historical_membership_path,
            as_of=policy.as_of_date,
        )
    }
    profile_controls = _mapping(
        policy.payload["reporting_profiles"],
        "reporting_profiles",
    )
    allowed_statuses = set(profile_controls["allowed_profile_statuses"])
    allowed_forms = set(profile_controls["allowed_annual_forms"])
    allowed_regimes = set(profile_controls["allowed_filing_regimes"])
    allowed_bases = set(profile_controls["allowed_accounting_bases"])
    sources = _mapping(policy.payload["sources"], "sources")
    seen_profiles: set[str] = set()
    seen_tickers: set[str] = set()
    seen_ciks: set[str] = set()
    role_counts: Counter[str] = Counter()
    for index, row in enumerate(profiles, start=2):
        context = f"reporting profile row {index}"
        key = row["profile_key"]
        seed = expected_seeds.get(key)
        if seed is None or key in seen_profiles:
            raise FinancialContractError(f"{context} has invalid or duplicate profile_key")
        if row["ticker"] in seen_tickers or row["cik"] in seen_ciks:
            raise FinancialContractError(f"{context} duplicates ticker or CIK")
        seen_profiles.add(key)
        seen_tickers.add(row["ticker"])
        seen_ciks.add(row["cik"])
        role_counts[row["role_type"]] += 1
        expected_seed_fields = {
            "profile_key": seed.profile_key,
            "role_type": seed.role_type,
            "ticker": seed.ticker,
            "cik": seed.cik,
            "company_name": seed.company_name,
            "domicile_country": seed.domicile_country,
            "trading_currency": seed.trading_currency,
            "profile_asof_date": seed.profile_asof_date,
            "source_cutoff_date": seed.source_cutoff_date,
        }
        if any(row[field] != value for field, value in expected_seed_fields.items()):
            raise FinancialContractError(f"{context} does not match the governed universe")
        if row["primary_annual_form"] not in allowed_forms:
            raise FinancialContractError(f"{context} has an invalid annual form")
        if row["filing_regime"] not in allowed_regimes:
            raise FinancialContractError(f"{context} has an invalid filing regime")
        if row["accounting_basis"] not in allowed_bases:
            raise FinancialContractError(f"{context} has an invalid accounting basis")
        if row["profile_status"] not in allowed_statuses:
            raise FinancialContractError(f"{context} has an invalid profile status")
        form_contract = {
            "10-K": ("domestic_sec", "quarterly"),
            "20-F": ("foreign_private_issuer", "annual_or_interim_6k"),
            "40-F": ("canadian_mjds", "annual_or_interim_6k"),
            "UNKNOWN": ("unknown", "unknown"),
        }
        if (
            row["filing_regime"],
            row["expected_cadence"],
        ) != form_contract[row["primary_annual_form"]]:
            raise FinancialContractError(f"{context} has inconsistent form/cadence fields")
        basis_contract = {
            "us-gaap": "US_GAAP",
            "ifrs-full": "IFRS",
            "mixed": "MIXED_REVIEW",
            "unknown": "UNKNOWN",
        }
        if row["accounting_basis"] != basis_contract.get(row["primary_taxonomy"]):
            raise FinancialContractError(f"{context} has inconsistent taxonomy/basis")
        currency = row["reporting_currency"]
        method = row["reporting_currency_method"]
        if (not currency and method != "unresolved") or (
            currency and (len(currency) != 3 or not currency.isalpha() or method == "unresolved")
        ):
            raise FinancialContractError(f"{context} has invalid reporting-currency evidence")
        if method == "listing_currency_default":
            raise FinancialContractError(f"{context} uses prohibited listing-currency fallback")
        if row["submissions_source_id"] != sources["submissions_source_id"]:
            raise FinancialContractError(f"{context} has the wrong submissions source")
        if row["companyfacts_source_id"] != sources["companyfacts_source_id"]:
            raise FinancialContractError(f"{context} has the wrong Company Facts source")
        if seed.role_type == "current_universe" and len(row["submissions_sha256"]) != 64:
            raise FinancialContractError(f"{context} lacks current SEC submissions evidence")
        for field in ("submissions_sha256", "companyfacts_sha256", "profile_sha256"):
            if row[field] and (
                len(row[field]) != 64
                or any(character not in "0123456789abcdef" for character in row[field])
            ):
                raise FinancialContractError(f"{context} has an invalid {field}")
        if row["profile_sha256"] != profile_hash(row):
            raise FinancialContractError(f"{context} profile hash is invalid")
        if row["contract_version"] != policy.version:
            raise FinancialContractError(f"{context} has the wrong contract version")
        if row["calibration_eligible"] != "0" or row["evidence_label"] != "fact_source_reported":
            raise FinancialContractError(f"{context} activates or mislabels evidence")
        if row["profile_status"] != "ready_for_ingestion" and not row["review_reason"]:
            raise FinancialContractError(f"{context} lacks a review reason")
        if row["profile_status"] == "ready_for_ingestion" and (
            row["primary_annual_form"] == "UNKNOWN"
            or row["primary_taxonomy"] in {"unknown", "mixed"}
            or not row["reporting_currency"]
            or not row["fiscal_year_end"]
            or not row["companyfacts_sha256"]
        ):
            raise FinancialContractError(f"{context} is not actually ingestion-ready")
        for field in (
            "latest_financial_accepted_at",
            "latest_annual_accepted_at",
            "latest_companyfacts_accepted_at",
        ):
            _timestamp_on_or_before(row[field], row["source_cutoff_date"], f"{context}.{field}")
        if row["companyfacts_lag_days"]:
            int(row["companyfacts_lag_days"])
        if row["inline_xbrl_fallback_expected"] not in {"0", "1"}:
            raise FinancialContractError(f"{context} has an invalid fallback flag")
        try:
            confidence = float(row["confidence"])
        except ValueError as exc:
            raise FinancialContractError(f"{context} has invalid confidence") from exc
        if not 0 <= confidence <= 1:
            raise FinancialContractError(f"{context} confidence is out of bounds")
        try:
            evidence = json.loads(row["evidence_json"])
        except json.JSONDecodeError as exc:
            raise FinancialContractError(f"{context} evidence_json is invalid") from exc
        if evidence.get("trading_currency_used_as_reporting_fallback") is not False:
            raise FinancialContractError(f"{context} violates reporting-currency policy")
    if seen_profiles != set(expected_seeds):
        raise FinancialContractError("Reporting-profile contract misses governed issuers")
    if role_counts != Counter(
        {
            "current_universe": policy.expected_current_profiles,
            "historical_pilot": policy.expected_historical_profiles,
        }
    ):
        raise FinancialContractError("Reporting-profile role counts are invalid")

    metrics = load_financial_concept_map(
        manifest.artifacts["financial_concept_map"].path,
        policy,
    )
    override_fields, overrides = _read_csv(manifest.artifacts["reporting_overrides"].path)
    expected_override_fields = (
        "ticker",
        "field_name",
        "override_value",
        "effective_from",
        "effective_to",
        "reason",
        "evidence_url",
        "reviewed_on",
    )
    if override_fields != expected_override_fields or overrides:
        raise FinancialContractError("Stage 4A reporting overrides must be an empty exact contract")
    return FinancialContractBundle(
        profiles=tuple(profiles),
        metrics=metrics,
        overrides=tuple(overrides),
    )


def _insert_contract_payloads(
    conn: sqlite3.Connection,
    manifest: FinancialManifest,
    *,
    loaded_at: str,
) -> int:
    media_types = {
        "reporting_profiles": "text/csv",
        "financial_concept_map": "application/yaml",
        "reporting_overrides": "text/csv",
    }
    for name, artifact in manifest.artifacts.items():
        conn.execute(
            """
            INSERT INTO raw_source_payloads (
                snapshot_id, source_id, source_snapshot_date, source_path, sha256,
                byte_size, row_count, media_type, payload, manifest_version,
                ingested_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                source_id = excluded.source_id,
                source_snapshot_date = excluded.source_snapshot_date,
                source_path = excluded.source_path,
                byte_size = excluded.byte_size,
                row_count = excluded.row_count,
                media_type = excluded.media_type,
                payload = excluded.payload,
                manifest_version = excluded.manifest_version,
                ingested_at_utc = excluded.ingested_at_utc
            """,
            (
                f"{artifact.source_id}:{artifact.sha256[:24]}",
                artifact.source_id,
                manifest.as_of_date,
                str(artifact.path),
                artifact.sha256,
                artifact.byte_size,
                artifact.row_count,
                media_types[name],
                artifact.path.read_bytes(),
                manifest.policy_version,
                loaded_at,
            ),
        )
    return len(manifest.artifacts)


def load_financial_contract(
    conn: sqlite3.Connection,
    *,
    policy: FinancialDataPolicy,
    manifest: FinancialManifest,
    bundle: FinancialContractBundle,
) -> FinancialContractLoadStats:
    assert_database_identity(conn)
    now = utc_now()
    metric_source = str(policy.payload["sources"]["metric_policy_source_id"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        raw_count = _insert_contract_payloads(conn, manifest, loaded_at=now)
        metric_ids: dict[str, int] = {}
        for metric in bundle.metrics:
            conn.execute(
                """
                INSERT INTO dim_financial_metric (
                    canonical_metric, statement_type, period_type, sign_policy,
                    definition_version, source_id, contract_sha256,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_metric) DO UPDATE SET
                    statement_type = excluded.statement_type,
                    period_type = excluded.period_type,
                    sign_policy = excluded.sign_policy,
                    definition_version = excluded.definition_version,
                    source_id = excluded.source_id,
                    contract_sha256 = excluded.contract_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    metric.canonical_metric,
                    metric.statement_type,
                    metric.period_type,
                    metric.sign_policy,
                    str(policy.payload["canonical_metrics"]["concept_map_version"]),
                    metric_source,
                    manifest.checksum,
                    now,
                    now,
                ),
            )
            metric_ids[metric.canonical_metric] = int(
                conn.execute(
                    "SELECT metric_id FROM dim_financial_metric WHERE canonical_metric = ?",
                    (metric.canonical_metric,),
                ).fetchone()[0]
            )
        conn.execute(
            "DELETE FROM bridge_financial_metric_concept "
            "WHERE metric_id IN (SELECT metric_id FROM dim_financial_metric)"
        )
        concept_links = 0
        for metric in bundle.metrics:
            metric_id = metric_ids[metric.canonical_metric]
            for taxonomy, concepts in metric.concepts.items():
                for priority, concept in enumerate(concepts, start=1):
                    conn.execute(
                        """
                        INSERT INTO bridge_financial_metric_concept (
                            metric_id, taxonomy, concept, priority, source_id,
                            contract_version, contract_sha256,
                            created_at_utc, updated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            metric_id,
                            taxonomy,
                            concept,
                            priority,
                            metric_source,
                            policy.version,
                            manifest.checksum,
                            now,
                            now,
                        ),
                    )
                    concept_links += 1
        for profile in bundle.profiles:
            identity = conn.execute(
                """
                SELECT s.security_id, c.company_id, c.cik
                FROM dim_security AS s
                JOIN dim_company AS c ON c.company_id = s.company_id
                WHERE UPPER(s.ticker) = ?
                """,
                (profile["ticker"],),
            ).fetchone()
            if identity is None or str(identity["cik"]) != profile["cik"]:
                raise FinancialContractError(
                    f"Reporting profile identity is absent for {profile['ticker']}"
                )
            conn.execute(
                """
                INSERT INTO dim_issuer_reporting_profile (
                    profile_key, company_id, security_id, ticker, role_type,
                    profile_asof_date, source_cutoff_date, cik, sec_entity_name,
                    domicile_country, primary_annual_form, filing_regime,
                    accounting_basis, primary_taxonomy, fiscal_year_end,
                    reporting_currency, reporting_currency_method, trading_currency,
                    expected_cadence, latest_financial_form,
                    latest_financial_accession, latest_financial_accepted_at,
                    latest_annual_accession, latest_annual_accepted_at,
                    latest_companyfacts_accepted_at, companyfacts_lag_days,
                    inline_xbrl_fallback_expected, submissions_source_id,
                    companyfacts_source_id, submissions_sha256, companyfacts_sha256,
                    profile_status, review_reason, confidence, calibration_eligible,
                    evidence_json, contract_version, profile_sha256, contract_sha256,
                    created_at_utc, updated_at_utc
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(profile_key) DO UPDATE SET
                    company_id = excluded.company_id,
                    security_id = excluded.security_id,
                    ticker = excluded.ticker,
                    role_type = excluded.role_type,
                    profile_asof_date = excluded.profile_asof_date,
                    source_cutoff_date = excluded.source_cutoff_date,
                    cik = excluded.cik,
                    sec_entity_name = excluded.sec_entity_name,
                    domicile_country = excluded.domicile_country,
                    primary_annual_form = excluded.primary_annual_form,
                    filing_regime = excluded.filing_regime,
                    accounting_basis = excluded.accounting_basis,
                    primary_taxonomy = excluded.primary_taxonomy,
                    fiscal_year_end = excluded.fiscal_year_end,
                    reporting_currency = excluded.reporting_currency,
                    reporting_currency_method = excluded.reporting_currency_method,
                    trading_currency = excluded.trading_currency,
                    expected_cadence = excluded.expected_cadence,
                    latest_financial_form = excluded.latest_financial_form,
                    latest_financial_accession = excluded.latest_financial_accession,
                    latest_financial_accepted_at = excluded.latest_financial_accepted_at,
                    latest_annual_accession = excluded.latest_annual_accession,
                    latest_annual_accepted_at = excluded.latest_annual_accepted_at,
                    latest_companyfacts_accepted_at = excluded.latest_companyfacts_accepted_at,
                    companyfacts_lag_days = excluded.companyfacts_lag_days,
                    inline_xbrl_fallback_expected = excluded.inline_xbrl_fallback_expected,
                    submissions_source_id = excluded.submissions_source_id,
                    companyfacts_source_id = excluded.companyfacts_source_id,
                    submissions_sha256 = excluded.submissions_sha256,
                    companyfacts_sha256 = excluded.companyfacts_sha256,
                    profile_status = excluded.profile_status,
                    review_reason = excluded.review_reason,
                    confidence = excluded.confidence,
                    calibration_eligible = excluded.calibration_eligible,
                    evidence_json = excluded.evidence_json,
                    contract_version = excluded.contract_version,
                    profile_sha256 = excluded.profile_sha256,
                    contract_sha256 = excluded.contract_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    profile["profile_key"],
                    int(identity["company_id"]),
                    int(identity["security_id"]),
                    profile["ticker"],
                    profile["role_type"],
                    profile["profile_asof_date"],
                    profile["source_cutoff_date"],
                    profile["cik"],
                    profile["sec_entity_name"],
                    profile["domicile_country"],
                    profile["primary_annual_form"],
                    profile["filing_regime"],
                    profile["accounting_basis"],
                    profile["primary_taxonomy"],
                    profile["fiscal_year_end"],
                    profile["reporting_currency"],
                    profile["reporting_currency_method"],
                    profile["trading_currency"],
                    profile["expected_cadence"],
                    profile["latest_financial_form"],
                    profile["latest_financial_accession"],
                    profile["latest_financial_accepted_at"],
                    profile["latest_annual_accession"],
                    profile["latest_annual_accepted_at"],
                    profile["latest_companyfacts_accepted_at"],
                    int(profile["companyfacts_lag_days"])
                    if profile["companyfacts_lag_days"]
                    else None,
                    int(profile["inline_xbrl_fallback_expected"]),
                    profile["submissions_source_id"],
                    profile["companyfacts_source_id"],
                    profile["submissions_sha256"],
                    profile["companyfacts_sha256"],
                    profile["profile_status"],
                    profile["review_reason"],
                    float(profile["confidence"]),
                    0,
                    profile["evidence_json"],
                    policy.version,
                    profile["profile_sha256"],
                    manifest.checksum,
                    now,
                    now,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return FinancialContractLoadStats(
        profiles=len(bundle.profiles),
        current_profiles=sum(row["role_type"] == "current_universe" for row in bundle.profiles),
        historical_profiles=sum(
            row["role_type"] == "historical_pilot" for row in bundle.profiles
        ),
        metrics=len(bundle.metrics),
        concept_links=concept_links,
        raw_contract_payloads=raw_count,
        contract_sha256=manifest.checksum,
    )


def validate_financial_contract_database(
    conn: sqlite3.Connection,
    *,
    policy: FinancialDataPolicy,
    manifest: FinancialManifest,
    bundle: FinancialContractBundle,
) -> FinancialContractValidationReport:
    assert_database_identity(conn)
    issues: list[FinancialValidationIssue] = []
    expected_concepts = sum(
        len(concepts)
        for metric in bundle.metrics
        for concepts in metric.concepts.values()
    )
    actual_counts = {
        "profiles": int(
            conn.execute("SELECT COUNT(*) FROM dim_issuer_reporting_profile").fetchone()[0]
        ),
        "current_profiles": int(
            conn.execute(
                "SELECT COUNT(*) FROM dim_issuer_reporting_profile "
                "WHERE role_type = 'current_universe'"
            ).fetchone()[0]
        ),
        "historical_profiles": int(
            conn.execute(
                "SELECT COUNT(*) FROM dim_issuer_reporting_profile "
                "WHERE role_type = 'historical_pilot'"
            ).fetchone()[0]
        ),
        "metrics": int(conn.execute("SELECT COUNT(*) FROM dim_financial_metric").fetchone()[0]),
        "concept_links": int(
            conn.execute("SELECT COUNT(*) FROM bridge_financial_metric_concept").fetchone()[0]
        ),
    }
    expected_counts = {
        "profiles": policy.expected_total_profiles,
        "current_profiles": policy.expected_current_profiles,
        "historical_profiles": policy.expected_historical_profiles,
        "metrics": policy.expected_metrics,
        "concept_links": expected_concepts,
    }
    for name, expected in expected_counts.items():
        if actual_counts[name] != expected:
            issues.append(
                FinancialValidationIssue(
                    "error",
                    "FINANCIAL_CONTRACT_COUNT_MISMATCH",
                    f"{name} expected {expected}, found {actual_counts[name]}",
                )
            )
    expected_profile_hashes = {
        row["profile_key"]: row["profile_sha256"] for row in bundle.profiles
    }
    actual_profile_hashes = {
        str(row["profile_key"]): str(row["profile_sha256"])
        for row in conn.execute(
            """
            SELECT profile_key, profile_sha256
            FROM dim_issuer_reporting_profile
            WHERE contract_version = ? AND contract_sha256 = ?
            """,
            (policy.version, manifest.checksum),
        )
    }
    if actual_profile_hashes != expected_profile_hashes:
        issues.append(
            FinancialValidationIssue(
                "error",
                "REPORTING_PROFILE_CONTRACT_MISMATCH",
                "Loaded reporting profiles do not match the sealed row hashes",
            )
        )
    expected_metric_names = {metric.canonical_metric for metric in bundle.metrics}
    actual_metric_names = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT canonical_metric
            FROM dim_financial_metric
            WHERE definition_version = ? AND contract_sha256 = ?
            """,
            (
                str(policy.payload["canonical_metrics"]["concept_map_version"]),
                manifest.checksum,
            ),
        )
    }
    if actual_metric_names != expected_metric_names:
        issues.append(
            FinancialValidationIssue(
                "error",
                "FINANCIAL_METRIC_CONTRACT_MISMATCH",
                "Loaded canonical metric set does not match the sealed concept map",
            )
        )
    missing_sources = [
        source_id
        for source_id in (
            policy.payload["sources"]["reporting_profile_source_id"],
            policy.payload["sources"]["metric_policy_source_id"],
            policy.payload["sources"]["submissions_source_id"],
            policy.payload["sources"]["companyfacts_source_id"],
            policy.payload["sources"]["inline_xbrl_source_id"],
            policy.payload["sources"]["fx_source_id"],
        )
        if conn.execute(
            "SELECT 1 FROM source_registry WHERE source_id = ? AND active = 1",
            (source_id,),
        ).fetchone()
        is None
    ]
    if missing_sources:
        issues.append(
            FinancialValidationIssue(
                "error",
                "FINANCIAL_SOURCE_REGISTRY_MISSING",
                "Required Stage 4 sources are missing or inactive",
                details={"source_ids": missing_sources},
            )
        )
    raw_matches = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM raw_source_payloads
            WHERE (source_id, sha256) IN (
                (?, ?), (?, ?), (?, ?)
            )
            """,
            (
                manifest.artifacts["reporting_profiles"].source_id,
                manifest.artifacts["reporting_profiles"].sha256,
                manifest.artifacts["financial_concept_map"].source_id,
                manifest.artifacts["financial_concept_map"].sha256,
                manifest.artifacts["reporting_overrides"].source_id,
                manifest.artifacts["reporting_overrides"].sha256,
            ),
        ).fetchone()[0]
    )
    if raw_matches != 3:
        issues.append(
            FinancialValidationIssue(
                "error",
                "FINANCIAL_CONTRACT_RAW_PAYLOAD_MISSING",
                f"Expected three sealed contract payloads, found {raw_matches}",
            )
        )
    unsafe_control = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM model_control_state
            WHERE promotion_state <> 'shadow_monitor'
               OR portfolio_candidate_gate <> 0
               OR oos_score_valid_flag <> 0
               OR current_universe_calibration_eligible <> 0
            """
        ).fetchone()[0]
    )
    unsafe_profiles = int(
        conn.execute(
            "SELECT COUNT(*) FROM dim_issuer_reporting_profile "
            "WHERE calibration_eligible <> 0"
        ).fetchone()[0]
    )
    if unsafe_control or unsafe_profiles:
        issues.append(
            FinancialValidationIssue(
                "error",
                "FINANCIAL_CONTRACT_PROMOTION_VIOLATION",
                "Stage 4A activated a prohibited calibration or portfolio state",
            )
        )
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        issues.append(
            FinancialValidationIssue(
                "error",
                "FINANCIAL_CONTRACT_FOREIGN_KEY_ERROR",
                f"Database has {len(foreign_key_errors)} foreign-key violations",
            )
        )

    profile_status_counts = {
        str(row["profile_status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT profile_status, COUNT(*) AS count
            FROM dim_issuer_reporting_profile
            GROUP BY profile_status ORDER BY profile_status
            """
        )
    }
    filing_regime_counts = {
        str(row["filing_regime"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT filing_regime, COUNT(*) AS count
            FROM dim_issuer_reporting_profile
            GROUP BY filing_regime ORDER BY filing_regime
            """
        )
    }
    accounting_basis_counts = {
        str(row["accounting_basis"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT accounting_basis, COUNT(*) AS count
            FROM dim_issuer_reporting_profile
            GROUP BY accounting_basis ORDER BY accounting_basis
            """
        )
    }
    reporting_currency_counts = {
        str(row["reporting_currency"] or "UNRESOLVED"): int(row["count"])
        for row in conn.execute(
            """
            SELECT reporting_currency, COUNT(*) AS count
            FROM dim_issuer_reporting_profile
            GROUP BY reporting_currency ORDER BY reporting_currency
            """
        )
    }
    review_counts = {
        status: count
        for status, count in profile_status_counts.items()
        if status != "ready_for_ingestion"
    }
    if review_counts:
        issues.append(
            FinancialValidationIssue(
                "warning",
                "REPORTING_PROFILE_REVIEW_QUEUE_OPEN",
                f"{sum(review_counts.values())} issuer profiles require targeted Stage 4B handling",
                details={"status_counts": review_counts},
            )
        )
    ingestion_counts = {
        "snapshots": int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_financial_ingestion_snapshot"
            ).fetchone()[0]
        ),
        "filings": int(conn.execute("SELECT COUNT(*) FROM fact_sec_filing").fetchone()[0]),
        "raw_facts": int(
            conn.execute("SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw").fetchone()[0]
        ),
        "canonical_facts": int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_financial_statement_canonical"
            ).fetchone()[0]
        ),
        "features": int(
            conn.execute("SELECT COUNT(*) FROM feature_financial_statement").fetchone()[0]
        ),
    }
    if not ingestion_counts["snapshots"]:
        issues.append(
            FinancialValidationIssue(
                "info",
                "FINANCIAL_INGESTION_NOT_STARTED",
                "Stage 4A contracts are loaded; SEC fact ingestion and features remain closed",
            )
        )
    issues.append(
        FinancialValidationIssue(
            "warning",
            "CALIBRATION_GATE_CLOSED",
            "Reporting profiles are engineering inputs and do not activate calibration or scoring",
        )
    )
    profile_columns = (
        "profile_key",
        "role_type",
        "ticker",
        "cik",
        "domicile_country",
        "primary_annual_form",
        "filing_regime",
        "accounting_basis",
        "fiscal_year_end",
        "reporting_currency",
        "expected_cadence",
        "latest_financial_form",
        "latest_financial_accepted_at",
        "profile_status",
        "review_reason",
        "confidence",
        "profile_sha256",
        "contract_sha256",
    )
    profiles = tuple(
        dict(row)
        for row in conn.execute(
            f"SELECT {', '.join(profile_columns)} "
            "FROM dim_issuer_reporting_profile ORDER BY role_type, ticker"
        )
    )
    metric_rows: list[dict[str, Any]] = []
    for metric in conn.execute(
        """
        SELECT metric_id, canonical_metric, statement_type, period_type,
               sign_policy, definition_version, contract_sha256
        FROM dim_financial_metric ORDER BY metric_id
        """
    ):
        concepts = conn.execute(
            """
            SELECT taxonomy, concept, priority
            FROM bridge_financial_metric_concept
            WHERE metric_id = ? ORDER BY taxonomy, priority
            """,
            (metric["metric_id"],),
        ).fetchall()
        metric_rows.append(
            {
                "canonical_metric": metric["canonical_metric"],
                "statement_type": metric["statement_type"],
                "period_type": metric["period_type"],
                "sign_policy": metric["sign_policy"],
                "us_gaap_concepts": "|".join(
                    str(row["concept"]) for row in concepts if row["taxonomy"] == "us-gaap"
                ),
                "ifrs_full_concepts": "|".join(
                    str(row["concept"]) for row in concepts if row["taxonomy"] == "ifrs-full"
                ),
                "definition_version": metric["definition_version"],
                "contract_sha256": metric["contract_sha256"],
            }
        )
    return FinancialContractValidationReport(
        passed=not any(issue.severity == "error" for issue in issues),
        policy_version=policy.version,
        manifest_checksum=manifest.checksum,
        as_of_date=policy.as_of_date,
        expected_counts=expected_counts,
        actual_counts=actual_counts,
        profile_status_counts=profile_status_counts,
        filing_regime_counts=filing_regime_counts,
        accounting_basis_counts=accounting_basis_counts,
        reporting_currency_counts=reporting_currency_counts,
        issues=tuple(issues),
        profiles=profiles,
        metrics=tuple(metric_rows),
        database_table_counts=database_counts(conn),
    )


def write_financial_contract_reports(
    report: FinancialContractValidationReport,
    *,
    report_dir: str | Path,
) -> dict[str, str]:
    target = Path(report_dir).resolve(strict=False)
    target.mkdir(parents=True, exist_ok=True)
    summary_path = target / "financial_contract_validation_summary.json"
    issues_path = target / "financial_contract_validation_issues.csv"
    profiles_path = target / "reporting_profile_snapshot.csv"
    metrics_path = target / "financial_metric_registry.csv"
    census_path = target / "reporting_profile_census.csv"
    artifact_path = target / "artifact_manifest.json"
    summary = {
        **report.summary_dict(),
        "database_counts": dict(report.database_table_counts),
        "validated_at_utc": utc_now(),
    }
    atomic_write_json(summary_path, summary)
    atomic_write_csv(
        issues_path,
        (
            {
                "severity": issue.severity,
                "issue_code": issue.issue_code,
                "ticker": issue.ticker,
                "message": issue.message,
                "details_json": json.dumps(issue.details or {}, sort_keys=True),
            }
            for issue in report.issues
        ),
        ("severity", "issue_code", "ticker", "message", "details_json"),
    )
    profile_fields = tuple(report.profiles[0]) if report.profiles else ("profile_key",)
    atomic_write_csv(profiles_path, report.profiles, profile_fields)
    metric_fields = tuple(report.metrics[0]) if report.metrics else ("canonical_metric",)
    atomic_write_csv(metrics_path, report.metrics, metric_fields)
    census: Counter[tuple[str, str, str, str, str]] = Counter()
    for row in report.profiles:
        census[
            (
                str(row["role_type"]),
                str(row["filing_regime"]),
                str(row["accounting_basis"]),
                str(row["profile_status"]),
                str(row["reporting_currency"] or "UNRESOLVED"),
            )
        ] += 1
    census_rows = [
        {
            "role_type": key[0],
            "filing_regime": key[1],
            "accounting_basis": key[2],
            "profile_status": key[3],
            "reporting_currency": key[4],
            "issuer_count": count,
        }
        for key, count in sorted(census.items())
    ]
    atomic_write_csv(
        census_path,
        census_rows,
        (
            "role_type",
            "filing_regime",
            "accounting_basis",
            "profile_status",
            "reporting_currency",
            "issuer_count",
        ),
    )
    row_counts = {
        summary_path: 1,
        issues_path: len(report.issues),
        profiles_path: len(report.profiles),
        metrics_path: len(report.metrics),
        census_path: len(census_rows),
    }
    artifacts = []
    for path, row_count in row_counts.items():
        artifacts.append(
            {
                "path": str(path),
                "sha256": _sha256(path),
                "byte_size": path.stat().st_size,
                "row_count": row_count,
            }
        )
    atomic_write_json(
        artifact_path,
        {
            "artifact_id": "basic_materials_stage4a_validation_v1",
            "as_of_date": report.as_of_date,
            "policy_version": report.policy_version,
            "contract_manifest_sha256": report.manifest_checksum,
            "passed": report.passed,
            "artifacts": artifacts,
        },
    )
    return {
        "summary": str(summary_path),
        "issues": str(issues_path),
        "profiles": str(profiles_path),
        "metrics": str(metrics_path),
        "census": str(census_path),
        "artifact_manifest": str(artifact_path),
    }
