"""Execute a preregistered Consumer Defensive v2 calibration from real PIT data."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from consumer_defensive.core.calibration_preregistration_v2 import (
    COST_POLICY,
    LIQUIDITY_POLICY,
    PORTFOLIO_POLICY,
    SPLIT_POLICY,
    load_json,
    methodology_hashes,
    publish_immutable_json,
    validate_candidate_registry,
    validate_preregistration,
    verify_factor_campaign,
)
from consumer_defensive.core.calibration_v2 import (
    OUTER_TEST_ROLE,
    RealizedReturnObservation,
    ReturnObservation,
    SelectedPortfolioObservation,
    WalkForwardFold,
    build_calibration_decision,
    build_nested_purged_walk_forward,
    evaluate_cohort,
)
from consumer_defensive.core.calibration_scope import (
    apply_calibration_scope,
    filter_label_mapping,
)
from consumer_defensive.core.config import ConfigBundle, cfg_get, resolve_path
from consumer_defensive.core.market_data import load_market_policy
from consumer_defensive.core.promotion_framework_v2 import (
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    canonical_sha256,
    validate_calibration_decision,
    validate_framework,
)
from consumer_defensive.core.promotion_input_v3 import (
    build_matched_benchmark_paths,
)
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.stage3_runtime import DEFAULT_TERMINAL_POLICY
from consumer_defensive.core.stage6c_panel import validate_stage6c_panel
from consumer_defensive.core.terminal_events import (
    load_terminal_event_policy,
    terminal_horizon_value,
    validate_terminal_events,
)
from consumer_defensive.core.historical_features_v2 import build_historical_core_panel_v2
from consumer_defensive.core.stage8_calibration import _membership_rows


INPUT_MANIFEST_SCHEMA = "consumer_defensive_calibration_input_manifest_v2"
FOLD_REGISTRY_SCHEMA = "consumer_defensive_calibration_fold_registry_v2"
RESULTS_SCHEMA = "consumer_defensive_calibration_results_v2"
VALIDATION_SCHEMA = "consumer_defensive_calibration_independent_validation_v2"
PATH_ATTESTATION_SCHEMA = "consumer_defensive_calibration_realized_path_attestation_v2"
_CORE_SPEC_BY_NAME = {spec.name: spec for spec in CORE_COMPONENT_SPECS}

PRICE_MARK_POLICY = {
    "internal_missing_session": "carry_last_observable_mark_until_next_observed_bar",
    "verified_terminal_event": (
        "replace_original_security_with_reviewed_terminal_value_on_economic_event"
    ),
    "verified_successor_transition": (
        "carry_last_observable_mark_only_until_successor_reference_is_tradable"
    ),
    "post_event_provider_quote": "ignore_when_economic_terminal_event_has_occurred",
    "entry_requires_observed_original_bar": True,
    "pre_listing_carry": False,
    "unverified_or_ineligible_terminal_event": "fail_closed_if_path_crosses_event",
    "unclassified_terminal_carry": False,
}
REALIZED_PATH_POLICY = {
    "position_accounting": PORTFOLIO_POLICY["live_path_rebalance_policy"],
    "rebalance_schedule": "next_selected_month_end_signal_plus_entry_lag",
    "final_block_sessions": PORTFOLIO_POLICY["final_path_sessions_after_last_signal"],
    "absolute_metric_role": PORTFOLIO_POLICY["realized_path_metric_policy"],
    "forward_label_role": PORTFOLIO_POLICY["candidate_selection_metric_policy"],
    "overlapping_forward_label_reconciliation": "not_a_trade_pnl_identity",
    "terminal_accounting": "reviewed_economic_value_per_original_share",
}

def _sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_json_mapping(value: Any, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must decode to an object")
    return parsed


def _turnover(previous: Mapping[str, float], current: Mapping[str, float]) -> float:
    names = set(previous) | set(current)
    previous_cash = 1.0 - sum(previous.values())
    current_cash = 1.0 - sum(current.values())
    return 0.5 * (
        sum(abs(float(current.get(name, 0.0)) - float(previous.get(name, 0.0))) for name in names)
        + abs(current_cash - previous_cash)
    )


def _exact_stage6c_labels(
    conn: sqlite3.Connection, *, stage6c_run_id: int
) -> dict[tuple[str, str], dict[str, Any]]:
    columns = [
        "asof_date",
        "ticker",
        "cohort_id",
        "membership_eligible_flag",
        "investable_flag",
        "terminal_event_status",
        *[
            f"forward_total_return_{horizon}d"
            for horizon in REQUIRED_HORIZONS
        ],
        *[
            f"forward_xlp_residual_return_{horizon}d"
            for horizon in REQUIRED_HORIZONS
        ],
    ]
    sql = (
        "SELECT " + ",".join(columns) + " FROM stage6c_specialized_factor_panel "
        "WHERE stage6c_run_id=? ORDER BY asof_date,ticker,factor_id"
    )
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in conn.execute(sql, (stage6c_run_id,)):
        row = dict(raw)
        key = (str(row["asof_date"]), str(row["ticker"]))
        prior = grouped.get(key)
        if prior is None:
            grouped[key] = row
            continue
        if any(prior[column] != row[column] for column in columns):
            raise ValueError(f"Stage 6C duplicated factor rows disagree on label lineage: {key}")
    if not grouped:
        raise ValueError("Stage 6C label panel is empty")
    return grouped


def _load_price_history(
    conn: sqlite3.Connection,
    *,
    tickers: Iterable[str],
    maximum_date: str,
) -> tuple[
    dict[str, dict[str, float]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, dict[str, Any]],
    str,
    int,
    dict[str, Any],
]:
    required = set(str(value) for value in tickers) | {"XLP", "SPY"}
    selections: dict[str, dict[str, Any]] = {}
    selection_payload: list[dict[str, Any]] = []
    for raw in conn.execute(
        """SELECT ticker,selected_source_id,selection_asof_date,adjustment_basis,
                  selection_reason,coverage_status,first_bar_date,last_bar_date,
                  expected_start_date,expected_end_date,bar_count
           FROM dim_price_series_selection
           WHERE purpose='scoring_return_series' ORDER BY ticker"""
    ):
        row = dict(raw)
        ticker = str(row["ticker"])
        if ticker in selections:
            raise ValueError(f"duplicate frozen price selection: {ticker}")
        selections[ticker] = row
        if ticker in required:
            selection_payload.append(row)
    missing = sorted(required - set(selections))
    if missing:
        raise ValueError(f"frozen price selections are missing: {missing}")

    terminal_policy = load_terminal_event_policy(DEFAULT_TERMINAL_POLICY)
    terminal_validation = validate_terminal_events(
        conn,
        terminal_policy,
        as_of=maximum_date,
    )
    if terminal_validation["status"] != "PASS":
        raise ValueError("reviewed terminal-event evidence failed validation")
    terminal_rows = {
        str(row["ticker"]): dict(row)
        for row in conn.execute(
            """SELECT ticker,security_id,event_type,economic_event_date,
                      last_trade_date,provider_last_quoted_date,terminal_type,
                      cash_consideration,cash_currency,successor_ticker,
                      successor_share_ratio,successor_security_type,
                      successor_reference_date,successor_price_source_id,
                      successor_provider_symbol,contingent_right_id,
                      contingent_right_units,contingent_max_cash,
                      contingent_status,fixed_terminal_value,
                      terminal_value_method,survivorship_complete,
                      calibration_eligible,reconciliation_status,
                      primary_source_url,secondary_source_url,
                      source_document_date,notes,source_id,reviewed_at
               FROM fact_terminal_event_reconciliation ORDER BY ticker"""
        )
    }

    raw_history: dict[str, dict[str, float]] = {}
    contracts: dict[str, dict[str, Any]] = {}
    raw_digest = hashlib.sha256()
    raw_count = 0
    for ticker in sorted(required):
        selection = selections[ticker]
        source_id = str(selection["selected_source_id"])
        rows = conn.execute(
            """SELECT bar_date,adjusted_close FROM fact_price_ohlcv
               WHERE ticker=? AND source_id=? AND bar_date>=? AND bar_date<=?
                 AND adjusted_close>0 ORDER BY bar_date""",
            (
                ticker,
                source_id,
                str(selection["first_bar_date"]),
                maximum_date,
            ),
        ).fetchall()
        values: dict[str, float] = {}
        for raw in rows:
            bar_date = str(raw["bar_date"])
            adjusted = float(raw["adjusted_close"])
            if bar_date in values:
                raise ValueError(f"duplicate selected price row: {ticker}/{bar_date}")
            values[bar_date] = adjusted
            raw_digest.update(
                json.dumps(
                    [ticker, source_id, bar_date, adjusted],
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            raw_count += 1
        if not values:
            raise ValueError(f"selected price history is empty: {ticker}")
        observed_first = min(values)
        observed_last = max(values)
        if (
            str(selection["first_bar_date"]) != observed_first
            or str(selection["last_bar_date"]) != observed_last
            or int(selection["bar_count"]) != len(values)
        ):
            raise ValueError(f"frozen price selection boundary mismatch: {ticker}")

        terminal = terminal_rows.get(ticker)
        if terminal is not None:
            if (
                str(selection["coverage_status"]) != "complete"
                or str(selection["selection_reason"]) != "delisted_primary_complete"
                or str(terminal["provider_last_quoted_date"]) != observed_last
            ):
                raise ValueError(f"terminal price boundary is not source-certified: {ticker}")
        terminal_eligible = bool(
            terminal is not None
            and int(terminal["survivorship_complete"]) == 1
            and int(terminal["calibration_eligible"]) == 1
            and str(terminal["reconciliation_status"]) == "verified"
        )
        terminal_hash = "" if terminal is None else _sha(terminal)
        raw_history[ticker] = values
        contracts[ticker] = {
            "ticker": ticker,
            "selected_source_id": source_id,
            "observed_first_bar_date": observed_first,
            "observed_last_bar_date": observed_last,
            "observed_row_count": len(values),
            "coverage_status": str(selection["coverage_status"]),
            "selection_reason": str(selection["selection_reason"]),
            "terminal_event_sha256": terminal_hash,
            "terminal_event_date": (
                "" if terminal is None else str(terminal["economic_event_date"])
            ),
            "terminal_last_trade_date": (
                "" if terminal is None else str(terminal["last_trade_date"])
            ),
            "terminal_type": "" if terminal is None else str(terminal["terminal_type"]),
            "terminal_calibration_eligible": terminal_eligible,
            "internal_carried_dates": [],
            "terminal_transition_carried_dates": [],
            "terminal_value_start_date": "",
            "terminal_value_session_count": 0,
            "terminal_resolution_statuses": [],
            "suppressed_post_event_raw_dates": [],
            "normalized_mark_count": 0,
        }

    calendar = sorted(raw_history["XLP"])
    history: dict[str, dict[str, float]] = {}
    special_states: dict[str, dict[str, dict[str, Any]]] = {}
    for ticker in sorted(required):
        raw_values = raw_history[ticker]
        contract = contracts[ticker]
        terminal = terminal_rows.get(ticker)
        observed_first = str(contract["observed_first_bar_date"])
        observed_last = str(contract["observed_last_bar_date"])
        terminal_date = "" if terminal is None else str(terminal["economic_event_date"])
        eligible = bool(contract["terminal_calibration_eligible"])
        values: dict[str, float] = {}
        states: dict[str, dict[str, Any]] = {}
        internal_carried_dates: list[str] = []
        transition_carried_dates: list[str] = []
        suppressed_post_event_raw_dates: list[str] = []
        resolution_statuses: set[str] = set()
        terminal_value_start = ""
        terminal_value_count = 0
        last_mark: float | None = None

        for session in calendar:
            if session < observed_first:
                continue
            raw_mark = raw_values.get(session)
            if terminal is not None and session >= terminal_date:
                if not eligible:
                    if raw_mark is not None:
                        suppressed_post_event_raw_dates.append(session)
                    continue
                outcome = terminal_horizon_value(
                    conn,
                    terminal_policy,
                    ticker=ticker,
                    horizon_date=session,
                )
                status = str(outcome["calculation_status"])
                if status.startswith("resolved_"):
                    if raw_mark is not None:
                        suppressed_post_event_raw_dates.append(session)
                    terminal_mark = _finite(outcome.get("terminal_value"))
                    if terminal_mark is None or terminal_mark < 0.0:
                        raise ValueError(f"terminal value is invalid: {ticker}/{session}")
                    cash_component = float(outcome.get("cash_component") or 0.0)
                    stock_component = float(outcome.get("stock_component") or 0.0)
                    if not math.isclose(
                        terminal_mark,
                        cash_component + stock_component,
                        rel_tol=0.0,
                        abs_tol=1e-8,
                    ):
                        raise ValueError(f"terminal components do not reconcile: {ticker}/{session}")
                    values[session] = terminal_mark
                    states[session] = {
                        "provenance": "terminal_value",
                        "calculation_status": status,
                        "cash_component": cash_component,
                        "market_component": stock_component,
                        "terminal_event_sha256": contract["terminal_event_sha256"],
                    }
                    last_mark = terminal_mark
                    terminal_value_count += 1
                    resolution_statuses.add(status)
                    if not terminal_value_start:
                        terminal_value_start = session
                    continue
                if status == "successor_not_yet_trading":
                    transition_mark = raw_mark if raw_mark is not None else last_mark
                    if transition_mark is None:
                        raise ValueError(
                            f"successor transition lacks a mark: {ticker}/{session}"
                        )
                    provenance = (
                        "observed_last_trade_pending_successor"
                        if raw_mark is not None
                        else "terminal_transition_carry"
                    )
                    values[session] = transition_mark
                    states[session] = {
                        "provenance": provenance,
                        "calculation_status": status,
                        "cash_component": 0.0,
                        "market_component": transition_mark,
                        "terminal_event_sha256": contract["terminal_event_sha256"],
                    }
                    if raw_mark is None:
                        transition_carried_dates.append(session)
                    last_mark = transition_mark
                    resolution_statuses.add(status)
                    continue
                raise ValueError(
                    f"eligible terminal event cannot be marked: {ticker}/{session}/{status}"
                )

            if raw_mark is not None:
                values[session] = raw_mark
                last_mark = raw_mark
                continue
            if session < observed_last:
                if last_mark is None:
                    raise ValueError(f"internal price carry lacks a prior mark: {ticker}/{session}")
                values[session] = last_mark
                states[session] = {
                    "provenance": "internal_carry",
                    "calculation_status": "internal_nontrading_session",
                    "cash_component": 0.0,
                    "market_component": last_mark,
                    "terminal_event_sha256": "",
                }
                internal_carried_dates.append(session)
                continue
            if terminal is not None and eligible and session < terminal_date:
                if last_mark is None:
                    raise ValueError(
                        f"terminal transition lacks a prior mark: {ticker}/{session}"
                    )
                values[session] = last_mark
                states[session] = {
                    "provenance": "terminal_transition_carry",
                    "calculation_status": "pre_economic_terminal_event",
                    "cash_component": 0.0,
                    "market_component": last_mark,
                    "terminal_event_sha256": contract["terminal_event_sha256"],
                }
                transition_carried_dates.append(session)

        contract["internal_carried_dates"] = internal_carried_dates
        contract["terminal_transition_carried_dates"] = transition_carried_dates
        contract["terminal_value_start_date"] = terminal_value_start
        contract["terminal_value_session_count"] = terminal_value_count
        contract["terminal_resolution_statuses"] = sorted(resolution_statuses)
        contract["suppressed_post_event_raw_dates"] = suppressed_post_event_raw_dates
        contract["normalized_mark_count"] = len(values)
        history[ticker] = values
        special_states[ticker] = states

    normalized_digest = hashlib.sha256()
    normalized_count = 0
    for ticker in sorted(required):
        for bar_date, adjusted in sorted(history[ticker].items()):
            state = special_states[ticker].get(
                bar_date,
                {
                    "provenance": "observed",
                    "calculation_status": "observed_selected_adjusted_close",
                    "cash_component": 0.0,
                    "market_component": adjusted,
                    "terminal_event_sha256": "",
                },
            )
            normalized_digest.update(
                json.dumps(
                    [
                        ticker,
                        bar_date,
                        adjusted,
                        state["provenance"],
                        state["calculation_status"],
                        state["cash_component"],
                        state["market_component"],
                        state["terminal_event_sha256"],
                    ],
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            normalized_count += 1
    price_hash = _sha(
        {
            "selection": selection_payload,
            "raw_selected_price_rows_sha256": raw_digest.hexdigest(),
            "raw_row_count": raw_count,
            "price_mark_policy": PRICE_MARK_POLICY,
            "terminal_event_rows_sha256": _sha(
                [terminal_rows[ticker] for ticker in sorted(terminal_rows)]
            ),
            "terminal_validation": terminal_validation,
            "series_contracts": [contracts[ticker] for ticker in sorted(contracts)],
            "normalized_price_marks_sha256": normalized_digest.hexdigest(),
            "normalized_mark_count": normalized_count,
        }
    )
    return (
        history,
        special_states,
        contracts,
        price_hash,
        raw_count,
        terminal_validation,
    )


def _price_state(
    *,
    ticker: str,
    session: str,
    mark: float,
    special_states: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    state = special_states.get(ticker, {}).get(session)
    if state is not None:
        return dict(state)
    return {
        "provenance": "observed",
        "calculation_status": "observed_selected_adjusted_close",
        "cash_component": 0.0,
        "market_component": mark,
        "terminal_event_sha256": "",
    }

def _true_month_ends(calendar: Sequence[str], *, asof_date: str) -> set[str]:
    month_end: dict[str, str] = {}
    for session in calendar:
        if session <= asof_date:
            month_end[session[:7]] = session
    return set(month_end.values())


def _completion_date(
    calendar: Sequence[str], *, signal_date: str, entry_lag: int, horizon: int
) -> str:
    index = bisect.bisect_left(calendar, signal_date)
    if index >= len(calendar) or calendar[index] != signal_date:
        raise ValueError(f"signal date is absent from the benchmark calendar: {signal_date}")
    exit_index = index + entry_lag + horizon
    if exit_index >= len(calendar):
        raise ValueError(f"forward label does not complete in the selected calendar: {signal_date}/{horizon}")
    return calendar[exit_index]


def _prepare_panel(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for raw in rows:
        row = dict(raw)
        identity = (str(row["asof_date"]), str(row["ticker"]))
        if identity in identities:
            raise ValueError(f"historical feature panel identity is duplicated: {identity}")
        identities.add(identity)
        row["_component_scores"] = _parse_json_mapping(
            row["component_scores_json"], label="component_scores_json"
        )
        row["_component_quality"] = _parse_json_mapping(
            row["component_quality_json"], label="component_quality_json"
        )
        row["_component_raw_values"] = _parse_json_mapping(
            row["component_raw_values_json"], label="component_raw_values_json"
        )
        row["_specialized_scores"] = _parse_json_mapping(
            row["specialized_scores_json"], label="specialized_scores_json"
        )
        prepared.append(row)
    prepared.sort(key=lambda row: (str(row["asof_date"]), str(row["ticker"])))
    return prepared


def _usable_components(row: Mapping[str, Any]) -> set[str]:
    scores = row["_component_scores"]
    quality = row["_component_quality"]
    return {
        spec.name
        for spec in CORE_COMPONENT_SPECS
        if float(quality.get(spec.name, 0.0)) > 0.0 and _finite(scores.get(spec.name)) is not None
    }


def _era_aware_requirements(
    row: Mapping[str, Any], *, short_interest_birthdate: str
) -> bool:
    usable = _usable_components(row)
    if any(
        spec.rank_requirement == "required" and spec.name not in usable
        for spec in CORE_COMPONENT_SPECS
    ):
        return False
    if not any(
        spec.name in usable
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == "any_financial"
    ):
        return False
    if str(row["asof_date"]) >= short_interest_birthdate and not any(
        spec.name in usable
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == "any_short"
    ):
        return False
    return True


def _candidate_score(
    row: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    short_interest_birthdate: str,
    minimum_quality: float,
    maximum_missing: float,
) -> tuple[float, bool]:
    if int(row["membership_eligible_flag"]) != 1 or int(row["investable_flag"]) != 1:
        return 0.0, False
    if not _era_aware_requirements(row, short_interest_birthdate=short_interest_birthdate):
        return 0.0, False
    neutral = 50.0
    score = 0.0
    available = 0.0
    missing = 0.0
    applicable = 0.0
    component_scores = row["_component_scores"]
    component_quality = row["_component_quality"]
    asof_date = str(row["asof_date"])
    for name, raw_weight in candidate["core_weights"].items():
        weight = float(raw_weight)
        value = _finite(component_scores.get(name))
        usable = float(component_quality.get(name, 0.0)) > 0.0 and value is not None
        score += weight * (min(100.0, max(0.0, float(value))) if usable else neutral)
        spec = _CORE_SPEC_BY_NAME[name]
        structurally_unavailable = (
            spec.rank_requirement == "any_short"
            and asof_date < short_interest_birthdate
        )
        quality_relevant = (
            spec.rank_requirement != "optional" and not structurally_unavailable
        )
        if quality_relevant:
            applicable += weight
            if usable:
                available += weight
            else:
                missing += weight
    specialized_scores = row["_specialized_scores"]
    for name, raw_weight in candidate["specialized_weights"].items():
        weight = float(raw_weight)
        value = _finite(specialized_scores.get(name))
        score += weight * (min(100.0, max(0.0, float(value))) if value is not None else neutral)
        applicable += weight
        if value is None:
            missing += weight
        else:
            available += weight
    if applicable <= 0.0:
        return score, False
    return (
        score,
        available / applicable >= minimum_quality
        and missing / applicable <= maximum_missing,
    )


def _holdings_for_date(
    rows: Sequence[Mapping[str, Any]],
    *,
    candidate: Mapping[str, Any],
    short_interest_birthdate: str,
    minimum_quality: float,
    maximum_missing: float,
) -> dict[str, Any]:
    """Freeze a portfolio from PIT features without reading outcome labels."""

    scored: list[tuple[float, str, Mapping[str, Any]]] = []
    for row in rows:
        score, eligible = _candidate_score(
            row,
            candidate,
            short_interest_birthdate=short_interest_birthdate,
            minimum_quality=minimum_quality,
            maximum_missing=maximum_missing,
        )
        if eligible:
            scored.append((score, str(row["ticker"]), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if len(scored) < int(PORTFOLIO_POLICY["minimum_cross_section"]):
        raise ValueError("candidate date lacks the preregistered minimum cross-section")
    count = max(
        int(PORTFOLIO_POLICY["minimum_positions"]),
        int(math.ceil(len(scored) * float(PORTFOLIO_POLICY["top_fraction"]))),
    )
    count = min(count, int(PORTFOLIO_POLICY["maximum_positions"]), len(scored))
    if count < int(PORTFOLIO_POLICY["minimum_positions"]):
        raise ValueError("candidate date lacks the preregistered minimum positions")
    selected = scored[:count]
    weight = 1.0 / count
    weights = {ticker: weight for _, ticker, _ in selected}
    liquidity_ratios: list[float] = []
    reference_notional = float(LIQUIDITY_POLICY["reference_gross_notional_usd"])
    fraction_adv = float(LIQUIDITY_POLICY["maximum_fraction_of_adv"])
    for _, ticker, row in selected:
        adv = _finite(row["_component_raw_values"].get("avg_dollar_volume_63d"))
        if adv is None or adv <= 0.0:
            raise ValueError(f"selected portfolio lacks positive ADV: {ticker}")
        liquidity_ratios.append((adv * fraction_adv) / (reference_notional * weight))
    return {
        "asof_date": str(selected[0][2]["asof_date"]),
        "candidate_id": str(candidate["candidate_id"]),
        "weights": weights,
        "selected_rows": selected,
        "liquidity_capacity_ratio": min(liquidity_ratios),
    }


def _portfolio_for_date(
    rows: Sequence[Mapping[str, Any]],
    *,
    candidate: Mapping[str, Any],
    horizon: int,
    labels: Mapping[tuple[str, str], Mapping[str, Any]],
    short_interest_birthdate: str,
    minimum_quality: float,
    maximum_missing: float,
) -> dict[str, Any]:
    snapshot = _holdings_for_date(
        rows,
        candidate=candidate,
        short_interest_birthdate=short_interest_birthdate,
        minimum_quality=minimum_quality,
        maximum_missing=maximum_missing,
    )
    total_field = f"forward_total_return_{horizon}d"
    residual_field = f"forward_xlp_residual_return_{horizon}d"
    gross_return = 0.0
    benchmark_values: list[float] = []
    weights = snapshot["weights"]
    for _, ticker, row in snapshot.pop("selected_rows"):
        label = labels.get((str(row["asof_date"]), ticker))
        total_return = None if label is None else _finite(label.get(total_field))
        residual = None if label is None else _finite(label.get(residual_field))
        if total_return is None or residual is None:
            raise ValueError(
                f"selected holding lacks a completed frozen label: "
                f"{row['asof_date']}/{ticker}/{horizon}"
            )
        benchmark_values.append(total_return - residual)
        gross_return += float(weights[ticker]) * total_return
    if max(benchmark_values) - min(benchmark_values) > 1e-8:
        raise ValueError("ticker label rows disagree on the XLP benchmark return")
    snapshot["gross_return"] = gross_return
    snapshot["benchmark_return"] = statistics.fmean(benchmark_values)
    return snapshot

def _candidate_path(
    candidate: Mapping[str, Any],
    *,
    dates: Sequence[str],
    rows_by_date: Mapping[str, Sequence[Mapping[str, Any]]],
    horizon: int,
    labels: Mapping[tuple[str, str], Mapping[str, Any]],
    short_interest_birthdate: str,
    minimum_quality: float,
    maximum_missing: float,
) -> list[dict[str, Any]]:
    previous: dict[str, float] = {}
    path: list[dict[str, Any]] = []
    cost_rate = float(COST_POLICY["one_way_transaction_cost_bps"]) / 10_000.0
    for signal in dates:
        snapshot = _portfolio_for_date(
            rows_by_date.get(signal, ()),
            candidate=candidate,
            horizon=horizon,
            labels=labels,
            short_interest_birthdate=short_interest_birthdate,
            minimum_quality=minimum_quality,
            maximum_missing=maximum_missing,
        )
        turnover = _turnover(previous, snapshot["weights"])
        cost = turnover * cost_rate
        snapshot["turnover"] = turnover
        snapshot["transaction_cost"] = cost
        snapshot["net_alpha"] = snapshot["gross_return"] - cost - snapshot["benchmark_return"]
        path.append(snapshot)
        previous = dict(snapshot["weights"])
    return path


def _fold_payload(fold: WalkForwardFold) -> dict[str, Any]:
    return {
        "fold_id": fold.fold_id,
        "train_dates": [value.isoformat() for value in fold.train_dates],
        "validation_dates": [value.isoformat() for value in fold.validation_dates],
        "test_dates": [value.isoformat() for value in fold.test_dates],
        "purged_train_count": fold.purged_train_count,
        "purged_validation_count": fold.purged_validation_count,
    }


def _build_folds(
    signal_dates: Sequence[str],
    *,
    completion_by_date: Mapping[str, str],
    portfolio_ready_dates: Sequence[str] | None = None,
    diagnostics_out: dict[str, Any] | None = None,
) -> tuple[WalkForwardFold, ...]:
    signal_census = tuple(signal_dates)
    if len(set(signal_census)) != len(signal_census):
        raise ValueError("split signal-date census contains duplicates")
    if tuple(sorted(signal_census)) != signal_census:
        raise ValueError("split signal-date census is not chronological")
    missing_completion = sorted(set(signal_census) - set(completion_by_date))
    if missing_completion:
        raise ValueError(
            "split signal-date census lacks label-completion dates: "
            + ",".join(missing_completion)
        )
    if portfolio_ready_dates is None:
        supplied_ready_census = signal_census
    else:
        supplied_ready_census = tuple(portfolio_ready_dates)
        if len(set(supplied_ready_census)) != len(supplied_ready_census):
            raise ValueError("portfolio-ready signal-date census contains duplicates")
    unknown_ready_dates = sorted(set(supplied_ready_census) - set(signal_census))
    if unknown_ready_dates:
        raise ValueError(
            "portfolio-ready dates are outside the split chronology: "
            + ",".join(unknown_ready_dates)
        )
    portfolio_ready = frozenset(supplied_ready_census)
    ready_census = tuple(value for value in signal_census if value in portfolio_ready)

    signal_set = set(signal_census)
    dates = tuple(date.fromisoformat(value) for value in signal_census)
    completion = {
        date.fromisoformat(signal): date.fromisoformat(value)
        for signal, value in completion_by_date.items()
        if signal in signal_set
    }
    raw_folds = list(
        build_nested_purged_walk_forward(
            dates,
            label_completion_by_date=completion,
            initial_train_size=int(SPLIT_POLICY["initial_train_observations_after_purge"]),
            validation_size=int(SPLIT_POLICY["validation_observations_before_purge"]),
            test_size=int(SPLIT_POLICY["outer_test_observations_per_fold"]),
            step_size=int(SPLIT_POLICY["outer_step_observations"]),
            embargo_observations=int(SPLIT_POLICY["embargo_observations"]),
        )
    )

    readiness_rejected: list[dict[str, Any]] = []
    readiness_accepted: list[WalkForwardFold] = []
    for fold in raw_folds:
        unready_validation = [
            value.isoformat()
            for value in fold.validation_dates
            if value.isoformat() not in portfolio_ready
        ]
        unready_test = [
            value.isoformat()
            for value in fold.test_dates
            if value.isoformat() not in portfolio_ready
        ]
        if unready_validation or unready_test:
            readiness_rejected.append(
                {
                    "fold_id": fold.fold_id,
                    "rejection_stage": "portfolio_readiness",
                    "reason": "validation_or_test_portfolio_unready",
                    "unready_validation_dates": unready_validation,
                    "unready_test_dates": unready_test,
                }
            )
            continue
        readiness_accepted.append(fold)

    maximum = int(SPLIT_POLICY["maximum_outer_folds"])
    maximum_rejected_folds = readiness_accepted[maximum:]
    folds = readiness_accepted[:maximum]
    maximum_rejected = [
        {
            "fold_id": fold.fold_id,
            "rejection_stage": "maximum_outer_folds",
            "reason": "outside_largest_chronological_maximum_prefix",
            "unready_validation_dates": [],
            "unready_test_dates": [],
        }
        for fold in maximum_rejected_folds
    ]
    odd_rejected: list[dict[str, Any]] = []
    if len(folds) % 2:
        rejected = folds[-1]
        odd_rejected.append(
            {
                "fold_id": rejected.fold_id,
                "rejection_stage": "odd_fold_rule",
                "reason": "outside_largest_even_chronological_prefix",
                "unready_validation_dates": [],
                "unready_test_dates": [],
            }
        )
        folds = folds[:-1]

    retained_train_readiness = [
        {
            "fold_id": fold.fold_id,
            "unready_train_date_count": sum(
                value.isoformat() not in portfolio_ready
                for value in fold.train_dates
            ),
            "unready_train_dates": [
                value.isoformat()
                for value in fold.train_dates
                if value.isoformat() not in portfolio_ready
            ],
        }
        for fold in folds
    ]
    observation_count = sum(len(fold.test_dates) for fold in folds)
    rejected_folds = readiness_rejected + maximum_rejected + odd_rejected
    diagnostics = {
        "split_chronology_policy": SPLIT_POLICY["split_chronology_census"],
        "portfolio_readiness_scope": SPLIT_POLICY["portfolio_readiness_scope"],
        "portfolio_readiness_fold_policy": SPLIT_POLICY[
            "portfolio_readiness_fold_policy"
        ],
        "unready_train_date_policy": SPLIT_POLICY["unready_train_date_policy"],
        "train_partition_role": SPLIT_POLICY["train_partition_role"],
        "candidate_fit_performed_on_train": SPLIT_POLICY[
            "candidate_fit_performed_on_train"
        ],
        "fold_filter_order": [
            "portfolio_readiness",
            "maximum_outer_folds",
            "odd_fold_rule",
            "minimum_outer_folds",
            "minimum_outer_test_observations",
        ],
        "split_signal_date_count": len(signal_census),
        "split_signal_date_census": list(signal_census),
        "portfolio_ready_signal_date_count": len(ready_census),
        "portfolio_ready_signal_date_census": list(ready_census),
        "portfolio_unready_signal_date_count": len(signal_census) - len(ready_census),
        "raw_admissible_fold_count": len(raw_folds),
        "portfolio_readiness_rejected_fold_count": len(readiness_rejected),
        "post_readiness_fold_count": len(readiness_accepted),
        "maximum_outer_folds_rejected_count": len(maximum_rejected),
        "odd_fold_rule_rejected_count": len(odd_rejected),
        "retained_fold_count": len(folds),
        "outer_test_observation_count": observation_count,
        "rejected_fold_reason_counts": {
            "validation_or_test_portfolio_unready": len(readiness_rejected),
            "outside_largest_chronological_maximum_prefix": len(maximum_rejected),
            "outside_largest_even_chronological_prefix": len(odd_rejected),
        },
        "rejected_folds": rejected_folds,
        "retained_fold_train_readiness": retained_train_readiness,
    }
    if diagnostics_out is not None:
        diagnostics_out.clear()
        diagnostics_out.update(diagnostics)

    if len(folds) < int(SPLIT_POLICY["minimum_outer_folds"]):
        raise ValueError(
            "v2 calibration has fewer than four even outer folds after "
            "portfolio-readiness filtering: "
            f"retained={len(folds)},raw={len(raw_folds)},"
            f"readiness_rejected={len(readiness_rejected)}"
        )
    if observation_count < int(SPLIT_POLICY["minimum_outer_test_observations"]):
        raise ValueError(
            "v2 calibration has insufficient completed outer-test observations: "
            f"{observation_count}"
        )
    return tuple(folds)


def _evaluate_horizon(
    *,
    cohort: str,
    horizon: int,
    candidates: Sequence[Mapping[str, Any]],
    panel_rows: Sequence[Mapping[str, Any]],
    labels: Mapping[tuple[str, str], Mapping[str, Any]],
    calendar: Sequence[str],
    prices: Mapping[str, Mapping[str, float]],
    price_special_states: Mapping[str, Mapping[str, Mapping[str, Any]]],
    price_series_contracts: Mapping[str, Mapping[str, Any]],
    decision_asof: date,
    framework: Mapping[str, Any],
    short_interest_birthdate: str,
    minimum_quality: float,
    maximum_missing: float,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    rows_by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in panel_rows:
        if str(row["cohort_id"]) == cohort:
            rows_by_date[str(row["asof_date"])].append(row)
    seed = next((row for row in candidates if row["candidate_kind"] == "stage7_seed"), None)
    if seed is None:
        raise ValueError(f"{cohort}/{horizon}: Stage 7 seed candidate is missing")
    true_months = _true_month_ends(calendar, asof_date=decision_asof.isoformat())
    split_signal_dates: list[str] = []
    completion_by_date: dict[str, str] = {}
    for signal in sorted(rows_by_date):
        if signal not in true_months:
            continue
        completion = _completion_date(
            calendar,
            signal_date=signal,
            entry_lag=int(PORTFOLIO_POLICY["entry_lag_trading_sessions"]),
            horizon=horizon,
        )
        if completion > decision_asof.isoformat():
            continue
        split_signal_dates.append(signal)
        completion_by_date[signal] = completion

    # Portfolio readiness is deliberately feature-only and label-blind.  Train
    # dates are chronology/purge burn-in, while every candidate later evaluated
    # in the candidate matrix must be constructible on validation and test dates.
    ordered_candidates = sorted(candidates, key=lambda row: str(row["candidate_id"]))
    portfolio_ready_dates: list[str] = []
    readiness_failures_by_date: dict[str, dict[str, Any]] = {}
    for signal in split_signal_dates:
        failures: dict[str, str] = {}
        for candidate in ordered_candidates:
            candidate_id = str(candidate["candidate_id"])
            try:
                _holdings_for_date(
                    rows_by_date[signal],
                    candidate=candidate,
                    short_interest_birthdate=short_interest_birthdate,
                    minimum_quality=minimum_quality,
                    maximum_missing=maximum_missing,
                )
            except ValueError as exc:
                failures[candidate_id] = str(exc)
        if failures:
            readiness_failures_by_date[signal] = {
                "failed_candidate_count": len(failures),
                "failed_candidate_ids": sorted(failures),
                "failure_reason_by_candidate": dict(sorted(failures.items())),
            }
        else:
            portfolio_ready_dates.append(signal)

    fold_diagnostics: dict[str, Any] = {}
    try:
        folds = _build_folds(
            split_signal_dates,
            completion_by_date=completion_by_date,
            portfolio_ready_dates=portfolio_ready_dates,
            diagnostics_out=fold_diagnostics,
        )
    except ValueError as exc:
        raise ValueError(
            f"{cohort}/{horizon}: walk-forward construction failed with "
            f"{len(split_signal_dates)} true-month-end horizon-complete dates, "
            f"{len(portfolio_ready_dates)} all-candidate feature-ready dates, "
            f"{fold_diagnostics.get('raw_admissible_fold_count', 0)} raw folds, "
            "and "
            f"{fold_diagnostics.get('portfolio_readiness_rejected_fold_count', 0)} "
            "readiness-rejected folds"
        ) from exc
    selected_by_fold: dict[str, str] = {}
    for fold in folds:
        validation_dates = [value.isoformat() for value in fold.validation_dates]
        scores: list[tuple[float, str]] = []
        for candidate in candidates:
            try:
                path = _candidate_path(
                    candidate,
                    dates=validation_dates,
                    rows_by_date=rows_by_date,
                    horizon=horizon,
                    labels=labels,
                    short_interest_birthdate=short_interest_birthdate,
                    minimum_quality=minimum_quality,
                    maximum_missing=maximum_missing,
                )
            except ValueError:
                continue
            scores.append((statistics.fmean(row["net_alpha"] for row in path), str(candidate["candidate_id"])))
        if not scores:
            raise ValueError(f"{cohort}/{horizon}/{fold.fold_id}: no candidate completed inner validation")
        selected_by_fold[fold.fold_id] = sorted(scores, key=lambda item: (-item[0], item[1]))[0][1]

    test_fold_by_date = {
        value.isoformat(): fold.fold_id for fold in folds for value in fold.test_dates
    }
    test_dates = sorted(test_fold_by_date)
    candidate_by_id = {str(row["candidate_id"]): row for row in candidates}
    candidate_matrix: dict[str, dict[str, float]] = {}
    for candidate in candidates:
        path = _candidate_path(
            candidate,
            dates=test_dates,
            rows_by_date=rows_by_date,
            horizon=horizon,
            labels=labels,
            short_interest_birthdate=short_interest_birthdate,
            minimum_quality=minimum_quality,
            maximum_missing=maximum_missing,
        )
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in path:
            grouped[test_fold_by_date[str(row["asof_date"])]].append(float(row["net_alpha"]))
        candidate_matrix[str(candidate["candidate_id"])] = {
            fold.fold_id: statistics.fmean(grouped[fold.fold_id]) for fold in folds
        }

    selected_snapshots: list[dict[str, Any]] = []
    previous: dict[str, float] = {}
    cost_rate = float(COST_POLICY["one_way_transaction_cost_bps"]) / 10_000.0
    for signal in test_dates:
        fold_id = test_fold_by_date[signal]
        candidate = candidate_by_id[selected_by_fold[fold_id]]
        snapshot = _portfolio_for_date(
            rows_by_date[signal],
            candidate=candidate,
            horizon=horizon,
            labels=labels,
            short_interest_birthdate=short_interest_birthdate,
            minimum_quality=minimum_quality,
            maximum_missing=maximum_missing,
        )
        turnover = _turnover(previous, snapshot["weights"])
        snapshot.update(
            {
                "fold_id": fold_id,
                "turnover": turnover,
                "transaction_cost": turnover * cost_rate,
            }
        )
        snapshot["net_alpha"] = (
            snapshot["gross_return"]
            - snapshot["transaction_cost"]
            - snapshot["benchmark_return"]
        )
        identity_payload = {
            "cohort": cohort,
            "horizon": horizon,
            "asof_date": signal,
            "fold_id": fold_id,
            "candidate_id": snapshot["candidate_id"],
            "weights": sorted(snapshot["weights"].items()),
        }
        snapshot["observation_id"] = f"cdv2o_{_sha(identity_payload)[:28]}"
        selected_snapshots.append(snapshot)
        previous = dict(snapshot["weights"])

    observations = [
        ReturnObservation(
            observation_id=str(row["observation_id"]),
            fold_id=str(row["fold_id"]),
            evaluation_role=OUTER_TEST_ROLE,
            asof_date=date.fromisoformat(str(row["asof_date"])),
            label_completion_date=date.fromisoformat(completion_by_date[str(row["asof_date"])]),
            cohort=cohort,
            horizon_sessions=horizon,
            strategy_return=float(row["gross_return"]),
            benchmark_return=float(row["benchmark_return"]),
            transaction_cost=float(row["transaction_cost"]),
            turnover=float(row["turnover"]),
            liquidity_capacity_ratio=float(row["liquidity_capacity_ratio"]),
        )
        for row in selected_snapshots
    ]
    selected_portfolios = [
        SelectedPortfolioObservation(
            observation_id=str(row["observation_id"]),
            fold_id=str(row["fold_id"]),
            asof_date=date.fromisoformat(str(row["asof_date"])),
            cohort=cohort,
            horizon_sessions=horizon,
            selected_candidate_id=str(row["candidate_id"]),
            weights=tuple(sorted((str(ticker), float(weight)) for ticker, weight in row["weights"].items())),
        )
        for row in selected_snapshots
    ]
    realized, path_attestations = _realized_daily_path(
        selected_snapshots,
        cohort=cohort,
        horizon=horizon,
        calendar=calendar,
        prices=prices,
        price_special_states=price_special_states,
        price_series_contracts=price_series_contracts,
    )
    _validate_path_attestations(realized, path_attestations)
    result = evaluate_cohort(
        observations,
        realized_returns=realized,
        outer_test_folds=folds,
        decision_asof=decision_asof,
        framework=framework,
        candidate_performance_by_fold=candidate_matrix,
        selected_portfolios=selected_portfolios,
    )
    candidate_matrix_sha256 = canonical_sha256({"value": candidate_matrix})
    if result["evidence"]["candidate_matrix_sha256"] != candidate_matrix_sha256:
        raise ValueError(f"{cohort}/{horizon}: candidate-matrix evidence hash drift")
    detail = {
        "cohort": cohort,
        "horizon_sessions": horizon,
        "signal_date_census": split_signal_dates,
        "split_signal_date_census": split_signal_dates,
        "portfolio_ready_signal_date_census": portfolio_ready_dates,
        "portfolio_readiness_policy": (
            "all_preregistered_candidates_feature_only_label_blind"
        ),
        "portfolio_readiness_failures_by_signal_date": readiness_failures_by_date,
        "fold_construction_diagnostics": fold_diagnostics,
        "completion_by_signal_date": completion_by_date,
        "folds": [_fold_payload(fold) for fold in folds],
        "selected_candidate_by_fold": dict(sorted(selected_by_fold.items())),
        "candidate_matrix_sha256": candidate_matrix_sha256,
        "outer_observation_count": len(observations),
        "realized_daily_return_count": len(realized),
        "realized_path_role": REALIZED_PATH_POLICY["absolute_metric_role"],
        "realized_path_attestation_sha256": _sha(path_attestations),
        "terminal_event_tickers_used": sorted(
            {
                str(position["ticker"])
                for row in path_attestations
                for position in row["positions"]
                if position["terminal_event_sha256"]
            }
        ),
    }
    return result, detail, path_attestations


def _realized_daily_path(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    cohort: str,
    horizon: int,
    calendar: Sequence[str],
    prices: Mapping[str, Mapping[str, float]],
    price_special_states: Mapping[str, Mapping[str, Mapping[str, Any]]],
    price_series_contracts: Mapping[str, Mapping[str, Any]],
) -> tuple[list[RealizedReturnObservation], list[dict[str, Any]]]:
    ordered = sorted(snapshots, key=lambda row: str(row["asof_date"]))
    if not ordered:
        raise ValueError("daily realized path requires selected portfolios")
    calendar_index = {value: index for index, value in enumerate(calendar)}
    used_dates: set[str] = set()
    output: list[RealizedReturnObservation] = []
    attestations: list[dict[str, Any]] = []
    entry_lag = int(PORTFOLIO_POLICY["entry_lag_trading_sessions"])
    final_holding = int(PORTFOLIO_POLICY["final_path_sessions_after_last_signal"])
    for position, snapshot in enumerate(ordered):
        signal = str(snapshot["asof_date"])
        entry_index = calendar_index[signal] + entry_lag
        if position + 1 < len(ordered):
            next_signal = str(ordered[position + 1]["asof_date"])
            stop_index = calendar_index[next_signal] + entry_lag
        else:
            stop_index = min(len(calendar) - 1, entry_index + final_holding)
        first_return_index = entry_index + 1
        if stop_index < first_return_index:
            raise ValueError("realized holding interval is empty or overlapping")
        weights = {str(name): float(value) for name, value in snapshot["weights"].items()}
        entry_date = calendar[entry_index]
        entry_marks: dict[str, float] = {}
        units: dict[str, float] = {}
        prior_states: dict[str, dict[str, Any]] = {}
        for ticker, weight in weights.items():
            contract = price_series_contracts.get(ticker)
            entry_mark = prices.get(ticker, {}).get(entry_date)
            if contract is None:
                raise ValueError(f"price-series contract is missing: {ticker}")
            entry_state = (
                None
                if entry_mark is None
                else _price_state(
                    ticker=ticker,
                    session=entry_date,
                    mark=entry_mark,
                    special_states=price_special_states,
                )
            )
            if (
                entry_mark is None
                or entry_mark <= 0.0
                or entry_state is None
                or entry_state["provenance"] != "observed"
                or (
                    contract["terminal_last_trade_date"]
                    and entry_date > str(contract["terminal_last_trade_date"])
                )
            ):
                raise ValueError(
                    "selected holding lacks an observed tradable entry bar: "
                    f"{ticker}/{entry_date}"
                )
            entry_marks[ticker] = entry_mark
            units[ticker] = weight / entry_mark
            prior_states[ticker] = entry_state
        entry_cash = 1.0 - sum(weights.values())
        if entry_cash < -1e-12:
            raise ValueError("selected portfolio exceeds unit gross capital")
        entry_cash = max(0.0, entry_cash)
        prior_nav = entry_cash + sum(
            units[ticker] * entry_marks[ticker] for ticker in weights
        )
        if not math.isclose(prior_nav, 1.0, rel_tol=0.0, abs_tol=1e-10):
            raise ValueError("entry sleeves do not reconcile to unit NAV")
        prior_marks = dict(entry_marks)

        for day_index in range(first_return_index, stop_index + 1):
            return_date = calendar[day_index]
            if return_date in used_dates:
                raise ValueError("realized daily return dates overlap")
            used_dates.add(return_date)
            position_rows: list[dict[str, Any]] = []
            current_nav = entry_cash
            cash_value = entry_cash
            market_exposure_value = 0.0
            current_marks: dict[str, float] = {}
            current_states: dict[str, dict[str, Any]] = {}
            for ticker in weights:
                current_mark = prices.get(ticker, {}).get(return_date)
                if current_mark is None or current_mark < 0.0:
                    raise ValueError(
                        f"realized price path is incomplete: "
                        f"{ticker}/{calendar[day_index - 1]}/{return_date}"
                    )
                current_state = _price_state(
                    ticker=ticker,
                    session=return_date,
                    mark=current_mark,
                    special_states=price_special_states,
                )
                if current_mark == 0.0 and not (
                    current_state["provenance"] == "terminal_value"
                    and price_series_contracts[ticker]["terminal_type"] == "wipeout"
                ):
                    raise ValueError(f"zero mark is not a verified wipeout: {ticker}/{return_date}")
                ticker_units = units[ticker]
                prior_mark = prior_marks[ticker]
                prior_state = prior_states[ticker]
                prior_value = ticker_units * prior_mark
                current_value = ticker_units * current_mark
                current_nav += current_value
                cash_value += ticker_units * float(current_state["cash_component"])
                market_exposure_value += abs(
                    ticker_units * float(current_state["market_component"])
                )
                position_rows.append(
                    {
                        "ticker": ticker,
                        "units": ticker_units,
                        "prior_mark": prior_mark,
                        "current_mark": current_mark,
                        "prior_value": prior_value,
                        "current_value": current_value,
                        "prior_provenance": str(prior_state["provenance"]),
                        "current_provenance": str(current_state["provenance"]),
                        "prior_cash_component": float(prior_state["cash_component"]),
                        "current_cash_component": float(current_state["cash_component"]),
                        "prior_market_component": float(prior_state["market_component"]),
                        "current_market_component": float(current_state["market_component"]),
                        "terminal_event_sha256": str(
                            current_state["terminal_event_sha256"]
                            or prior_state["terminal_event_sha256"]
                        ),
                    }
                )
                current_marks[ticker] = current_mark
                current_states[ticker] = current_state
            if current_nav <= 0.0 or not math.isfinite(current_nav):
                raise ValueError(f"realized portfolio NAV is invalid: {return_date}")
            gross = current_nav / prior_nav - 1.0
            cost = float(snapshot["transaction_cost"]) if day_index == first_return_index else 0.0
            identity = _sha(
                {
                    "cohort": cohort,
                    "horizon": horizon,
                    "fold_id": snapshot["fold_id"],
                    "signal": signal,
                    "return_date": return_date,
                    "weights": sorted(weights.items()),
                }
            )
            observation_id = f"cdv2r_{identity[:28]}"
            output.append(
                RealizedReturnObservation(
                    observation_id=observation_id,
                    source_portfolio_observation_id=str(snapshot["observation_id"]),
                    fold_id=str(snapshot["fold_id"]),
                    evaluation_role=OUTER_TEST_ROLE,
                    return_date=date.fromisoformat(return_date),
                    cohort=cohort,
                    horizon_sessions=horizon,
                    strategy_return=gross,
                    transaction_cost=cost,
                )
            )
            attestations.append(
                {
                    "observation_id": observation_id,
                    "source_portfolio_observation_id": str(snapshot["observation_id"]),
                    "fold_id": str(snapshot["fold_id"]),
                    "cohort": cohort,
                    "horizon_sessions": horizon,
                    "signal_date": signal,
                    "entry_date": entry_date,
                    "return_date": return_date,
                    "prior_nav": prior_nav,
                    "current_nav": current_nav,
                    "entry_cash_value": entry_cash,
                    "cash_value": cash_value,
                    "market_exposure_value": market_exposure_value,
                    "gross_exposure_ratio": market_exposure_value / current_nav,
                    "gross_return": gross,
                    "transaction_cost": cost,
                    "net_return": gross - cost,
                    "positions": sorted(position_rows, key=lambda row: str(row["ticker"])),
                }
            )
            prior_nav = current_nav
            prior_marks = current_marks
            prior_states = current_states
    return output, attestations


def _validate_path_attestations(
    realized: Sequence[RealizedReturnObservation],
    attestations: Sequence[Mapping[str, Any]],
) -> None:
    by_id = {row.observation_id: row for row in realized}
    if len(by_id) != len(realized) or len(attestations) != len(realized):
        raise ValueError("realized path attestation census is inconsistent")
    observed_ids: set[str] = set()
    for raw in attestations:
        row = dict(raw)
        observation_id = str(row["observation_id"])
        if observation_id in observed_ids or observation_id not in by_id:
            raise ValueError("realized path attestation identity is invalid")
        observed_ids.add(observation_id)
        realized_row = by_id[observation_id]
        if (
            row["source_portfolio_observation_id"]
            != realized_row.source_portfolio_observation_id
            or row["fold_id"] != realized_row.fold_id
            or row["cohort"] != realized_row.cohort
            or int(row["horizon_sessions"]) != realized_row.horizon_sessions
            or row["return_date"] != realized_row.return_date.isoformat()
        ):
            raise ValueError("realized path attestation lineage mismatch")
        positions = row["positions"]
        if not isinstance(positions, list) or not positions:
            raise ValueError("realized path attestation requires position detail")
        entry_cash = float(row["entry_cash_value"])
        prior_nav = entry_cash + sum(float(item["prior_value"]) for item in positions)
        current_nav = entry_cash + sum(float(item["current_value"]) for item in positions)
        cash_value = entry_cash + sum(
            float(item["units"]) * float(item["current_cash_component"])
            for item in positions
        )
        market_value = sum(
            abs(float(item["units"]) * float(item["current_market_component"]))
            for item in positions
        )
        for item in positions:
            if (
                not math.isclose(
                    float(item["prior_value"]),
                    float(item["units"]) * float(item["prior_mark"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    float(item["current_value"]),
                    float(item["units"]) * float(item["current_mark"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    float(item["prior_mark"]),
                    float(item["prior_cash_component"])
                    + float(item["prior_market_component"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    float(item["current_mark"]),
                    float(item["current_cash_component"])
                    + float(item["current_market_component"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError("realized path position-value reconciliation failed")
        expected_gross = current_nav / prior_nav - 1.0
        comparisons = (
            (prior_nav, float(row["prior_nav"])),
            (current_nav, float(row["current_nav"])),
            (cash_value, float(row["cash_value"])),
            (market_value, float(row["market_exposure_value"])),
            (market_value / current_nav, float(row["gross_exposure_ratio"])),
            (expected_gross, float(row["gross_return"])),
            (expected_gross, realized_row.strategy_return),
            (
                expected_gross - float(row["transaction_cost"]),
                float(row["net_return"]),
            ),
            (float(row["transaction_cost"]), realized_row.transaction_cost),
        )
        if any(
            not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-12)
            for left, right in comparisons
        ):
            raise ValueError("realized path NAV attestation failed")
    if observed_ids != set(by_id):
        raise ValueError("realized path attestation does not cover every return")

def _hash_panel(rows: Sequence[Mapping[str, Any]]) -> str:
    return _sha([str(row["row_sha256"]) for row in rows])


def _input_manifest(
    *,
    preregistration: Mapping[str, Any],
    feature_rows: Sequence[Mapping[str, Any]],
    labels: Mapping[tuple[str, str], Mapping[str, Any]],
    price_sha256: str,
    price_row_count: int,
    price_series_contracts: Mapping[str, Mapping[str, Any]],
    terminal_validation: Mapping[str, Any],
) -> dict[str, Any]:
    label_payload = [
        {key: row[key] for key in sorted(row)}
        for _, row in sorted(labels.items())
    ]
    payload: dict[str, Any] = {
        "schema_version": INPUT_MANIFEST_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": preregistration["asof_date"],
        "preregistration_sha256": preregistration["payload_sha256"],
        "feature_panel_sha256": _hash_panel(feature_rows),
        "label_panel_sha256": _sha(label_payload),
        "realized_price_panel_sha256": price_sha256,
        "realized_price_mark_policy": PRICE_MARK_POLICY,
        "realized_path_policy": REALIZED_PATH_POLICY,
        "realized_price_series_contracts": [
            dict(price_series_contracts[ticker])
            for ticker in sorted(price_series_contracts)
        ],
        "terminal_event_validation": dict(terminal_validation),
        "cost_input_sha256": _sha(COST_POLICY),
        "source_row_counts": {
            "feature_rows": len(feature_rows),
            "label_ticker_dates": len(labels),
            "selected_price_rows": price_row_count,
            "normalized_price_marks": sum(
                int(contract["normalized_mark_count"])
                for contract in price_series_contracts.values()
            ),
            "terminal_event_rows": int(terminal_validation["counts"]["events"]),
        },
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload

def _validate_runtime_bindings(
    *,
    repository_root: Path,
    bundle: ConfigBundle,
    preregistration: Mapping[str, Any],
    candidate_registry: Mapping[str, Any],
    framework: Mapping[str, Any],
) -> None:
    registry = validate_candidate_registry(candidate_registry)
    prereg = validate_preregistration(preregistration, candidate_registry=registry)
    validate_framework(framework)
    current_files = methodology_hashes(repository_root, bundle)
    if current_files != prereg["code_file_sha256s"] or _sha(current_files) != prereg["code_sha256"]:
        raise ValueError("methodology changed after calibration preregistration")


def run_sequence1_calibration(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    repository_root: Path,
    framework: Mapping[str, Any],
    preregistration: Mapping[str, Any],
    candidate_registry: Mapping[str, Any],
    factor_root: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Run sequence 1 without mutating the database or Portfolio Layer."""

    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise RuntimeError("v2 calibration requires a query-only SQLite connection")
    validated_framework = validate_framework(framework)
    registry = validate_candidate_registry(candidate_registry)
    prereg = validate_preregistration(preregistration, candidate_registry=registry)
    _validate_runtime_bindings(
        repository_root=repository_root,
        bundle=bundle,
        preregistration=prereg,
        candidate_registry=registry,
        framework=validated_framework,
    )
    stage6c_run_id = int(prereg["source_contract"]["stage6c_run_id"])
    stage6c_validation = validate_stage6c_panel(conn, stage6c_run_id=stage6c_run_id)
    if stage6c_validation["status"] != "PASS":
        raise RuntimeError("source Stage 6C validation failed")
    if stage6c_validation["panel_sha256"] != prereg["source_contract"]["stage6c_panel_sha256"]:
        raise ValueError("Stage 6C panel changed after preregistration")
    campaign_summary, accepted_cells = verify_factor_campaign(
        factor_root,
        campaign_id=str(prereg["source_contract"]["factor_campaign_id"]),
    )
    if (
        campaign_summary["registry_sha256"] != prereg["source_contract"]["factor_registry_sha256"]
        or _sha(sorted((dict(cell) for cell in accepted_cells), key=lambda row: str(row["cell_id"])))
        != prereg["source_contract"]["accepted_factor_cells_sha256"]
    ):
        raise ValueError("factor-validation evidence changed after preregistration")
    source_membership = _membership_rows(
        conn, stage6c_run_id=stage6c_run_id
    )
    membership, calibration_scope_summary = apply_calibration_scope(
        source_membership, bundle
    )
    market_policy = load_market_policy(
        resolve_path(cfg_get(bundle.payload, "market_data_policy.policy_path"), base_dir=bundle.base_dir)
    )
    feature_rows, feature_summary = build_historical_core_panel_v2(
        conn,
        bundle,
        stage6c_run_id=stage6c_run_id,
        membership_rows=membership,
        accepted_factor_cells=accepted_cells,
        market_policy=market_policy,
    )
    feature_summary = {
        **feature_summary,
        'calibration_scope': calibration_scope_summary,
    }
    prepared = _prepare_panel(feature_rows)
    labels = _exact_stage6c_labels(conn, stage6c_run_id=stage6c_run_id)
    labels = filter_label_mapping(
        labels,
        excluded_tickers=calibration_scope_summary['contract'][
            'excluded_tickers'
        ],
    )
    prepared_identities = {
        (str(row['asof_date']), str(row['ticker'])) for row in prepared
    }
    if set(labels) != prepared_identities:
        raise ValueError(
            'Calibration-scoped feature and label identities do not tie.'
        )
    tickers = {str(row["ticker"]) for row in prepared}
    (
        prices,
        price_special_states,
        price_contracts,
        price_sha,
        price_count,
        terminal_validation,
    ) = _load_price_history(
        conn,
        tickers=tickers,
        maximum_date=str(prereg["asof_date"]),
    )
    calendar = sorted(prices["XLP"])
    input_manifest = _input_manifest(
        preregistration=prereg,
        feature_rows=prepared,
        labels=labels,
        price_sha256=price_sha,
        price_row_count=price_count,
        price_series_contracts=price_contracts,
        terminal_validation=terminal_validation,
    )
    candidates_by_key: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in registry["candidates"]:
        candidates_by_key[(str(row["cohort"]), int(row["horizon_sessions"]))].append(dict(row))
    short_birth = str(cfg_get(bundle.payload, "positioning.source_birthdates.short_interest"))
    minimum_quality = float(cfg_get(bundle.payload, "stage7_scoring.minimum_data_quality_confidence"))
    maximum_missing = float(cfg_get(bundle.payload, "stage7_scoring.maximum_missing_component_weight"))
    decision_asof = date.fromisoformat(str(prereg["asof_date"]))
    results_by_cohort: dict[str, dict[str, dict[str, Any]]] = {}
    detail_by_cohort: dict[str, dict[str, dict[str, Any]]] = {}
    path_by_cohort: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        results_by_cohort[cohort] = {}
        detail_by_cohort[cohort] = {}
        path_by_cohort[cohort] = {}
        for horizon in REQUIRED_HORIZONS:
            result, detail, path_attestations = _evaluate_horizon(
                cohort=cohort,
                horizon=horizon,
                candidates=candidates_by_key[(cohort, horizon)],
                panel_rows=prepared,
                labels=labels,
                calendar=calendar,
                prices=prices,
                price_special_states=price_special_states,
                price_series_contracts=price_contracts,
                decision_asof=decision_asof,
                framework=validated_framework,
                short_interest_birthdate=short_birth,
                minimum_quality=minimum_quality,
                maximum_missing=maximum_missing,
            )
            results_by_cohort[cohort][str(horizon)] = result
            detail_by_cohort[cohort][str(horizon)] = detail
            path_by_cohort[cohort][str(horizon)] = path_attestations
    path_attestation: dict[str, Any] = {
        "schema_version": PATH_ATTESTATION_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": prereg["asof_date"],
        "preregistration_sha256": prereg["payload_sha256"],
        "path_policy": REALIZED_PATH_POLICY,
        "cohorts": path_by_cohort,
    }
    path_attestation["payload_sha256"] = canonical_sha256(path_attestation)
    benchmark_membership_rows = [
        {
            "asof_date": str(row["asof_date"]),
            "ticker": str(row["ticker"]),
            "cohort_id": str(row["cohort_id"]),
            "membership_eligible_flag": int(row["membership_eligible_flag"]),
            "investable_flag": int(row["investable_flag"]),
        }
        for row in prepared
    ]
    _, benchmark_attestation = build_matched_benchmark_paths(
        strategy_path_rows_by_cohort=path_by_cohort,
        membership_rows=benchmark_membership_rows,
        prices=prices,
        calendar=calendar,
    )
    fold_registry: dict[str, Any] = {
        "schema_version": FOLD_REGISTRY_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": prereg["asof_date"],
        "preregistration_sha256": prereg["payload_sha256"],
        "split_policy": dict(SPLIT_POLICY),
        "realized_path_attestation_sha256": path_attestation["payload_sha256"],
        "matched_benchmark_attestation_sha256": benchmark_attestation[
            "payload_sha256"
        ],
        "cohorts": detail_by_cohort,
    }
    fold_registry["payload_sha256"] = canonical_sha256(fold_registry)
    decision = build_calibration_decision(
        asof_date=decision_asof,
        framework=validated_framework,
        horizon_results_by_cohort=results_by_cohort,
        input_panel_sha256=input_manifest["payload_sha256"],
        fold_registry_sha256=fold_registry["payload_sha256"],
        candidate_registry_sha256=registry["payload_sha256"],
        code_sha256=prereg["code_sha256"],
        previous_decision=None,
    )
    validate_calibration_decision(decision, framework=validated_framework)
    results: dict[str, Any] = {
        "schema_version": RESULTS_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": prereg["asof_date"],
        "preregistration_sha256": prereg["payload_sha256"],
        "candidate_registry_sha256": registry["payload_sha256"],
        "input_manifest_sha256": input_manifest["payload_sha256"],
        "fold_registry_sha256": fold_registry["payload_sha256"],
        "realized_path_attestation_sha256": path_attestation["payload_sha256"],
        "matched_benchmark_attestation_sha256": benchmark_attestation[
            "payload_sha256"
        ],
        "feature_summary": feature_summary,
        "factor_campaign_summary": campaign_summary,
        "accepted_specialized_factor_cell_count": len(accepted_cells),
        "cohort_horizon_results": results_by_cohort,
        "decision_payload_sha256": decision["payload_sha256"],
        "production_promotion_enabled": False,
        "portfolio_write_enabled": False,
    }
    results["payload_sha256"] = canonical_sha256(results)
    validation: dict[str, Any] = {
        "schema_version": VALIDATION_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": prereg["asof_date"],
        "status": "PASS",
        "framework_sha256": decision["framework_sha256"],
        "decision_payload_sha256": decision["payload_sha256"],
        "input_manifest_sha256": input_manifest["payload_sha256"],
        "fold_registry_sha256": fold_registry["payload_sha256"],
        "realized_path_attestation_sha256": path_attestation["payload_sha256"],
        "matched_benchmark_attestation_sha256": benchmark_attestation[
            "payload_sha256"
        ],
        "candidate_registry_sha256": registry["payload_sha256"],
        "code_sha256": prereg["code_sha256"],
        "decision_sequence": 1,
        "production_write_performed": False,
        "portfolio_write_performed": False,
    }
    validation["payload_sha256"] = canonical_sha256(validation)
    payload = {
        "input_manifest": input_manifest,
        "fold_registry": fold_registry,
        "path_attestation": path_attestation,
        "benchmark_attestation": benchmark_attestation,
        "results": results,
        "decision": decision,
        "independent_validation": validation,
    }
    if output_dir is not None:
        root = output_dir.expanduser().resolve()
        publish_immutable_json(root / "consumer_defensive_calibration_input_manifest_v2.json", input_manifest)
        publish_immutable_json(root / "consumer_defensive_calibration_fold_registry_v2.json", fold_registry)
        publish_immutable_json(
            root / "consumer_defensive_calibration_realized_path_attestation_v2.json",
            path_attestation,
        )
        publish_immutable_json(
            root / "consumer_defensive_matched_benchmark_attestation_v3.json",
            benchmark_attestation,
        )
        publish_immutable_json(root / "consumer_defensive_calibration_results_v2.json", results)
        publish_immutable_json(root / "consumer_defensive_calibration_decision_v2.json", decision)
        publish_immutable_json(
            root / "consumer_defensive_calibration_independent_validation_v2.json",
            validation,
        )
    return payload


def load_preregistration_pair(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = root.expanduser().resolve()
    registry = load_json(resolved / "consumer_defensive_calibration_candidate_registry_v2.json")
    prereg = load_json(resolved / "consumer_defensive_calibration_preregistration_v2.json")
    validate_preregistration(prereg, candidate_registry=registry)
    return prereg, registry


__all__ = [
    "FOLD_REGISTRY_SCHEMA",
    "INPUT_MANIFEST_SCHEMA",
    "PATH_ATTESTATION_SCHEMA",
    "RESULTS_SCHEMA",
    "VALIDATION_SCHEMA",
    "load_preregistration_pair",
    "run_sequence1_calibration",
]














