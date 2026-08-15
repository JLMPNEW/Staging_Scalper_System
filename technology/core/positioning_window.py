from __future__ import annotations

from datetime import date, datetime, timedelta


DEFAULT_INCREMENTAL_LOOKBACK_DAYS = 550


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
