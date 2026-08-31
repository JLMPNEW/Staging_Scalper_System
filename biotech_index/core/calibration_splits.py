from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping


TRADING_BARS_PER_CALENDAR_YEAR = 252.0
CALENDAR_DAYS_PER_YEAR = 365.25
DEFAULT_EMBARGO_BUFFER_DAYS = 10


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + int(months)
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + int(years))
    except ValueError:
        return value.replace(year=value.year + int(years), month=2, day=28)


def minimum_calendar_embargo_days(
    horizon_bars: int,
    *,
    buffer_days: int = DEFAULT_EMBARGO_BUFFER_DAYS,
) -> int:
    return int(
        math.ceil(max(0, int(horizon_bars)) * CALENDAR_DAYS_PER_YEAR / TRADING_BARS_PER_CALENDAR_YEAR)
        + max(0, int(buffer_days))
    )


@dataclass(frozen=True)
class WalkForwardWindow:
    horizon_bars: int
    validation_months: int
    test_months: int
    step_months: int
    embargo_days: int
    min_training_years: int = 3
    min_train_dates: int = 24
    min_validation_dates: int = 6
    min_test_dates: int = 6

    def __post_init__(self) -> None:
        if self.horizon_bars <= 0:
            raise ValueError("horizon_bars must be positive")
        for field_name in ("validation_months", "test_months", "step_months"):
            if int(getattr(self, field_name)) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.min_training_years < 1:
            raise ValueError("min_training_years must be at least 1")
        minimum_embargo = minimum_calendar_embargo_days(self.horizon_bars)
        if self.embargo_days < minimum_embargo:
            raise ValueError(
                f"embargo_days={self.embargo_days} is below the horizon minimum {minimum_embargo} "
                f"for {self.horizon_bars} trading bars"
            )
        if self.step_months < self.test_months:
            raise ValueError("step_months must be at least test_months so outer-test windows do not overlap")


@dataclass(frozen=True)
class WalkForwardFold:
    fold_id: str
    horizon_bars: int
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    embargo_days: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "horizon_bars": self.horizon_bars,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
            "embargo_days": self.embargo_days,
        }


@dataclass(frozen=True)
class FoldRows:
    train: tuple[Mapping[str, Any], ...]
    validation: tuple[Mapping[str, Any], ...]
    test: tuple[Mapping[str, Any], ...]
    excluded: tuple[Mapping[str, Any], ...]
    exclusion_reasons: Mapping[str, int]


def _clean_dates(values: Iterable[object]) -> list[date]:
    return sorted({parsed for value in values if (parsed := parse_date(value)) is not None})


def build_expanding_walk_forward_folds(
    eligible_signal_dates: Iterable[object],
    window: WalkForwardWindow,
) -> list[WalkForwardFold]:
    dates = _clean_dates(eligible_signal_dates)
    if not dates:
        return []
    first_date = dates[0]
    last_date = dates[-1]
    validation_start = add_years(first_date, window.min_training_years)
    folds: list[WalkForwardFold] = []
    fold_number = 1
    while validation_start <= last_date:
        validation_nominal_end = add_months(validation_start, window.validation_months) - timedelta(days=1)
        test_start = validation_nominal_end + timedelta(days=1)
        test_end = add_months(test_start, window.test_months) - timedelta(days=1)
        if test_end > last_date:
            break
        train_end = validation_start - timedelta(days=window.embargo_days + 1)
        validation_end = test_start - timedelta(days=window.embargo_days + 1)
        if train_end >= first_date and validation_end >= validation_start:
            folds.append(
                WalkForwardFold(
                    fold_id=f"h{window.horizon_bars}_f{fold_number:02d}",
                    horizon_bars=window.horizon_bars,
                    train_start=first_date,
                    train_end=train_end,
                    validation_start=validation_start,
                    validation_end=validation_end,
                    test_start=test_start,
                    test_end=test_end,
                    embargo_days=window.embargo_days,
                )
            )
            fold_number += 1
        validation_start = add_months(validation_start, window.step_months)
    return folds


def _finite_number(raw: object) -> bool:
    try:
        return math.isfinite(float(str(raw)))
    except (TypeError, ValueError):
        return False


def partition_rows_for_fold(
    rows: Iterable[Mapping[str, Any]],
    fold: WalkForwardFold,
    *,
    return_key: str,
    signal_date_key: str = "asof_date",
    target_date_key: str | None = None,
) -> FoldRows:
    target_key = target_date_key or f"fwd_{fold.horizon_bars}d_target_date"
    train: list[Mapping[str, Any]] = []
    validation: list[Mapping[str, Any]] = []
    test: list[Mapping[str, Any]] = []
    excluded: list[Mapping[str, Any]] = []
    reasons: dict[str, int] = {}

    def reject(row: Mapping[str, Any], reason: str) -> None:
        excluded.append(row)
        reasons[reason] = reasons.get(reason, 0) + 1

    for row in rows:
        signal_date = parse_date(row.get(signal_date_key))
        target_date = parse_date(row.get(target_key))
        if signal_date is None:
            reject(row, "invalid_signal_date")
            continue
        if target_date is None:
            reject(row, "missing_target_date")
            continue
        if target_date < signal_date:
            reject(row, "target_before_signal")
            continue
        if not _finite_number(row.get(return_key)):
            reject(row, "missing_return")
            continue
        if fold.train_start <= signal_date <= fold.train_end:
            if target_date >= fold.validation_start:
                reject(row, "train_target_crosses_validation")
            else:
                train.append(row)
            continue
        if fold.validation_start <= signal_date <= fold.validation_end:
            if target_date >= fold.test_start:
                reject(row, "validation_target_crosses_test")
            else:
                validation.append(row)
            continue
        if fold.test_start <= signal_date <= fold.test_end:
            if target_date > fold.test_end:
                reject(row, "test_target_after_test_end")
            else:
                test.append(row)
            continue
        reject(row, "outside_fold_windows")

    return FoldRows(
        train=tuple(train),
        validation=tuple(validation),
        test=tuple(test),
        excluded=tuple(excluded),
        exclusion_reasons=dict(sorted(reasons.items())),
    )


def validate_fold_support(fold_rows: FoldRows, window: WalkForwardWindow) -> list[str]:
    errors: list[str] = []
    counts = {
        "train": len({str(row.get("asof_date") or "") for row in fold_rows.train}),
        "validation": len({str(row.get("asof_date") or "") for row in fold_rows.validation}),
        "test": len({str(row.get("asof_date") or "") for row in fold_rows.test}),
    }
    requirements = {
        "train": window.min_train_dates,
        "validation": window.min_validation_dates,
        "test": window.min_test_dates,
    }
    for split, minimum in requirements.items():
        if counts[split] < minimum:
            errors.append(f"{split}_dates<{minimum}:{counts[split]}")
    return errors

