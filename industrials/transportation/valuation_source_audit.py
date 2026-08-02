from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PRIMARY_SHARE_CONCEPTS = (
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("ifrs-full", "NumberOfSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
)
FALLBACK_SHARE_CONCEPTS = (
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
    ("ifrs-full", "WeightedAverageNumberOfSharesOutstanding"),
)
FOREIGN_REPORTING_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A", "6-K"})
CONVERSION_FIELDS = (
    "ticker",
    "effective_from",
    "effective_to",
    "listing_instrument",
    "underlying_shares_per_traded_security",
    "review_status",
    "source_url",
    "notes",
)
CONVERSION_STATUSES = frozenset({"PENDING_REVIEW", "REVIEWED_ADR", "REVIEWED_DIRECT"})


@dataclass(frozen=True)
class ShareConversion:
    ticker: str
    effective_from: date
    effective_to: date | None
    listing_instrument: str
    underlying_shares_per_traded_security: float | None
    review_status: str
    source_url: str
    notes: str

    def active_on(self, asof: date) -> bool:
        return self.effective_from <= asof and (
            self.effective_to is None or asof <= self.effective_to
        )


def _date(value: object, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as exc:
        raise ValueError(f"invalid {field}={value!r}") from exc


def _positive_finite(value: object) -> bool:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0


def load_share_conversions(
    path: Path,
) -> dict[str, tuple[ShareConversion, ...]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CONVERSION_FIELDS:
            raise ValueError(
                f"{path}: expected fields={list(CONVERSION_FIELDS)} actual={reader.fieldnames}"
            )
        raw_rows = list(reader)
    grouped: dict[str, list[ShareConversion]] = {}
    for raw in raw_rows:
        ticker = str(raw["ticker"] or "").strip().upper()
        if not ticker:
            raise ValueError(f"{path}: blank ticker")
        effective_from = _date(raw["effective_from"], field="effective_from")
        end_text = str(raw["effective_to"] or "").strip()
        effective_to = _date(end_text, field="effective_to") if end_text else None
        if effective_to is not None and effective_to < effective_from:
            raise ValueError(f"{path}: {ticker} effective_to precedes effective_from")
        status = str(raw["review_status"] or "").strip().upper()
        if status not in CONVERSION_STATUSES:
            raise ValueError(f"{path}: {ticker} invalid review_status={status!r}")
        ratio_text = str(raw["underlying_shares_per_traded_security"] or "").strip()
        ratio = float(ratio_text) if ratio_text else None
        if ratio is not None and (not math.isfinite(ratio) or ratio <= 0.0):
            raise ValueError(f"{path}: {ticker} ratio must be positive")
        source_url = str(raw["source_url"] or "").strip()
        if status == "PENDING_REVIEW" and ratio is not None:
            raise ValueError(f"{path}: {ticker} pending row cannot set a ratio")
        if status == "REVIEWED_ADR" and (ratio is None or not source_url):
            raise ValueError(f"{path}: {ticker} reviewed ADR requires ratio and source_url")
        if status == "REVIEWED_DIRECT" and (ratio != 1.0 or not source_url):
            raise ValueError(f"{path}: {ticker} reviewed direct row requires ratio=1 and source_url")
        item = ShareConversion(
            ticker=ticker,
            effective_from=effective_from,
            effective_to=effective_to,
            listing_instrument=str(raw["listing_instrument"] or "").strip(),
            underlying_shares_per_traded_security=ratio,
            review_status=status,
            source_url=source_url,
            notes=str(raw["notes"] or "").strip(),
        )
        grouped.setdefault(ticker, []).append(item)
    result: dict[str, tuple[ShareConversion, ...]] = {}
    for ticker, items in grouped.items():
        ordered = sorted(items, key=lambda item: item.effective_from)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None or current.effective_from <= previous.effective_to:
                raise ValueError(f"{path}: overlapping share conversions for {ticker}")
        result[ticker] = tuple(ordered)
    return result


def resolve_share_conversion(
    ticker: str,
    *,
    asof: date,
    conversions: Mapping[str, Iterable[ShareConversion]],
) -> ShareConversion | None:
    matches = [item for item in conversions.get(ticker.upper(), ()) if item.active_on(asof)]
    if len(matches) > 1:
        raise ValueError(f"multiple active share conversions for {ticker} at {asof}")
    return matches[0] if matches else None


def _usable_facts(
    payload: Mapping[str, object],
    *,
    namespace: str,
    concept: str,
    asof: date,
) -> list[dict[str, object]]:
    facts = payload.get("facts")
    if not isinstance(facts, Mapping):
        return []
    namespace_facts = facts.get(namespace)
    if not isinstance(namespace_facts, Mapping):
        return []
    concept_payload = namespace_facts.get(concept)
    if not isinstance(concept_payload, Mapping):
        return []
    units = concept_payload.get("units")
    if not isinstance(units, Mapping):
        return []
    usable: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for unit_rows in units.values():
        if not isinstance(unit_rows, Sequence) or isinstance(unit_rows, (str, bytes)):
            continue
        for fact in unit_rows:
            if not isinstance(fact, Mapping) or not _positive_finite(fact.get("val")):
                continue
            filed_text = str(fact.get("filed") or "").strip()[:10]
            end_text = str(fact.get("end") or "").strip()[:10]
            try:
                filed = date.fromisoformat(filed_text)
                period_end = date.fromisoformat(end_text)
            except ValueError:
                continue
            if filed > asof or period_end > asof:
                continue
            key = (
                filed_text,
                end_text,
                str(fact.get("accn") or ""),
                str(fact.get("val") or ""),
            )
            if key not in seen:
                seen.add(key)
                usable.append(dict(fact))
    return sorted(
        usable,
        key=lambda fact: (
            str(fact.get("filed") or ""),
            str(fact.get("end") or ""),
            str(fact.get("accn") or ""),
        ),
    )


def inspect_companyfacts_share_sources(
    payload: Mapping[str, object],
    *,
    asof: date,
) -> dict[str, object]:
    selected_namespace = ""
    selected_concept = ""
    selected_kind = ""
    selected: list[dict[str, object]] = []
    for kind, candidates in (
        ("primary", PRIMARY_SHARE_CONCEPTS),
        ("fallback", FALLBACK_SHARE_CONCEPTS),
    ):
        for namespace, concept in candidates:
            rows = _usable_facts(
                payload,
                namespace=namespace,
                concept=concept,
                asof=asof,
            )
            if rows:
                selected_namespace = namespace
                selected_concept = concept
                selected_kind = kind
                selected = rows
                break
        if selected:
            break
    forms = sorted({str(row.get("form") or "").strip() for row in selected if row.get("form")})
    return {
        "share_source_kind": selected_kind or "none",
        "share_namespace": selected_namespace,
        "share_concept": selected_concept,
        "usable_fact_count": len(selected),
        "first_period_end": str(selected[0].get("end") or "") if selected else "",
        "last_period_end": str(selected[-1].get("end") or "") if selected else "",
        "first_filed_date": str(selected[0].get("filed") or "") if selected else "",
        "last_filed_date": str(selected[-1].get("filed") or "") if selected else "",
        "reporting_forms": "|".join(forms),
        "foreign_reporting_flag": int(bool(set(forms) & FOREIGN_REPORTING_FORMS)),
    }


def load_companyfacts(path: Path) -> Mapping[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: CompanyFacts root must be an object")
    return payload


def companyfacts_path(cache_root: Path, cik: object) -> Path | None:
    digits = "".join(character for character in str(cik or "") if character.isdigit())
    if not digits:
        return None
    return cache_root / f"CIK{int(digits):010d}.json"


def summarize_audit(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    required = [row for row in rows if str(row.get("required_for_rebuild") or "") == "1"]
    blockers = [row for row in required if str(row.get("readiness_status") or "") != "READY"]
    dispositions: dict[str, int] = {}
    for row in rows:
        key = str(row.get("disposition") or "")
        dispositions[key] = dispositions.get(key, 0) + 1
    return {
        "audited_ticker_count": len(rows),
        "required_ticker_count": len(required),
        "ready_required_ticker_count": len(required) - len(blockers),
        "blocked_required_ticker_count": len(blockers),
        "valuation_rebuild_readiness": "PASS" if not blockers else "FAIL",
        "blocked_required_tickers": sorted(str(row.get("ticker") or "") for row in blockers),
        "disposition_counts": dict(sorted(dispositions.items())),
    }
