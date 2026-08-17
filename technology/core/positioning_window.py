from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


DEFAULT_INCREMENTAL_LOOKBACK_DAYS = 550
DEFAULT_FORM4_LOOKBACK_DAYS = 120
DEFAULT_SHORT_INTEREST_LOOKBACK_DAYS = 120
DEFAULT_13F_LOOKBACK_DAYS = 550
DEFAULT_BORROW_LOOKBACK_DAYS = 45
DEFAULT_FLOAT_DENOMINATOR_LOOKBACK_DAYS = 550


@dataclass(frozen=True)
class PositioningWindows:
    end: date
    form4_start: date
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


def resolve_positioning_window(
    *,
    asof: object,
    configured_start: object,
    requested_start: object = "",
    full_history: bool = False,
    lookback_days: int = DEFAULT_INCREMENTAL_LOOKBACK_DAYS,
) -> tuple[date, date]:
    """Return a PIT-safe positioning import window.

    Incremental windows retain enough history for annual foreign-filer float
    inputs and trailing positioning features. Full-history mode is reserved for
    explicit restatements and initial database construction.
    """
    if lookback_days <= 0:
        raise ValueError("positioning lookback_days must be positive")

    end = parse_iso_date(asof, field_name="asof") if str(asof or "").strip() else date.today()
    floor = parse_iso_date(configured_start, field_name="configured_start")
    if full_history:
        start = floor
    elif str(requested_start or "").strip():
        start = max(
            floor,
            parse_iso_date(requested_start, field_name="history_start"),
        )
    else:
        start = max(floor, end - timedelta(days=lookback_days))

    if start > end:
        raise ValueError(f"positioning history_start {start.isoformat()} is after asof {end.isoformat()}")
    return start, end


def resolve_positioning_windows(
    *,
    asof: object,
    configured_start: object,
    requested_start: object = "",
    form4_requested_start: object = "",
    short_interest_requested_start: object = "",
    institutional_13f_requested_start: object = "",
    borrow_requested_start: object = "",
    float_denominator_requested_start: object = "",
    full_history: bool = False,
    form4_lookback_days: int = DEFAULT_FORM4_LOOKBACK_DAYS,
    short_interest_lookback_days: int = DEFAULT_SHORT_INTEREST_LOOKBACK_DAYS,
    institutional_13f_lookback_days: int = DEFAULT_13F_LOOKBACK_DAYS,
    borrow_lookback_days: int = DEFAULT_BORROW_LOOKBACK_DAYS,
    float_denominator_lookback_days: int = DEFAULT_FLOAT_DENOMINATOR_LOOKBACK_DAYS,
) -> PositioningWindows:
    """Resolve source-specific incremental windows against one PIT-safe end date.

    ``requested_start`` remains a backwards-compatible common override. A
    source-specific override wins when both are supplied. Full-history mode
    always resolves every source to the configured floor.
    """
    common_start = "" if full_history else requested_start

    def source_window(source_start: object, lookback_days: int) -> tuple[date, date]:
        return resolve_positioning_window(
            asof=asof,
            configured_start=configured_start,
            requested_start=(source_start or common_start),
            full_history=full_history,
            lookback_days=lookback_days,
        )

    form4_start, end = source_window(form4_requested_start, form4_lookback_days)
    short_start, short_end = source_window(
        short_interest_requested_start,
        short_interest_lookback_days,
    )
    institutional_start, institutional_end = source_window(
        institutional_13f_requested_start,
        institutional_13f_lookback_days,
    )
    borrow_start, borrow_end = source_window(
        borrow_requested_start,
        borrow_lookback_days,
    )
    float_start, float_end = source_window(
        float_denominator_requested_start,
        float_denominator_lookback_days,
    )
    if len({end, short_end, institutional_end, borrow_end, float_end}) != 1:
        raise AssertionError("positioning source windows resolved to different end dates")
    return PositioningWindows(
        end=end,
        form4_start=form4_start,
        short_interest_start=short_start,
        institutional_13f_start=institutional_start,
        borrow_start=borrow_start,
        float_denominator_start=float_start,
    )
