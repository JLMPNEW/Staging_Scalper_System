"""Point-in-time (PIT) gating helpers for static CSV and dimension metadata rows.

PIT policy: "Event-dated rows apply historically." A row's effectiveness starts at
``valid_from`` (same-day inclusive) whenever ``valid_from`` (or another
start/effective date) is present; ``reviewed_at`` is provenance documentation and
does NOT gate effectiveness for such rows. ``reviewed_at`` gates effectiveness
(same-day-exclusive) ONLY for rows that have no start/effective date.
``reviewed_at > valid_from`` is the EXPECTED honest pattern for event-dated rows,
not a violation.

Boundary semantics for ``row_is_effective_asof`` (target = asof date):
- ``valid_from`` / ``start_date`` / ``effective_date`` / source-available dates are
  same-day-INCLUSIVE: a row is usable on its start date and fails only when the
  date is strictly after asof.
- ``reviewed_at`` / ``review_date`` / ``source_reviewed_at`` are same-day-EXCLUSIVE
  and apply ONLY when the row has no ``valid_from`` / ``start_date`` /
  ``effective_date``: such a row is usable only strictly after its review date
  (``reviewed_at >= asof`` fails), because the review outcome is not knowable
  intraday. When a start/effective date is present, ``reviewed_at`` is
  provenance-only and never gates effectiveness.
- ``valid_to`` / ``end_date`` are same-day-inclusive on expiry (``valid_to < asof``
  fails).

Invariant: sources that require review provenance are expected to carry a
``reviewed_at`` value on every row. ``validate_pit_invariants`` (with
``require_reviewed_at=True``) flags rows missing ``reviewed_at`` entirely so
loaders can log the provenance gap; the ordering of ``reviewed_at`` versus
``valid_from`` is NOT flagged in either direction.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any


LOGGER = logging.getLogger(__name__)

START_DATE_COLUMNS = (
    "valid_from",
    "start_date",
)
EFFECTIVE_DATE_COLUMNS = (
    "effective_date",
)
END_DATE_COLUMNS = (
    "valid_to",
    "effective_to",
    "end_date",
    "expires_at",
)
REVIEW_DATE_COLUMNS = (
    "reviewed_at",
    "review_date",
    "source_reviewed_at",
)
SOURCE_AVAILABLE_DATE_COLUMNS = (
    "source_published_at",
    "source_date",
    "available_from",
)
PIT_METADATA_COLUMNS = START_DATE_COLUMNS + EFFECTIVE_DATE_COLUMNS + END_DATE_COLUMNS + REVIEW_DATE_COLUMNS + SOURCE_AVAILABLE_DATE_COLUMNS


def parse_iso_date(raw: object) -> date | None:
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    head = text[:10]
    try:
        return datetime.strptime(head, "%Y-%m-%d").date()
    except ValueError:
        pass
    compact = text[:8]
    if len(compact) == 8 and compact.isdigit():
        try:
            return datetime.strptime(compact, "%Y%m%d").date()
        except ValueError:
            LOGGER.warning("Invalid PIT date value ignored: %r", raw)
            return None
    LOGGER.warning("Invalid PIT date value ignored: %r", raw)
    return None


def row_value(row: dict[str, Any], *names: str) -> str:
    lowered = {str(key).strip().lower(): ("" if value is None else str(value).strip()) for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return ""


def row_has_pit_metadata(row: dict[str, Any]) -> bool:
    return any(row_value(row, column) for column in PIT_METADATA_COLUMNS)


def pit_date_parse_errors(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for column in PIT_METADATA_COLUMNS:
        raw = row_value(row, column)
        if raw and parse_iso_date(raw) is None:
            errors.append(column)
    return errors


def validate_pit_invariants(row: dict[str, Any], *, require_reviewed_at: bool = False) -> list[str]:
    # Policy: reviewed_at > valid_from is the expected honest pattern for
    # event-dated rows and is NOT a violation; neither ordering of reviewed_at
    # versus valid_from is flagged. The only invariant checked here is missing
    # review provenance on sources that require it.
    issues: list[str] = []
    if require_reviewed_at and not row_value(row, *REVIEW_DATE_COLUMNS):
        issues.append("reviewed_at is missing entirely on a source that requires review provenance")
    return issues


def warn_pit_invariant_violations(
    row: dict[str, Any],
    *,
    context: str,
    logger: logging.Logger | None = None,
    require_reviewed_at: bool = False,
) -> list[str]:
    issues = validate_pit_invariants(row, require_reviewed_at=require_reviewed_at)
    for issue in issues:
        (logger or LOGGER).warning("PIT invariant violation (%s): %s row=%s", context, issue, row)
    return issues


def row_is_effective_asof(row: dict[str, Any], asof: date | str | None, *, include_missing: bool = False) -> bool:
    target = parse_iso_date(asof) if not isinstance(asof, date) else asof
    if target is None:
        return include_missing

    if pit_date_parse_errors(row):
        return False

    valid_from = parse_iso_date(row_value(row, *START_DATE_COLUMNS))
    effective_date = parse_iso_date(row_value(row, *EFFECTIVE_DATE_COLUMNS))
    valid_to = parse_iso_date(row_value(row, *END_DATE_COLUMNS))
    reviewed_at = parse_iso_date(row_value(row, *REVIEW_DATE_COLUMNS))
    source_available = parse_iso_date(row_value(row, *SOURCE_AVAILABLE_DATE_COLUMNS))

    has_start_or_effective_date = valid_from is not None or effective_date is not None
    if valid_from is not None and valid_from > target:
        return False
    if effective_date is not None and effective_date > target:
        return False
    if source_available is not None and source_available > target:
        return False
    # Event-dated rows apply historically: reviewed_at is provenance-only when a
    # start/effective date is present and gates effectiveness only without one.
    if reviewed_at is not None and not has_start_or_effective_date and reviewed_at >= target:
        return False
    if valid_to is not None and valid_to < target:
        return False
    if valid_from is None and effective_date is None and valid_to is None and reviewed_at is None and source_available is None:
        return include_missing
    return True


def pit_metadata_status(row: dict[str, Any], asof: date | str | None) -> str:
    if not row_has_pit_metadata(row):
        return "missing_pit_metadata"
    return "effective" if row_is_effective_asof(row, asof, include_missing=False) else "not_effective_asof"
