from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True)
class CohortAssignment:
    cohort: str
    valid_from: date | None
    valid_to: date | None
    source: str
    reason: str

    def contains(self, asof: date) -> bool:
        return (self.valid_from is None or asof >= self.valid_from) and (
            self.valid_to is None or asof <= self.valid_to
        )


@dataclass(frozen=True)
class CohortHistory:
    assignments_by_ticker: Mapping[str, tuple[CohortAssignment, ...]]
    current_cohort_by_ticker: Mapping[str, str]

    def contains_ticker(self, ticker: str) -> bool:
        return str(ticker).strip().upper() in self.assignments_by_ticker

    def resolve(self, ticker: str, asof: date) -> CohortAssignment | None:
        clean = str(ticker).strip().upper()
        matches = [
            assignment
            for assignment in self.assignments_by_ticker.get(clean, ())
            if assignment.contains(asof)
        ]
        if len(matches) > 1:
            raise ValueError(f"Overlapping cohort assignments for {clean} on {asof.isoformat()}")
        return matches[0] if matches else None

    def current_cohort(self, ticker: str) -> str:
        return self.current_cohort_by_ticker.get(str(ticker).strip().upper(), "")


def _parse_date(raw: object, *, field: str, ticker: str) -> date:
    text = str(raw or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid {field}={text!r} for cohort ticker={ticker}") from exc


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Cohort CSV has no header: {path}")
        return [dict(row) for row in reader]


def load_cohort_history(
    current_path: Path,
    *,
    migration_path: Path | None = None,
) -> CohortHistory:
    current_rows = _read_rows(current_path)
    current: dict[str, tuple[str, str, str]] = {}
    for line_no, row in enumerate(current_rows, start=2):
        ticker = str(row.get("ticker") or "").strip().upper()
        cohort = str(
            row.get("biotech_calibration_cohort")
            or row.get("official_cohort")
            or row.get("biotech_primary_cohort")
            or ""
        ).strip()
        if not ticker or not cohort:
            raise ValueError(f"Invalid current cohort row {line_no}: {current_path}")
        if ticker in current:
            raise ValueError(f"Duplicate current cohort ticker={ticker}: {current_path}")
        current[ticker] = (
            cohort,
            str(row.get("source") or "manual_calibration_cohort_csv").strip()
            or "manual_calibration_cohort_csv",
            str(row.get("reason") or "").strip(),
        )

    transitions: dict[str, list[tuple[date, str, str, str]]] = {}
    if migration_path is not None:
        for line_no, row in enumerate(_read_rows(migration_path), start=2):
            ticker = str(row.get("ticker") or "").strip().upper()
            expected = str(row.get("expected_current_cohort") or "").strip()
            new = str(row.get("new_cohort") or "").strip()
            if not ticker or not expected or not new:
                raise ValueError(f"Invalid cohort migration row {line_no}: {migration_path}")
            effective = _parse_date(row.get("effective_date"), field="effective_date", ticker=ticker)
            transitions.setdefault(ticker, []).append(
                (effective, expected, new, str(row.get("reason") or "").strip())
            )

    assignments: dict[str, tuple[CohortAssignment, ...]] = {}
    for ticker, (current_cohort, current_source, current_reason) in current.items():
        ticker_transitions = sorted(transitions.pop(ticker, []), key=lambda item: item[0])
        if not ticker_transitions:
            assignments[ticker] = (
                CohortAssignment(
                    cohort=current_cohort,
                    valid_from=None,
                    valid_to=None,
                    source=current_source,
                    reason=current_reason,
                ),
            )
            continue
        seen_dates: set[date] = set()
        prior_cohort = ticker_transitions[0][1]
        ticker_assignments: list[CohortAssignment] = []
        prior_start: date | None = None
        for effective, expected, new, reason in ticker_transitions:
            if effective in seen_dates:
                raise ValueError(f"Duplicate cohort effective date for {ticker}: {effective}")
            seen_dates.add(effective)
            if expected != prior_cohort:
                raise ValueError(
                    f"Broken cohort transition chain for {ticker}: expected={expected!r} "
                    f"prior={prior_cohort!r}"
                )
            ticker_assignments.append(
                CohortAssignment(
                    cohort=expected,
                    valid_from=prior_start,
                    valid_to=effective - timedelta(days=1),
                    source="effective_dated_cohort_migration_prior",
                    reason=reason,
                )
            )
            prior_start = effective
            prior_cohort = new
        if prior_cohort != current_cohort:
            raise ValueError(
                f"Current cohort does not match final migration for {ticker}: "
                f"current={current_cohort!r} migration={prior_cohort!r}"
            )
        ticker_assignments.append(
            CohortAssignment(
                cohort=current_cohort,
                valid_from=prior_start,
                valid_to=None,
                source="effective_dated_cohort_migration_current",
                reason=current_reason or ticker_transitions[-1][3],
            )
        )
        assignments[ticker] = tuple(ticker_assignments)
    if transitions:
        raise ValueError(
            "Cohort migrations reference tickers absent from the current map: "
            + ",".join(sorted(transitions))
        )
    return CohortHistory(
        assignments_by_ticker=assignments,
        current_cohort_by_ticker={ticker: values[0] for ticker, values in current.items()},
    )


def extend_cohort_history(
    history: CohortHistory,
    additions: Iterable[tuple[str, str, str, str]],
) -> CohortHistory:
    assignments = dict(history.assignments_by_ticker)
    current = dict(history.current_cohort_by_ticker)
    for raw_ticker, cohort, source, reason in additions:
        ticker = str(raw_ticker).strip().upper()
        if not ticker or not cohort:
            continue
        assignment = CohortAssignment(
            cohort=str(cohort).strip(),
            valid_from=None,
            valid_to=None,
            source=str(source).strip() or "cohort_history_addition",
            reason=str(reason).strip(),
        )
        assignments[ticker] = (assignment,)
        current[ticker] = assignment.cohort
    return CohortHistory(assignments_by_ticker=assignments, current_cohort_by_ticker=current)
