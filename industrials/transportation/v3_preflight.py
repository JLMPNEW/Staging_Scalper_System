"""Transportation v3 research preflight.

Read-only diagnostics over the hash-frozen v3 complete panel and the loaded
price store, executed under the pre-registered contract in
``data/transportation_v3_preflight_policy.yaml``. The preflight measures
peer-group breadth, metric coverage, and candidate-signal IC stability on the
FULL point-in-time surface-freight membership (delisted included), then
evaluates the pre-declared architecture gates.

Everything computed here is design evidence only. The historical windows are
research-revealed; nothing in this module produces promotion-eligible
results.
"""
from __future__ import annotations

import gzip
import csv
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from industrials.transportation.oos_outcomes import (
    MembershipEvent,
    PricePoint,
    finite_float,
    outcome_window,
    parse_date,
    optional_date,
    price_source_order,
)
from industrials.transportation.walk_forward_calibration import spearman


MODEL_FAMILY = "transportation"
PREFLIGHT_VERSION = "transportation_v3_preflight_v1"
SURFACE_COHORT = "surface_freight_and_logistics"

BREADTH_FIELDS = (
    "peer_group",
    "post_merge_group",
    "member_count_total",
    "active_member_count",
    "delisted_member_count",
    "mean_pit_members",
    "min_pit_members",
    "max_pit_members",
    "breadth_gate_pass",
)
COVERAGE_FIELDS = (
    "post_merge_group",
    "metric_id",
    "member_date_rows",
    "observed_rows",
    "coverage_rate",
)
STABILITY_FIELDS = (
    "post_merge_group",
    "signal_id",
    "horizon_sessions",
    "expected_sign",
    "period_count",
    "regime_a_periods",
    "regime_b_periods",
    "mean_ic",
    "regime_a_mean_ic",
    "regime_b_mean_ic",
    "sign_consistent_with_expectation",
    "qualifies",
)


@dataclass(frozen=True)
class PeerGroupRow:
    ticker: str
    membership_status: str
    peer_group: str
    merge_target: str


def read_peer_groups(path: Path) -> dict[str, PeerGroupRow]:
    output: dict[str, PeerGroupRow] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = str(row["ticker"]).strip().upper()
            if not ticker or ticker in output:
                raise ValueError(f"blank or duplicate peer-group ticker={ticker!r}")
            output[ticker] = PeerGroupRow(
                ticker=ticker,
                membership_status=str(row["membership_status"]).strip(),
                peer_group=str(row["peer_group"]).strip(),
                merge_target=str(row["merge_target"]).strip(),
            )
    if not output:
        raise ValueError(f"peer-group map is empty: {path}")
    return output


