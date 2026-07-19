from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from industrials.core.config import load_yaml
from industrials.core.reports import write_csv_atomic
from industrials.core.text_norm import normalize_cik, normalize_ticker


MODEL_FAMILY = "machinery"
DEFAULT_HISTORY_START = "2019-01-02"

ACTIVE_REQUIRED_COLUMNS = {
    "ticker",
    "investability_status",
    "company_name",
    "cik",
    "exchange",
    "sector",
    "industry",
    "subsector",
    "country",
    "currency",
    "security_type",
    "listing_status",
    "is_primary_listing",
    "calibration_cohort",
}
DELISTED_REQUIRED_COLUMNS = {
    "ticker",
    "company",
    "cohort",
    "exit_type",
    "terminal_type",
    "exit_year",
    "cik",
    "confidence",
}
NORGATE_MAP_FIELDS = [
    "internal_ticker",
    "actual_ticker",
    "norgate_symbol",
    "source_database",
    "company_name",
    "norgate_security_name",
    "first_quoted_date",
    "last_quoted_date",
    "mapping_status",
    "mapping_reason",
    "name_similarity",
    "exit_year",
    "calibration_usable_flag",
    "review_status",
    "notes",
]
HISTORICAL_MEMBERSHIP_FIELDS = [
    "internal_ticker",
    "exchange_ticker",
    "price_source_symbol",
    "company_name",
    "cik",
    "exchange",
    "country",
    "currency",
    "security_type",
    "calibration_cohort_id",
    "calibration_cohort",
    "start_date",
    "end_date",
    "membership_status",
    "successor_ticker",
    "event_type",
    "confidence",
    "source_url",
    "notes",
]
LISTING_DATE_FIELDS = [
    "ticker",
    "first_eligible_date",
    "last_eligible_date",
    "eligibility_basis",
    "source",
    "confidence",
    "notes",
]


@dataclass(frozen=True)
class NorgateMapping:
    internal_ticker: str
    actual_ticker: str
    norgate_symbol: str
    source_database: str
    company_name: str
    norgate_security_name: str
    first_quoted_date: str
    last_quoted_date: str
    mapping_status: str
    mapping_reason: str
    name_similarity: str
    exit_year: str
    calibration_usable_flag: str
    review_status: str
    notes: str

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [
            {str(key): str(value or "").strip() for key, value in row.items() if key is not None}
            for row in reader
            if any(str(value or "").strip() for value in row.values())
        ]


def cohort_metadata(path: Path) -> dict[str, dict[str, str]]:
    payload = load_yaml(path)
    if str(payload.get("model_family") or "").strip() != MODEL_FAMILY:
        raise ValueError(f"{path}: model_family must be {MODEL_FAMILY}")
    raw_cohorts = payload.get("cohorts")
    if not isinstance(raw_cohorts, list):
        raise ValueError(f"{path}: cohorts must be a list")
    result: dict[str, dict[str, str]] = {}
    for raw in raw_cohorts:
        if not isinstance(raw, dict):
            continue
        cohort_id = str(raw.get("cohort_id") or "").strip()
        if not cohort_id:
            continue
        if cohort_id in result:
            raise ValueError(f"{path}: duplicate cohort_id={cohort_id}")
        result[cohort_id] = {str(key): str(value or "").strip() for key, value in raw.items()}
    if not result:
        raise ValueError(f"{path}: no cohort definitions")
    return result


