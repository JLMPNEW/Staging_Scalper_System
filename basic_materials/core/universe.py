"""Strict current-universe policy and atomic loader."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence

import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.db import assert_database_identity, utc_now
from basic_materials.core.input_manifest import ManifestValidation


class UniversePolicyError(ValueError):
    """Raised when the versioned universe policy is invalid."""


class UniverseValidationError(ValueError):
    """Raised when source rows violate the universe policy."""


@dataclass(frozen=True)
class CohortRule:
    cohort_id: str
    display_name: str
    expected_count: int
    calibration_parent: str


@dataclass(frozen=True)
class UniversePolicy:
    policy_version: str
    sector: str
    source_id: str
    source_snapshot_date: str
    membership_basis: str
    expected_current_rows: int
    expected_foreign_rows: int
    calibration_group_rule: str
    default_lifecycle_state: str
    current_source_only: bool
    survivorship_corrected: bool
    calibration_eligible: bool
    required_columns: tuple[str, ...]
    allowed_values: Mapping[str, tuple[Any, ...]]
    cohorts: Mapping[str, CohortRule]

    def cohort_counts(self) -> dict[str, int]:
        return {cohort_id: rule.expected_count for cohort_id, rule in self.cohorts.items()}

    def cohort_parents(self) -> dict[str, str]:
        return {cohort_id: rule.calibration_parent for cohort_id, rule in self.cohorts.items()}


@dataclass(frozen=True)
class UniverseRow:
    ticker: str
    investability_status: str
    company_name: str
    cik: str
    exchange: str
    sector: str
    industry: str
    subsector: str
    country: str
    currency: str
    security_type: str
    listing_status: str
    is_primary_listing: bool
    calibration_group: str
    calibration_parent: str
    lifecycle_state: str
    calibration_group_derived: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UniverseLoadStats:
    rows_loaded: int
    companies: int
    securities: int
    memberships: int
    calibration_groups_derived: int
    foreign_rows: int
    cohort_counts: Mapping[str, int]
    policy_version: str
    input_sha256: str
    snapshot_id: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cohort_counts"] = dict(self.cohort_counts)
        return payload


_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")
_CIK_PATTERN = re.compile(r"^[0-9]{10}$")


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UniversePolicyError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise UniversePolicyError(
            f"Invalid keys for {context}; missing={sorted(expected - set(value))}, "
            f"unexpected={sorted(set(value) - expected)}"
        )


def _policy_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise UniversePolicyError(f"{context} must be true or false")
    return value


def _policy_count(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UniversePolicyError(f"{context} must be a non-negative integer")
    return value


def load_universe_policy(path: str | Path) -> UniversePolicy:
    policy_path = Path(path).resolve()
    if not policy_path.is_file():
        raise UniversePolicyError(f"Universe policy not found: {policy_path}")
    root = _mapping(yaml.safe_load(policy_path.read_text(encoding="utf-8")), "universe policy")
    expected_root = {
        "policy_version",
        "sector",
        "source_id",
        "source_snapshot_date",
        "membership_basis",
        "expected_current_rows",
        "expected_foreign_rows",
        "calibration_group_rule",
        "default_lifecycle_state",
        "current_source_only",
        "survivorship_corrected",
        "calibration_eligible",
        "required_columns",
        "allowed_values",
        "cohorts",
    }
    _exact_keys(root, expected_root, "universe policy")

    columns_raw = root["required_columns"]
    if not isinstance(columns_raw, Sequence) or isinstance(columns_raw, (str, bytes)):
        raise UniversePolicyError("required_columns must be a list")
    required_columns = tuple(str(value) for value in columns_raw)
    if len(required_columns) != len(set(required_columns)):
        raise UniversePolicyError("required_columns contains duplicates")

    allowed_raw = _mapping(root["allowed_values"], "allowed_values")
    expected_allowed = {
        "investability_status",
        "exchange",
        "currency",
        "security_type",
        "listing_status",
        "is_primary_listing",
    }
    _exact_keys(allowed_raw, expected_allowed, "allowed_values")
    allowed_values: dict[str, tuple[Any, ...]] = {}
    for field, values in allowed_raw.items():
        if not isinstance(values, list) or not values:
            raise UniversePolicyError(f"allowed_values.{field} must be a non-empty list")
        allowed_values[field] = tuple(values)

    cohorts_raw = _mapping(root["cohorts"], "cohorts")
    if not cohorts_raw:
        raise UniversePolicyError("At least one cohort is required")
    cohorts: dict[str, CohortRule] = {}
    for cohort_id, raw_rule in cohorts_raw.items():
        rule = _mapping(raw_rule, f"cohorts.{cohort_id}")
        _exact_keys(rule, {"display_name", "expected_count", "calibration_parent"}, f"cohorts.{cohort_id}")
        cohorts[str(cohort_id)] = CohortRule(
            cohort_id=str(cohort_id),
            display_name=str(rule["display_name"]).strip(),
            expected_count=_policy_count(rule["expected_count"], f"cohorts.{cohort_id}.expected_count"),
            calibration_parent=str(rule["calibration_parent"]).strip(),
        )

    policy = UniversePolicy(
        policy_version=str(root["policy_version"]),
        sector=str(root["sector"]),
        source_id=str(root["source_id"]),
        source_snapshot_date=str(root["source_snapshot_date"]),
        membership_basis=str(root["membership_basis"]),
        expected_current_rows=_policy_count(root["expected_current_rows"], "expected_current_rows"),
        expected_foreign_rows=_policy_count(root["expected_foreign_rows"], "expected_foreign_rows"),
        calibration_group_rule=str(root["calibration_group_rule"]),
        default_lifecycle_state=str(root["default_lifecycle_state"]),
        current_source_only=_policy_bool(root["current_source_only"], "current_source_only"),
        survivorship_corrected=_policy_bool(root["survivorship_corrected"], "survivorship_corrected"),
        calibration_eligible=_policy_bool(root["calibration_eligible"], "calibration_eligible"),
        required_columns=required_columns,
        allowed_values=allowed_values,
        cohorts=cohorts,
    )
    validate_policy_contract(policy)
    return policy


def validate_policy_contract(policy: UniversePolicy) -> None:
    if policy.policy_version != "basic_materials_universe_policy_v1":
        raise UniversePolicyError(f"Unsupported policy_version: {policy.policy_version}")
    if policy.sector != SECTOR:
        raise UniversePolicyError(f"Policy sector must be {SECTOR!r}")
    if policy.source_id != "basic_materials_current_universe":
        raise UniversePolicyError("Policy source_id is not package-owned")
    try:
        date.fromisoformat(policy.source_snapshot_date)
    except ValueError as exc:
        raise UniversePolicyError("source_snapshot_date must be ISO YYYY-MM-DD") from exc
    if policy.membership_basis != "current_authoritative_seed":
        raise UniversePolicyError("membership_basis must identify the current authoritative seed")
    if policy.calibration_group_rule != "subsector":
        raise UniversePolicyError("calibration_group_rule must be subsector")
    if policy.default_lifecycle_state != "unreviewed":
        raise UniversePolicyError("default_lifecycle_state must remain unreviewed")
    if not policy.current_source_only or policy.survivorship_corrected or policy.calibration_eligible:
        raise UniversePolicyError("Current universe historical/calibration flags violate the fail-closed contract")
    if sum(policy.cohort_counts().values()) != policy.expected_current_rows:
        raise UniversePolicyError("Cohort expected counts do not sum to expected_current_rows")


def _parse_bool(value: str, context: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise UniverseValidationError(f"{context} must be TRUE or FALSE, got {value!r}")


def read_and_validate_universe(path: str | Path, policy: UniversePolicy) -> list[UniverseRow]:
    source_path = Path(path).resolve()
    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual_columns = tuple(reader.fieldnames or ())
        if actual_columns != policy.required_columns:
            raise UniverseValidationError(
                f"Universe columns do not match policy; expected {policy.required_columns}, got {actual_columns}"
            )
        raw_rows = list(reader)

    if len(raw_rows) != policy.expected_current_rows:
        raise UniverseValidationError(
            f"Expected {policy.expected_current_rows} universe rows, found {len(raw_rows)}"
        )

    rows: list[UniverseRow] = []
    seen_tickers: set[str] = set()
    seen_ciks: set[str] = set()
    errors: list[str] = []
    for line_number, raw in enumerate(raw_rows, start=2):
        if None in raw:
            errors.append(f"line {line_number}: unexpected extra CSV fields")
            continue
        ticker = str(raw["ticker"]).strip().upper()
        cik = str(raw["cik"]).strip()
        subsector = str(raw["subsector"]).strip()
        input_calibration_group = str(raw["calibration_group"]).strip()
        context = f"line {line_number} ({ticker or 'blank ticker'})"

        row_errors: list[str] = []
        if not _TICKER_PATTERN.fullmatch(ticker):
            row_errors.append(f"invalid ticker {ticker!r}")
        if ticker in seen_tickers:
            row_errors.append(f"duplicate ticker {ticker!r}")
        if not _CIK_PATTERN.fullmatch(cik):
            row_errors.append(f"invalid ten-digit CIK {cik!r}")
        if cik in seen_ciks:
            row_errors.append(f"duplicate CIK {cik!r}")
        if str(raw["sector"]).strip() != policy.sector:
            row_errors.append(f"sector must be {policy.sector!r}")
        if subsector not in policy.cohorts:
            row_errors.append(f"unknown subsector/cohort {subsector!r}")
        for required_text in ("company_name", "industry", "country"):
            if not str(raw[required_text]).strip():
                row_errors.append(f"blank required field {required_text}")

        parsed_primary = False
        try:
            parsed_primary = _parse_bool(str(raw["is_primary_listing"]), f"{context}.is_primary_listing")
        except UniverseValidationError as exc:
            row_errors.append(str(exc))

        source_values: dict[str, Any] = {
            "investability_status": str(raw["investability_status"]).strip(),
            "exchange": str(raw["exchange"]).strip(),
            "currency": str(raw["currency"]).strip(),
            "security_type": str(raw["security_type"]).strip(),
            "listing_status": str(raw["listing_status"]).strip(),
            "is_primary_listing": parsed_primary,
        }
        for field, value in source_values.items():
            if value not in policy.allowed_values[field]:
                row_errors.append(f"{field} value {value!r} is not allowed")

        if input_calibration_group and input_calibration_group != subsector:
            row_errors.append(
                f"calibration_group {input_calibration_group!r} conflicts with subsector {subsector!r}"
            )
        if row_errors:
            errors.extend(f"{context}: {message}" for message in row_errors)
            continue

        seen_tickers.add(ticker)
        seen_ciks.add(cik)
        cohort_rule = policy.cohorts[subsector]
        rows.append(
            UniverseRow(
                ticker=ticker,
                investability_status=source_values["investability_status"],
                company_name=str(raw["company_name"]).strip(),
                cik=cik,
                exchange=source_values["exchange"],
                sector=policy.sector,
                industry=str(raw["industry"]).strip(),
                subsector=subsector,
                country=str(raw["country"]).strip(),
                currency=source_values["currency"],
                security_type=source_values["security_type"],
                listing_status=source_values["listing_status"],
                is_primary_listing=parsed_primary,
                calibration_group=subsector,
                calibration_parent=cohort_rule.calibration_parent,
                lifecycle_state=policy.default_lifecycle_state,
                calibration_group_derived=not bool(input_calibration_group),
            )
        )

    if errors:
        preview = "\n".join(errors[:25])
        remainder = len(errors) - min(len(errors), 25)
        suffix = f"\n... and {remainder} more errors" if remainder else ""
        raise UniverseValidationError(f"Universe validation failed:\n{preview}{suffix}")

    actual_counts = Counter(row.subsector for row in rows)
    expected_counts = policy.cohort_counts()
    if dict(sorted(actual_counts.items())) != dict(sorted(expected_counts.items())):
        raise UniverseValidationError(
            f"Cohort counts do not match policy: expected {expected_counts}, got {dict(actual_counts)}"
        )
    foreign_count = sum(row.country != "United States" for row in rows)
    if foreign_count != policy.expected_foreign_rows:
        raise UniverseValidationError(
            f"Expected {policy.expected_foreign_rows} foreign-domiciled rows, found {foreign_count}"
        )
    return sorted(rows, key=lambda row: row.ticker)


def _ensure_source_registered(conn: sqlite3.Connection, source_id: str) -> None:
    row = conn.execute(
        "SELECT active FROM source_registry WHERE source_id = ?", (source_id,)
    ).fetchone()
    if row is None or int(row["active"]) != 1:
        raise UniverseValidationError(f"Active source registry entry is required for {source_id}")


def _upsert_identifier(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    security_id: int | None,
    identifier_type: str,
    identifier_value: str,
    valid_from_date: str,
    source_id: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO dim_identifier (
            company_id, security_id, identifier_type, identifier_value, is_primary,
            valid_from_date, valid_to_date, source_id, created_at_utc
        ) VALUES (?, ?, ?, ?, 1, ?, NULL, ?, ?)
        ON CONFLICT(identifier_type, identifier_value, valid_from_date) DO UPDATE SET
            company_id = excluded.company_id,
            security_id = excluded.security_id,
            is_primary = 1,
            valid_to_date = NULL,
            source_id = excluded.source_id
        """,
        (
            company_id,
            security_id,
            identifier_type,
            identifier_value,
            valid_from_date,
            source_id,
            now,
        ),
    )


