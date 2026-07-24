#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.csv_utils import read_csv_flexible  # noqa: E402
from industrials.core.config import family_config, resolve_path  # noqa: E402
from industrials.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402
from industrials.transportation.security_continuity import (  # noqa: E402
    load_security_continuity_policies,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, resolve_foundation  # noqa: E402


MAP_FIELDS = [
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
MEMBERSHIP_FIELDS = [
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
LISTING_FIELDS = [
    "ticker",
    "first_eligible_date",
    "last_eligible_date",
    "eligibility_basis",
    "source",
    "confidence",
    "notes",
]


@dataclass(frozen=True)
class Override:
    ticker: str
    symbol: str
    database: str
    start: str
    end: str
    reason: str
    calibration_usable: bool
    review_status: str
    notes: str


def finite_float(value: object, *, default: float = 0.0) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def integer_flag(value: object) -> int:
    parsed = finite_float(value)
    return int(parsed) if parsed.is_integer() else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve transportation active/delisted tickers to local Norgate history contracts."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--allow-review-required", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def load_provider() -> Any:
    try:
        import norgatedata  # type: ignore
    except ImportError as exc:
        raise RuntimeError("norgatedata is required for transportation symbol reconciliation") from exc
    return norgatedata


def clean_name(raw: object) -> str:
    text = str(raw or "").upper().replace("&", " AND ")
    text = re.sub(
        r"\b(THE|INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|PLC|LLC|LP|HOLDINGS?|GROUP|COMMON|ORDINARY|SHARES?|ADR|ADS)\b",
        " ",
        text,
    )
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", text).split())


def similarity(left: object, right: object) -> float:
    a, b = clean_name(left), clean_name(right)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def provider_date(provider: Any, method: str, symbol: str) -> str:
    try:
        value = getattr(provider, method)(symbol)
        return str(value)[:10] if value else ""
    except Exception:
        return ""


def provider_name(provider: Any, symbol: str) -> str:
    try:
        return str(provider.security_name(symbol) or "")
    except Exception:
        return ""


def load_overrides(path: Path) -> dict[str, Override]:
    out: dict[str, Override] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row.get("ticker"))
        symbol = normalize_ticker(row.get("norgate_symbol"))
        if not ticker or not symbol:
            raise ValueError(f"Invalid Norgate override row: {row}")
        if ticker in out:
            raise ValueError(f"Duplicate Norgate override ticker={ticker}")
        if str(row.get("review_status") or "").strip().lower() != "reviewed":
            raise ValueError(f"Norgate override must be reviewed: {ticker}")
        usable_raw = str(row.get("calibration_usable_flag") or "").strip()
        if usable_raw not in {"0", "1"}:
            raise ValueError(f"Norgate override requires calibration_usable_flag 0/1: {ticker}")
        out[ticker] = Override(
            ticker=ticker,
            symbol=symbol,
            database=str(row.get("source_database") or "").strip(),
            start=str(row.get("override_start_date") or "").strip(),
            end=str(row.get("override_end_date") or "").strip(),
            reason=str(row.get("mapping_reason") or "reviewed_override").strip(),
            calibration_usable=usable_raw == "1",
            review_status="reviewed",
            notes=str(row.get("notes") or "").strip(),
        )
    return out


def choose_delisted_symbol(
    *,
    provider: Any,
    ticker: str,
    company: str,
    exit_year: int,
    current: set[str],
    delisted: set[str],
    override: Override | None,
) -> dict[str, object]:
    if override is not None:
        universe = delisted if override.database == "US Equities Delisted" else current
        if override.symbol not in universe:
            return {
                "symbol": override.symbol,
                "database": override.database,
                "status": "invalid_override",
                "reason": "reviewed_override_symbol_not_found",
                "review": "review_required",
            }
        name = provider_name(provider, override.symbol)
        return {
            "symbol": override.symbol,
            "database": override.database,
            "name": name,
            "first": override.start or provider_date(provider, "first_quoted_date", override.symbol),
            "last": override.end or provider_date(provider, "last_quoted_date", override.symbol),
            "score": similarity(company, name),
            "status": "verified_override" if override.calibration_usable else "verified_excluded",
            "reason": override.reason,
            "review": "reviewed",
            "usable": override.calibration_usable,
            "notes": override.notes,
        }
    candidates = {
        symbol
        for symbol in delisted
        if symbol == ticker
        or symbol.startswith(f"{ticker}-")
        or symbol.startswith(f"{ticker}Q-")
        or symbol.startswith(f"{ticker}D-")
    }
    if ticker in current:
        candidates.add(ticker)
    scored: list[tuple[float, str, str, str, str, str]] = []
    for symbol in candidates:
        database = "US Equities Delisted" if symbol in delisted else "US Equities"
        name = provider_name(provider, symbol)
        last = provider_date(provider, "last_quoted_date", symbol)
        first = provider_date(provider, "first_quoted_date", symbol)
        name_score = similarity(company, name)
        year_score = 0.0
        if last[:4].isdigit():
            delta = abs(int(last[:4]) - exit_year)
            year_score = 1.0 if delta == 0 else 0.6 if delta == 1 else 0.0
        exact_bonus = 0.15 if symbol == ticker else 0.0
        current_penalty = 0.35 if database == "US Equities" else 0.0
        score = 0.65 * name_score + 0.35 * year_score + exact_bonus - current_penalty
        scored.append((score, symbol, database, name, first, last))
    if not scored:
        return {"symbol": "", "database": "", "status": "unresolved", "reason": "no_candidate", "review": "review_required"}
    scored.sort(reverse=True)
    score, symbol, database, name, first, last = scored[0]
    year_ok = last[:4].isdigit() and abs(int(last[:4]) - exit_year) <= 1
    auto = score >= 0.70 and year_ok and database == "US Equities Delisted"
    return {
        "symbol": symbol,
        "database": database,
        "name": name,
        "first": first,
        "last": last,
        "score": similarity(company, name),
        "status": "verified_auto" if auto else "review_required",
        "reason": "symbol_name_exit_year_match" if auto else "best_candidate_requires_review",
        "review": "auto_verified" if auto else "review_required",
        "usable": auto,
        "notes": f"composite_match_score={score:.6f}",
    }


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def main() -> int:
    args = parse_args()
    paths = resolve_foundation(args.config)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else paths.active_path.parent
    map_path = output_dir / "transportation_norgate_symbol_map.csv"
    membership_path = output_dir / "transportation_historical_membership.csv"
    listing_path = output_dir / "transportation_listing_dates.csv"
    overrides_path = paths.active_path.parent / "transportation_norgate_symbol_overrides.csv"
    family = family_config(paths.config, "transportation")
    universe = family["universe"]
    continuity_path = resolve_path(
        universe["security_continuity_overrides_csv"],
        base_dir=paths.config_path.parent,
    )
    continuity_policies = load_security_continuity_policies(continuity_path)
    provider = load_provider()
    current = set(provider.database_symbols("US Equities"))
    delisted_symbols = set(provider.database_symbols("US Equities Delisted"))
    overrides = load_overrides(overrides_path)
    active_rows = read_csv_flexible(paths.active_path)
    delisted_rows = read_csv_flexible(paths.delisted_path)
    active_tickers = {normalize_ticker(row["ticker"]) for row in active_rows}
    unknown_continuity_tickers = sorted(set(continuity_policies) - active_tickers)
    if unknown_continuity_tickers:
        raise ValueError(
            "Security-continuity policies reference non-active tickers="
            f"{unknown_continuity_tickers}"
        )
    history_start = "2019-01-02"
    mapping_rows: list[dict[str, object]] = []
    membership_rows: list[dict[str, object]] = []
    listing_rows: list[dict[str, object]] = []
    cohort_names = {
        "surface_freight_and_logistics": "Surface Freight & Logistics",
        "air_transport_and_aviation_services": "Air Transport & Aviation Services",
        "marine_shipping_and_maritime": "Marine Shipping & Maritime",
        "development_stage_and_speculative_transport": "Development-stage & Speculative Transport",
    }
    for row in active_rows:
        ticker = normalize_ticker(row["ticker"])
        continuity = continuity_policies.get(ticker)
        database = "US Equities" if ticker in current else "US Equities Delisted" if ticker in delisted_symbols else ""
        symbol = ticker if database else ""
        name = provider_name(provider, symbol) if symbol else ""
        first = provider_date(provider, "first_quoted_date", symbol) if symbol else ""
        # Active rows intentionally publish a blank last_quoted_date, so the
        # provider terminal-date lookup is skipped entirely for them.
        score = similarity(row["company_name"], name)
        usable = bool(symbol and database == "US Equities" and first and score >= 0.40)
        status = "verified_exact_active" if usable else "review_required"
        start = (
            continuity.current_security_start_date
            if continuity is not None
            else max(history_start, first)
            if first
            else history_start
        )
        mapping_rows.append(
            {
                "internal_ticker": ticker,
                "actual_ticker": ticker,
                "norgate_symbol": symbol,
                "source_database": database,
                "company_name": row["company_name"],
                "norgate_security_name": name,
                "first_quoted_date": first,
                "last_quoted_date": "",
                "mapping_status": status,
                "mapping_reason": "exact_active_symbol_and_name_match" if usable else "active_symbol_requires_review",
                "name_similarity": f"{score:.6f}",
                "exit_year": "",
                "calibration_usable_flag": int(usable),
                "review_status": "auto_verified" if usable else "review_required",
                "notes": (
                    f"continuity_policy={continuity.continuity_policy}; "
                    f"history_treatment={continuity.history_treatment}"
                    if continuity is not None
                    else ""
                ),
            }
        )
        membership_rows.append(
            {
                "internal_ticker": ticker,
                "exchange_ticker": ticker,
                "price_source_symbol": symbol,
                "company_name": row["company_name"],
                "cik": normalize_cik(row["cik"]),
                "exchange": row["exchange"],
                "country": row["country"],
                "currency": row["currency"],
                "security_type": row["security_type"],
                "calibration_cohort_id": row["calibration_cohort"],
                "calibration_cohort": cohort_names[row["calibration_cohort"]],
                "start_date": start,
                "end_date": "",
                "membership_status": "active",
                "successor_ticker": "",
                "event_type": (
                    continuity.continuity_policy.lower()
                    if continuity is not None
                    else "active_at_contract_build"
                ),
                "confidence": (
                    f"{continuity.confidence:.2f}"
                    if continuity is not None
                    else "0.95"
                    if usable
                    else "0.50"
                ),
                "source_url": (
                    continuity.primary_source_url
                    if continuity is not None
                    else "norgate_local_metadata"
                ),
                "notes": (
                    f"{continuity.history_treatment}; {continuity.notes}"
                    if continuity is not None
                    else "Exact active mapping."
                    if usable
                    else "Requires Norgate identity review."
                ),
            }
        )
        listing_rows.append(
            {
                "ticker": ticker,
                "first_eligible_date": start,
                "last_eligible_date": "",
                "eligibility_basis": (
                    continuity.history_treatment
                    if continuity is not None
                    else "max_history_start_and_norgate_first_quoted_date"
                ),
                "source": (
                    continuity.primary_source_url
                    if continuity is not None
                    else "norgate_local_metadata"
                ),
                "confidence": (
                    f"{continuity.confidence:.2f}"
                    if continuity is not None
                    else "0.95"
                    if usable
                    else "0.50"
                ),
                "notes": (
                    f"continuity_policy={continuity.continuity_policy}; "
                    f"related_price_symbols={continuity.related_price_symbols}; "
                    f"raw_norgate_first_quoted_date={first}"
                    if continuity is not None
                    else f"norgate_symbol={symbol}; raw_first_quoted_date={first}"
                ),
            }
        )
    for row in delisted_rows:
        ticker = normalize_ticker(row["ticker"])
        exit_year = int(row["exit_year"])
        match = choose_delisted_symbol(
            provider=provider,
            ticker=ticker,
            company=row["company"],
            exit_year=exit_year,
            current=current,
            delisted=delisted_symbols,
            override=overrides.get(ticker),
        )
        internal = f"{ticker}-DEL{exit_year}" if ticker in active_tickers else ticker
        internal = normalize_ticker(internal)
        first = str(match.get("first") or "")
        last = str(match.get("last") or "")
        usable = bool(match.get("usable") and first and last)
        # Historical issuers may have exited before the active-model history
        # floor. Preserve their provider listing boundary so start never falls
        # after the verified final quote; downstream panels apply their own floor.
        start = first or history_start
        mapping_rows.append(
            {
                "internal_ticker": internal,
                "actual_ticker": ticker,
                "norgate_symbol": match.get("symbol", ""),
                "source_database": match.get("database", ""),
                "company_name": row["company"],
                "norgate_security_name": match.get("name", ""),
                "first_quoted_date": first,
                "last_quoted_date": last,
                "mapping_status": match.get("status", "unresolved"),
                "mapping_reason": match.get("reason", ""),
                "name_similarity": f"{finite_float(match.get('score')):.6f}",
                "exit_year": exit_year,
                "calibration_usable_flag": int(usable),
                "review_status": match.get("review", "review_required"),
                "notes": match.get("notes", ""),
            }
        )
        if usable:
            membership_rows.append(
                {
                    "internal_ticker": internal,
                    "exchange_ticker": ticker,
                    "price_source_symbol": match["symbol"],
                    "company_name": row["company"],
                    "cik": normalize_cik(row["cik"]),
                    "exchange": "historical_delisted",
                    "country": "United States",
                    "currency": "USD",
                    "security_type": "Common Stock",
                    "calibration_cohort_id": row["cohort"],
                    "calibration_cohort": cohort_names[row["cohort"]],
                    "start_date": start,
                    "end_date": last,
                    "membership_status": "delisted",
                    "successor_ticker": "",
                    "event_type": row["exit_type"],
                    "confidence": "0.95" if row["confidence"].lower() == "verified" else "0.90",
                    "source_url": "norgate_local_metadata",
                    "notes": f"terminal_type={row['terminal_type']}; acquirer={row['acquirer']}",
                }
            )
            listing_rows.append(
                {
                    "ticker": internal,
                    "first_eligible_date": start,
                    "last_eligible_date": last,
                    "eligibility_basis": "norgate_first_and_last_quoted_dates",
                    "source": "norgate_local_metadata",
                    "confidence": "0.95",
                    "notes": f"actual_ticker={ticker}; norgate_symbol={match['symbol']}",
                }
            )
    write_csv(map_path, MAP_FIELDS, mapping_rows)
    write_csv(membership_path, MEMBERSHIP_FIELDS, membership_rows)
    write_csv(listing_path, LISTING_FIELDS, listing_rows)
    review = [row["internal_ticker"] for row in mapping_rows if row["review_status"] == "review_required"]
    summary = {
        "status": "PASS" if not review or args.allow_review_required else "FAIL",
        "mapping_rows": len(mapping_rows),
        "calibration_usable": sum(integer_flag(row["calibration_usable_flag"]) for row in mapping_rows),
        "historical_membership_rows": len(membership_rows),
        "listing_rows": len(listing_rows),
        "review_required": review,
        "mapping_path": str(map_path),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
