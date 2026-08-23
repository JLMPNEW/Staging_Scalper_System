from __future__ import annotations

"""Outcome-blind freeze and input-delta checks for accepted specialized metrics."""

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Sequence

from industrials.transportation.contemporaneous_metric_coverage import (
    availability_date,
    comparison_key,
)


OBSERVED_STATUSES = frozenset({"REPORTED", "DERIVED", "PROXY"})


@dataclass(frozen=True)
class AcceptedDomain:
    cohort: str
    metric_id: str
    domain_id: str
    tickers: tuple[str, ...]
    minimum_breadth: int
    max_staleness_days: int


def finite(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def accepted_summary_rows(
    rows: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    accepted = [
        dict(row)
        for row in rows
        if str(row.get("calibration_gate") or "").upper() == "PASS"
    ]
    if not accepted:
        raise ValueError("no specialized metric-domain passed the PIT coverage gate")
    keys = [
        (
            str(row.get("cohort") or ""),
            str(row.get("metric_id") or ""),
            str(row.get("comparison_domain_id") or ""),
        )
        for row in accepted
    ]
    if any(not all(key) for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("accepted specialized metric-domain identities are invalid")
    return sorted(
        accepted,
        key=lambda row: (
            str(row["cohort"]),
            str(row["metric_id"]),
            str(row["comparison_domain_id"]),
        ),
    )


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _latest_compatible_values(
    rows: Sequence[Mapping[str, object]],
    *,
    domain: AcceptedDomain,
    score_date: date,
) -> dict[str, float]:
    eligible: defaultdict[
        str, list[tuple[date, date, Mapping[str, object]]]
    ] = defaultdict(list)
    ticker_scope = set(domain.tickers)
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if ticker not in ticker_scope:
            continue
        if str(row.get("metric_id") or "") != domain.metric_id:
            continue
        if str(row.get("replay_status") or "ACCEPTED").upper() != "ACCEPTED":
            continue
        available = availability_date(row)
        period_end = _date(row.get("period_end"))
        value = finite(row.get("value"))
        if available is None or period_end is None or value is None:
            continue
        if available > score_date or period_end > score_date:
            continue
        if (score_date - period_end).days > domain.max_staleness_days:
            continue
        eligible[ticker].append((available, period_end, row))
    latest = {
        ticker: max(candidates, key=lambda item: (item[0], item[1]))[2]
        for ticker, candidates in eligible.items()
    }
    by_definition: defaultdict[tuple[str, ...], set[str]] = defaultdict(set)
    for ticker, row in latest.items():
        by_definition[comparison_key(row)].add(ticker)
    if not by_definition:
        return {}
    _, selected_tickers = max(
        by_definition.items(), key=lambda item: (len(item[1]), item[0])
    )
    return {
        ticker: float(latest[ticker]["value"])
        for ticker in selected_tickers
        if finite(latest[ticker].get("value")) is not None
    }


def _panel_metric(row: Mapping[str, object], metric_id: str) -> float | None:
    try:
        values = json.loads(str(row.get("metric_values_json") or "{}"))
        statuses = json.loads(str(row.get("metric_status_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid metric JSON for {row.get('asof_date')}/{row.get('ticker')}"
        ) from exc
    if str(statuses.get(metric_id) or "") not in OBSERVED_STATUSES:
        return None
    return finite(values.get(metric_id))


def compare_replay_with_panel(
    *,
    panel_rows: Sequence[Mapping[str, object]],
    replay_rows: Sequence[Mapping[str, object]],
    domains: Sequence[AcceptedDomain],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    panel: dict[tuple[str, str], Mapping[str, object]] = {}
    for row in panel_rows:
        if str(row.get("horizon_sessions") or "63") != "63":
            continue
        key = (
            str(row.get("asof_date") or "")[:10],
            str(row.get("ticker") or "").upper(),
        )
        if key in panel:
            raise ValueError(f"duplicate frozen PIT panel row={key}")
        panel[key] = row
    score_dates = sorted({key[0] for key in panel})
    if not score_dates:
        raise ValueError("frozen PIT panel has no score dates")

    detail: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    for domain in domains:
        counts: defaultdict[str, int] = defaultdict(int)
        old_passing_dates = 0
        new_passing_dates = 0
        for asof in score_dates:
            new_values = _latest_compatible_values(
                replay_rows,
                domain=domain,
                score_date=date.fromisoformat(asof),
            )
            old_values: dict[str, float] = {}
            for ticker in domain.tickers:
                row = panel.get((asof, ticker))
                if row is None:
                    continue
                value = _panel_metric(row, domain.metric_id)
                if value is not None:
                    old_values[ticker] = value
            old_passing_dates += len(old_values) >= domain.minimum_breadth
            new_passing_dates += len(new_values) >= domain.minimum_breadth
            for ticker in domain.tickers:
                old = old_values.get(ticker)
                new = new_values.get(ticker)
                if old is None and new is None:
                    disposition = "BOTH_MISSING"
                elif old is None:
                    disposition = "NEW_FILL"
                elif new is None:
                    disposition = "NOT_IN_NEW_COMPATIBLE_SET"
                elif math.isclose(old, new, rel_tol=1e-9, abs_tol=1e-12):
                    disposition = "UNCHANGED"
                else:
                    disposition = "CHANGED_VALUE"
                counts[disposition] += 1
                detail.append(
                    {
                        "cohort": domain.cohort,
                        "metric_id": domain.metric_id,
                        "comparison_domain_id": domain.domain_id,
                        "asof_date": asof,
                        "ticker": ticker,
                        "prior_panel_value": old,
                        "new_replay_value": new,
                        "value_disposition": disposition,
                        "absolute_delta": (
                            abs(new - old)
                            if old is not None and new is not None
                            else None
                        ),
                    }
                )
        new_information = counts["NEW_FILL"] + counts["CHANGED_VALUE"]
        summaries.append(
            {
                "cohort": domain.cohort,
                "metric_id": domain.metric_id,
                "comparison_domain_id": domain.domain_id,
                "ticker_count": len(domain.tickers),
                "score_date_count": len(score_dates),
                "minimum_breadth": domain.minimum_breadth,
                "prior_panel_passing_date_count": old_passing_dates,
                "new_replay_passing_date_count": new_passing_dates,
                "prior_panel_passing_date_fraction": old_passing_dates / len(score_dates),
                "new_replay_passing_date_fraction": new_passing_dates / len(score_dates),
                "unchanged_cell_count": counts["UNCHANGED"],
                "new_fill_cell_count": counts["NEW_FILL"],
                "changed_value_cell_count": counts["CHANGED_VALUE"],
                "new_information_cell_count": new_information,
                "not_in_new_compatible_set_count": counts[
                    "NOT_IN_NEW_COMPATIBLE_SET"
                ],
                "input_delta_gate": "PASS" if new_information else "FAIL",
            }
        )
    return detail, summaries