def load_universe(
    conn: sqlite3.Connection,
    *,
    policy: UniversePolicy,
    manifest: ManifestValidation,
) -> UniverseLoadStats:
    """Load all reviewed rows atomically after complete validation."""

    assert_database_identity(conn)
    if manifest.source_id != policy.source_id:
        raise UniverseValidationError("Manifest and policy source identifiers do not match")
    if manifest.row_count != policy.expected_current_rows:
        raise UniverseValidationError("Manifest and policy row counts do not match")
    rows = read_and_validate_universe(manifest.path, policy)
    _ensure_source_registered(conn, policy.source_id)
    if conn.in_transaction:
        raise RuntimeError("load_universe requires a clean connection with no active transaction")

    now = utc_now()
    snapshot_id = f"{policy.source_id}:{manifest.sha256}"
    incoming_tickers = {row.ticker for row in rows}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO raw_source_payloads (
                snapshot_id, source_id, source_snapshot_date, source_path, sha256,
                byte_size, row_count, media_type, payload, manifest_version, ingested_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'text/csv', ?, ?, ?)
            ON CONFLICT(snapshot_id) DO NOTHING
            """,
            (
                snapshot_id,
                policy.source_id,
                policy.source_snapshot_date,
                str(manifest.path),
                manifest.sha256,
                manifest.byte_size,
                manifest.row_count,
                manifest.path.read_bytes(),
                manifest.manifest_version,
                now,
            ),
        )

        existing_tickers = {
            str(record["ticker"]).upper(): str(record["cik"])
            for record in conn.execute(
                """
                SELECT s.ticker, c.cik
                FROM dim_security AS s
                JOIN dim_company AS c ON c.company_id = s.company_id
                """
            ).fetchall()
        }
        for row in rows:
            existing_cik = existing_tickers.get(row.ticker)
            if existing_cik is not None and existing_cik != row.cik:
                raise UniverseValidationError(
                    f"Ticker {row.ticker} is already attached to CIK {existing_cik}, not {row.cik}"
                )

            conn.execute(
                """
                INSERT INTO dim_company (
                    cik, legal_name, primary_ticker, domicile_country, universe_status,
                    is_active, first_seen_date, last_seen_date, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(cik) DO UPDATE SET
                    legal_name = excluded.legal_name,
                    primary_ticker = excluded.primary_ticker,
                    domicile_country = excluded.domicile_country,
                    universe_status = excluded.universe_status,
                    is_active = 1,
                    last_seen_date = excluded.last_seen_date,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    row.cik,
                    row.company_name,
                    row.ticker,
                    row.country,
                    row.investability_status,
                    policy.source_snapshot_date,
                    policy.source_snapshot_date,
                    now,
                    now,
                ),
            )
            company_id = int(
                conn.execute("SELECT company_id FROM dim_company WHERE cik = ?", (row.cik,)).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO dim_security (
                    company_id, ticker, exchange, trading_currency, security_type,
                    listing_status, is_primary_listing, source_id, valid_from_date,
                    valid_to_date, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    company_id = excluded.company_id,
                    exchange = excluded.exchange,
                    trading_currency = excluded.trading_currency,
                    security_type = excluded.security_type,
                    listing_status = excluded.listing_status,
                    is_primary_listing = excluded.is_primary_listing,
                    source_id = excluded.source_id,
                    valid_to_date = NULL,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    company_id,
                    row.ticker,
                    row.exchange,
                    row.currency,
                    row.security_type,
                    row.listing_status,
                    int(row.is_primary_listing),
                    policy.source_id,
                    policy.source_snapshot_date,
                    now,
                    now,
                ),
            )
            security_id = int(
                conn.execute("SELECT security_id FROM dim_security WHERE ticker = ?", (row.ticker,)).fetchone()[0]
            )
            _upsert_identifier(
                conn,
                company_id=company_id,
                security_id=None,
                identifier_type="cik",
                identifier_value=row.cik,
                valid_from_date=policy.source_snapshot_date,
                source_id=policy.source_id,
                now=now,
            )
            _upsert_identifier(
                conn,
                company_id=company_id,
                security_id=security_id,
                identifier_type="ticker",
                identifier_value=row.ticker,
                valid_from_date=policy.source_snapshot_date,
                source_id=policy.source_id,
                now=now,
            )
            conn.execute(
                """
                INSERT INTO dim_basic_materials_taxonomy (
                    company_id, security_id, ticker, sector, industry, cohort_id,
                    calibration_group, calibration_parent, lifecycle_state,
                    classification_confidence, source_id, policy_version, input_sha256,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)
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
                    row.ticker,
                    row.sector,
                    row.industry,
                    row.subsector,
                    row.calibration_group,
                    row.calibration_parent,
                    row.lifecycle_state,
                    policy.source_id,
                    policy.policy_version,
                    manifest.sha256,
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
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'current', ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id, membership_source_id, membership_start_date) DO UPDATE SET
                    company_id = excluded.company_id,
                    ticker = excluded.ticker,
                    cohort_id = excluded.cohort_id,
                    membership_end_date = NULL,
                    membership_status = 'current',
                    membership_basis = excluded.membership_basis,
                    current_source_only = excluded.current_source_only,
                    survivorship_corrected = excluded.survivorship_corrected,
                    calibration_eligible = excluded.calibration_eligible,
                    membership_confidence = excluded.membership_confidence,
                    source_snapshot_date = excluded.source_snapshot_date,
                    policy_version = excluded.policy_version,
                    input_sha256 = excluded.input_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    company_id,
                    security_id,
                    row.ticker,
                    MODEL_FAMILY,
                    row.subsector,
                    policy.source_snapshot_date,
                    policy.source_id,
                    policy.membership_basis,
                    int(policy.current_source_only),
                    int(policy.survivorship_corrected),
                    int(policy.calibration_eligible),
                    policy.source_snapshot_date,
                    policy.policy_version,
                    manifest.sha256,
                    now,
                    now,
                ),
            )

        stale = conn.execute(
            """
            SELECT s.security_id, s.company_id, s.ticker
            FROM dim_security AS s
            JOIN dim_universe_membership AS m ON m.security_id = s.security_id
            WHERE m.membership_source_id = ? AND m.membership_status = 'current'
            """,
            (policy.source_id,),
        ).fetchall()
        stale_rows = [record for record in stale if str(record["ticker"]).upper() not in incoming_tickers]
        for record in stale_rows:
            conn.execute(
                """
                UPDATE dim_universe_membership
                SET membership_status = 'historical', membership_end_date = ?, updated_at_utc = ?
                WHERE security_id = ? AND membership_source_id = ? AND membership_status = 'current'
                """,
                (policy.source_snapshot_date, now, record["security_id"], policy.source_id),
            )
            conn.execute(
                """
                UPDATE dim_security
                SET listing_status = 'inactive', valid_to_date = ?, updated_at_utc = ?
                WHERE security_id = ?
                """,
                (policy.source_snapshot_date, now, record["security_id"]),
            )
            conn.execute(
                "UPDATE dim_company SET is_active = 0, updated_at_utc = ? WHERE company_id = ?",
                (now, record["company_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    cohort_counts = dict(sorted(Counter(row.subsector for row in rows).items()))
    return UniverseLoadStats(
        rows_loaded=len(rows),
        companies=len({row.cik for row in rows}),
        securities=len({row.ticker for row in rows}),
        memberships=len(rows),
        calibration_groups_derived=sum(row.calibration_group_derived for row in rows),
        foreign_rows=sum(row.country != "United States" for row in rows),
        cohort_counts=cohort_counts,
        policy_version=policy.policy_version,
        input_sha256=manifest.sha256,
        snapshot_id=snapshot_id,
    )

