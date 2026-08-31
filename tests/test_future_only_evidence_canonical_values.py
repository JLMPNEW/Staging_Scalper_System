from __future__ import annotations

from datetime import date, datetime

import pytest

from future_only_evidence.canonical_values import exact_date, exact_utc


def test_exact_date_accepts_only_exact_iso_date() -> None:
    assert exact_date("2026-09-30", label="date") == date(2026, 9, 30)


@pytest.mark.parametrize(
    "value",
    [
        date(2026, 9, 30),
        "2026-09-30T00:00:00Z",
        "2026-9-30",
        " 2026-09-30",
        "2026-02-30",
    ],
)
def test_exact_date_rejects_coercion_truncation_and_invalid_dates(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        exact_date(value, label="date")


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-30T20:00:00Z",
        "2026-09-30T20:00:00+00:00",
        "2026-09-30T20:00:00.123456+00:00",
    ],
)
def test_exact_utc_accepts_only_explicit_rfc3339_utc_forms(value: str) -> None:
    assert exact_utc(value, label="time").utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 9, 30, 20, 0),
        "2026-09-30 20:00:00+00:00",
        "2026-09-30T20:00+00:00",
        "2026-09-30T20:00:00+0000",
        "2026-09-30T20:00:00+01:00",
        "2026-09-30T20:00:00z",
        "2026-09-30T20:00:00.1234567Z",
    ],
)
def test_exact_utc_rejects_coercion_and_noncanonical_forms(value: object) -> None:
    with pytest.raises(ValueError, match="exact RFC3339 UTC"):
        exact_utc(value, label="time")
