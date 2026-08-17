from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


DEFAULT_SHORT_INTEREST_LOOKBACK_DAYS = 120
DEFAULT_13F_LOOKBACK_DAYS = 550
DEFAULT_BORROW_LOOKBACK_DAYS = 120
DEFAULT_FLOAT_DENOMINATOR_LOOKBACK_DAYS = 550


@dataclass(frozen=True)
class PositioningSourceWindows:
    end: date
    short_interest_start: date
    institutional_13f_start: date
    borrow_start: date
    float_denominator_start: date


def parse_iso_date(raw: object, *, field_name: str) -> date:
    text = str(raw or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD, got {raw!r}") from exc


def bounded_start(*, end: date, floor: date, lookback_days: int, full_history: bool) -> date:
    if lookback_days <= 0:
        raise ValueError("source lookback_days must be positive")
    return floor if full_history else max(floor, end - timedelta(days=lookback_days))


def resolve_positioning_source_windows(
    *,
    asof: object,
    configured_start: object,
    full_history: bool = False,
    short_interest_lookback_days: int = DEFAULT_SHORT_INTEREST_LOOKBACK_DAYS,
    institutional_13f_lookback_days: int = DEFAULT_13F_LOOKBACK_DAYS,
    borrow_lookback_days: int = DEFAULT_BORROW_LOOKBACK_DAYS,
    float_denominator_lookback_days: int = DEFAULT_FLOAT_DENOMINATOR_LOOKBACK_DAYS,
) -> PositioningSourceWindows:
    """Resolve PIT-safe source windows without truncating stored history.

    Biotech borrow features use 90-day averages, so their initial/import window
    is deliberately longer than the 45-day provider overlap used after a ticker
    is already hydrated. Full-history mode is reserved for explicit backfills.
    """
    end = parse_iso_date(asof, field_name="asof") if str(asof or "").strip() else date.today()
    floor = parse_iso_date(configured_start, field_name="configured_start")
    if floor > end:
        raise ValueError(f"configured_start {floor.isoformat()} is after asof {end.isoformat()}")
    return PositioningSourceWindows(
        end=end,
        short_interest_start=bounded_start(
            end=end,
            floor=floor,
            lookback_days=short_interest_lookback_days,
            full_history=full_history,
        ),
        institutional_13f_start=bounded_start(
            end=end,
            floor=floor,
            lookback_days=institutional_13f_lookback_days,
            full_history=full_history,
        ),
        borrow_start=bounded_start(
            end=end,
            floor=floor,
            lookback_days=borrow_lookback_days,
            full_history=full_history,
        ),
        float_denominator_start=bounded_start(
            end=end,
            floor=floor,
            lookback_days=float_denominator_lookback_days,
            full_history=full_history,
        ),
    )
