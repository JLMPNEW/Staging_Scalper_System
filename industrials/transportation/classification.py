from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping


POLICY_VERSION = "transportation_classification_v1"
CALIBRATION_POOLS = frozenset(
    {
        "surface_freight_and_logistics",
        "air_transport_and_aviation_services",
        "marine_shipping_and_maritime",
    }
)
RISK_TIERS = frozenset({"operating", "development_speculative"})
PORTFOLIO_ROLES = frozenset(
    {
        "core_candidate",
        "airline_satellite_research",
        "speculative_research",
        "universe_review",
    }
)
OVERLAY_FIELDS = (
    "ticker",
    "effective_from",
    "effective_to",
    "economic_peer_group",
    "portfolio_role",
    "review_status",
    "source",
    "notes",
)


@dataclass(frozen=True)
class Classification:
    calibration_pool: str
    economic_peer_group: str
    risk_tier: str
    portfolio_role: str
    policy_version: str = POLICY_VERSION

    @property
    def production_portfolio_authorized(self) -> bool:
        return self.portfolio_role == "core_candidate"


@dataclass(frozen=True)
class ClassificationOverlay:
    ticker: str
    effective_from: date
    effective_to: date | None
    economic_peer_group: str
    portfolio_role: str
    review_status: str
    source: str
    notes: str

    def active_on(self, asof: date) -> bool:
        return self.effective_from <= asof and (
            self.effective_to is None or asof <= self.effective_to
        )


def _parse_date(value: object, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as exc:
        raise ValueError(f"invalid {field}={value!r}") from exc


def load_classification_overlays(
    path: Path,
) -> dict[str, tuple[ClassificationOverlay, ...]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != OVERLAY_FIELDS:
            raise ValueError(
                f"{path}: expected fields={list(OVERLAY_FIELDS)} "
                f"actual={reader.fieldnames}"
            )
        raw_rows = list(reader)
    grouped: dict[str, list[ClassificationOverlay]] = {}
    for raw in raw_rows:
        ticker = str(raw["ticker"] or "").strip().upper()
        if not ticker:
            raise ValueError(f"{path}: blank ticker")
        start = _parse_date(raw["effective_from"], field="effective_from")
        raw_end = str(raw["effective_to"] or "").strip()
        end = _parse_date(raw_end, field="effective_to") if raw_end else None
        if end is not None and end < start:
            raise ValueError(f"{path}: {ticker} effective_to precedes effective_from")
        peer = str(raw["economic_peer_group"] or "").strip()
        role = str(raw["portfolio_role"] or "").strip()
        if not peer:
            raise ValueError(f"{path}: {ticker} blank economic_peer_group")
        if role and role not in PORTFOLIO_ROLES:
            raise ValueError(f"{path}: {ticker} invalid portfolio_role={role!r}")
        overlay = ClassificationOverlay(
            ticker=ticker,
            effective_from=start,
            effective_to=end,
            economic_peer_group=peer,
            portfolio_role=role,
            review_status=str(raw["review_status"] or "").strip(),
            source=str(raw["source"] or "").strip(),
            notes=str(raw["notes"] or "").strip(),
        )
        grouped.setdefault(ticker, []).append(overlay)
    output: dict[str, tuple[ClassificationOverlay, ...]] = {}
    for ticker, rows in grouped.items():
        ordered = sorted(rows, key=lambda item: item.effective_from)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None or current.effective_from <= previous.effective_to:
                raise ValueError(f"{path}: overlapping overlays for {ticker}")
        output[ticker] = tuple(ordered)
    return output


def _calibration_pool(*, existing_cohort: str, industry: str) -> str:
    if existing_cohort in CALIBRATION_POOLS:
        return existing_cohort
    if industry == "Marine Shipping":
        return "marine_shipping_and_maritime"
    if industry in {"Airlines", "Airports & Air Services"}:
        return "air_transport_and_aviation_services"
    return "surface_freight_and_logistics"


def _base_peer_group(*, calibration_pool: str, industry: str) -> str:
    if industry == "Airlines":
        return "airlines"
    if industry == "Airports & Air Services":
        return "aviation_services"
    if industry == "Rental & Leasing Services":
        return (
            "aviation_leasing"
            if calibration_pool == "air_transport_and_aviation_services"
            else "surface_equipment_leasing"
        )
    if industry == "Railroads":
        return "rail_and_rail_equipment"
    if industry == "Trucking":
        return "trucking"
    if industry == "Marine Shipping":
        return "marine_shipping"
    return "integrated_freight_and_logistics"


def resolve_classification(
    row: Mapping[str, object],
    *,
    asof: str,
    overlays: Mapping[str, Iterable[ClassificationOverlay]] | None = None,
) -> Classification:
    ticker = str(row.get("ticker") or "").strip().upper()
    industry = str(row.get("industry") or "").strip()
    existing_cohort = str(row.get("calibration_cohort") or "").strip()
    calibration_use = str(row.get("calibration_use") or "").strip()
    development_stage = str(row.get("development_stage") or "").strip()
    pool = _calibration_pool(existing_cohort=existing_cohort, industry=industry)
    operating_core = calibration_use == "core" and development_stage == "operating"
    risk_tier = "operating" if operating_core else "development_speculative"
    if not operating_core:
        portfolio_role = "speculative_research"
    elif industry == "Airlines":
        portfolio_role = "airline_satellite_research"
    else:
        portfolio_role = "core_candidate"
    peer = _base_peer_group(calibration_pool=pool, industry=industry)
    effective = _parse_date(asof, field="asof")
    matches = [
        item
        for item in (overlays or {}).get(ticker, ())
        if item.active_on(effective)
    ]
    if len(matches) > 1:
        raise ValueError(f"multiple active classification overlays for {ticker} at {asof}")
    if matches:
        overlay = matches[0]
        peer = overlay.economic_peer_group
        portfolio_role = overlay.portfolio_role or portfolio_role
    classification = Classification(
        calibration_pool=pool,
        economic_peer_group=peer,
        risk_tier=risk_tier,
        portfolio_role=portfolio_role,
    )
    if classification.calibration_pool not in CALIBRATION_POOLS:
        raise ValueError(f"{ticker}: invalid calibration pool")
    if classification.risk_tier not in RISK_TIERS:
        raise ValueError(f"{ticker}: invalid risk tier")
    if classification.portfolio_role not in PORTFOLIO_ROLES:
        raise ValueError(f"{ticker}: invalid portfolio role")
    return classification
