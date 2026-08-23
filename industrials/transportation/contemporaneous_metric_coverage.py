from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping


POLICY_VERSION = "transportation_contemporaneous_specialized_coverage_v2"


@dataclass(frozen=True)
class DomainRule:
    cohort: str
    metric_id: str
    domain_id: str
    tickers: tuple[str, ...]
    minimum_breadth: int
    calibration_eligibility: str = "CANDIDATE"


def _date(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def availability_date(row: Mapping[str, object]) -> date | None:
    accepted = _date(row.get("accepted_at"))
    filing = _date(row.get("filing_date"))
    if accepted and filing:
        return max(accepted, filing)
    return accepted or filing


def comparison_key(row: Mapping[str, object]) -> tuple[str, ...]:
    definition_basis = str(row.get("definition_basis") or "").strip().casefold()
    comparability = str(row.get("comparability_class") or "").strip().casefold()
    fallback = "|".join(
        str(row.get(field) or "").strip().casefold()
        for field in ("concept_name", "formula", "numerator_concept", "denominator_concept")
    )
    return (
        comparability or "unspecified",
        definition_basis or fallback,
        str(row.get("unit") or "").strip().casefold(),
        str(row.get("denominator_basis") or "").strip().casefold(),
        str(row.get("weighting_basis") or "").strip().casefold(),
        str(row.get("capacity_basis") or "").strip().casefold(),
    )


def _latest_by_ticker(
    rows: Iterable[Mapping[str, object]],
    *,
    metric_id: str,
    tickers: set[str],
    score_date: date,
    max_staleness_days: int,
) -> tuple[dict[str, Mapping[str, object]], set[str], set[str]]:
    eligible: defaultdict[str, list[tuple[date, date, Mapping[str, object]]]] = defaultdict(list)
    stale: set[str] = set()
    future: set[str] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if ticker not in tickers or str(row.get("metric_id") or "") != metric_id:
            continue
        if str(row.get("replay_status") or "ACCEPTED") != "ACCEPTED":
            continue
        available = availability_date(row)
        period_end = _date(row.get("period_end"))
        if available is None or period_end is None:
            continue
        if available > score_date or period_end > score_date:
            future.add(ticker)
            continue
        if (score_date - period_end).days > max_staleness_days:
            stale.add(ticker)
            continue
        eligible[ticker].append((available, period_end, row))
    latest = {
        ticker: max(values, key=lambda item: (item[0], item[1]))[2]
        for ticker, values in eligible.items()
    }
    return latest, stale, future


def audit_contemporaneous_coverage(
    *,
    score_dates: Iterable[str],
    rules: Iterable[DomainRule],
    accepted_rows: Iterable[Mapping[str, object]],
    max_staleness_days: Mapping[str, int],
    minimum_date_pass_fraction: float = 0.75,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    if not 0 < minimum_date_pass_fraction <= 1:
        raise ValueError("minimum_date_pass_fraction must be in (0, 1]")
    parsed_dates = {_date(value) for value in score_dates}
    if None in parsed_dates:
        raise ValueError("score dates must be ISO dates")
    clean_dates = sorted(value for value in parsed_dates if value is not None)
    if not clean_dates:
        raise ValueError("at least one score date is required")
    evidence = list(accepted_rows)
    detail: list[dict[str, object]] = []
    rules_list = list(rules)
    for rule in rules_list:
        if rule.minimum_breadth < 1:
            raise ValueError(
                f"{rule.metric_id}/{rule.domain_id}: invalid minimum breadth"
            )
        if (
            rule.minimum_breadth > len(rule.tickers)
            and rule.calibration_eligibility.upper() != "DIAGNOSTIC_ONLY"
        ):
            raise ValueError(
                f"{rule.metric_id}/{rule.domain_id}: invalid minimum breadth"
            )
        freshness = int(max_staleness_days.get(rule.metric_id, 0))
        if freshness <= 0:
            raise ValueError(f"{rule.metric_id}: max staleness must be positive")
        tickers = {ticker.upper() for ticker in rule.tickers}
        for score_date in clean_dates:
            latest, stale, future = _latest_by_ticker(
                evidence,
                metric_id=rule.metric_id,
                tickers=tickers,
                score_date=score_date,
                max_staleness_days=freshness,
            )
            by_definition: defaultdict[tuple[str, ...], set[str]] = defaultdict(set)
            for ticker, row in latest.items():
                by_definition[comparison_key(row)].add(ticker)
            if by_definition:
                selected_key, selected_tickers = max(
                    by_definition.items(),
                    key=lambda item: (len(item[1]), item[0]),
                )
            else:
                selected_key, selected_tickers = (), set()
            incompatible = set(latest) - selected_tickers
            missing = tickers - set(latest)
            passes = len(selected_tickers) >= rule.minimum_breadth
            detail.append(
                {
                    "policy_version": POLICY_VERSION,
                    "cohort": rule.cohort,
                    "metric_id": rule.metric_id,
                    "comparison_domain_id": rule.domain_id,
                    "score_date": score_date.isoformat(),
                    "applicable_ticker_count": len(tickers),
                    "minimum_breadth": rule.minimum_breadth,
                    "accepted_compatible_breadth": len(selected_tickers),
                    "accepted_tickers": "|".join(sorted(selected_tickers)),
                    "missing_or_unusable_tickers": "|".join(sorted(missing)),
                    "stale_tickers": "|".join(sorted(stale - set(latest))),
                    "future_only_tickers": "|".join(sorted(future - set(latest))),
                    "incompatible_definition_tickers": "|".join(sorted(incompatible)),
                    "selected_comparison_key": "|".join(selected_key),
                    "date_gate": "PASS" if passes else "FAIL",
                    "calibration_eligibility": rule.calibration_eligibility,
                }
            )
    grouped: defaultdict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in detail:
        grouped[
            (
                str(row["cohort"]),
                str(row["metric_id"]),
                str(row["comparison_domain_id"]),
            )
        ].append(row)
    summary_rows: list[dict[str, object]] = []
    for (cohort, metric, domain), rows in sorted(grouped.items()):
        passing = sum(row["date_gate"] == "PASS" for row in rows)
        pass_fraction = passing / len(rows)
        latest_pass = rows[-1]["date_gate"] == "PASS"
        eligibility = str(rows[0]["calibration_eligibility"])
        accepted = (
            eligibility == "CANDIDATE"
            and latest_pass
            and pass_fraction >= minimum_date_pass_fraction
        )
        summary_rows.append(
            {
                "policy_version": POLICY_VERSION,
                "cohort": cohort,
                "metric_id": metric,
                "comparison_domain_id": domain,
                "score_date_count": len(rows),
                "passing_score_date_count": passing,
                "passing_score_date_fraction": pass_fraction,
                "minimum_date_pass_fraction": minimum_date_pass_fraction,
                "latest_score_date": rows[-1]["score_date"],
                "latest_date_gate": rows[-1]["date_gate"],
                "calibration_eligibility": eligibility,
                "calibration_gate": "PASS" if accepted else "FAIL",
            }
        )
    accepted_domains = [
        row for row in summary_rows if row["calibration_gate"] == "PASS"
    ]
    manifest = {
        "acceptance": "PASS",
        "policy_version": POLICY_VERSION,
        "score_date_count": len(clean_dates),
        "score_date_min": clean_dates[0].isoformat(),
        "score_date_max": clean_dates[-1].isoformat(),
        "domain_rule_count": len(rules_list),
        "calibration_accepted_domain_count": len(accepted_domains),
        "calibration_accepted_metric_count": len(
            {str(row["metric_id"]) for row in accepted_domains}
        ),
        "date_gate_counts": dict(
            sorted(Counter(str(row["date_gate"]) for row in detail).items())
        ),
        "minimum_date_pass_fraction": minimum_date_pass_fraction,
        "point_in_time_availability_enforced": True,
        "period_end_lookahead_prohibited": True,
        "staleness_enforced": True,
        "definition_compatibility_enforced": True,
    }
    return detail, summary_rows, manifest