def iter_surface_generic_rows(
    panel_path: Path,
    *,
    excluded_metrics: frozenset[str],
) -> Iterator[dict[str, str]]:
    with gzip.open(panel_path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if (
                row.get("calibration_cohort") == SURFACE_COHORT
                and row.get("metric_family") == "generic"
                and row.get("metric_id") not in excluded_metrics
            ):
                yield {
                    "asof_date": str(row.get("asof_date") or ""),
                    "ticker": str(row.get("ticker") or "").upper(),
                    "metric_id": str(row.get("metric_id") or ""),
                    "metric_value": str(row.get("metric_value") or ""),
                }


def load_memberships(
    connection: sqlite3.Connection,
    *,
    source_id: str,
) -> dict[str, MembershipEvent]:
    rows = connection.execute(
        """
        SELECT m.ticker, m.start_date, m.end_date, m.membership_status,
               COALESCE(s.terminal_type, '') AS terminal_type
        FROM dim_universe_membership AS m
        LEFT JOIN dim_delisted_calibration_seed AS s
          ON s.model_family=m.model_family AND s.internal_ticker=m.ticker
        WHERE m.model_family=? AND m.membership_source_id=?
          AND m.point_in_time_flag=1
        ORDER BY m.ticker
        """,
        (MODEL_FAMILY, source_id),
    )
    return {
        str(row["ticker"]).upper(): MembershipEvent(
            ticker=str(row["ticker"]).upper(),
            start_date=parse_date(row["start_date"], field="membership.start"),
            end_date=optional_date(row["end_date"], field="membership.end"),
            membership_status=str(row["membership_status"] or ""),
            terminal_type=str(row["terminal_type"] or "").lower(),
        )
        for row in rows
    }


def load_prices(
    connection: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    sources: Sequence[str],
) -> dict[str, dict[str, list[PricePoint]]]:
    ticker_slots = ",".join("?" for _ in tickers)
    source_slots = ",".join("?" for _ in sources)
    output: dict[str, dict[str, list[PricePoint]]] = {}
    for row in connection.execute(
        f"""
        SELECT ticker, source_id, bar_date, adj_close
        FROM fact_price_ohlcv
        WHERE UPPER(ticker) IN ({ticker_slots})
          AND source_id IN ({source_slots})
          AND is_adjusted=1 AND adj_close IS NOT NULL AND adj_close>=0
        ORDER BY ticker, source_id, bar_date
        """,
        (*[t.upper() for t in tickers], *sources),
    ):
        value = finite_float(row["adj_close"])
        if value is None or value < 0:
            continue
        output.setdefault(str(row["ticker"]).upper(), {}).setdefault(
            str(row["source_id"]), []
        ).append(
            PricePoint(
                bar_date=parse_date(row["bar_date"], field="bar_date"),
                value=value,
                source_id=str(row["source_id"]),
                price_basis="adj_close",
            )
        )
    return output


def alive_members(
    memberships: Mapping[str, MembershipEvent],
    tickers: Sequence[str],
    asof: str,
) -> list[str]:
    asof_date = parse_date(asof, field="asof")
    output = []
    for ticker in tickers:
        event = memberships.get(ticker)
        if event is None:
            continue
        if event.start_date <= asof_date and (
            event.end_date is None or event.end_date >= asof_date
        ):
            output.append(ticker)
    return output


def resolve_post_merge(
    peer_groups: Mapping[str, PeerGroupRow],
    *,
    memberships: Mapping[str, MembershipEvent],
    dates: Sequence[str],
    minimum_mean: float,
    minimum_floor: float,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Apply the pre-declared breadth gate and merge map.

    Returns (group_id -> post_merge_group_id, breadth report rows).
    """
    tickers_by_group: dict[str, list[str]] = defaultdict(list)
    for row in peer_groups.values():
        tickers_by_group[row.peer_group].append(row.ticker)
    merge_of = {
        row.peer_group: row.merge_target for row in peer_groups.values()
    }
    breadth_stats: dict[str, tuple[float, int, int]] = {}
    for group, tickers in tickers_by_group.items():
        counts = [
            len(alive_members(memberships, tickers, asof)) for asof in dates
        ]
        breadth_stats[group] = (
            sum(counts) / len(counts) if counts else 0.0,
            min(counts) if counts else 0,
            max(counts) if counts else 0,
        )
    post_merge: dict[str, str] = {}
    for group in tickers_by_group:
        mean_members, floor_members, _ = breadth_stats[group]
        if mean_members >= minimum_mean and floor_members >= minimum_floor:
            post_merge[group] = group
        else:
            target = merge_of.get(group, group)
            post_merge[group] = target if target != group else group
    # One merge round is contract-defined; a target that itself failed breadth
    # still absorbs its sources (their union is re-checked by the caller).
    report: list[dict[str, Any]] = []
    for group, tickers in sorted(tickers_by_group.items()):
        mean_members, floor_members, max_members = breadth_stats[group]
        report.append(
            {
                "peer_group": group,
                "post_merge_group": post_merge[group],
                "member_count_total": len(tickers),
                "active_member_count": sum(
                    peer_groups[t].membership_status == "active"
                    for t in tickers
                ),
                "delisted_member_count": sum(
                    peer_groups[t].membership_status != "active"
                    for t in tickers
                ),
                "mean_pit_members": round(mean_members, 4),
                "min_pit_members": floor_members,
                "max_pit_members": max_members,
                "breadth_gate_pass": int(post_merge[group] == group),
            }
        )
    return post_merge, report


def build_signal_values(
    panel_rows: Sequence[Mapping[str, str]],
    *,
    signals: Mapping[str, tuple[str, str, int]],
    dates: Sequence[str],
) -> dict[tuple[str, str, str], float]:
    """(signal_id, asof_date, ticker) -> signed-eligible raw value."""
    by_metric: dict[tuple[str, str, str], float] = {}
    for row in panel_rows:
        value = finite_float(row.get("metric_value"))
        if value is None:
            continue
        by_metric[(row["metric_id"], row["asof_date"], row["ticker"])] = value
    date_index = {asof: index for index, asof in enumerate(dates)}
    output: dict[tuple[str, str, str], float] = {}
    for signal_id, (metric_id, transform, _sign) in signals.items():
        if transform == "level":
            for (metric, asof, ticker), value in by_metric.items():
                if metric == metric_id:
                    output[(signal_id, asof, ticker)] = value
        elif transform == "yoy_change":
            for (metric, asof, ticker), value in by_metric.items():
                if metric != metric_id:
                    continue
                index = date_index.get(asof)
                if index is None or index < 12:
                    continue
                prior = by_metric.get(
                    (metric_id, dates[index - 12], ticker)
                )
                if prior is not None:
                    output[(signal_id, asof, ticker)] = value - prior
        else:
            raise ValueError(f"unsupported transform={transform!r}")
    return output


def forward_excess_returns(
    *,
    prices: Mapping[str, Mapping[str, Sequence[PricePoint]]],
    memberships: Mapping[str, MembershipEvent],
    peer_groups: Mapping[str, PeerGroupRow],
    benchmark: str,
    dates: Sequence[str],
    horizon: int,
    active_source: str,
    delisted_source: str,
) -> dict[tuple[str, str], float]:
    """(asof_date, ticker) -> forward excess return vs benchmark."""
    output: dict[tuple[str, str], float] = {}
    benchmark_windows: dict[str, Any] = {}
    for asof in dates:
        benchmark_windows[asof] = outcome_window(
            prices.get(benchmark, {}),
            asof=asof,
            forward_trading_days=horizon,
            source_order=(active_source, delisted_source),
        )
    for asof in dates:
        bench = benchmark_windows[asof]
        bench_return = bench.forward_return
        if bench_return is None:
            continue
        horizon_end = bench.forward.bar_date if bench.forward else None
        for ticker, row in peer_groups.items():
            membership = memberships.get(ticker)
            if membership is None:
                continue
            role = (
                "active"
                if row.membership_status == "active"
                else "delisted_usable"
            )
            window = outcome_window(
                prices.get(ticker, {}),
                asof=asof,
                forward_trading_days=horizon,
                source_order=price_source_order(role),
                membership=membership,
                horizon_end=horizon_end,
            )
            security_return = window.forward_return
            if security_return is not None:
                output[(asof, ticker)] = security_return - bench_return
    return output


def stability_rows(
    *,
    signals: Mapping[str, tuple[str, str, int]],
    signal_values: Mapping[tuple[str, str, str], float],
    excess: Mapping[tuple[str, str], float],
    memberships: Mapping[str, MembershipEvent],
    peer_groups: Mapping[str, PeerGroupRow],
    post_merge: Mapping[str, str],
    dates: Sequence[str],
    horizon: int,
    stride: int,
    regime_split: str,
    minimum_total_periods: int,
    minimum_regime_periods: int,
    minimum_abs_ic: float,
    minimum_cross_section: int = 4,
) -> list[dict[str, Any]]:
    groups = sorted(set(post_merge.values()))
    tickers_by_post_merge: dict[str, list[str]] = defaultdict(list)
    for row in peer_groups.values():
        tickers_by_post_merge[post_merge[row.peer_group]].append(row.ticker)
    sampled_dates = list(dates)[::stride]
    output: list[dict[str, Any]] = []
    for group in groups:
        tickers = tickers_by_post_merge[group]
        for signal_id, (_metric, _transform, sign) in sorted(signals.items()):
            period_ics: list[tuple[str, float]] = []
            for asof in sampled_dates:
                scores: list[float] = []
                outcomes: list[float] = []
                for ticker in alive_members(memberships, tickers, asof):
                    value = signal_values.get((signal_id, asof, ticker))
                    outcome = excess.get((asof, ticker))
                    if value is not None and outcome is not None:
                        scores.append(value)
                        outcomes.append(outcome)
                if len(scores) < minimum_cross_section:
                    continue
                ic = spearman(scores, outcomes)
                if ic is not None:
                    period_ics.append((asof, ic))
            regime_a = [ic for asof, ic in period_ics if asof <= regime_split]
            regime_b = [ic for asof, ic in period_ics if asof > regime_split]
            mean_ic = (
                sum(ic for _, ic in period_ics) / len(period_ics)
                if period_ics
                else 0.0
            )
            mean_a = sum(regime_a) / len(regime_a) if regime_a else 0.0
            mean_b = sum(regime_b) / len(regime_b) if regime_b else 0.0
            sign_ok = bool(
                regime_a
                and regime_b
                and mean_a * sign > 0
                and mean_b * sign > 0
            )
            qualifies = bool(
                len(period_ics) >= minimum_total_periods
                and len(regime_a) >= minimum_regime_periods
                and len(regime_b) >= minimum_regime_periods
                and abs(mean_ic) >= minimum_abs_ic
                and mean_ic * sign > 0
                and sign_ok
            )
            output.append(
                {
                    "post_merge_group": group,
                    "signal_id": signal_id,
                    "horizon_sessions": horizon,
                    "expected_sign": sign,
                    "period_count": len(period_ics),
                    "regime_a_periods": len(regime_a),
                    "regime_b_periods": len(regime_b),
                    "mean_ic": round(mean_ic, 6),
                    "regime_a_mean_ic": round(mean_a, 6),
                    "regime_b_mean_ic": round(mean_b, 6),
                    "sign_consistent_with_expectation": int(sign_ok),
                    "qualifies": int(qualifies),
                }
            )
    return output