def _header_errors(rows: list[dict[str, str]], required: set[str], label: str) -> list[str]:
    if not rows:
        return [f"{label}: no data rows"]
    missing = sorted(required - set(rows[0]))
    return [f"{label}: missing columns={missing}"] if missing else []


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def validate_seed_contracts(
    active_rows: list[dict[str, str]],
    delisted_rows: list[dict[str, str]],
    cohorts: dict[str, dict[str, str]],
    *,
    expected_active: int,
    expected_delisted: int,
) -> list[str]:
    errors = _header_errors(active_rows, ACTIVE_REQUIRED_COLUMNS, "active")
    errors.extend(_header_errors(delisted_rows, DELISTED_REQUIRED_COLUMNS, "delisted"))
    if errors:
        return errors
    if len(active_rows) != expected_active:
        errors.append(f"active: expected {expected_active} rows, found {len(active_rows)}")
    if len(delisted_rows) != expected_delisted:
        errors.append(f"delisted: expected {expected_delisted} rows, found {len(delisted_rows)}")

    active_tickers = [normalize_ticker(row.get("ticker")) for row in active_rows]
    delisted_tickers = [normalize_ticker(row.get("ticker")) for row in delisted_rows]
    if blanks := [index + 2 for index, ticker in enumerate(active_tickers) if not ticker]:
        # read_csv_rows filters fully-blank physical lines, so these are
        # header-relative data-row ordinals, not raw file line numbers.
        errors.append(f"active: blank/invalid tickers at data rows={blanks[:20]}")
    if duplicates := _duplicates(active_tickers):
        errors.append(f"active: duplicate tickers={duplicates}")
    if duplicates := _duplicates(delisted_tickers):
        errors.append(f"delisted: duplicate tickers={duplicates}")
    overlap = sorted(set(active_tickers).intersection(delisted_tickers))
    if overlap:
        errors.append(f"active/delisted ticker overlap={overlap}")

    ciks = [normalize_cik(row.get("cik")) for row in active_rows]
    if missing_cik := [active_tickers[index] for index, cik in enumerate(ciks) if not cik]:
        errors.append(f"active: missing/invalid CIK={missing_cik[:20]}")
    if duplicate_ciks := _duplicates([cik for cik in ciks if cik]):
        errors.append(f"active: duplicate CIKs={duplicate_ciks}")

    valid_security_types = {"common stock", "ordinary shares"}
    for index, row in enumerate(active_rows, start=2):
        ticker = normalize_ticker(row.get("ticker")) or f"line_{index}"
        required_blanks = sorted(field for field in ACTIVE_REQUIRED_COLUMNS if not str(row.get(field) or "").strip())
        if required_blanks:
            errors.append(f"active:{ticker}: blank required fields={required_blanks}")
        if str(row.get("sector") or "").strip() != "Industrials":
            errors.append(f"active:{ticker}: sector must be Industrials")
        if str(row.get("subsector") or "").strip() != "Machinery":
            errors.append(f"active:{ticker}: subsector must be Machinery")
        if str(row.get("listing_status") or "").strip().lower() != "active":
            errors.append(f"active:{ticker}: listing_status must be active")
        if str(row.get("investability_status") or "").strip().lower() != "investable":
            errors.append(f"active:{ticker}: investability_status must be investable")
        if str(row.get("is_primary_listing") or "").strip().lower() not in {"1", "true", "yes", "y"}:
            errors.append(f"active:{ticker}: is_primary_listing must be true")
        if str(row.get("security_type") or "").strip().lower() not in valid_security_types:
            errors.append(f"active:{ticker}: unsupported security_type={row.get('security_type')!r}")
        cohort = str(row.get("calibration_cohort") or "").strip()
        if cohort not in cohorts:
            errors.append(f"active:{ticker}: unknown calibration_cohort={cohort!r}")

    for index, row in enumerate(delisted_rows, start=2):
        ticker = normalize_ticker(row.get("ticker")) or f"line_{index}"
        cohort = str(row.get("cohort") or "").strip()
        if cohort not in cohorts:
            errors.append(f"delisted:{ticker}: unknown cohort={cohort!r}")
        try:
            exit_year = int(str(row.get("exit_year") or ""))
        except ValueError:
            errors.append(f"delisted:{ticker}: invalid exit_year={row.get('exit_year')!r}")
        else:
            if not 1900 <= exit_year <= date.today().year + 1:
                errors.append(f"delisted:{ticker}: exit_year out of range={exit_year}")
    return errors


def _normalized_name(value: object) -> str:
    text = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper())
    return " ".join(text.split())


def _similarity(left: object, right: object) -> float:
    left_text = _normalized_name(left)
    right_text = _normalized_name(right)
    if not left_text or not right_text:
        return 0.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def _date_text(value: object) -> str:
    text = str(value or "").strip()[:10]
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def _provider_text(provider: Any, method: str, symbol: str) -> str:
    try:
        return str(getattr(provider, method)(symbol) or "").strip()
    except Exception:
        return ""


def _provider_date(provider: Any, method: str, symbol: str) -> str:
    return _date_text(_provider_text(provider, method, symbol))


