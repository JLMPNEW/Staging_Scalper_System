from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from biotech_index.core.pipeline_guards import normalize_ticker


@dataclass(frozen=True)
class SecurityIdentityRule:
    ticker: str
    company_name: str
    cik: str
    historical_ciks: tuple[str, ...]
    calibration_cohort: str
    membership_start_date: date
    membership_end_date: date | None
    historical_price_ticker: str
    institutional_13f_issuer_aliases: tuple[str, ...]
    cusip: str
    source_reference: str

    def contains(self, asof: date) -> bool:
        return self.membership_start_date <= asof and (
            self.membership_end_date is None or asof <= self.membership_end_date
        )


def _parse_date(raw: object, *, field: str, ticker: str, required: bool) -> date | None:
    text = str(raw or "").strip()
    if not text:
        if required:
            raise ValueError(f"Security identity {ticker} is missing {field}")
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"Security identity {ticker} has invalid {field}={text!r}") from exc


def _normalize_cik(raw: object) -> str:
    digits = "".join(character for character in str(raw or "") if character.isdigit())
    return digits.zfill(10) if digits else ""


def _split_values(raw: object) -> tuple[str, ...]:
    return tuple(value.strip() for value in str(raw or "").split(";") if value.strip())


def load_security_identity_rules(path: Path | None) -> dict[str, SecurityIdentityRule]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Security identity CSV has no header: {path}")
        required = {
            "ticker",
            "company_name",
            "cik",
            "calibration_cohort",
            "membership_start_date",
            "historical_price_ticker",
        }
        missing = sorted(required - set(reader.fieldnames))
        if missing:
            raise ValueError(f"Security identity CSV missing columns: {','.join(missing)}")
        rules: dict[str, SecurityIdentityRule] = {}
        for row_number, row in enumerate(reader, start=2):
            if str(row.get("approved") or "true").strip().lower() not in {"1", "true", "yes", "y"}:
                continue
            ticker = normalize_ticker(row.get("ticker"))
            if not ticker:
                raise ValueError(f"Security identity CSV row {row_number} has invalid ticker")
            if ticker in rules:
                raise ValueError(f"Security identity CSV has duplicate ticker: {ticker}")
            start = _parse_date(
                row.get("membership_start_date"), field="membership_start_date", ticker=ticker, required=True
            )
            assert start is not None
            end = _parse_date(
                row.get("membership_end_date"), field="membership_end_date", ticker=ticker, required=False
            )
            if end is not None and end < start:
                raise ValueError(f"Security identity {ticker} ends before it starts")
            cik = _normalize_cik(row.get("cik"))
            if not cik:
                raise ValueError(f"Security identity {ticker} is missing cik")
            historical_ciks = tuple(
                dict.fromkeys(
                    normalized
                    for value in _split_values(row.get("historical_ciks"))
                    if (normalized := _normalize_cik(value))
                )
            )
            price_ticker = normalize_ticker(row.get("historical_price_ticker"))
            if not price_ticker:
                raise ValueError(f"Security identity {ticker} has invalid historical_price_ticker")
            rules[ticker] = SecurityIdentityRule(
                ticker=ticker,
                company_name=str(row.get("company_name") or ticker).strip() or ticker,
                cik=cik,
                historical_ciks=historical_ciks,
                calibration_cohort=str(row.get("calibration_cohort") or "").strip(),
                membership_start_date=start,
                membership_end_date=end,
                historical_price_ticker=price_ticker,
                institutional_13f_issuer_aliases=_split_values(
                    row.get("institutional_13f_issuer_alias") or row.get("institutional_13f_issuer_aliases")
                ),
                cusip=str(row.get("cusip") or "").strip().upper(),
                source_reference=str(row.get("source_reference") or "").strip(),
            )
    return rules


def security_history_start(
    rules: dict[str, SecurityIdentityRule],
    ticker: object,
    *,
    default: date,
) -> date:
    rule = rules.get(normalize_ticker(ticker))
    return max(default, rule.membership_start_date) if rule is not None else default


def identity_start_dates_by_company(
    rules: dict[str, SecurityIdentityRule],
    company_ids_by_ticker: dict[str, int],
) -> dict[int, date]:
    """Map governed company IDs to the first valid date for the current security identity."""
    out: dict[int, date] = {}
    for ticker, rule in rules.items():
        company_id = company_ids_by_ticker.get(normalize_ticker(ticker))
        if company_id is not None and company_id > 0:
            out[int(company_id)] = rule.membership_start_date
    return out


def rule_manifest(rule: SecurityIdentityRule) -> dict[str, Any]:
    return {
        "ticker": rule.ticker,
        "company_name": rule.company_name,
        "cik": rule.cik,
        "historical_ciks": ";".join(rule.historical_ciks),
        "calibration_cohort": rule.calibration_cohort,
        "membership_start_date": rule.membership_start_date.isoformat(),
        "membership_end_date": rule.membership_end_date.isoformat() if rule.membership_end_date else "",
        "historical_price_ticker": rule.historical_price_ticker,
        "institutional_13f_issuer_aliases": ";".join(rule.institutional_13f_issuer_aliases),
        "cusip": rule.cusip,
        "source_reference": rule.source_reference,
    }
