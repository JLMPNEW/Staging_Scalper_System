"""SEC-backed issuer reporting-profile census for Basic Materials."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.request

from basic_materials.core.atomic_io import atomic_write_bytes, atomic_write_json


ANNUAL_FORMS = {
    "10-K": "10-K",
    "10-K/A": "10-K",
    "10-KT": "10-K",
    "10-KT/A": "10-K",
    "20-F": "20-F",
    "20-F/A": "20-F",
    "40-F": "40-F",
    "40-F/A": "40-F",
}
INTERIM_FORMS = {
    "10-Q": "10-Q",
    "10-Q/A": "10-Q",
    "10-QT": "10-Q",
    "10-QT/A": "10-Q",
    "6-K": "6-K",
    "6-K/A": "6-K",
}
ANCHOR_CONCEPTS = {
    "Assets",
    "CashAndCashEquivalents",
    "CashAndCashEquivalentsAtCarryingValue",
    "NetIncomeLoss",
    "OperatingIncomeLoss",
    "ProfitLoss",
    "Revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
}
PROFILE_FIELDS = (
    "profile_key",
    "role_type",
    "ticker",
    "cik",
    "company_name",
    "domicile_country",
    "trading_currency",
    "profile_asof_date",
    "source_cutoff_date",
    "sec_entity_name",
    "primary_annual_form",
    "filing_regime",
    "accounting_basis",
    "primary_taxonomy",
    "fiscal_year_end",
    "reporting_currency",
    "reporting_currency_method",
    "expected_cadence",
    "latest_financial_form",
    "latest_financial_accession",
    "latest_financial_accepted_at",
    "latest_annual_accession",
    "latest_annual_accepted_at",
    "latest_companyfacts_accepted_at",
    "companyfacts_lag_days",
    "inline_xbrl_fallback_expected",
    "submissions_source_id",
    "companyfacts_source_id",
    "submissions_url",
    "companyfacts_url",
    "submissions_sha256",
    "companyfacts_sha256",
    "profile_status",
    "review_reason",
    "confidence",
    "calibration_eligible",
    "evidence_label",
    "evidence_json",
    "contract_version",
    "profile_sha256",
)
_CIK = re.compile(r"^[0-9]{10}$")
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class IssuerSeed:
    profile_key: str
    role_type: str
    ticker: str
    cik: str
    company_name: str
    domicile_country: str
    trading_currency: str
    profile_asof_date: str
    source_cutoff_date: str


@dataclass(frozen=True)
class PayloadResult:
    payload: dict[str, Any] | None
    raw: bytes | None
    sha256: str
    url: str
    cache_path: Path
    status: str


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    source = Path(path).resolve()
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def issuer_seeds(
    current_path: str | Path,
    historical_path: str | Path,
    *,
    as_of: str,
) -> list[IssuerSeed]:
    date.fromisoformat(as_of)
    seeds: list[IssuerSeed] = []
    for row in _read_csv(current_path):
        seeds.append(
            IssuerSeed(
                profile_key=f"current:{row['ticker'].upper()}",
                role_type="current_universe",
                ticker=row["ticker"].upper(),
                cik=row["cik"].zfill(10),
                company_name=row["company_name"].strip(),
                domicile_country=row["country"].strip(),
                trading_currency=row["currency"].upper(),
                profile_asof_date=as_of,
                source_cutoff_date=as_of,
            )
        )
    for row in _read_csv(historical_path):
        cutoff = min(as_of, row["membership_end_date"])
        seeds.append(
            IssuerSeed(
                profile_key=f"historical:{row['historical_ticker'].upper()}",
                role_type="historical_pilot",
                ticker=row["historical_ticker"].upper(),
                cik=row["sec_cik"].zfill(10),
                company_name=row["company_name"].strip(),
                domicile_country=row["country"].strip(),
                trading_currency=row["trading_currency"].upper(),
                profile_asof_date=as_of,
                source_cutoff_date=cutoff,
            )
        )
    if len(seeds) != 154 or len({seed.cik for seed in seeds}) != len(seeds):
        raise ValueError("Reporting-profile seed must contain 154 unique CIKs")
    if any(not _CIK.fullmatch(seed.cik) for seed in seeds):
        raise ValueError("Reporting-profile seed contains an invalid CIK")
    return sorted(seeds, key=lambda seed: (seed.role_type, seed.ticker))


def _normalize_accepted(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def _filing_rows(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    recent = (payload.get("filings") or {}).get("recent")
    if not isinstance(recent, Mapping):
        raise ValueError("SEC submissions filings.recent is missing")
    keys = (
        "accessionNumber",
        "filingDate",
        "acceptanceDateTime",
        "reportDate",
        "form",
        "primaryDocument",
    )
    arrays = {key: recent.get(key) for key in keys}
    if any(not isinstance(value, list) for value in arrays.values()):
        raise ValueError("SEC submissions required recent fields must be arrays")
    lengths = {len(value) for value in arrays.values()}
    if len(lengths) != 1:
        raise ValueError("SEC submissions required recent arrays have unequal lengths")
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for position in range(next(iter(lengths), 0)):
        row = {key: str(arrays[key][position] or "").strip() for key in keys}
        accession = row["accessionNumber"]
        if not _ACCESSION.fullmatch(accession):
            raise ValueError(f"SEC submissions row {position} has invalid accession")
        if accession in seen:
            raise ValueError(f"SEC submissions duplicates accession {accession}")
        seen.add(accession)
        row["acceptanceDateTime"] = _normalize_accepted(row["acceptanceDateTime"])
        output.append(row)
    return output


def validate_submissions(payload: object, *, cik: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("SEC submissions payload must be an object")
    observed = str(payload.get("cik") or "").zfill(10)
    if observed != cik:
        raise ValueError(f"SEC submissions CIK mismatch: expected {cik}, got {observed}")
    _filing_rows(payload)
    return payload


def validate_companyfacts(payload: object, *, cik: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("SEC Company Facts payload must be an object")
    observed = str(payload.get("cik") or "").zfill(10)
    if observed != cik:
        raise ValueError(f"SEC Company Facts CIK mismatch: expected {cik}, got {observed}")
    if not isinstance(payload.get("facts"), dict):
        raise ValueError("SEC Company Facts payload has no facts object")
    return payload


def validate_submissions_archive(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("SEC submissions archive payload must be an object")
    _filing_rows({"filings": {"recent": payload}})
    return payload


def _merge_submission_archives(
    root: Mapping[str, Any],
    archives: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = _filing_rows(root)
    for archive in archives:
        rows.extend(_filing_rows({"filings": {"recent": archive}}))
    by_accession: dict[str, dict[str, str]] = {}
    for row in rows:
        accession = row["accessionNumber"]
        previous = by_accession.get(accession)
        if previous is not None and previous != row:
            raise ValueError(f"SEC submissions conflict for accession {accession}")
        by_accession[accession] = row
    ordered = sorted(
        by_accession.values(),
        key=lambda row: (row["acceptanceDateTime"], row["accessionNumber"]),
        reverse=True,
    )
    keys = (
        "accessionNumber",
        "filingDate",
        "acceptanceDateTime",
        "reportDate",
        "form",
        "primaryDocument",
    )
    return {
        **root,
        "filings": {
            **dict(root.get("filings") or {}),
            "recent": {key: [row[key] for row in ordered] for key in keys},
        },
    }


def _archive_names(
    payload: Mapping[str, Any],
    *,
    cutoff: str,
    include_history: bool,
) -> list[str]:
    root_rows = [
        row
        for row in _filing_rows(payload)
        if row["acceptanceDateTime"] and row["acceptanceDateTime"][:10] <= cutoff
    ]
    has_annual = any(row["form"].upper() in ANNUAL_FORMS for row in root_rows)
    if not include_history and has_annual:
        return []
    names: list[str] = []
    for descriptor in (payload.get("filings") or {}).get("files") or []:
        if not isinstance(descriptor, Mapping):
            raise ValueError("SEC submissions archive descriptor must be an object")
        name = str(descriptor.get("name") or "")
        filing_from = str(descriptor.get("filingFrom") or "")
        if not re.fullmatch(r"CIK[0-9]{10}-submissions-[0-9]{3}[.]json", name):
            raise ValueError(f"SEC submissions archive name is invalid: {name!r}")
        if not filing_from or filing_from <= cutoff:
            names.append(name)
    return sorted(set(names))


def _request_bytes(
    url: str,
    *,
    user_agent: str,
    timeout_seconds: float,
    max_retries: int,
    allow_not_found: bool,
) -> bytes | None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_not_found:
                return None
            if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 == max_retries:
                raise
        except urllib.error.URLError:
            if attempt + 1 == max_retries:
                raise
        time.sleep(min(2.0**attempt, 8.0))
    raise RuntimeError("SEC request retry loop exited unexpectedly")


def fetch_json_cached(
    url: str,
    cache_path: str | Path,
    *,
    validator: Callable[[object], dict[str, Any]],
    user_agent: str,
    timeout_seconds: float,
    max_retries: int,
    request_interval_seconds: float,
    force_refresh: bool,
    allow_not_found: bool = False,
    fetch: Callable[..., bytes | None] = _request_bytes,
) -> PayloadResult:
    target = Path(cache_path).resolve(strict=False)
    raw: bytes | None
    status: str
    if target.is_file() and not force_refresh:
        raw = target.read_bytes()
        status = "cache_hit"
    else:
        raw = fetch(
            url,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            allow_not_found=allow_not_found,
        )
        status = "not_found" if raw is None else "fetched"
        if raw is not None:
            atomic_write_bytes(target, raw)
        time.sleep(request_interval_seconds)
    if raw is None:
        return PayloadResult(None, None, "", url, target, status)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid SEC JSON at {url}") from exc
    payload = validator(decoded)
    return PayloadResult(
        payload=payload,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        url=url,
        cache_path=target,
        status=status,
    )


def _companyfacts_index(
    payload: Mapping[str, Any] | None,
    *,
    accepted_by_accession: Mapping[str, str],
) -> dict[str, Any]:
    taxonomy_by_accession: dict[str, Counter[str]] = defaultdict(Counter)
    anchor_currency_by_accession: dict[str, Counter[str]] = defaultdict(Counter)
    currency_by_accession: dict[str, Counter[str]] = defaultdict(Counter)
    eligible_taxonomy: Counter[str] = Counter()
    eligible_currency: Counter[str] = Counter()
    fact_accessions: set[str] = set()
    if payload is None:
        return {
            "taxonomy_by_accession": taxonomy_by_accession,
            "anchor_currency_by_accession": anchor_currency_by_accession,
            "currency_by_accession": currency_by_accession,
            "eligible_taxonomy": eligible_taxonomy,
            "eligible_currency": eligible_currency,
            "fact_accessions": fact_accessions,
            "latest_accepted_at": "",
            "latest_accession": "",
        }
    for taxonomy, concepts in (payload.get("facts") or {}).items():
        if taxonomy not in {"us-gaap", "ifrs-full"} or not isinstance(concepts, Mapping):
            continue
        for concept, definition in concepts.items():
            units = (definition or {}).get("units") or {}
            if not isinstance(units, Mapping):
                continue
            for unit, observations in units.items():
                currency = str(unit).upper()
                for observation in observations or []:
                    accession = str(observation.get("accn") or "")
                    if accession not in accepted_by_accession:
                        continue
                    fact_accessions.add(accession)
                    taxonomy_by_accession[accession][taxonomy] += 1
                    eligible_taxonomy[taxonomy] += 1
                    if _CURRENCY.fullmatch(currency):
                        currency_by_accession[accession][currency] += 1
                        eligible_currency[currency] += 1
                        if concept in ANCHOR_CONCEPTS:
                            anchor_currency_by_accession[accession][currency] += 1
    latest = max(
        (accepted_by_accession[accession] for accession in fact_accessions),
        default="",
    )
    latest_accession = max(
        fact_accessions,
        key=lambda accession: (accepted_by_accession[accession], accession),
        default="",
    )
    return {
        "taxonomy_by_accession": taxonomy_by_accession,
        "anchor_currency_by_accession": anchor_currency_by_accession,
        "currency_by_accession": currency_by_accession,
        "eligible_taxonomy": eligible_taxonomy,
        "eligible_currency": eligible_currency,
        "fact_accessions": fact_accessions,
        "latest_accepted_at": latest,
        "latest_accession": latest_accession,
    }


def _unique_winner(counter: Counter[str]) -> str:
    if not counter:
        return ""
    ordered = counter.most_common()
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
        return ""
    return ordered[0][0]


def profile_hash(row: Mapping[str, str]) -> str:
    payload = {field: row[field] for field in PROFILE_FIELDS if field != "profile_sha256"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_reporting_profile(
    seed: IssuerSeed,
    *,
    submissions: PayloadResult,
    companyfacts: PayloadResult,
    policy_version: str,
    source_ids: Mapping[str, str],
    companyfacts_lag_days: int,
) -> dict[str, str]:
    rows = _filing_rows(submissions.payload) if submissions.payload is not None else []
    rows = [
        row
        for row in rows
        if row["acceptanceDateTime"]
        and row["acceptanceDateTime"][:10] <= seed.source_cutoff_date
    ]
    accepted_by_accession = {
        row["accessionNumber"]: row["acceptanceDateTime"] for row in rows
    }
    index = _companyfacts_index(
        companyfacts.payload,
        accepted_by_accession=accepted_by_accession,
    )
    financial_rows: list[dict[str, str]] = []
    annual_rows: list[dict[str, str]] = []
    for row in rows:
        form = row["form"].upper()
        if form in ANNUAL_FORMS:
            annual_rows.append(row)
            financial_rows.append(row)
        elif form in INTERIM_FORMS and (
            INTERIM_FORMS[form] != "6-K"
            or row["accessionNumber"] in index["fact_accessions"]
        ):
            financial_rows.append(row)
    financial_rows.sort(key=lambda row: (row["acceptanceDateTime"], row["accessionNumber"]), reverse=True)
    annual_rows.sort(key=lambda row: (row["acceptanceDateTime"], row["accessionNumber"]), reverse=True)
    latest_financial = financial_rows[0] if financial_rows else None
    latest_annual = annual_rows[0] if annual_rows else None
    annual_form = ANNUAL_FORMS.get(latest_annual["form"].upper(), "UNKNOWN") if latest_annual else "UNKNOWN"
    annual_accession = latest_annual["accessionNumber"] if latest_annual else ""

    latest_fact_accession = str(index["latest_accession"])
    taxonomy_counts = index["taxonomy_by_accession"].get(annual_accession, Counter())
    if not taxonomy_counts and latest_fact_accession:
        taxonomy_counts = index["taxonomy_by_accession"].get(
            latest_fact_accession,
            Counter(),
        )
    if not taxonomy_counts:
        taxonomy_counts = index["eligible_taxonomy"]
    observed_taxonomies = {key for key, value in taxonomy_counts.items() if value}
    if observed_taxonomies == {"us-gaap"}:
        taxonomy, accounting = "us-gaap", "US_GAAP"
    elif observed_taxonomies == {"ifrs-full"}:
        taxonomy, accounting = "ifrs-full", "IFRS"
    elif observed_taxonomies:
        taxonomy, accounting = "mixed", "MIXED_REVIEW"
    else:
        taxonomy, accounting = "unknown", "UNKNOWN"

    currency_method = "unresolved"
    currency = _unique_winner(index["anchor_currency_by_accession"].get(annual_accession, Counter()))
    if currency:
        currency_method = "latest_annual_anchor_concepts"
    if not currency:
        currency = _unique_winner(index["currency_by_accession"].get(annual_accession, Counter()))
        if currency:
            currency_method = "latest_annual_all_monetary_facts"
    if not currency and latest_fact_accession:
        currency = _unique_winner(
            index["anchor_currency_by_accession"].get(
                latest_fact_accession,
                Counter(),
            )
        )
        if not currency:
            currency = _unique_winner(
                index["currency_by_accession"].get(
                    latest_fact_accession,
                    Counter(),
                )
            )
        if currency:
            currency_method = "eligible_companyfacts_plurality"
    if not currency:
        currency = _unique_winner(index["eligible_currency"])
        if currency:
            currency_method = "eligible_companyfacts_plurality"

    regime = {
        "10-K": "domestic_sec",
        "20-F": "foreign_private_issuer",
        "40-F": "canadian_mjds",
    }.get(annual_form, "unknown")
    cadence = {
        "10-K": "quarterly",
        "20-F": "annual_or_interim_6k",
        "40-F": "annual_or_interim_6k",
    }.get(annual_form, "unknown")
    fiscal_raw = str((submissions.payload or {}).get("fiscalYearEnd") or "")
    fiscal_year_end = (
        f"{fiscal_raw[:2]}-{fiscal_raw[2:]}"
        if re.fullmatch(r"[0-9]{4}", fiscal_raw)
        else ""
    )
    fiscal_year_end_method = "submissions_entity_metadata" if fiscal_year_end else ""
    annual_report_date = latest_annual["reportDate"] if latest_annual else ""
    if not fiscal_year_end and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", annual_report_date):
        fiscal_year_end = annual_report_date[5:]
        fiscal_year_end_method = "latest_annual_report_date"
    latest_cf = str(index["latest_accepted_at"])
    latest_financial_accepted = latest_financial["acceptanceDateTime"] if latest_financial else ""
    lag: int | None = None
    if latest_financial_accepted and latest_cf:
        lag = (
            date.fromisoformat(latest_financial_accepted[:10])
            - date.fromisoformat(latest_cf[:10])
        ).days
    fallback = int(
        annual_form in {"20-F", "40-F"}
        and (
            not latest_cf
            or (lag is not None and lag > companyfacts_lag_days)
            or annual_accession not in index["fact_accessions"]
        )
    )

    reasons: list[str] = []
    if not submissions.sha256:
        reasons.append("submissions_unavailable")
    if annual_form == "UNKNOWN":
        reasons.append("annual_form_unresolved")
    if taxonomy == "unknown":
        reasons.append("accounting_taxonomy_unresolved")
    elif taxonomy == "mixed":
        reasons.append("mixed_accounting_taxonomies")
    if not currency:
        reasons.append("reporting_currency_unresolved")
    if not fiscal_year_end:
        reasons.append("fiscal_year_end_unresolved")
    if fallback:
        reasons.append("companyfacts_fallback_required")

    if not submissions.sha256 and seed.role_type == "historical_pilot":
        status, confidence = "legacy_archive_required", 0.40
    elif annual_form == "UNKNOWN":
        status, confidence = "annual_form_review_required", 0.50
    elif taxonomy in {"unknown", "mixed"}:
        status, confidence = "taxonomy_review_required", 0.60
    elif not currency:
        status, confidence = "currency_review_required", 0.65
    elif not fiscal_year_end:
        status, confidence = "profile_metadata_review_required", 0.70
    elif fallback:
        status, confidence = "companyfacts_fallback_required", 0.80
    else:
        status, confidence = "ready_for_ingestion", 1.00

    evidence = {
        "availability_boundary": "sec_acceptance_timestamp",
        "companyfacts_accession_count": len(index["fact_accessions"]),
        "fiscal_year_end_method": fiscal_year_end_method,
        "reporting_currency_method": currency_method,
        "source_cutoff_date": seed.source_cutoff_date,
        "trading_currency_used_as_reporting_fallback": False,
    }
    row = {
        "profile_key": seed.profile_key,
        "role_type": seed.role_type,
        "ticker": seed.ticker,
        "cik": seed.cik,
        "company_name": seed.company_name,
        "domicile_country": seed.domicile_country,
        "trading_currency": seed.trading_currency,
        "profile_asof_date": seed.profile_asof_date,
        "source_cutoff_date": seed.source_cutoff_date,
        "sec_entity_name": str((submissions.payload or {}).get("name") or seed.company_name),
        "primary_annual_form": annual_form,
        "filing_regime": regime,
        "accounting_basis": accounting,
        "primary_taxonomy": taxonomy,
        "fiscal_year_end": fiscal_year_end,
        "reporting_currency": currency,
        "reporting_currency_method": currency_method,
        "expected_cadence": cadence,
        "latest_financial_form": latest_financial["form"] if latest_financial else "",
        "latest_financial_accession": latest_financial["accessionNumber"] if latest_financial else "",
        "latest_financial_accepted_at": latest_financial_accepted,
        "latest_annual_accession": annual_accession,
        "latest_annual_accepted_at": latest_annual["acceptanceDateTime"] if latest_annual else "",
        "latest_companyfacts_accepted_at": latest_cf,
        "companyfacts_lag_days": "" if lag is None else str(lag),
        "inline_xbrl_fallback_expected": str(fallback),
        "submissions_source_id": source_ids["submissions_source_id"],
        "companyfacts_source_id": source_ids["companyfacts_source_id"],
        "submissions_url": submissions.url,
        "companyfacts_url": companyfacts.url,
        "submissions_sha256": submissions.sha256,
        "companyfacts_sha256": companyfacts.sha256,
        "profile_status": status,
        "review_reason": ";".join(reasons),
        "confidence": f"{confidence:.2f}",
        "calibration_eligible": "0",
        "evidence_label": "fact_source_reported",
        "evidence_json": json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        "contract_version": policy_version,
        "profile_sha256": "",
    }
    row["profile_sha256"] = profile_hash(row)
    return row


def build_reporting_profile_census(
    seeds: Iterable[IssuerSeed],
    *,
    cache_root: str | Path,
    submissions_url_template: str,
    submissions_archive_url_template: str,
    companyfacts_url_template: str,
    user_agent: str,
    timeout_seconds: float,
    max_retries: int,
    request_interval_seconds: float,
    force_refresh: bool,
    policy_version: str,
    source_ids: Mapping[str, str],
    companyfacts_lag_days: int,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    seed_rows = list(seeds)
    cache = Path(cache_root).resolve(strict=False)
    profiles: list[dict[str, str]] = []
    records: list[dict[str, str]] = []
    for index, seed in enumerate(seed_rows, start=1):
        submissions_url = submissions_url_template.format(cik=seed.cik)
        companyfacts_url = companyfacts_url_template.format(cik=seed.cik)
        submissions = fetch_json_cached(
            submissions_url,
            cache / "submissions" / f"CIK{seed.cik}.json",
            validator=lambda payload, cik=seed.cik: validate_submissions(payload, cik=cik),
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            request_interval_seconds=request_interval_seconds,
            force_refresh=force_refresh,
            allow_not_found=seed.role_type == "historical_pilot",
        )
        if submissions.payload is None and seed.role_type == "current_universe":
            raise RuntimeError(f"Current issuer {seed.ticker} has no SEC submissions payload")
        archive_results: list[PayloadResult] = []
        if submissions.payload is not None:
            for file_name in _archive_names(
                submissions.payload,
                cutoff=seed.source_cutoff_date,
                include_history=seed.role_type == "historical_pilot",
            ):
                archive_url = submissions_archive_url_template.format(file_name=file_name)
                archive_results.append(
                    fetch_json_cached(
                        archive_url,
                        cache / "submissions" / "archive" / file_name,
                        validator=validate_submissions_archive,
                        user_agent=user_agent,
                        timeout_seconds=timeout_seconds,
                        max_retries=max_retries,
                        request_interval_seconds=request_interval_seconds,
                        force_refresh=force_refresh,
                    )
                )
            if archive_results:
                merged_payload = _merge_submission_archives(
                    submissions.payload,
                    (
                        result.payload
                        for result in archive_results
                        if result.payload is not None
                    ),
                )
                combined_evidence = {
                    "root": submissions.sha256,
                    "archives": [result.sha256 for result in archive_results],
                }
                combined_sha = hashlib.sha256(
                    json.dumps(
                        combined_evidence,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                submissions = PayloadResult(
                    payload=merged_payload,
                    raw=submissions.raw,
                    sha256=combined_sha,
                    url=submissions.url,
                    cache_path=submissions.cache_path,
                    status="root_plus_archives",
                )
        companyfacts = fetch_json_cached(
            companyfacts_url,
            cache / "companyfacts" / f"CIK{seed.cik}.json",
            validator=lambda payload, cik=seed.cik: validate_companyfacts(payload, cik=cik),
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            request_interval_seconds=request_interval_seconds,
            force_refresh=force_refresh,
            allow_not_found=True,
        )
        profiles.append(
            build_reporting_profile(
                seed,
                submissions=submissions,
                companyfacts=companyfacts,
                policy_version=policy_version,
                source_ids=source_ids,
                companyfacts_lag_days=companyfacts_lag_days,
            )
        )
        for kind, result in (("submissions", submissions), ("companyfacts", companyfacts)):
            records.append(
                {
                    "ticker": seed.ticker,
                    "cik": seed.cik,
                    "kind": kind,
                    "url": result.url,
                    "cache_path": str(result.cache_path),
                    "sha256": result.sha256,
                    "status": result.status,
                }
            )
        for archive in archive_results:
            records.append(
                {
                    "ticker": seed.ticker,
                    "cik": seed.cik,
                    "kind": "submissions_archive",
                    "url": archive.url,
                    "cache_path": str(archive.cache_path),
                    "sha256": archive.sha256,
                    "status": archive.status,
                }
            )
        if progress is not None:
            progress(index, len(seed_rows), seed.ticker)
    profiles.sort(key=lambda row: (row["role_type"], row["ticker"]))
    records.sort(key=lambda row: (row["ticker"], row["kind"]))
    manifest_payload = {
        "artifact_id": "basic_materials_reporting_profile_cache_v1",
        "profile_count": len(profiles),
        "records": records,
    }
    encoded = json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_payload["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    manifest_path = cache / "reporting_profile_cache_manifest.json"
    atomic_write_json(manifest_path, manifest_payload)
    return profiles, {
        "cache_manifest_path": str(manifest_path),
        "cache_manifest_sha256": manifest_payload["manifest_sha256"],
        "payload_count": sum(bool(record["sha256"]) for record in records),
        "missing_payload_count": sum(not bool(record["sha256"]) for record in records),
    }