def load_norgate_overrides(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker"))
        symbol = str(row.get("norgate_symbol") or "").strip().upper()
        if not ticker and not symbol:
            continue
        if not ticker or not symbol:
            raise ValueError(f"{path}: override requires ticker and norgate_symbol")
        review_status = str(row.get("review_status") or "").strip().lower()
        if review_status not in {"reviewed", "approved"}:
            raise ValueError(f"{path}: {ticker} review_status must be reviewed or approved")
        if ticker in overrides:
            raise ValueError(f"{path}: duplicate ticker override={ticker}")
        overrides[ticker] = row
    return overrides


def _mapping(
    *,
    ticker: str,
    company_name: str,
    symbol: str = "",
    source_database: str = "",
    provider: Any,
    status: str,
    reason: str,
    exit_year: str = "",
    usable: bool = False,
    review_status: str = "",
    notes: str = "",
    first_override: str = "",
    last_override: str = "",
) -> NorgateMapping:
    norgate_name = _provider_text(provider, "security_name", symbol) if symbol else ""
    first_date = _date_text(first_override) or (_provider_date(provider, "first_quoted_date", symbol) if symbol else "")
    last_date = _date_text(last_override) or (_provider_date(provider, "last_quoted_date", symbol) if symbol else "")
    return NorgateMapping(
        internal_ticker=ticker,
        actual_ticker=ticker,
        norgate_symbol=symbol,
        source_database=source_database,
        company_name=company_name,
        norgate_security_name=norgate_name,
        first_quoted_date=first_date,
        last_quoted_date=last_date,
        mapping_status=status,
        mapping_reason=reason,
        name_similarity=f"{_similarity(company_name, norgate_name):.6f}" if norgate_name else "",
        exit_year=exit_year,
        calibration_usable_flag="1" if usable else "0",
        review_status=review_status,
        notes=notes,
    )


def resolve_norgate_mappings(
    *,
    active_rows: list[dict[str, str]],
    delisted_rows: list[dict[str, str]],
    provider: Any,
    history_start: str,
    known_exclusions: dict[str, str],
    overrides: dict[str, dict[str, str]],
) -> list[NorgateMapping]:
    current_symbols = {str(value or "").strip().upper() for value in provider.database_symbols("US Equities")}
    delisted_symbols = {
        str(value or "").strip().upper() for value in provider.database_symbols("US Equities Delisted")
    }
    mappings: list[NorgateMapping] = []

    for row in sorted(active_rows, key=lambda item: normalize_ticker(item.get("ticker"))):
        ticker = normalize_ticker(row.get("ticker"))
        company_name = str(row.get("company_name") or ticker).strip()
        if ticker not in current_symbols:
            mappings.append(
                _mapping(
                    ticker=ticker,
                    company_name=company_name,
                    provider=provider,
                    status="unresolved",
                    reason="active_ticker_not_found_in_norgate_us_equities",
                    notes="Active rows require an exact current Norgate symbol.",
                )
            )
            continue
        norgate_name = _provider_text(provider, "security_name", ticker)
        similarity = _similarity(company_name, norgate_name)
        verified = similarity >= 0.35
        mappings.append(
            _mapping(
                ticker=ticker,
                company_name=company_name,
                symbol=ticker,
                source_database="US Equities",
                provider=provider,
                status="verified_exact_active" if verified else "review_required",
                reason="exact_active_symbol_and_name_match" if verified else "exact_symbol_but_issuer_name_mismatch",
                usable=verified,
                review_status="auto_verified" if verified else "review_required",
            )
        )

    for row in sorted(delisted_rows, key=lambda item: normalize_ticker(item.get("ticker"))):
        ticker = normalize_ticker(row.get("ticker"))
        company_name = str(row.get("company") or ticker).strip()
        exit_year_text = str(row.get("exit_year") or "").strip()
        exit_year = int(exit_year_text) if exit_year_text.isdigit() else None
        override = overrides.get(ticker)
        if override is not None:
            symbol = str(override.get("norgate_symbol") or "").strip().upper()
            database = "US Equities Delisted" if symbol in delisted_symbols else "US Equities" if symbol in current_symbols else ""
            expected_database = str(override.get("source_database") or "").strip()
            valid = bool(database and (not expected_database or expected_database == database))
            mapping = _mapping(
                ticker=ticker,
                company_name=company_name,
                symbol=symbol,
                source_database=database or expected_database,
                provider=provider,
                status="verified_override" if valid else "invalid_override",
                reason=str(override.get("mapping_reason") or "reviewed_override").strip(),
                exit_year=exit_year_text,
                usable=valid,
                review_status=str(override.get("review_status") or "").strip().lower(),
                notes=str(override.get("notes") or "").strip(),
                first_override=str(override.get("override_start_date") or ""),
                last_override=str(override.get("override_end_date") or ""),
            )
            mappings.append(mapping)
            continue
        if ticker in known_exclusions:
            mappings.append(
                _mapping(
                    ticker=ticker,
                    company_name=company_name,
                    provider=provider,
                    status="excluded_known_unresolved",
                    reason="known_norgate_identity_exclusion",
                    exit_year=exit_year_text,
                    notes=known_exclusions[ticker],
                )
            )
            continue

        candidates = sorted(
            symbol
            for symbol in delisted_symbols
            if symbol == ticker
            or symbol.startswith(f"{ticker}-")
            or symbol.startswith(f"{ticker}Q-")
            or symbol.startswith(f"{ticker}D-")
        )
        if ticker in current_symbols and ticker not in candidates:
            candidates.append(ticker)
        scored: list[tuple[float, float, int, str, str, str]] = []
        for symbol in candidates:
            norgate_name = _provider_text(provider, "security_name", symbol)
            last_date = _provider_date(provider, "last_quoted_date", symbol)
            similarity = _similarity(company_name, norgate_name)
            year_gap = abs(int(last_date[:4]) - exit_year) if exit_year and last_date[:4].isdigit() else 99
            current_penalty = 0.75 if symbol in current_symbols and symbol not in delisted_symbols else 0.0
            score = similarity - 0.08 * year_gap - current_penalty
            scored.append((score, similarity, year_gap, symbol, norgate_name, last_date))
        scored.sort(reverse=True)
        if not scored:
            mappings.append(
                _mapping(
                    ticker=ticker,
                    company_name=company_name,
                    provider=provider,
                    status="unresolved",
                    reason="no_norgate_symbol_candidate",
                    exit_year=exit_year_text,
                )
            )
            continue
        best_score, best_similarity, year_gap, symbol, _name, _last = scored[0]
        margin = best_score - scored[1][0] if len(scored) > 1 else 9.0
        plausible = best_similarity >= 0.35 and year_gap <= 2 and best_score >= 0.20
        verified = plausible and year_gap <= 1 and margin >= 0.10
        status = "verified_auto_delisted" if verified else "review_required" if plausible else "unresolved"
        reason = (
            "symbol_name_and_exit_date_match"
            if verified
            else "plausible_mapping_requires_review"
            if plausible
            else "candidate_failed_name_or_exit_date_guard"
        )
        database = "US Equities Delisted" if symbol in delisted_symbols else "US Equities"
        mapping = _mapping(
            ticker=ticker,
            company_name=company_name,
            symbol=symbol if plausible else "",
            source_database=database if plausible else "",
            provider=provider,
            status=status,
            reason=reason,
            exit_year=exit_year_text,
            usable=verified,
            review_status="auto_verified" if verified else "review_required" if plausible else "",
            notes=f"candidate_count={len(scored)}; score={best_score:.6f}; margin={margin:.6f}; year_gap={year_gap}",
        )
        mappings.append(mapping)

    start = _date_text(history_start) or DEFAULT_HISTORY_START
    adjusted: list[NorgateMapping] = []
    for mapping in mappings:
        usable = mapping.calibration_usable_flag == "1"
        if mapping.last_quoted_date and mapping.last_quoted_date < start:
            usable = False
        if not mapping.first_quoted_date:
            usable = False
        if usable == (mapping.calibration_usable_flag == "1"):
            adjusted.append(mapping)
        else:
            row = mapping.to_row()
            row["calibration_usable_flag"] = "1" if usable else "0"
            if not usable and mapping.last_quoted_date and mapping.last_quoted_date < start:
                row["notes"] = "; ".join(filter(None, [mapping.notes, f"last_quote_before_history_start={start}"]))
            adjusted.append(NorgateMapping(**row))
    return adjusted


def build_membership_rows(
    *,
    active_rows: list[dict[str, str]],
    delisted_rows: list[dict[str, str]],
    mappings: list[NorgateMapping],
    cohorts: dict[str, dict[str, str]],
    history_start: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    mapping_by_ticker = {mapping.internal_ticker: mapping for mapping in mappings}
    start_floor = _date_text(history_start) or DEFAULT_HISTORY_START
    history_rows: list[dict[str, str]] = []
    listing_rows: list[dict[str, str]] = []

    def bounded_start(first_date: str) -> str:
        return max(start_floor, first_date) if first_date else start_floor

    for row in sorted(active_rows, key=lambda item: normalize_ticker(item.get("ticker"))):
        ticker = normalize_ticker(row.get("ticker"))
        mapping = mapping_by_ticker[ticker]
        if mapping.calibration_usable_flag != "1":
            continue
        cohort_id = str(row.get("calibration_cohort") or "").strip()
        cohort_name = cohorts[cohort_id]["cohort_name"]
        first_date = bounded_start(mapping.first_quoted_date)
        history_rows.append(
            {
                "internal_ticker": ticker,
                "exchange_ticker": ticker,
                "price_source_symbol": mapping.norgate_symbol,
                "company_name": str(row.get("company_name") or ticker),
                "cik": normalize_cik(row.get("cik")),
                "exchange": str(row.get("exchange") or ""),
                "country": str(row.get("country") or "United States"),
                "currency": str(row.get("currency") or "USD"),
                "security_type": str(row.get("security_type") or "Common Stock"),
                "calibration_cohort_id": cohort_id,
                "calibration_cohort": cohort_name,
                "start_date": first_date,
                "end_date": "",
                "membership_status": "active",
                "successor_ticker": "",
                "event_type": "active_at_contract_build",
                "confidence": "0.95",
                "source_url": "norgate_local_metadata",
                "notes": "Exact active ticker and issuer-name match in Norgate US Equities.",
            }
        )
        listing_rows.append(
            {
                "ticker": ticker,
                "first_eligible_date": first_date,
                "last_eligible_date": "",
                "eligibility_basis": "norgate_first_quoted_date",
                "source": "norgate_local_metadata",
                "confidence": "0.95",
                "notes": f"norgate_symbol={mapping.norgate_symbol}",
            }
        )

    for row in sorted(delisted_rows, key=lambda item: normalize_ticker(item.get("ticker"))):
        ticker = normalize_ticker(row.get("ticker"))
        mapping = mapping_by_ticker[ticker]
        if mapping.calibration_usable_flag != "1" or not mapping.last_quoted_date:
            continue
        first_date = bounded_start(mapping.first_quoted_date)
        if mapping.last_quoted_date < first_date:
            continue
        cohort_id = str(row.get("cohort") or "").strip()
        cohort_name = cohorts[cohort_id]["cohort_name"]
        history_rows.append(
            {
                "internal_ticker": ticker,
                "exchange_ticker": ticker,
                "price_source_symbol": mapping.norgate_symbol,
                "company_name": str(row.get("company") or ticker),
                "cik": normalize_cik(row.get("cik")),
                "exchange": "historical_delisted",
                "country": "United States",
                "currency": "USD",
                "security_type": "Common Stock",
                "calibration_cohort_id": cohort_id,
                "calibration_cohort": cohort_name,
                "start_date": first_date,
                "end_date": mapping.last_quoted_date,
                "membership_status": "historical_delisted",
                "successor_ticker": "",
                "event_type": str(row.get("terminal_type") or row.get("exit_type") or "delisted"),
                "confidence": "0.90" if mapping.mapping_status == "verified_override" else "0.85",
                "source_url": "norgate_local_metadata",
                "notes": f"actual_ticker={ticker}; norgate_symbol={mapping.norgate_symbol}; {mapping.mapping_reason}",
            }
        )
        listing_rows.append(
            {
                "ticker": ticker,
                "first_eligible_date": first_date,
                "last_eligible_date": mapping.last_quoted_date,
                "eligibility_basis": "norgate_first_and_last_quoted_date",
                "source": "norgate_local_metadata",
                "confidence": "0.90" if mapping.mapping_status == "verified_override" else "0.85",
                "notes": f"norgate_symbol={mapping.norgate_symbol}",
            }
        )
    return history_rows, listing_rows


def write_identity_contracts(
    *,
    mapping_path: Path,
    membership_path: Path,
    listing_path: Path,
    mappings: list[NorgateMapping],
    memberships: list[dict[str, str]],
    listing_rows: list[dict[str, str]],
) -> None:
    write_csv_atomic(mapping_path, NORGATE_MAP_FIELDS, [mapping.to_row() for mapping in mappings])
    write_csv_atomic(membership_path, HISTORICAL_MEMBERSHIP_FIELDS, memberships)
    write_csv_atomic(listing_path, LISTING_DATE_FIELDS, listing_rows)
