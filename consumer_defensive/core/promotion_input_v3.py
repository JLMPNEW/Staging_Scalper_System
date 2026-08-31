"""Build exact matched benchmark evidence for promotion engine v3."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date
from typing import Any, Mapping, Sequence

from consumer_defensive.core.promotion_engine_v3 import (
    PROMOTION_INPUT_SCHEMA,
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    framework_sha256,
    seal_promotion_input,
    validate_promotion_input,
    value_sha256,
)
BENCHMARK_ATTESTATION_SCHEMA = "consumer_defensive_matched_benchmark_attestation_v3"



def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric evidence")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite numeric evidence") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite numeric evidence")
    return parsed


def _positive_mark(
    prices: Mapping[str, Mapping[str, float]], *, ticker: str, session: str
) -> float:
    value = prices.get(ticker, {}).get(session)
    parsed = _finite(value, label=f"price {ticker}/{session}")
    if parsed <= 0.0:
        raise ValueError(f"price {ticker}/{session} must be positive")
    return parsed


def build_matched_benchmark_paths(
    *,
    strategy_path_rows_by_cohort: Mapping[
        str, Mapping[str, Sequence[Mapping[str, Any]]]
    ],
    membership_rows: Sequence[Mapping[str, Any]],
    prices: Mapping[str, Mapping[str, float]],
    calendar: Sequence[str],
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, Any]]:
    """Build PIT equal-weight peer, XLP, and SPY daily return paths.

    Membership is frozen at each strategy signal date.  A peer must have exact
    positive marks on both matched sessions; missing members fail closed rather
    than silently changing the benchmark composition.
    """

    if set(strategy_path_rows_by_cohort) != REQUIRED_COHORTS:
        raise ValueError("strategy paths must cover exactly four Consumer cohorts")
    sessions = list(calendar)
    parsed_sessions = [date.fromisoformat(value) for value in sessions]
    if (
        any(parsed.isoformat() != raw for parsed, raw in zip(parsed_sessions, sessions))
        or sessions != sorted(set(sessions))
        or len(sessions) < 2
    ):
        raise ValueError("benchmark calendar must be strictly increasing")
    session_index = {value: position for position, value in enumerate(sessions)}
    membership: dict[tuple[str, str], list[str]] = defaultdict(list)
    seen_membership: set[tuple[str, str]] = set()
    membership_evidence: list[dict[str, Any]] = []
    for raw in membership_rows:
        signal = str(raw.get("asof_date") or "")
        ticker = str(raw.get("ticker") or "").strip().upper()
        cohort = str(raw.get("cohort_id") or "")
        identity = (signal, ticker)
        if not signal or not ticker or cohort not in REQUIRED_COHORTS:
            raise ValueError("membership row has an invalid identity or cohort")
        if identity in seen_membership:
            raise ValueError(f"membership identity is duplicated: {identity}")
        seen_membership.add(identity)
        eligible = int(raw.get("membership_eligible_flag") or 0) == 1
        investable = int(raw.get("investable_flag") or 0) == 1
        if eligible and investable:
            membership[(signal, cohort)].append(ticker)
        membership_evidence.append(
            {
                "asof_date": signal,
                "ticker": ticker,
                "cohort_id": cohort,
                "membership_eligible_flag": int(eligible),
                "investable_flag": int(investable),
            }
        )

    output: dict[str, dict[str, list[dict[str, Any]]]] = {}
    benchmark_attestations: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        horizon_paths = strategy_path_rows_by_cohort[cohort]
        if set(horizon_paths) != {str(value) for value in REQUIRED_HORIZONS}:
            raise ValueError(f"{cohort}: exact 21/63/126 strategy paths are required")
        output[cohort] = {}
        benchmark_attestations[cohort] = {}
        for horizon in REQUIRED_HORIZONS:
            rows = sorted(
                (dict(row) for row in horizon_paths[str(horizon)]),
                key=lambda row: str(row.get("return_date") or ""),
            )
            if not rows:
                raise ValueError(f"{cohort}/{horizon}: strategy path is empty")
            matched: list[dict[str, Any]] = []
            attestations: list[dict[str, Any]] = []
            observed_dates: set[str] = set()
            for raw in rows:
                signal = str(raw.get("signal_date") or "")
                return_date = str(raw.get("return_date") or "")
                if return_date in observed_dates:
                    raise ValueError(f"{cohort}/{horizon}: duplicated return date")
                observed_dates.add(return_date)
                if return_date not in session_index or session_index[return_date] == 0:
                    raise ValueError(
                        f"{cohort}/{horizon}: return date is absent from benchmark calendar"
                    )
                prior_date = sessions[session_index[return_date] - 1]
                if signal not in session_index or session_index[signal] >= session_index[return_date]:
                    raise ValueError(
                        f"{cohort}/{horizon}: signal date must precede return date"
                    )
                peers = sorted(set(membership.get((signal, cohort), ())))
                if len(peers) < 2:
                    raise ValueError(
                        f"{cohort}/{horizon}/{signal}: peer benchmark has fewer than two names"
                    )
                peer_rows: list[dict[str, Any]] = []
                peer_returns: list[float] = []
                for ticker in peers:
                    prior_mark = _positive_mark(
                        prices, ticker=ticker, session=prior_date
                    )
                    current_mark = _positive_mark(
                        prices, ticker=ticker, session=return_date
                    )
                    realized = current_mark / prior_mark - 1.0
                    peer_returns.append(realized)
                    peer_rows.append(
                        {
                            "ticker": ticker,
                            "prior_mark": prior_mark,
                            "current_mark": current_mark,
                            "return": realized,
                        }
                    )
                benchmark_returns: dict[str, float] = {}
                marks: dict[str, dict[str, float]] = {}
                for ticker in ("XLP", "SPY"):
                    prior_mark = _positive_mark(
                        prices, ticker=ticker, session=prior_date
                    )
                    current_mark = _positive_mark(
                        prices, ticker=ticker, session=return_date
                    )
                    benchmark_returns[ticker] = current_mark / prior_mark - 1.0
                    marks[ticker] = {
                        "prior_mark": prior_mark,
                        "current_mark": current_mark,
                    }
                strategy_net_return = _finite(
                    raw.get("net_return"),
                    label=f"{cohort}/{horizon}/{return_date}.net_return",
                )
                if strategy_net_return <= -1.0:
                    raise ValueError("strategy net return must exceed -100%")
                primary = statistics.fmean(peer_returns)
                matched.append(
                    {
                        "date": return_date,
                        "strategy_net_return": strategy_net_return,
                        "primary_benchmark_return": primary,
                        "xlp_return": benchmark_returns["XLP"],
                        "spy_return": benchmark_returns["SPY"],
                    }
                )
                attestations.append(
                    {
                        "signal_date": signal,
                        "prior_date": prior_date,
                        "return_date": return_date,
                        "strategy_observation_id": str(raw.get("observation_id") or ""),
                        "strategy_net_return": strategy_net_return,
                        "peer_weighting": "point_in_time_equal_weight_daily_rebalanced",
                        "peer_count": len(peer_rows),
                        "peer_rows": peer_rows,
                        "primary_benchmark_return": primary,
                        "xlp_marks": marks["XLP"],
                        "xlp_return": benchmark_returns["XLP"],
                        "spy_marks": marks["SPY"],
                        "spy_return": benchmark_returns["SPY"],
                    }
                )
            output[cohort][str(horizon)] = matched
            benchmark_attestations[cohort][str(horizon)] = attestations
    evidence: dict[str, Any] = {
        "schema_version": BENCHMARK_ATTESTATION_SCHEMA,
        "model_family": "consumer_defensive",
        "primary_benchmark": "point_in_time_equal_weight_cohort",
        "diagnostic_benchmarks": ["XLP", "SPY"],
        "membership_sha256": value_sha256(
            sorted(
                membership_evidence,
                key=lambda row: (str(row["asof_date"]), str(row["ticker"])),
            )
        ),
        "cohorts": benchmark_attestations,
    }
    evidence["payload_sha256"] = value_sha256(
        {key: value for key, value in evidence.items() if key != "payload_sha256"}
    )
    return output, evidence


def validate_benchmark_attestation(
    payload: Mapping[str, Any],
    *,
    matched_paths_by_cohort: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "model_family",
        "primary_benchmark",
        "diagnostic_benchmarks",
        "membership_sha256",
        "cohorts",
        "payload_sha256",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_keys:
        raise ValueError("benchmark attestation has the wrong root schema")
    evidence = dict(payload)
    if (
        evidence["schema_version"] != BENCHMARK_ATTESTATION_SCHEMA
        or evidence["model_family"] != "consumer_defensive"
        or evidence["primary_benchmark"] != "point_in_time_equal_weight_cohort"
        or evidence["diagnostic_benchmarks"] != ["XLP", "SPY"]
    ):
        raise ValueError("benchmark attestation policy changed")
    for field in ("membership_sha256", "payload_sha256"):
        value = evidence[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"benchmark attestation {field} is invalid")
    if value_sha256(
        {key: value for key, value in evidence.items() if key != "payload_sha256"}
    ) != evidence["payload_sha256"]:
        raise ValueError("benchmark attestation self-hash mismatch")
    cohorts = evidence["cohorts"]
    if not isinstance(cohorts, Mapping) or set(cohorts) != REQUIRED_COHORTS:
        raise ValueError("benchmark attestation cohort census changed")
    if set(matched_paths_by_cohort) != REQUIRED_COHORTS:
        raise ValueError("matched path cohort census changed")
    for cohort in sorted(REQUIRED_COHORTS):
        horizon_evidence = cohorts[cohort]
        matched_horizons = matched_paths_by_cohort[cohort]
        if (
            not isinstance(horizon_evidence, Mapping)
            or set(horizon_evidence) != {str(value) for value in REQUIRED_HORIZONS}
            or set(matched_horizons) != {str(value) for value in REQUIRED_HORIZONS}
        ):
            raise ValueError(f"{cohort}: benchmark horizon census changed")
        for horizon in REQUIRED_HORIZONS:
            key = str(horizon)
            rows = horizon_evidence[key]
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
                raise ValueError(f"{cohort}/{key}: benchmark evidence is empty")
            reconstructed: list[dict[str, Any]] = []
            prior_return_date = ""
            for position, raw in enumerate(rows):
                expected_row_keys = {
                    "signal_date", "prior_date", "return_date",
                    "strategy_observation_id", "strategy_net_return",
                    "peer_weighting", "peer_count", "peer_rows",
                    "primary_benchmark_return", "xlp_marks", "xlp_return",
                    "spy_marks", "spy_return",
                }
                if not isinstance(raw, Mapping) or set(raw) != expected_row_keys:
                    raise ValueError(f"{cohort}/{key}/{position}: benchmark row schema changed")
                row = dict(raw)
                signal = date.fromisoformat(str(row["signal_date"]))
                prior = date.fromisoformat(str(row["prior_date"]))
                current = date.fromisoformat(str(row["return_date"]))
                if not signal < current or not prior < current:
                    raise ValueError(f"{cohort}/{key}/{position}: benchmark chronology is invalid")
                if prior_return_date and current.isoformat() <= prior_return_date:
                    raise ValueError(f"{cohort}/{key}: benchmark dates are not ordered")
                prior_return_date = current.isoformat()
                if row["peer_weighting"] != "point_in_time_equal_weight_daily_rebalanced":
                    raise ValueError(f"{cohort}/{key}/{position}: peer weighting changed")
                peer_rows = row["peer_rows"]
                if not isinstance(peer_rows, Sequence) or isinstance(peer_rows, (str, bytes)):
                    raise ValueError(f"{cohort}/{key}/{position}: peer rows are invalid")
                if int(row["peer_count"]) != len(peer_rows) or len(peer_rows) < 2:
                    raise ValueError(f"{cohort}/{key}/{position}: peer count does not reconcile")
                peer_returns: list[float] = []
                peer_tickers: list[str] = []
                for peer in peer_rows:
                    if not isinstance(peer, Mapping) or set(peer) != {"ticker", "prior_mark", "current_mark", "return"}:
                        raise ValueError(f"{cohort}/{key}/{position}: peer row schema changed")
                    ticker = str(peer["ticker"])
                    prior_mark = _finite(peer["prior_mark"], label="peer prior mark")
                    current_mark = _finite(peer["current_mark"], label="peer current mark")
                    observed_return = _finite(peer["return"], label="peer return")
                    if not ticker or prior_mark <= 0.0 or current_mark <= 0.0 or not math.isclose(current_mark / prior_mark - 1.0, observed_return, rel_tol=0.0, abs_tol=1e-12):
                        raise ValueError(f"{cohort}/{key}/{position}: peer return does not reconcile")
                    peer_tickers.append(ticker)
                    peer_returns.append(observed_return)
                if peer_tickers != sorted(set(peer_tickers)):
                    raise ValueError(f"{cohort}/{key}/{position}: peer census is not canonical")
                primary = _finite(row["primary_benchmark_return"], label="primary benchmark return")
                if not math.isclose(statistics.fmean(peer_returns), primary, rel_tol=0.0, abs_tol=1e-12):
                    raise ValueError(f"{cohort}/{key}/{position}: peer benchmark does not reconcile")
                for ticker in ("xlp", "spy"):
                    marks = row[f"{ticker}_marks"]
                    if not isinstance(marks, Mapping) or set(marks) != {"prior_mark", "current_mark"}:
                        raise ValueError(f"{cohort}/{key}/{position}: {ticker} marks changed")
                    prior_mark = _finite(marks["prior_mark"], label=f"{ticker} prior mark")
                    current_mark = _finite(marks["current_mark"], label=f"{ticker} current mark")
                    observed_return = _finite(row[f"{ticker}_return"], label=f"{ticker} return")
                    if prior_mark <= 0.0 or current_mark <= 0.0 or not math.isclose(current_mark / prior_mark - 1.0, observed_return, rel_tol=0.0, abs_tol=1e-12):
                        raise ValueError(f"{cohort}/{key}/{position}: {ticker} return does not reconcile")
                reconstructed.append({
                    "date": row["return_date"],
                    "strategy_net_return": row["strategy_net_return"],
                    "primary_benchmark_return": row["primary_benchmark_return"],
                    "xlp_return": row["xlp_return"],
                    "spy_return": row["spy_return"],
                })
            if reconstructed != [dict(row) for row in matched_horizons[key]]:
                raise ValueError(f"{cohort}/{key}: benchmark attestation/path mismatch")
    return evidence


def build_promotion_input(
    *,
    asof_date: str,
    evidence_role: str,
    framework: Mapping[str, Any],
    capital_allocation_context: Mapping[str, Any],
    source_lineage: Mapping[str, Any],
    safety_attestations: Mapping[str, bool],
    performance_by_cohort: Mapping[str, Mapping[str, Mapping[str, Any]]],
    matched_paths_by_cohort: Mapping[
        str, Mapping[str, Sequence[Mapping[str, Any]]]
    ],
    outer_oos_observations_by_cohort: Mapping[
        str, Mapping[str, Sequence[Mapping[str, Any]]]
    ],
    production_model_contracts: Mapping[str, Mapping[str, Any]],
    benchmark_attestation: Mapping[str, Any],
) -> dict[str, Any]:
    if set(performance_by_cohort) != REQUIRED_COHORTS:
        raise ValueError("performance must cover exactly four Consumer cohorts")
    if set(matched_paths_by_cohort) != REQUIRED_COHORTS:
        raise ValueError("matched paths must cover exactly four Consumer cohorts")
    if set(outer_oos_observations_by_cohort) != REQUIRED_COHORTS:
        raise ValueError("outer-OOS observations must cover exactly four Consumer cohorts")
    if set(production_model_contracts) != REQUIRED_COHORTS:
        raise ValueError("model contracts must cover exactly four Consumer cohorts")
    validated_benchmark = validate_benchmark_attestation(
        benchmark_attestation,
        matched_paths_by_cohort=matched_paths_by_cohort,
    )
    lineage = dict(source_lineage)
    observed_benchmark_hash = str(validated_benchmark["payload_sha256"])
    lineage["benchmark_path_source_sha256"] = observed_benchmark_hash
    cohorts = {
        cohort: {
            "production_model_contract": dict(production_model_contracts[cohort]),
            "horizons": {
                str(horizon): {
                    "performance": dict(
                        performance_by_cohort[cohort][str(horizon)]
                    ),
                    "daily_path": [
                        dict(row)
                        for row in matched_paths_by_cohort[cohort][str(horizon)]
                    ],
                    "outer_oos_observations": [
                        dict(row)
                        for row in outer_oos_observations_by_cohort[cohort][str(horizon)]
                    ],
                }
                for horizon in REQUIRED_HORIZONS
            },
        }
        for cohort in sorted(REQUIRED_COHORTS)
    }
    payload = seal_promotion_input(
        {
            "schema_version": PROMOTION_INPUT_SCHEMA,
            "model_family": "consumer_defensive",
            "asof_date": date.fromisoformat(asof_date).isoformat(),
            "framework_sha256": framework_sha256(framework),
            "evidence_role": evidence_role,
            "source_lineage": lineage,
            "capital_allocation_context": dict(capital_allocation_context),
            "safety_attestations": dict(safety_attestations),
            "cohorts": cohorts,
        }
    )
    return validate_promotion_input(payload, framework=framework)


__all__ = [
    "BENCHMARK_ATTESTATION_SCHEMA",
    "build_matched_benchmark_paths",
    "build_promotion_input",
    "validate_benchmark_attestation",
]
